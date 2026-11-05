"""Orchestrator. One tick: detect replies (email + LinkedIn notification mails), then advance every active sequence.

Email steps send through Graph. LinkedIn steps become tasks in the dashboard queue; the sequence waits
until you mark the task done or skipped. A reply on any channel stops the lead everywhere.
Each campaign sends from one mailbox; a lead's follow-ups stay in the mailbox that started its thread.
Opens/clicks/unsubscribes are pulled from the tracking worker when configured.
Idempotent: safe to run every few minutes.
"""
from __future__ import annotations

import json
import random
import re
import secrets
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from jinja2 import TemplateError

from . import attachments, db
from .db import event, iso, one, parse, rows, scalar, utcnow
from .graph import AuthError, GraphError, NetworkError
from .render import email_html, lead_context, linkedin_text, render_text
from .tracking import BOT_UA, MACHINE_WINDOW_S

CHANNELS = {"email": "Email", "li_view": "View profile", "li_connect": "Connect", "li_message": "LinkedIn message"}
LI = ("li_view", "li_connect", "li_message")
# Task-only channel: recurring "like + comment on a recent post" alongside the sequence. Not a sequence step.
ENGAGE = "li_engage"
TASK_LABELS = {**CHANNELS, ENGAGE: "Like + comment"}
ENGAGE_DEFAULTS = {"engage_every_days": 7, "engage_lookback_months": 6}
CONDITIONS = {"always": "Always", "if_connected": "Only if already connected", "if_not_connected": "Only if not connected"}
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
OPEN = ("pending", "active", "done")
REPLY_LOOKBACK = timedelta(days=30)   # keep watching finished sequences this long
WAIT_FOR_CONNECT = timedelta(days=7)  # connection-gated steps wait this long past due, then skip
# Test mode: a campaign with "dummy" in its name runs its whole sequence within 30 minutes. Waits are scaled to
# DUMMY_TOTAL, leaving headroom for the 1-minute send check and the LinkedIn bot's pace between actions.
DUMMY_TOTAL = timedelta(minutes=25)
DUMMY_WAIT_FOR_CONNECT = timedelta(minutes=5)
DUMMY_ENGAGE_EVERY = timedelta(minutes=10)
DUMMY_GAP = (5, 15)  # seconds between emails


def is_dummy(c: dict) -> bool:
    return "dummy" in (c.get("name") or c.get("campaign_name") or "").lower()


def step_delay(c: dict, steps: list[dict], k: int) -> timedelta:
    """Real wait before step k; dummy campaigns scale every wait so the whole sequence fits in DUMMY_TOTAL."""
    d = timedelta(days=steps[k]["delay_days"])
    if not is_dummy(c):
        return d
    total = timedelta(days=sum(st["delay_days"] for st in steps))
    return d * min(1.0, DUMMY_TOTAL / total) if total else timedelta(0)
LI_LOOKBACK = timedelta(days=14)      # first scan of LinkedIn notification mails
MAX_FAILURES = 3
MAX_EMAILS_PER_TICK = 10

AUTO_SUBJECTS = ("automatic reply", "auto reply", "autoreply", "auto-reply", "auto response", "autoresponse",
                 "auto-response", "out of office", "out of the office", "ooo", "away from", "on leave",
                 "réponse automatique", "respuesta automática", "automatische antwort", "abwesend", "abwesenheit",
                 "risposta automatica", "resposta automática", "automatisch antwoord", "afwezig")
AUTO_BODY_RE = re.compile(
    r"\b(out of (the )?office|i am (currently )?(out|away|on leave|travelling|traveling|on vacation|on holiday)"
    r"|i'm (currently )?(out|away|on leave|travelling|traveling|on vacation|on holiday)"
    r"|(on|taking) (annual |maternity |paternity |sick |parental )?leave|away from (the|my) (office|desk)"
    r"|limited (access to|connectivity|availability)|(automatic|automated|auto) (reply|response|message)"
    r"|(respond|reply|get back to you|attend to your (e-?mail|message)).{0,40}(when|upon|on|after) (i|my) return"
    r"|no longer (with|at|working)|this (mailbox|e-?mail address) is (no longer|not) (monitored|in use))")
AUTO_PRECEDENCE = ("auto_reply", "bulk", "junk", "list")
REPLY_PREFIX_RE = re.compile(r"^\s*((re|fw|fwd|aw|wg|sv|vs|tr|rv|r|antw|vb|rif|odp|res|enc)\s*(\[\d+\])?\s*:\s*)+", re.I)
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
    if (headers.get("auto-submitted", "no") != "no" or "x-autoreply" in headers or "x-autorespond" in headers
            or headers.get("precedence", "") in AUTO_PRECEDENCE or subject.startswith(AUTO_SUBJECTS)
            or topic(subject).startswith(AUTO_SUBJECTS)
            or AUTO_BODY_RE.search(_norm_space(msg.get("bodyPreview") or "")[:400])):
        return "auto"
    return "reply"


def topic(subject: str | None) -> str:
    """'RE: FW: Pick an account' -> 'pick an account': what a reply or forward is about."""
    return " ".join(REPLY_PREFIX_RE.sub("", subject or "").lower().split())


def _norm_space(s: str) -> str:
    return " ".join(s.lower().replace("\u2019", "'").split())


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
    """Stop the lead in every campaign (the one that got the reply is 'replied', others 'stopped') and move them
    to Leads. Still counts for someone whose sequences already ended (e.g. bounced). False if nothing changed."""
    ens = rows(conn, "SELECT * FROM enrollments WHERE lead_id = ? AND status IN ('pending', 'active', 'done')", lead_id)
    if not ens and scalar(conn, "SELECT stage FROM leads WHERE id = ?", lead_id) == "lead":
        return False
    if enrollment is None:
        contacted = [e for e in ens if e["step_pos"] >= 0] or ens or \
            rows(conn, "SELECT * FROM enrollments WHERE lead_id = ? ORDER BY id DESC LIMIT 1", lead_id)
        enrollment = max(contacted, key=lambda e: e["last_step_at"] or "") if contacted else None
    for e in ens:
        if e["id"] == enrollment["id"]:
            conn.execute("UPDATE enrollments SET status = 'replied', replied_at = ?, reply_channel = ? WHERE id = ?",
                         (iso(at), channel, e["id"]))
        else:
            conn.execute("UPDATE enrollments SET status = 'stopped', note = ? WHERE id = ?",
                         (f"replied via {channel} in another campaign", e["id"]))
        close_tasks(conn, e["id"], at)
    event(conn, lead_id, "reply", enrollment=enrollment, channel=channel,
          step_pos=enrollment["step_pos"] if enrollment else None, detail=detail, at=at)
    # They responded: out of every sequence (above) and into Leads.
    conn.execute("UPDATE leads SET stage = 'lead', became_lead_at = COALESCE(became_lead_at, ?) WHERE id = ?",
                 (iso(at), lead_id))
    return True


def unsubscribe(conn, lead_id: int, at: datetime, enrollment: dict | None = None, detail: str | None = None) -> bool:
    """Do-not-contact: stops every sequence for the lead, now and in future imports."""
    if scalar(conn, "SELECT dnc FROM leads WHERE id = ?", lead_id):
        return False
    conn.execute("UPDATE leads SET dnc = 1 WHERE id = ?", (lead_id,))
    for e in rows(conn, "SELECT * FROM enrollments WHERE lead_id = ? AND status IN ('pending', 'active', 'done')", lead_id):
        conn.execute("UPDATE enrollments SET status = 'stopped', note = 'unsubscribed' WHERE id = ?", (e["id"],))
        close_tasks(conn, e["id"], at)
    event(conn, lead_id, "unsubscribe", enrollment=enrollment, channel="email", detail=detail, at=at)
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


def _complete_engage(conn, t: dict, e: dict, action: str, comment: str | None, engage: dict, now: datetime,
                     actor: str = "you") -> None:
    """Engage tasks never move the sequence; they only record what you did."""
    if action == "connected":
        set_connected(conn, e["lead_id"], now, source="marked from queue")
        return
    if action == "done":
        url = (engage.get("post_url") or "").strip() or None
        liked, commented = bool(engage.get("liked")), bool(engage.get("commented"))
        if not (liked or commented):
            raise ValueError("Tick Liked and/or Commented, or use No recent posts")
        if url and scalar(conn, "SELECT 1 FROM engagements WHERE lead_id = ? AND post_url = ?", e["lead_id"], url):
            raise ValueError("You already engaged with this post. Pick another one")
        comment = (comment or "").strip() or None
        conn.execute("INSERT INTO engagements (lead_id, task_id, post_url, liked, commented, comment, at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)", (e["lead_id"], t["id"], url, int(liked), int(commented),
                                                      comment if commented else None, iso(now)))
        did = " + ".join(x for x, on in (("liked", liked), ("commented", commented)) if on)
        detail = did + (f" · {url}" if url else "") + (f"\n{comment}" if commented and comment else "")
        conn.execute("UPDATE tasks SET status = 'done', body = COALESCE(?, body), done_at = ? WHERE id = ?",
                     (comment, iso(now), t["id"]))
        event(conn, e["lead_id"], "task_done", enrollment=e, channel=ENGAGE, detail=_by(actor, detail[:600]), at=now)
    else:
        conn.execute("UPDATE tasks SET status = 'skipped', done_at = ? WHERE id = ?", (iso(now), t["id"]))
        event(conn, e["lead_id"], "task_skipped", enrollment=e, channel=ENGAGE, detail=_by(actor, "no recent posts"), at=now)


def resume_enrollment(conn, enrollment_id: int, now: datetime | None = None) -> str:
    """Restart a lead's sequence from where it broke. Returns a short description; ValueError if it can't resume."""
    now = now or utcnow()
    e = one(conn, "SELECT e.*, l.dnc FROM enrollments e JOIN leads l ON l.id = e.lead_id WHERE e.id = ?", enrollment_id)
    if not e:
        raise ValueError("not found")
    if e["dnc"]:
        raise ValueError("this lead unsubscribed / is marked do-not-contact")
    if e["status"] == "replied" or (e["note"] or "").startswith("replied via"):
        raise ValueError("this lead replied; not restarting their sequence")
    if e["status"] not in ("bounced", "error", "stopped"):
        raise ValueError(f"nothing to resume (status is {e['status']})")
    step_pos, conv, first_email, how = e["step_pos"], e["conversation_id"], e["first_email_at"], "continues"
    if e["status"] == "bounced":
        # Re-run the email that bounced, as a fresh thread (the old one never reached them).
        b = one(conn, "SELECT step_pos FROM events WHERE enrollment_id = ? AND type = 'bounce' ORDER BY id DESC LIMIT 1",
                enrollment_id)
        bounced = b["step_pos"] if b and b["step_pos"] is not None else e["step_pos"]
        step_pos, conv, first_email, how = bounced - 1, None, None, f"resends step {bounced + 1}"
    conn.execute("""UPDATE enrollments SET status = ?, step_pos = ?, conversation_id = ?, first_email_at = ?,
                      failures = 0, note = NULL, last_step_at = CASE WHEN ? THEN NULL ELSE last_step_at END
                    WHERE id = ?""",
                 ("pending" if step_pos < 0 else "active", step_pos, conv, first_email,
                  1 if e["status"] == "bounced" else 0, enrollment_id))
    event(conn, e["lead_id"], "resumed", enrollment=e, step_pos=step_pos + 1, detail=f"after {e['status']}: {how}", at=now)
    return how


def copy_campaign(conn, cid: int, now: datetime | None = None) -> int:
    """A new draft with the same settings and sequence (attachments included), no leads. Returns its id."""
    c = one(conn, "SELECT * FROM campaigns WHERE id = ?", cid)
    if not c:
        raise ValueError("not found")
    names = {r["name"] for r in rows(conn, "SELECT name FROM campaigns")}
    name, n = f"{c['name']} (copy)", 2
    while name in names:
        name, n = f"{c['name']} (copy {n})", n + 1
    keep = [k for k in c if k not in ("id", "name", "status", "start_at", "created_at")]
    cols = ["name", "status", "created_at", *keep]
    cur = conn.execute(f"INSERT INTO campaigns ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                       (name, "draft", iso(now or utcnow()), *(c[k] for k in keep)))
    new = cur.lastrowid
    st_cols = [r["name"] for r in rows(conn, "PRAGMA table_info(steps)") if r["name"] not in ("id", "campaign_id")]
    conn.execute(f"INSERT INTO steps (campaign_id, {', '.join(st_cols)}) "
                 f"SELECT ?, {', '.join(st_cols)} FROM steps WHERE campaign_id = ? ORDER BY pos", (new, cid))
    return new


def undo_reply(conn, lead_id: int, now: datetime | None = None) -> int:
    """'Not a reply': put a lead back into the sequences their last reply stopped, from where they were.
    Messages up to now are ignored from then on. Returns how many sequences restart; ValueError if none."""
    now = now or utcnow()
    r = one(conn, "SELECT * FROM events WHERE lead_id = ? AND type = 'reply' ORDER BY at DESC, id DESC LIMIT 1", lead_id)
    if not r:
        raise ValueError("no reply to undo")
    if scalar(conn, "SELECT dnc FROM leads WHERE id = ?", lead_id):
        raise ValueError("this lead is marked do-not-contact")
    ens = rows(conn, """SELECT * FROM enrollments WHERE lead_id = ?
                        AND (status = 'replied' OR (status = 'stopped' AND note LIKE 'replied via %'))""", lead_id)
    for e in ens:
        conn.execute("""UPDATE enrollments SET status = ?, replied_at = NULL, reply_channel = NULL, note = NULL
                        WHERE id = ?""", ("pending" if e["step_pos"] < 0 else "active", e["id"]))
    conn.execute("UPDATE events SET type = 'reply_ignored' WHERE id = ?", (r["id"],))
    conn.execute("UPDATE leads SET stage = 'prospect', became_lead_at = NULL, reply_ignored_before = ? WHERE id = ?",
                 (iso(now), lead_id))
    event(conn, lead_id, "resumed", enrollment=ens[0] if ens else None, detail="not a reply: sequence continues", at=now)
    return len(ens)


def set_lead_contact(conn, lead_id: int, email: str | None, linkedin_url: str | None, now: datetime | None = None) -> None:
    """Edit a lead's address/URL. A changed email clears a bounce, so email steps resume from the next one."""
    old = one(conn, "SELECT email FROM leads WHERE id = ?", lead_id)
    conn.execute("UPDATE leads SET email = ?, linkedin_url = ? WHERE id = ?", (email, linkedin_url, lead_id))
    if old and (old["email"] or "") != (email or ""):
        conn.execute("UPDATE leads SET email_bounced = 0, email_updated_at = ? WHERE id = ?", (iso(now or utcnow()), lead_id))


def _advance(conn, e: dict, k: int, now: datetime) -> None:
    conn.execute("UPDATE enrollments SET step_pos = ?, last_step_at = ?, status = 'active' WHERE id = ?",
                 (k, iso(now), e["id"]))
    e.update(step_pos=k, last_step_at=iso(now), status="active")


def complete_task(conn, task_id: int, action: str, body: str | None = None, now: datetime | None = None,
                  engage: dict | None = None, actor: str = "you") -> None:
    """action: done | skip | connected (lead already connected) | replied (they answered on LinkedIn).
    engage: for Engage tasks, {"post_url", "liked", "commented"}; body is the comment."""
    now = now or utcnow()
    t = one(conn, "SELECT * FROM tasks WHERE id = ?", task_id)
    if not t or t["status"] != "open":
        raise ValueError("task already handled")
    e = one(conn, "SELECT * FROM enrollments WHERE id = ?", t["enrollment_id"])
    if action == "replied":
        mark_replied(conn, e["lead_id"], "linkedin", now, enrollment=e, detail="marked from queue")
        return
    if t["channel"] == ENGAGE:
        _complete_engage(conn, t, e, action, body, engage or {}, now, actor)
        return
    if action == "connected":
        set_connected(conn, e["lead_id"], now, source="seen by bot" if actor == "bot" else "marked from queue")
    status = "done" if action == "done" else "skipped"
    conn.execute("UPDATE tasks SET status = ?, body = COALESCE(?, body), done_at = ? WHERE id = ?",
                 (status, body, iso(now), task_id))
    if status == "done" and t["channel"] == "li_connect":
        conn.execute("UPDATE leads SET li_state = 'invited' WHERE id = ? AND li_state = 'none'", (e["lead_id"],))
    _advance(conn, e, t["step_pos"], now)
    event(conn, e["lead_id"], "task_done" if status == "done" else "task_skipped", enrollment=e,
          channel=t["channel"], step_pos=t["step_pos"],
          detail=_by(actor, (body or t["body"])[:300] if status == "done" else action), at=now)


def _by(actor: str, detail: str | None) -> str | None:
    return f"by bot · {detail}" if actor == "bot" else detail


# ---------- mailboxes ----------

class Mailboxes:
    """Per-run Graph clients keyed by sender account. A mailbox that can't sign in, or whose reply check fails,
    is held for the rest of the run: no sends, since follow-ups could reach people who already answered."""

    def __init__(self, factory, log):
        self.factory, self.log = factory, log
        self._g: dict = {}
        self.held: set[str] = set()

    def get(self, account: str | None):
        if not account or not self.factory or account in self.held:
            return None
        if account not in self._g:
            try:
                self._g[account] = self.factory(account)
            except AuthError as ex:
                self.hold(account, str(ex))
                return None
        return self._g[account]

    def hold(self, account: str, why: str) -> None:
        if account not in self.held:
            self.held.add(account)
            self.log(f"{account}: {why} Its emails wait this run.")


def default_sender(conn) -> str | None:
    return scalar(conn, "SELECT account FROM senders ORDER BY created_at, account LIMIT 1")


def sender_email(conn, account: str) -> str:
    return scalar(conn, "SELECT email FROM senders WHERE account = ?", account) or account


# ---------- reply detection ----------

def check_lead_email(conn, graph, es: list[dict], log) -> bool:
    """es: one lead's enrollments in one mailbox (conversation_id + email). True if the lead replied or bounced.
    Out-of-office and other automatic replies are logged but don't stop anything."""
    me = graph.my_address()
    msgs: dict[str, tuple[dict, dict]] = {}
    for e in es:
        msgs.update({m["id"]: (m, e) for m in graph.conversation(e["conversation_id"])})
    since = min(parse(e["first_email_at"]) for e in es)
    latest = max(es, key=lambda e: e["last_step_at"] or "")
    lead_id = es[0]["lead_id"]
    # Mail outside the thread counts only when it's about our email (they replied to or forwarded a copy),
    # not every message they send you: colleagues and existing contacts email you about other things.
    ours = {topic(r["detail"]) for r in rows(conn, "SELECT detail FROM events WHERE lead_id = ? AND type = 'sent'", lead_id)}
    ours |= {topic(m.get("subject")) for m, _ in msgs.values() if _sender(m) == me}
    ours.discard("")
    for m in graph.inbound_from(es[0]["email"], since):
        hs = {h["name"].lower(): h["value"] for h in m.get("internetMessageHeaders") or []}
        if m["id"] in msgs or topic(m.get("subject")) in ours or topic(hs.get("thread-topic")) in ours:
            msgs.setdefault(m["id"], (m, latest))
    ignored = parse(scalar(conn, "SELECT reply_ignored_before FROM leads WHERE id = ?", lead_id))
    reply = bounce = None
    for m, e in sorted(msgs.values(), key=lambda p: p[0].get("receivedDateTime") or ""):
        kind = classify(m, me)
        if kind == "reply" and not reply and not (ignored and parse(m["receivedDateTime"]) <= ignored):
            reply = (m, e)
        elif kind == "bounce" and not bounce:
            bounce = (m, e)
        elif kind == "auto":
            at = iso(parse(m["receivedDateTime"]))
            if not scalar(conn, "SELECT 1 FROM events WHERE lead_id = ? AND type = 'auto_reply' AND at = ?", lead_id, at):
                event(conn, lead_id, "auto_reply", enrollment=e, channel="email", step_pos=e["step_pos"],
                      detail=m.get("subject"), at=parse(at))
                log(f"automatic reply from {e['email']} ({m.get('subject')}): sequence continues")
    if reply:
        m, e = reply
        mark_replied(conn, e["lead_id"], "email", parse(m["receivedDateTime"]), enrollment=e, detail=m.get("subject"))
        log(f"reply (email) from {e['email']}")
        return True
    if bounce:
        m, e = bounce
        at = parse(m["receivedDateTime"])
        lead = one(conn, "SELECT email, email_bounced, email_updated_at FROM leads WHERE id = ?", e["lead_id"])
        text = f"{m.get('subject') or ''} {m.get('bodyPreview') or ''}".lower()
        # Only for the current address: an old bounce must not re-flag an address you've since fixed.
        about_now = (lead["email"] or "").lower() in text if "@" in text else True
        if lead["email_bounced"]:
            return True  # already flagged: don't email
        if not about_now or (lead["email_updated_at"] and at <= parse(lead["email_updated_at"])):
            return False  # a bounce for an address you've since fixed: carry on
        conn.execute("UPDATE leads SET email_bounced = 1 WHERE id = ?", (e["lead_id"],))
        conn.execute("UPDATE enrollments SET note = 'email bounced' WHERE id = ?", (e["id"],))
        event(conn, e["lead_id"], "bounce", enrollment=e, channel="email", step_pos=e["step_pos"],
              detail=m.get("subject"), at=at)
        log(f"bounce for {e['email']}: email steps skipped until the address is fixed; LinkedIn steps continue")
        return True
    return False


EMAIL_WATCH_SQL = """
    SELECT e.*, l.email, l.email_bounced, COALESCE(e.sender, c.sender) AS acct FROM enrollments e
    JOIN leads l ON l.id = e.lead_id JOIN campaigns c ON c.id = e.campaign_id
    WHERE c.status != 'draft' AND e.status IN ('active', 'done') AND e.conversation_id IS NOT NULL
"""


def check_email(conn, boxes: Mailboxes, now: datetime, log) -> None:
    fallback = default_sender(conn)
    groups = defaultdict(list)
    for e in rows(conn, EMAIL_WATCH_SQL + " AND e.last_step_at >= ?", iso(now - REPLY_LOOKBACK)):
        groups[(e["acct"] or fallback, e["lead_id"])].append(e)
    for (acct, _), es in groups.items():
        graph = boxes.get(acct)
        if not graph:
            continue
        try:
            check_lead_email(conn, graph, es, log)
        except GraphError as ex:
            boxes.hold(acct, f"reply check failed ({ex}).")


def check_linkedin(conn, boxes: Mailboxes, now: datetime, log) -> None:
    acct = db.get_setting(conn, "li_mailbox") or default_sender(conn)
    graph = boxes.get(acct)
    if not graph:
        return
    cursor = parse(db.get_setting(conn, "li_cursor")) or now - LI_LOOKBACK
    try:
        msgs = graph.messages_since(cursor)
    except GraphError as ex:
        log(f"LinkedIn notification check failed: {ex}")
        return
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


# ---------- open / click / unsubscribe tracking ----------

def apply_tracking_event(conn, ev: dict) -> None:
    s = one(conn, "SELECT * FROM sends WHERE id = ?", ev["sid"])
    if not s:
        return
    at = datetime.fromtimestamp(ev["ts"] / 1000, timezone.utc).replace(microsecond=0)
    enr = {"id": s["enrollment_id"], "campaign_id": s["campaign_id"]}
    if ev["type"] == "unsubscribe":
        unsubscribe(conn, s["lead_id"], at, enrollment=enr, detail="unsubscribe link")
        return
    if ev.get("head") or BOT_UA.search(ev.get("ua") or "") or (at - parse(s["sent_at"])).total_seconds() < MACHINE_WINDOW_S:
        conn.execute("UPDATE sends SET machine_events = machine_events + 1 WHERE id = ?", (s["id"],))
        return
    if ev["type"] == "open":
        conn.execute("UPDATE sends SET opens = opens + 1, opened_at = COALESCE(opened_at, ?) WHERE id = ?",
                     (iso(at), s["id"]))
        if not s["opened_at"]:
            event(conn, s["lead_id"], "open", enrollment=enr, channel="email", step_pos=s["step_pos"],
                  detail=s["subject"], at=at)
    elif ev["type"] == "click":
        # A click proves an open even when images were blocked.
        conn.execute("""UPDATE sends SET clicks = clicks + 1, clicked_at = COALESCE(clicked_at, ?),
                          opened_at = COALESCE(opened_at, ?) WHERE id = ?""", (iso(at), iso(at), s["id"]))
        event(conn, s["lead_id"], "click", enrollment=enr, channel="email", step_pos=s["step_pos"],
              detail=ev.get("url"), at=at)


def ingest_tracking(conn, tracker, log) -> None:
    cursor = db.get_setting(conn, "track_cursor", 0)
    while True:
        evs = tracker.pull(cursor)
        for ev in evs:
            apply_tracking_event(conn, ev)
            cursor = max(cursor, ev["id"])
        db.set_setting(conn, "track_cursor", cursor)
        if evs:
            log(f"tracking: {len(evs)} events")
        if len(evs) < 1000:
            return


# ---------- sequencing ----------

def gate(st: dict, e: dict, now: datetime, due: datetime,
         wait_for_connect: timedelta = WAIT_FOR_CONNECT) -> tuple[str | None, bool]:
    """(skip reason, wait). Neither = go."""
    ch, li_state = st["channel"], e["li_state"]
    if ch == "email" and not e["email"]:
        return "no email address", False
    if ch == "email" and e.get("email_bounced"):
        return "email address bounced (fix it on the contact to resume email)", False
    if ch in LI and not e["linkedin_url"]:
        return "no LinkedIn URL", False
    if st["condition"] == "if_connected" and li_state != "connected":
        return "not connected", False  # an explicit branch: decide now, don't wait
    if ch == "li_message" and li_state != "connected":
        # A plain LinkedIn message waits for the invite to be accepted, then gives up.
        return ("not connected", False) if now >= due + wait_for_connect else (None, True)
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
    SELECT e.*, l.email, l.linkedin_url, l.first_name, l.last_name, l.company, l.title, l.data, l.li_state, l.dnc,
           l.email_bounced
    FROM enrollments e JOIN leads l ON l.id = e.lead_id
    WHERE e.campaign_id = ? AND e.status IN ('pending', 'active')
      AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.enrollment_id = e.id AND t.status = 'open'
                      AND t.channel != 'li_engage')
    ORDER BY e.id
"""

# Leads in a live sequence who've had a LinkedIn invite or an email, with no Engage task open or created within
# the cadence window (checked across campaigns, so one person never gets two in a week).
ENGAGE_SQL = """
    SELECT e.* FROM enrollments e JOIN leads l ON l.id = e.lead_id
    WHERE e.campaign_id = ? AND e.status = 'active' AND l.linkedin_url IS NOT NULL AND l.dnc = 0
      AND (l.li_state IN ('invited', 'connected') OR EXISTS (SELECT 1 FROM sends s WHERE s.enrollment_id = e.id))
      AND NOT EXISTS (SELECT 1 FROM tasks t JOIN enrollments e2 ON e2.id = t.enrollment_id
                      WHERE e2.lead_id = e.lead_id AND t.channel = 'li_engage'
                        AND (t.status = 'open' OR t.created_at >= ?))
    ORDER BY e.last_step_at
"""


def engage_settings(conn) -> dict:
    return {k: db.get_setting(conn, k, v) for k, v in ENGAGE_DEFAULTS.items()}


def process_campaign(conn, c: dict, boxes: Mailboxes, tracker, now: datetime, send: bool, sleep, log) -> None:
    steps = rows(conn, "SELECT * FROM steps WHERE campaign_id = ? ORDER BY pos", c["id"])
    if not steps:
        return
    start = parse(c["start_at"])
    today = iso(day_start(c, now))
    li_today = scalar(conn, "SELECT COUNT(*) FROM events WHERE campaign_id = ? AND type = 'task_created' AND at >= ?",
                      c["id"], today)
    queue = []
    for e in rows(conn, ENROLLED_SQL, c["id"]):
        if e["dnc"]:
            stop_enrollment(conn, e["id"], "do not contact", now)
            continue
        while True:
            k = e["step_pos"] + 1
            if k >= len(steps):
                conn.execute("UPDATE enrollments SET status = 'done' WHERE id = ?", (e["id"],))
                break
            st = steps[k]
            base = parse(e["last_step_at"]) or max(d for d in (start, parse(e["enrolled_at"])) if d)
            due = base + step_delay(c, steps, k)
            if now < due:
                break
            reason, wait = gate(st, e, now, due, DUMMY_WAIT_FOR_CONNECT if is_dummy(c) else WAIT_FOR_CONNECT)
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
                body = linkedin_text(render_text(st["body"], lead_context(e)))
            except TemplateError as ex:
                _fail(conn, e, k, f"template: {ex}", log, permanent=True)
                break
            conn.execute("INSERT INTO tasks (enrollment_id, step_pos, channel, body, created_at) VALUES (?, ?, ?, ?, ?)",
                         (e["id"], k, st["channel"], body, iso(now)))
            event(conn, e["lead_id"], "task_created", enrollment=e, channel=st["channel"], step_pos=k, at=now)
            li_today += 1
            break
    if c["li_engage"]:
        # Sequence steps took their share of today's LinkedIn budget first; engagement uses what's left.
        every = DUMMY_ENGAGE_EVERY if is_dummy(c) else timedelta(days=engage_settings(conn)["engage_every_days"])
        for e in rows(conn, ENGAGE_SQL, c["id"], iso(now - every)):
            if li_today >= c["li_daily_cap"]:
                break
            conn.execute("INSERT INTO tasks (enrollment_id, step_pos, channel, body, created_at) VALUES (?, -1, ?, '', ?)",
                         (e["id"], ENGAGE, iso(now)))
            event(conn, e["lead_id"], "task_created", enrollment=e, channel=ENGAGE, at=now)
            li_today += 1
    if queue:
        send_emails(conn, c, steps, queue, boxes, tracker, now, send, sleep, log)


def mailbox_remaining(conn, account: str, c: dict, now: datetime) -> int:
    cap = scalar(conn, "SELECT daily_cap FROM senders WHERE account = ?", account) or 150
    used = scalar(conn, "SELECT COUNT(*) FROM sends WHERE sender = ? AND sent_at >= ?", account, iso(day_start(c, now)))
    return cap - used


def send_emails(conn, c, steps, queue, boxes: Mailboxes, tracker, now, send, sleep, log) -> None:
    if not in_window(c, now) and not is_dummy(c):
        log(f"{c['name']}: outside send window, {len(queue)} emails waiting")
        return
    sent_today = scalar(conn, "SELECT COUNT(*) FROM sends WHERE campaign_id = ? AND sent_at >= ?",
                        c["id"], iso(day_start(c, now)))
    budget = max(0, min(MAX_EMAILS_PER_TICK, c["email_daily_cap"] - sent_today))
    queue.sort(key=lambda q: q[0]["conversation_id"] is None)  # follow-ups first: time-sensitive
    first_email = next(s for s in steps if s["channel"] == "email")
    fallback = c["sender"] or default_sender(conn)
    remaining: dict[str, int] = {}
    warned: set[str] = set()
    sent = 0
    for e, k, st in queue:
        if sent >= budget:
            break
        acct = e["sender"] or fallback  # follow-ups stay in the mailbox that started the thread
        if not acct:
            log(f"{c['name']}: no mailbox connected, {len(queue)} emails waiting")
            return
        graph = boxes.get(acct) if send else None
        if send and not graph:
            continue
        if acct not in remaining:
            remaining[acct] = mailbox_remaining(conn, acct, c, now)
        if remaining[acct] <= 0:
            if acct not in warned:
                warned.add(acct)
                log(f"{acct}: daily cap reached, emails wait until tomorrow")
            continue
        sid = secrets.token_urlsafe(9)
        ctx = lead_context(e)
        ctx["unsubscribe_url"] = (tracker.unsub_url(sid) if tracker
                                  else f"mailto:{sender_email(conn, acct)}?subject=Unsubscribe")
        link = (lambda u, sid=sid: u if tracker.owns(u) else tracker.click_url(sid, u)) \
            if tracker and c["track_clicks"] else None
        pixel = tracker.open_url(sid) if tracker and c["track_opens"] else None
        try:
            subject = render_text(st["subject"] or first_email["subject"], ctx)
            html = email_html(st["body"], c["signature"], ctx, link=link, pixel=pixel)
            files = attachments.load(json.loads(st.get("attachments") or "[]"))
        except TemplateError as ex:
            _fail(conn, e, k, f"template: {ex}", log, permanent=True)
            continue
        except attachments.AttachmentError as ex:
            _fail(conn, e, k, str(ex), log, permanent=True)
            continue
        if not send:
            log(f"[dry run] {c['name']} step {k + 1} {acct} -> {e['email']}: {subject}")
            continue
        if sent:
            sleep(random.uniform(*(DUMMY_GAP if is_dummy(c) else (c["gap_min"], c["gap_max"]))))
        try:
            if e["conversation_id"]:
                # Replies can land between the batch check and now; recheck before nudging.
                watch = [w for w in rows(conn, EMAIL_WATCH_SQL + " AND e.lead_id = ?", e["lead_id"])
                         if (w["acct"] or fallback) == acct]
                if watch and check_lead_email(conn, graph, watch, log):
                    continue
            if st["in_thread"] and e["conversation_id"]:
                cid = graph.send_in_thread(e["conversation_id"], e["email"], ctx["name"], html, "Re: " + subject,
                                           strip_pixels=tracker.base if tracker else None, attachments=files)
            else:
                cid = graph.send_new(e["email"], ctx["name"], subject, html, attachments=files)
        except NetworkError as ex:
            boxes.hold(acct, f"network problem ({ex}).")  # not the lead's fault: don't count a failure
            continue
        except GraphError as ex:
            _fail(conn, e, k, str(ex), log, permanent=False)
            continue
        conn.execute("""UPDATE enrollments SET step_pos = ?, status = 'active', conversation_id = ?, failures = 0,
                          note = NULL, sender = ?, first_email_at = COALESCE(first_email_at, ?), last_step_at = ?
                        WHERE id = ?""", (k, cid, acct, iso(now), iso(now), e["id"]))
        conn.execute("""INSERT INTO sends (id, enrollment_id, campaign_id, lead_id, step_pos, sender, subject, tracked, sent_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                     (sid, e["id"], c["id"], e["lead_id"], k, acct, subject, (1 if pixel else 0) | (2 if link else 0),
                      iso(now)))
        event(conn, e["lead_id"], "sent", enrollment=e, channel="email", step_pos=k, detail=subject, at=now)
        remaining[acct] -= 1
        sent += 1
        log(f"sent {c['name']} step {k + 1} {acct} -> {e['email']}")


def tick(conn, factory, *, tracker=None, now: datetime | None = None, send: bool = True, sleep=time.sleep,
         log=print, checks: bool = True) -> None:
    """factory(account) -> Graph for that mailbox (raises AuthError if not signed in); None = no Outlook at all.
    checks=False is the quick pass (every minute): send what's due and queue LinkedIn tasks, skipping the
    inbox-wide reply/LinkedIn-notification/tracking scans. Follow-ups still recheck that lead's replies first."""
    now = now or utcnow()
    boxes = Mailboxes(factory, log)
    if checks and (not factory or not default_sender(conn)):
        log("No mailbox connected: email steps wait, LinkedIn tasks still queue")
    if checks:
        check_email(conn, boxes, now, log)
        check_linkedin(conn, boxes, now, log)
    if tracker and not checks:
        tracker = tracker if db.get_setting(conn, "tracker_ok", True) else None
    elif tracker:
        problem = tracker.health()
        db.set_setting(conn, "tracker_ok", not problem)
        if problem:
            # Never put links in real emails that point at a tracker that isn't answering.
            log(f"tracking off this run ({tracker.base}: {problem}); emails go out with plain links")
            tracker = None
        else:
            try:
                ingest_tracking(conn, tracker, log)
            except Exception as ex:  # tracking is best-effort; never block sending on it
                log(f"tracking pull failed: {ex}")
    for c in rows(conn, "SELECT * FROM campaigns WHERE status = 'active' ORDER BY id"):
        process_campaign(conn, c, boxes, tracker, now, send, sleep, log)
    db.set_setting(conn, "last_tick", iso(utcnow()))
