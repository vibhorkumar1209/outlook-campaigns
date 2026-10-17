"""Orchestrator. One tick: detect replies (email + LinkedIn notification mails), then advance every active sequence.

Email steps send through Graph. LinkedIn steps become tasks in the dashboard queue; the sequence waits
until you mark the task done or skipped. A reply on any channel stops the lead everywhere.
Idempotent: safe to run every few minutes.
"""
from __future__ import annotations

import random
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from jinja2 import TemplateError

from . import db
from .db import event, iso, one, parse, rows, scalar, utcnow
from .graph import GraphError
from .render import email_html, lead_context, render_text

CHANNELS = {"email": "Email", "li_view": "View profile", "li_connect": "Connect", "li_message": "LinkedIn message"}
LI = ("li_view", "li_connect", "li_message")
CONDITIONS = {"always": "Always", "if_connected": "Only if connected", "if_not_connected": "Only if not connected"}
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
OPEN = ("pending", "active", "done")
REPLY_LOOKBACK = timedelta(days=30)   # keep watching finished sequences this long
WAIT_FOR_CONNECT = timedelta(days=7)  # connection-gated steps wait this long past due, then skip
LI_LOOKBACK = timedelta(days=14)      # first scan of LinkedIn notification mails
MAX_FAILURES = 3
MAX_EMAILS_PER_TICK = 10

AUTO_SUBJECTS = ("automatic reply", "auto reply", "autoreply", "auto-reply", "out of office")
BOUNCE_SENDERS = ("postmaster@", "mailer-daemon@", "microsoftexchange")
BOUNCE_SUBJECTS = ("undeliverable", "delivery has failed", "mail delivery failed",
                   "delivery status notification (failure)", "returned mail")
LI_MESSAGE_RE = re.compile(r"sent you a (new )?message|messaged you|new message|replied to your")
LI_CONNECTED_RE = re.compile(r"accepted your invitation|now connected|your new connection")


# ---------- classification ----------

def _sender(msg: dict) -> str:
    return ((msg.get("from") or {}).get("emailAddress") or {}).get("address", "").lower()


def classify(msg: dict, me: str) -> str | None:
    """Email: 'reply' | 'auto' | 'bounce' | None (ours / irrelevant)."""
    if msg.get("isDraft"):
        return None
    sender = _sender(msg)
    if not sender or sender == me:
        return None
    subject = (msg.get("subject") or "").strip().lower()
    if sender.startswith(BOUNCE_SENDERS) or subject.startswith(BOUNCE_SUBJECTS):
        return "bounce"
    headers = {h["name"].lower(): h["value"].lower() for h in msg.get("internetMessageHeaders") or []}
    if headers.get("auto-submitted", "no") != "no" or "x-autoreply" in headers or subject.startswith(AUTO_SUBJECTS):
        return "auto"
    return "reply"


def li_kind(msg: dict) -> str | None:
    """LinkedIn notification mail: 'message' | 'connected' | None."""
    sender = _sender(msg)
    text = _norm(f"{msg.get('subject') or ''} {msg.get('bodyPreview') or ''}")
    if sender.startswith("messag") or LI_MESSAGE_RE.search(text):
        return "message"
    if LI_CONNECTED_RE.search(text):
        return "connected"
    return None


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


# ---------- campaign time ----------

def tz_of(c: dict):
    return ZoneInfo(c["timezone"]) if c.get("timezone") else datetime.now().astimezone().tzinfo


def in_window(c: dict, now: datetime) -> bool:
    local = now.astimezone(tz_of(c))
    days = {DAYS.index(d.strip()[:3].lower()) for d in c["window_days"].split(",") if d.strip()[:3].lower() in DAYS}
    return local.weekday() in days and c["window_start"] <= local.strftime("%H:%M") < c["window_end"]


def day_start(c: dict, now: datetime) -> datetime:
    return now.astimezone(tz_of(c)).replace(hour=0, minute=0, second=0, microsecond=0)


# ---------- actions (also used by the dashboard) ----------

def close_tasks(conn, enrollment_id: int, now: datetime) -> None:
    conn.execute("UPDATE tasks SET status = 'skipped', done_at = ? WHERE enrollment_id = ? AND status = 'open'",
                 (iso(now), enrollment_id))


def mark_replied(conn, lead_id: int, channel: str, at: datetime, *, enrollment: dict | None = None,
                 detail: str | None = None) -> bool:
    """Stop the lead in every campaign. The campaign that got the reply is 'replied', others 'stopped'."""
    ens = rows(conn, "SELECT * FROM enrollments WHERE lead_id = ? AND status IN ('pending', 'active', 'done')", lead_id)
    if not ens:
        return False
    if enrollment is None:
        contacted = [e for e in ens if e["step_pos"] >= 0] or ens
        enrollment = max(contacted, key=lambda e: e["last_step_at"] or "")
    for e in ens:
        if e["id"] == enrollment["id"]:
            conn.execute("UPDATE enrollments SET status = 'replied', replied_at = ?, reply_channel = ? WHERE id = ?",
                         (iso(at), channel, e["id"]))
        else:
            conn.execute("UPDATE enrollments SET status = 'stopped', note = ? WHERE id = ?",
                         (f"replied via {channel} in another campaign", e["id"]))
        close_tasks(conn, e["id"], at)
    event(conn, lead_id, "reply", enrollment=enrollment, channel=channel, step_pos=enrollment["step_pos"],
          detail=detail, at=at)
    return True


def set_connected(conn, lead_id: int, at: datetime, source: str = "notification") -> bool:
    lead = one(conn, "SELECT li_state FROM leads WHERE id = ?", lead_id)
    if not lead or lead["li_state"] == "connected":
        return False
    conn.execute("UPDATE leads SET li_state = 'connected' WHERE id = ?", (lead_id,))
    e = one(conn, "SELECT * FROM enrollments WHERE lead_id = ? AND status IN ('pending', 'active') "
                  "ORDER BY last_step_at DESC LIMIT 1", lead_id)
    event(conn, lead_id, "connected", enrollment=e, channel="linkedin", detail=source, at=at)
    return True


def stop_enrollment(conn, enrollment_id: int, note: str = "stopped manually", now: datetime | None = None) -> None:
    now = now or utcnow()
    e = one(conn, "SELECT * FROM enrollments WHERE id = ?", enrollment_id)
    if not e or e["status"] not in OPEN:
        return
    conn.execute("UPDATE enrollments SET status = 'stopped', note = ? WHERE id = ?", (note, enrollment_id))
    close_tasks(conn, enrollment_id, now)
    event(conn, e["lead_id"], "stopped", enrollment=e, detail=note, at=now)


def _advance(conn, e: dict, k: int, now: datetime) -> None:
    conn.execute("UPDATE enrollments SET step_pos = ?, last_step_at = ?, status = 'active' WHERE id = ?",
                 (k, iso(now), e["id"]))
    e.update(step_pos=k, last_step_at=iso(now), status="active")


def complete_task(conn, task_id: int, action: str, body: str | None = None, now: datetime | None = None) -> None:
    """action: done | skip | connected (lead already connected) | replied (they answered on LinkedIn)."""
    now = now or utcnow()
    t = one(conn, "SELECT * FROM tasks WHERE id = ?", task_id)
    if not t or t["status"] != "open":
        raise ValueError("task already handled")
    e = one(conn, "SELECT * FROM enrollments WHERE id = ?", t["enrollment_id"])
    if action == "replied":
        mark_replied(conn, e["lead_id"], "linkedin", now, enrollment=e, detail="marked from queue")
        return
    if action == "connected":
        set_connected(conn, e["lead_id"], now, source="marked from queue")
    status = "done" if action == "done" else "skipped"
    conn.execute("UPDATE tasks SET status = ?, body = COALESCE(?, body), done_at = ? WHERE id = ?",
                 (status, body, iso(now), task_id))
    if status == "done" and t["channel"] == "li_connect":
        conn.execute("UPDATE leads SET li_state = 'invited' WHERE id = ? AND li_state = 'none'", (e["lead_id"],))
    _advance(conn, e, t["step_pos"], now)
    event(conn, e["lead_id"], "task_done" if status == "done" else "task_skipped", enrollment=e,
          channel=t["channel"], step_pos=t["step_pos"],
          detail=(body or t["body"])[:300] if status == "done" else action, at=now)


# ---------- reply detection ----------

def check_lead_email(conn, graph, me: str, es: list[dict], log) -> bool:
    """es: one lead's enrollments with conversation_id + email. True if the lead replied or bounced."""
    msgs: dict[str, tuple[dict, dict]] = {}
    for e in es:
        msgs.update({m["id"]: (m, e) for m in graph.conversation(e["conversation_id"])})
    since = min(parse(e["first_email_at"]) for e in es)
    latest = max(es, key=lambda e: e["last_step_at"] or "")
    for m in graph.inbound_from(es[0]["email"], since):  # replies outside the thread count too
        msgs.setdefault(m["id"], (m, latest))
    reply = bounce = None
    for m, e in sorted(msgs.values(), key=lambda p: p[0].get("receivedDateTime") or ""):
        kind = classify(m, me)
        if kind == "reply" and not reply:
            reply = (m, e)
        elif kind == "bounce" and not bounce:
            bounce = (m, e)
    if reply:
        m, e = reply
        mark_replied(conn, e["lead_id"], "email", parse(m["receivedDateTime"]), enrollment=e, detail=m.get("subject"))
        log(f"reply (email) from {e['email']}")
        return True
    if bounce:
        m, e = bounce
        at = parse(m["receivedDateTime"])
        conn.execute("UPDATE enrollments SET status = 'bounced', note = ? WHERE id = ?", (m.get("subject"), e["id"]))
        close_tasks(conn, e["id"], at)
        event(conn, e["lead_id"], "bounce", enrollment=e, channel="email", step_pos=e["step_pos"],
              detail=m.get("subject"), at=at)
        log(f"bounce for {e['email']}")
        return True
    return False


EMAIL_WATCH_SQL = """
    SELECT e.*, l.email FROM enrollments e
    JOIN leads l ON l.id = e.lead_id JOIN campaigns c ON c.id = e.campaign_id
    WHERE c.status != 'draft' AND e.status IN ('active', 'done') AND e.conversation_id IS NOT NULL
"""


def check_email(conn, graph, me: str, now: datetime, log) -> None:
    by_lead = defaultdict(list)
    for e in rows(conn, EMAIL_WATCH_SQL + " AND e.last_step_at >= ?", iso(now - REPLY_LOOKBACK)):
        by_lead[e["lead_id"]].append(e)
    for es in by_lead.values():
        check_lead_email(conn, graph, me, es, log)


def check_linkedin(conn, graph, now: datetime, log) -> None:
    cursor = parse(db.get_setting(conn, "li_cursor")) or now - LI_LOOKBACK
    msgs = graph.messages_since(cursor)
    if not msgs:
        return
    leads = rows(conn, """
        SELECT DISTINCT l.id, l.first_name, l.last_name FROM leads l
        JOIN enrollments e ON e.lead_id = l.id JOIN campaigns c ON c.id = e.campaign_id
        WHERE c.status != 'draft' AND e.status IN ('pending', 'active', 'done')
          AND l.linkedin_url IS NOT NULL AND l.first_name != '' AND l.last_name != ''""")
    for m in sorted(msgs, key=lambda m: m.get("receivedDateTime") or ""):
        kind = li_kind(m) if _sender(m).endswith("linkedin.com") else None
        if not kind:
            continue
        text = _norm(f"{m.get('subject') or ''} {m.get('bodyPreview') or ''}")
        at = parse(m["receivedDateTime"])
        for lead in leads:
            full = _norm(f"{lead['first_name']} {lead['last_name']}")
            if full not in text:
                continue
            if kind == "connected" and set_connected(conn, lead["id"], at):
                log(f"LinkedIn: {full} accepted")
            elif kind == "message" and mark_replied(conn, lead["id"], "linkedin", at, detail=m.get("subject")):
                log(f"reply (LinkedIn) from {full}")
    db.set_setting(conn, "li_cursor", max(m["receivedDateTime"] for m in msgs))


# ---------- sequencing ----------

def gate(st: dict, e: dict, now: datetime, due: datetime) -> tuple[str | None, bool]:
    """(skip reason, wait). Neither = go."""
    ch, li_state = st["channel"], e["li_state"]
    if ch == "email" and not e["email"]:
        return "no email address", False
    if ch in LI and not e["linkedin_url"]:
        return "no LinkedIn URL", False
    if (st["condition"] == "if_connected" or ch == "li_message") and li_state != "connected":
        return ("not connected", False) if now >= due + WAIT_FOR_CONNECT else (None, True)
    if st["condition"] == "if_not_connected" and li_state == "connected":
        return "already connected", False
    if ch == "li_connect" and li_state != "none":
        return "already connected" if li_state == "connected" else "invite already sent", False
    return None, False


def _fail(conn, e: dict, k: int, msg: str, log, permanent: bool) -> None:
    failures = e["failures"] + 1
    status = "error" if permanent or failures >= MAX_FAILURES else e["status"]
    conn.execute("UPDATE enrollments SET failures = ?, note = ?, status = ? WHERE id = ?",
                 (failures, msg[:500], status, e["id"]))
    event(conn, e["lead_id"], "error", enrollment=e, step_pos=k, detail=msg[:500])
    log(f"FAILED step {k + 1} for lead {e['lead_id']}: {msg[:200]}")


ENROLLED_SQL = """
    SELECT e.*, l.email, l.linkedin_url, l.first_name, l.last_name, l.company, l.title, l.data, l.li_state
    FROM enrollments e JOIN leads l ON l.id = e.lead_id
    WHERE e.campaign_id = ? AND e.status IN ('pending', 'active')
      AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.enrollment_id = e.id AND t.status = 'open')
    ORDER BY e.id
"""


def process_campaign(conn, c: dict, graph, me: str | None, now: datetime, send: bool, sleep, log) -> None:
    steps = rows(conn, "SELECT * FROM steps WHERE campaign_id = ? ORDER BY pos", c["id"])
    if not steps:
        return
    start = parse(c["start_at"])
    today = iso(day_start(c, now))
    li_today = scalar(conn, "SELECT COUNT(*) FROM events WHERE campaign_id = ? AND type = 'task_created' AND at >= ?",
                      c["id"], today)
    queue = []
    for e in rows(conn, ENROLLED_SQL, c["id"]):
        while True:
            k = e["step_pos"] + 1
            if k >= len(steps):
                conn.execute("UPDATE enrollments SET status = 'done' WHERE id = ?", (e["id"],))
                break
            st = steps[k]
            base = parse(e["last_step_at"]) or max(d for d in (start, parse(e["enrolled_at"])) if d)
            due = base + timedelta(days=st["delay_days"])
            if now < due:
                break
            reason, wait = gate(st, e, now, due)
            if wait:
                break
            if reason:
                _advance(conn, e, k, now)
                event(conn, e["lead_id"], "step_skipped", enrollment=e, channel=st["channel"], step_pos=k,
                      detail=reason, at=now)
                continue
            if st["channel"] == "email":
                queue.append((e, k, st))
                break
            if li_today >= c["li_daily_cap"]:
                break
            try:
                body = render_text(st["body"], lead_context(e))
            except TemplateError as ex:
                _fail(conn, e, k, f"template: {ex}", log, permanent=True)
                break
            conn.execute("INSERT INTO tasks (enrollment_id, step_pos, channel, body, created_at) VALUES (?, ?, ?, ?, ?)",
                         (e["id"], k, st["channel"], body, iso(now)))
            event(conn, e["lead_id"], "task_created", enrollment=e, channel=st["channel"], step_pos=k, at=now)
            li_today += 1
            break
    if queue:
        send_emails(conn, c, steps, queue, graph, me, now, send, sleep, log)


def send_emails(conn, c, steps, queue, graph, me, now, send, sleep, log) -> None:
    if not graph:
        log(f"{c['name']}: {len(queue)} emails waiting for Outlook connection")
        return
    if not in_window(c, now):
        log(f"{c['name']}: outside send window, {len(queue)} emails waiting")
        return
    sent_today = scalar(conn, "SELECT COUNT(*) FROM events WHERE campaign_id = ? AND type = 'sent' AND at >= ?",
                        c["id"], iso(day_start(c, now)))
    budget = max(0, min(MAX_EMAILS_PER_TICK, c["email_daily_cap"] - sent_today))
    queue.sort(key=lambda q: q[0]["conversation_id"] is None)  # follow-ups first: time-sensitive
    first_email = next(s for s in steps if s["channel"] == "email")
    sent = 0
    for e, k, st in queue[:budget]:
        ctx = lead_context(e)
        try:
            subject = render_text(st["subject"] or first_email["subject"], ctx)
            html = email_html(st["body"], c["signature"], ctx)
        except TemplateError as ex:
            _fail(conn, e, k, f"template: {ex}", log, permanent=True)
            continue
        if not send:
            log(f"[dry run] {c['name']} step {k + 1} -> {e['email']}: {subject}")
            continue
        if sent:
            sleep(random.uniform(c["gap_min"], c["gap_max"]))
        try:
            if e["conversation_id"]:
                # Replies can land between the batch check and now; recheck before nudging.
                watch = rows(conn, EMAIL_WATCH_SQL + " AND e.lead_id = ?", e["lead_id"])
                if watch and check_lead_email(conn, graph, me, watch, log):
                    continue
            if st["in_thread"] and e["conversation_id"]:
                cid = graph.send_in_thread(e["conversation_id"], e["email"], ctx["name"], html, "Re: " + subject)
            else:
                cid = graph.send_new(e["email"], ctx["name"], subject, html)
        except GraphError as ex:
            _fail(conn, e, k, str(ex), log, permanent=False)
            continue
        conn.execute("""UPDATE enrollments SET step_pos = ?, status = 'active', conversation_id = ?, failures = 0,
                          note = NULL, first_email_at = COALESCE(first_email_at, ?), last_step_at = ? WHERE id = ?""",
                     (k, cid, iso(now), iso(now), e["id"]))
        event(conn, e["lead_id"], "sent", enrollment=e, channel="email", step_pos=k, detail=subject, at=now)
        sent += 1
        log(f"sent {c['name']} step {k + 1} -> {e['email']}")


def tick(conn, graph, *, now: datetime | None = None, send: bool = True, sleep=time.sleep, log=print) -> None:
    now = now or utcnow()
    me = None
    if graph:
        try:
            me = graph.my_address()
            check_email(conn, graph, me, now, log)
            check_linkedin(conn, graph, now, log)
        except GraphError as ex:
            # Without a fresh reply check, follow-ups could go to someone who already answered.
            log(f"reply check failed, emails held this run: {ex}")
            graph = None
    else:
        log("Outlook not connected: email steps wait, LinkedIn tasks still queue")
    for c in rows(conn, "SELECT * FROM campaigns WHERE status = 'active' ORDER BY id"):
        process_campaign(conn, c, graph, me, now, send, sleep, log)
    db.set_setting(conn, "last_tick", iso(utcnow()))
