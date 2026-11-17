"""CRM layer: lead owners and statuses, calls/meetings/notes, follow-up reminders, the daily reminder email, and one
communications history per lead.

Emails, LinkedIn and WhatsApp steps are never re-entered by hand: they're read from what the app already records
(sends, events, tasks, engagements, bot_actions). Only calls, meetings, notes and messages sent outside the app
live in `activities`, logged by whoever works the lead.
"""
from __future__ import annotations

import html as _html
import os
import re
from datetime import date, datetime, timedelta
from urllib.parse import quote

from . import db
from .db import event, iso, one, parse, rows, scalar, utcnow
from .render import lead_phone, wa_digits

STATUSES = ["New", "Warm", "Cold", "Converted", "Closed/Lost"]
CLOSED = ("Converted", "Closed/Lost")           # no more outreach: sequences stop
STATUS_TONE = {"New": "info", "Warm": "warning", "Cold": "muted", "Converted": "good", "Closed/Lost": "critical"}
SETTINGS = {"crm_warm_days": 3, "crm_cold_days": 90, "digest_enabled": True, "digest_hour": 8, "digest_team": True,
            "digest_skip_weekends": False}
KINDS = {"call": "Call", "meeting": "Meeting", "whatsapp": "WhatsApp / SMS", "linkedin": "LinkedIn message", "note": "Note"}
OUTCOMES = {
    "call": ["Connected – interested", "Connected – not interested", "Connected – call back later", "Meeting booked",
             "Left voicemail", "No answer", "Gatekeeper", "Wrong number"],
    "meeting": ["Held", "No-show", "Rescheduled", "Cancelled"],
    "whatsapp": ["Sent", "They replied"],
    "linkedin": ["Sent", "They replied"],
    "note": [],
}
# Outcomes where they actually responded: offered as "stop their sequences" (ticked by default).
RESPONDED = {"Connected – interested", "Connected – not interested", "Connected – call back later", "Meeting booked",
             "Held", "They replied"}
# The status the log dialog proposes for an outcome; the person logging confirms or changes it.
SUGGESTED_STATUS = {"Connected – interested": "Warm", "Connected – call back later": "Warm", "Meeting booked": "Warm",
                    "Held": "Warm", "They replied": "Warm", "Connected – not interested": "Cold"}
VOICEMAIL = "Left voicemail"
LOGGED = "logged · "  # prefix on the reply event a logged call/message creates; the log entry itself is the record
ROLES = ["BDR", "SDR", "AE", "Manager", "Other"]


def local_tz():
    return datetime.now().astimezone().tzinfo


# ---------- tags ----------

MAX_TAGS = 6
TAG_LEN = 40


def clean_tag(t) -> str:
    """One tag: trimmed, single-spaced, no commas (they separate tags), at most TAG_LEN characters."""
    return " ".join(str(t or "").replace(",", " ").replace(";", " ").split())[:TAG_LEN].strip()


def split_tags(s) -> list[str]:
    """'Fintech, P1; 2024' -> ['Fintech', 'P1', '2024'], duplicates (any case) dropped."""
    out: list[str] = []
    for t in re.split(r"[,;|\n]", str(s or "")):
        t = clean_tag(t)
        if t and t.lower() not in {x.lower() for x in out}:
            out.append(t)
    return out


def _spelling(conn, tag: str) -> str:
    """The spelling already in use for this tag (tags match case-insensitively), so 'fintech' joins 'Fintech'."""
    return scalar(conn, "SELECT tag FROM lead_tags WHERE tag = ? LIMIT 1", tag) or tag


def tags_of(conn, lead_ids) -> dict[int, list[str]]:
    ids = [int(i) for i in lead_ids]
    out: dict[int, list[str]] = {i: [] for i in ids}
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        for r in rows(conn, f"SELECT lead_id, tag FROM lead_tags WHERE lead_id IN ({','.join('?' * len(part))}) "
                            "ORDER BY rowid", *part):
            out[r["lead_id"]].append(r["tag"])
    return out


def set_tags(conn, lead_id: int, tags) -> list[str]:
    """Replace a lead's tags. ValueError over MAX_TAGS."""
    tags = split_tags(",".join(tags) if isinstance(tags, (list, tuple)) else tags)
    if len(tags) > MAX_TAGS:
        raise ValueError(f"At most {MAX_TAGS} tags per lead")
    tags = [_spelling(conn, t) for t in tags]
    conn.execute("DELETE FROM lead_tags WHERE lead_id = ?", (lead_id,))
    for t in tags:
        conn.execute("INSERT OR IGNORE INTO lead_tags (lead_id, tag) VALUES (?, ?)", (lead_id, t))
    return tags


def add_tags(conn, lead_ids, tags) -> dict:
    """Add tags to many leads; a lead that would go over MAX_TAGS keeps what it has. Returns counts."""
    tags = [_spelling(conn, t) for t in split_tags(",".join(tags) if isinstance(tags, (list, tuple)) else tags)]
    have = tags_of(conn, lead_ids)
    added = full = 0
    for lid, cur in have.items():
        new = [t for t in tags if t.lower() not in {c.lower() for c in cur}]
        if not new:
            continue
        if len(cur) + len(new) > MAX_TAGS:
            full += 1
            continue
        for t in new:
            conn.execute("INSERT OR IGNORE INTO lead_tags (lead_id, tag) VALUES (?, ?)", (lid, t))
        added += 1
    return {"tagged": added, "full": full}


def remove_tag(conn, lead_ids, tag: str) -> int:
    ids = [int(i) for i in lead_ids]
    n = 0
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        n += conn.execute(f"DELETE FROM lead_tags WHERE tag = ? AND lead_id IN ({','.join('?' * len(part))})",
                          (clean_tag(tag), *part)).rowcount
    return n


def all_tags(conn) -> list[dict]:
    """Every tag in use with how many leads carry it, most used first."""
    return rows(conn, "SELECT MIN(tag) tag, COUNT(*) n FROM lead_tags GROUP BY tag ORDER BY n DESC, tag LIMIT 300")


def tag_filter_sql(tags: list[str], alias: str = "l") -> tuple[str, list]:
    """SQL (AND ...) keeping leads that carry every one of these tags."""
    sql, args = "", []
    for t in tags:
        sql += f" AND EXISTS (SELECT 1 FROM lead_tags lt WHERE lt.lead_id = {alias}.id AND lt.tag = ?)"
        args.append(clean_tag(t))
    return sql, args


# ---------- WhatsApp: only for people who know you ----------

WA_STATUSES = ("Warm", "Converted")


def wa_blocked(lead: dict) -> str | None:
    """Why WhatsApp mustn't be used with this lead, or None. WhatsApp is personal: only Warm (they responded) or
    Converted leads, and people marked as old contacts."""
    if lead.get("dnc"):
        return "do not contact"
    if lead.get("status") == "Closed/Lost":
        return "Closed/Lost"
    if lead.get("status") in WA_STATUSES or lead.get("old_contact"):
        return None
    return "WhatsApp is only for Warm leads and old contacts"


# ---------- out-of-office replies ----------

_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_MON = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
_WD = r"\b(mon|tue|wed|thu|fri|sat|sun)[a-z]*\b"
_DAY = r"(\d{1,2})(?:st|nd|rd|th)?"
_CUE = re.compile(r"\b(return\w*|back|until|till|til|through|thru|resum\w*|in the office|reachable|available again"
                  r"|away from .{0,30}? to|out of (?:the )?office from .{0,30}? to|on leave from .{0,30}? to)\b", re.I)
_DATE_PATTERNS = [
    ("dmy", re.compile(_DAY + r"\s*(?:of\s+)?" + _MON + r",?(?:\s+(\d{4}))?", re.I)),
    ("mdy", re.compile(_MON + r"\s+" + _DAY + r"\b,?(?:\s+(\d{4}))?", re.I)),
    ("iso", re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")),
    ("num", re.compile(r"\b(\d{1,2})[/.](\d{1,2})(?:[/.](\d{2,4}))?\b(?!\s*(?:am|pm|h\b))", re.I)),
    ("tomorrow", re.compile(r"\btomorrow\b", re.I)),
    ("weekday", re.compile(_WD, re.I)),
]
OOO_RE = re.compile(r"out of (the )?office|\booo\b|on (annual |maternity |paternity |sick |parental )?leave|vacation|holiday"
                    r"|travel|away from|\baway\b|limited (access|connectivity)|return|back (in|on|at)|abwesen|absent", re.I)
LEFT_RE = re.compile(r"no longer (with|at|work|employed|part of)|has left|left the (company|organi[sz]ation)"
                     r"|(mailbox|e-?mail address|address) is (no longer|not) (monitored|in use|active)", re.I)
OOO_FRESH = timedelta(days=14)   # an out-of-office without a return date counts this long


def _ymd(y: int | None, m: int, d: int, after: date) -> date | None:
    """The date; without a year, the first one on or after `after` (less a day: 'back today')."""
    try:
        if y:
            return date(y + 2000 if y < 100 else y, m, d)
        dt = date(after.year, m, d)
        return dt if dt >= after - timedelta(days=1) else date(after.year + 1, m, d)
    except ValueError:
        return None


def ooo_return(text: str, received: datetime) -> date | None:
    """The day an out-of-office reply says they're back, if it says. Heuristic, English dates only:
    'back on Monday 24 November', 'returning 24/11', 'out until Nov 24th', 'back tomorrow'."""
    t = " ".join((text or "").replace("\u2019", "'").split())
    after = received.date()
    for cue in _CUE.finditer(t):
        win = t[cue.end(): cue.end() + 60]
        best = None
        for kind, rx in _DATE_PATTERNS:
            m = rx.search(win)
            # A weekday alone counts only when no actual date follows it ('back Monday 24 Nov' is the 24th).
            if m and (best is None or m.start() < best[1].start()) and not (kind == "weekday" and best):
                best = (kind, m)
        if not best:
            continue
        kind, m = best
        if kind == "dmy":
            got = _ymd(int(m.group(3)) if m.group(3) else None, _MONTHS.index(m.group(2)[:3].lower()) + 1, int(m.group(1)), after)
        elif kind == "mdy":
            got = _ymd(int(m.group(3)) if m.group(3) else None, _MONTHS.index(m.group(1)[:3].lower()) + 1, int(m.group(2)), after)
        elif kind == "iso":
            got = _ymd(int(m.group(1)), int(m.group(2)), int(m.group(3)), after)
        elif kind == "num":
            a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3)) if m.group(3) else None
            # Day-first or month-first: whichever is a real date soonest after the reply.
            options = [x for x in (_ymd(y, b, a, after), _ymd(y, a, b, after)) if x and x >= after - timedelta(days=1)]
            got = min(options) if options else None
        elif kind == "tomorrow":
            got = after + timedelta(days=1)
        else:
            want = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].index(m.group(1).lower())
            got = after + timedelta(days=(want - after.weekday() - 1) % 7 + 1)
        if got and after - timedelta(days=1) <= got <= after + timedelta(days=180):
            return got
    return None


def auto_reply_kind(text: str) -> str:
    """'left' (they've left the company), 'ooo' (out of office) or 'auto' (some other automatic reply)."""
    if LEFT_RE.search(text or ""):
        return "left"
    return "ooo" if OOO_RE.search(text or "") else "auto"


def auto_reply_info(subject: str | None, content: str | None, at: str) -> dict:
    text = f"{subject or ''} \n {content or ''}"
    kind = auto_reply_kind(text)
    back = ooo_return(text, parse(at)) if kind == "ooo" else None
    return {"kind": kind, "back": back, "at": at}


def out_of_office(conn, lead_ids=None, now: datetime | None = None, tz=None) -> dict[int, dict]:
    """Per lead, their latest automatic reply when it still applies: an out-of-office with a return date still
    ahead (or with none, from the last OOO_FRESH), or a 'no longer with the company' notice.
    {lead_id: {kind, back (date|None), at, label}}."""
    now, tz = now or utcnow(), tz or local_tz()
    where, args = "", []
    if lead_ids is not None:
        ids = [int(i) for i in lead_ids]
        if not ids:
            return {}
        where, args = f" AND lead_id IN ({','.join('?' * len(ids))})", ids
    out = {}
    for r in rows(conn, f"""SELECT lead_id, detail, content, at FROM events WHERE id IN (
                              SELECT MAX(id) FROM events WHERE type = 'auto_reply' AND at >= ?{where} GROUP BY lead_id)""",
                  iso(now - timedelta(days=180)), *args):
        # Ignore it once they've written to us since (a real reply or email after the auto-reply).
        info = auto_reply_info(r["detail"], r["content"], r["at"])
        today = now.astimezone(tz).date()
        if info["kind"] == "ooo":
            if info["back"]:
                if info["back"] < today:
                    continue
            elif now - parse(r["at"]) > OOO_FRESH:
                continue
            info["label"] = f"Out of office · back {info['back'].strftime('%d %b')}" if info["back"] else "Out of office"
        elif info["kind"] == "left":
            info["label"] = "Left the company?"
        else:
            continue
        out[r["lead_id"]] = info
    return out


def settings(conn) -> dict:
    return {k: db.get_setting(conn, k, v) for k, v in SETTINGS.items()}


def cadence(conn) -> dict[str, int]:
    """Days between follow-ups, by status."""
    s = settings(conn)
    return {"Warm": max(1, int(s["crm_warm_days"])), "Cold": max(1, int(s["crm_cold_days"]))}


# ---------- team & owners ----------

def team(conn, active_only: bool = False) -> list[dict]:
    return rows(conn, f"SELECT * FROM team {'WHERE active = 1' if active_only else ''} ORDER BY active DESC, name")


def me(conn) -> int | None:
    """The team member using this computer: the default "logged by"."""
    return db.get_setting(conn, "crm_me")


def add_member(conn, name: str, email: str = "", role: str = "BDR") -> int:
    name = name.strip()
    if not name:
        raise ValueError("Name is required")
    return conn.execute("INSERT INTO team (name, email, role, created_at) VALUES (?, ?, ?, ?)",
                        (name[:80], email.strip()[:120], role if role in ROLES else "Other", iso(utcnow()))).lastrowid


def set_owner(conn, lead_id: int, owner_id: int | None) -> None:
    if owner_id is not None and not one(conn, "SELECT id FROM team WHERE id = ?", owner_id):
        raise ValueError("Unknown team member")
    conn.execute("UPDATE leads SET owner_id = ? WHERE id = ?", (owner_id, lead_id))


def assign_campaign_owner(conn, campaign_id: int) -> int:
    """Give the campaign's unowned leads to its default owner. Returns how many were assigned."""
    owner = scalar(conn, "SELECT owner_id FROM campaigns WHERE id = ?", campaign_id)
    if not owner:
        return 0
    return conn.execute("""UPDATE leads SET owner_id = ? WHERE owner_id IS NULL
                           AND id IN (SELECT lead_id FROM enrollments WHERE campaign_id = ?)""",
                        (owner, campaign_id)).rowcount


# ---------- status ----------

def normalize_status(text: str) -> str | None:
    """'warm', 'Closed', 'lost', 'closed-lost'... -> the status name, or None."""
    t = "".join(ch for ch in (text or "").lower() if ch.isalpha())
    if not t:
        return None
    for s in STATUSES:
        if t == "".join(ch for ch in s.lower() if ch.isalpha()):
            return s
    return {"closed": "Closed/Lost", "lost": "Closed/Lost", "won": "Converted", "customer": "Converted",
            "hot": "Warm"}.get(t)


def set_status(conn, lead_id: int, status: str, now: datetime | None = None) -> bool:
    """Change a lead's status. Converted and Closed/Lost end their sequences. False if it was already that."""
    from . import engine

    if status not in STATUSES:
        raise ValueError("Unknown status")
    now = now or utcnow()
    cur = scalar(conn, "SELECT status FROM leads WHERE id = ?", lead_id)
    if cur == status:
        return False
    conn.execute("UPDATE leads SET status = ?, status_changed_at = ?, followup_snoozed_until = NULL WHERE id = ?",
                 (status, iso(now), lead_id))
    event(conn, lead_id, "status", detail=f"{cur} → {status}", at=now)
    if status in CLOSED:
        for e in rows(conn, "SELECT id FROM enrollments WHERE lead_id = ? AND status IN ('pending', 'active')", lead_id):
            engine.stop_enrollment(conn, e["id"], f"lead {status.lower()}", now=now)
    refresh_followups(conn, now, lead_id=lead_id)
    return True


# ---------- calls, meetings, notes, messages sent outside the app ----------

def log_activity(conn, lead_id: int, *, kind: str = "call", at: datetime | None = None, outcome: str = "",
                 duration_min: int | None = None, notes: str = "", next_step: str = "", next_step_at: str | None = None,
                 by_id: int | None = None, campaign_id: int | None = None, stop_sequences: bool = False,
                 status: str | None = None) -> int:
    from . import engine

    if kind not in KINDS:
        raise ValueError("Unknown activity type")
    if outcome and OUTCOMES[kind] and outcome not in OUTCOMES[kind]:
        raise ValueError("Unknown outcome")
    if kind == "note" and not notes.strip():
        raise ValueError("Write the note")
    if status and status not in STATUSES:
        raise ValueError("Unknown status")
    if next_step_at:
        datetime.strptime(next_step_at, "%Y-%m-%d")  # ValueError if malformed
    at = at or utcnow()
    if campaign_id is None:  # the campaign they're being worked in, for the export
        campaign_id = scalar(conn, "SELECT campaign_id FROM enrollments WHERE lead_id = ? ORDER BY "
                                   "status IN ('pending', 'active') DESC, id DESC LIMIT 1", lead_id)
    aid = conn.execute("""INSERT INTO activities (lead_id, campaign_id, kind, outcome, duration_min, notes, next_step,
                            next_step_at, by_id, at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                       (lead_id, campaign_id, kind, outcome, duration_min, notes.strip()[:5000], next_step.strip()[:300],
                        next_step_at or None, by_id, iso(at), iso(utcnow()))).lastrowid
    # A new log supersedes an earlier snooze: the follow-up clock restarts from this contact.
    conn.execute("UPDATE leads SET followup_snoozed_until = NULL WHERE id = ?", (lead_id,))
    if outcome == "Meeting booked" and not scalar(conn, "SELECT lead_status FROM leads WHERE id = ?", lead_id):
        conn.execute("UPDATE leads SET lead_status = 'Meeting Booked' WHERE id = ?", (lead_id,))
    if stop_sequences:
        engine.mark_replied(conn, lead_id, kind, at, detail=LOGGED + (f"{KINDS[kind]}: {outcome}" if outcome else KINDS[kind]),
                            content=notes.strip() or None)
    if status:
        set_status(conn, lead_id, status, at)
    refresh_followups(conn, lead_id=lead_id)
    return aid


def delete_activity(conn, activity_id: int) -> None:
    lead_id = scalar(conn, "SELECT lead_id FROM activities WHERE id = ?", activity_id)
    conn.execute("DELETE FROM activities WHERE id = ?", (activity_id,))
    if lead_id:
        refresh_followups(conn, lead_id=lead_id)


# ---------- last contacted & next follow-up ----------

_LAST_CONTACT_SQL = """
    UPDATE leads SET last_contacted_at = NULLIF(MAX(
      COALESCE((SELECT MAX(at) FROM events ev WHERE ev.lead_id = leads.id
                AND ev.type IN ('sent', 'reply', 'email_in', 'email_out', 'wa_in', 'wa_out')), ''),
      COALESCE((SELECT MAX(t.done_at) FROM tasks t JOIN enrollments e ON e.id = t.enrollment_id
                WHERE e.lead_id = leads.id AND t.status = 'done'
                  AND t.channel IN ('li_connect', 'li_message', 'whatsapp')), ''),
      COALESCE((SELECT MAX(at) FROM activities a WHERE a.lead_id = leads.id AND a.kind != 'note'), '')), '')
"""


def _explicit_next(conn, lead_id: int | None = None) -> dict[int, dict]:
    """The next step set by each lead's latest call/meeting/note, if it has a date."""
    where = "AND x.lead_id = ?" if lead_id else ""
    return {r["lead_id"]: r for r in rows(conn, f"""
        SELECT x.lead_id, x.next_step, x.next_step_at, x.at FROM activities x
        WHERE x.id = (SELECT id FROM activities y WHERE y.lead_id = x.lead_id ORDER BY y.at DESC, y.id DESC LIMIT 1)
          AND x.next_step_at IS NOT NULL {where}""", *([lead_id] if lead_id else []))}


def _plan(l: dict, explicit: dict | None, cad: dict, tz) -> tuple[str | None, str, str]:
    """(next follow-up date, kind, reason) for one lead. kind: next_step | warm | cold | ''."""
    if l["dnc"] or l["status"] == "Closed/Lost":
        return None, "", ""
    last = parse(l["last_contacted_at"]).astimezone(tz).date() if l["last_contacted_at"] else None
    nxt, kind, reason = None, "", ""
    # A dated next step you set stands until you've been in touch again after that date.
    if explicit and not (last and last.isoformat() > explicit["next_step_at"]):
        nxt, kind, reason = explicit["next_step_at"], "next_step", explicit["next_step"] or "Follow up"
    elif l["status"] in cad:
        base = parse(l["last_contacted_at"] or l["status_changed_at"] or l["created_at"]).astimezone(tz).date()
        nxt = (base + timedelta(days=cad[l["status"]])).isoformat()
        kind = l["status"].lower()
        reason = (f"Warm: follow up every {cad['Warm']} days" if kind == "warm"
                  else f"Cold: re-engage every {cad['Cold']} days")
    if nxt and l["followup_snoozed_until"] and l["followup_snoozed_until"] > nxt:
        nxt = l["followup_snoozed_until"]
    return nxt, kind, reason


def refresh_followups(conn, now: datetime | None = None, lead_id: int | None = None, tz=None) -> None:
    """Recompute Last Contacted (newest email, LinkedIn message, WhatsApp, call or meeting, in either direction)
    and Next Follow-up for every lead, or one. Cheap enough to run on every pass."""
    tz = tz or local_tz()
    where, args = (" WHERE id = ?", [lead_id]) if lead_id else ("", [])
    conn.execute(_LAST_CONTACT_SQL + where, args)
    cad, explicit = cadence(conn), _explicit_next(conn, lead_id)
    for l in rows(conn, f"""SELECT id, status, dnc, created_at, status_changed_at, last_contacted_at,
                              followup_snoozed_until, next_followup_at FROM leads{where}""", *args):
        nxt = _plan(l, explicit.get(l["id"]), cad, tz)[0]
        if nxt != l["next_followup_at"]:
            conn.execute("UPDATE leads SET next_followup_at = ? WHERE id = ?", (nxt, l["id"]))


def snooze(conn, lead_id: int, days: int | None = None, tz=None) -> str:
    """Push a reminder back: by the status's own cadence (Warm 3 days, Cold 90) unless told otherwise."""
    tz = tz or local_tz()
    status = scalar(conn, "SELECT status FROM leads WHERE id = ?", lead_id)
    days = days or cadence(conn).get(status, 1)
    until = (datetime.now(tz).date() + timedelta(days=max(1, int(days)))).isoformat()
    conn.execute("UPDATE leads SET followup_snoozed_until = ? WHERE id = ?", (until, lead_id))
    refresh_followups(conn, lead_id=lead_id, tz=tz)
    return until


def reminders(conn, tz=None, owner_id: int | None = None, ahead: int = 0) -> list[dict]:
    """Follow-ups due by today (+ahead days), most overdue first: each with the lead, status, last contact, why,
    and what's needed to act (email, phone, LinkedIn)."""
    tz = tz or local_tz()
    today = datetime.now(tz).date()
    args: list = [(today + timedelta(days=ahead)).isoformat()]
    own = ""
    if owner_id:
        own, args = " AND l.owner_id = ?", args + [owner_id]
    cad, explicit, out = cadence(conn), _explicit_next(conn), []
    for l in rows(conn, f"""SELECT l.*, tm.name owner FROM leads l LEFT JOIN team tm ON tm.id = l.owner_id
                            WHERE l.next_followup_at IS NOT NULL AND l.next_followup_at <= ? AND l.dnc = 0{own}
                            ORDER BY l.next_followup_at, l.id""", *args):
        nxt, kind, reason = _plan(l, explicit.get(l["id"]), cad, tz)
        if not nxt:
            continue
        due = date.fromisoformat(l["next_followup_at"])
        phone = lead_phone(l)
        out.append({"lead_id": l["id"], "name": f"{l['first_name']} {l['last_name']}".strip() or l["email"] or "(no name)",
                    "first_name": l["first_name"], "company": l["company"], "status": l["status"], "kind": kind,
                    "reason": reason, "due": l["next_followup_at"], "overdue_days": max(0, (today - due).days),
                    "today": due == today, "last_contacted_at": l["last_contacted_at"], "owner": l["owner"],
                    "owner_id": l["owner_id"], "email": l["email"], "phone": phone, "wa": wa_digits(phone),
                    "wa_block": wa_blocked(l), "ooo": None,
                    "linkedin_url": l["linkedin_url"]})
    ooo = out_of_office(conn, [r["lead_id"] for r in out], tz=tz)
    for r in out:
        r["ooo"] = ooo.get(r["lead_id"])
    return out


# ---------- everything waiting on a person ----------

def actions(conn, tz=None, owner_id: int | None = None) -> dict:
    """What needs a human today: follow-up reminders, email drafts to approve, LinkedIn/WhatsApp tasks, comments
    to approve. owner_id narrows it to one person's leads."""
    own, args = (" AND l.owner_id = ?", [owner_id]) if owner_id else ("", [])
    drafts = rows(conn, f"""SELECT d.id, d.subject, d.created_at, d.step_pos, l.id lead_id, l.first_name, l.last_name,
                              l.company, c.name campaign FROM email_drafts d JOIN leads l ON l.id = d.lead_id
                            LEFT JOIN campaigns c ON c.id = d.campaign_id
                            WHERE d.status = 'pending'{own} ORDER BY d.created_at""", *args)
    # With the bot on, LinkedIn tasks are its job; only the ones it couldn't do come to you.
    bot_on = db.get_setting(conn, "li_mode", "assisted") == "bot" and not db.get_setting(conn, "li_bot_paused")
    wa_on = db.get_setting(conn, "wa_mode", "assisted") == "bot" and not db.get_setting(conn, "wa_bot_paused")
    wa_approve = db.get_setting(conn, "wa_approve", True)
    li = wa = comments = 0
    for t in rows(conn, f"""SELECT t.channel, t.comment_status, t.bot_attempts FROM tasks t
                            JOIN enrollments e ON e.id = t.enrollment_id JOIN campaigns c ON c.id = e.campaign_id
                            JOIN leads l ON l.id = e.lead_id WHERE t.status = 'open' AND c.status = 'active'{own}""", *args):
        if t["channel"] == "whatsapp":
            # With the WhatsApp bot on, only drafts to approve and messages it couldn't send need you.
            wa += not wa_on or (wa_approve and t["comment_status"] != "approved") or t["bot_attempts"] >= 2
        elif t["comment_status"] == "needs_approval":
            comments += 1
        elif not bot_on or t["bot_attempts"] >= 2:
            li += 1
    rem = reminders(conn, tz, owner_id)
    alerts = []
    paused = db.get_setting(conn, "li_bot_paused")
    if paused and db.get_setting(conn, "li_mode", "assisted") == "bot":
        alerts.append(f"The LinkedIn bot is paused: {paused.get('reason', 'needs you')}. Resume it in Settings.")
    wa_paused = db.get_setting(conn, "wa_bot_paused")
    if wa_paused and db.get_setting(conn, "wa_mode", "assisted") == "bot":
        alerts.append(f"The WhatsApp bot is paused: {wa_paused.get('reason', 'needs you')}. Resume it in Settings.")
    return {"reminders": rem, "drafts": drafts, "linkedin": li, "whatsapp": wa, "comments": comments, "alerts": alerts,
            "total": len(rem) + len(drafts) + li + wa + comments}


# ---------- daily reminder email ----------

def base_url() -> str:
    return f"http://127.0.0.1:{os.environ.get('OC_PORT', '8765')}"


def _fmt_day(s: str | None, tz) -> str:
    return parse(s).astimezone(tz).strftime("%d %b %Y") if s else "never"


def digest(conn, tz=None, member: dict | None = None) -> tuple[str, str, int]:
    """(subject, html, number of actions) for today's reminder email: everything, or one team member's leads."""
    tz = tz or local_tz()
    a = actions(conn, tz, member["id"] if member else None)
    url, esc = base_url(), _html.escape
    today = datetime.now(tz).strftime("%A, %d %B")
    btn = ("display:inline-block;padding:6px 12px;border-radius:6px;background:#1f6fd0;color:#ffffff;"
           "text-decoration:none;font-size:13px")
    link = "color:#1f6fd0;text-decoration:none"
    th = "text-align:left;padding:6px 10px;border-bottom:2px solid #e3e8ef;font-size:12px;color:#5b6b7f"
    td = "padding:8px 10px;border-bottom:1px solid #eef1f5;font-size:13px;vertical-align:top"
    parts = [f'<div style="font-family:Segoe UI,Helvetica,Arial,sans-serif;color:#1b2533;max-width:760px">'
             f'<h2 style="margin:0 0 4px;font-size:18px">Your sales actions for {esc(today)}</h2>'
             f'<p style="margin:0 0 16px;color:#5b6b7f;font-size:13px">'
             f'{esc(member["name"]) + ": " if member else ""}{a["total"]} thing{"s" if a["total"] != 1 else ""} waiting on you. '
             f'<a style="{link}" href="{url}/today">Open today\'s list</a></p>']
    for alert in a["alerts"]:
        parts.append(f'<p style="background:#fff7e6;border:1px solid #f3d38a;border-radius:6px;padding:8px 12px;'
                     f'font-size:13px">{esc(alert)}</p>')

    def section(title: str, items: list[dict]) -> None:
        if not items:
            return
        parts.append(f'<h3 style="margin:18px 0 6px;font-size:14px">{esc(title)} ({len(items)})</h3>'
                     f'<table style="border-collapse:collapse;width:100%"><tr><th style="{th}">Lead</th>'
                     f'<th style="{th}">Status</th><th style="{th}">Last log</th><th style="{th}">Why</th>'
                     f'<th style="{th}"></th></tr>')
        for r in items[:25]:
            due = "today" if r["today"] else f"{r['overdue_days']} day{'s' if r['overdue_days'] != 1 else ''} overdue"
            act = f'<a style="{btn}" href="{url}/leads/{r["lead_id"]}?compose=1">Send email / message</a>'
            if r["email"]:
                act += f' <a style="{link};font-size:12px" href="mailto:{quote(r["email"])}">or email directly</a>'
            parts.append(
                f'<tr><td style="{td}"><a style="{link};font-weight:600" href="{url}/leads/{r["lead_id"]}">{esc(r["name"])}</a>'
                f'<br><span style="color:#5b6b7f;font-size:12px">{esc(r["company"] or "")}</span></td>'
                f'<td style="{td}">{esc(r["status"])}</td><td style="{td}">{esc(_fmt_day(r["last_contacted_at"], tz))}</td>'
                f'<td style="{td}">{esc(r["reason"])}<br><span style="color:#5b6b7f;font-size:12px">due {esc(due)}</span></td>'
                f'<td style="{td};white-space:nowrap">{act}</td></tr>')
        parts.append("</table>")
        if len(items) > 25:
            parts.append(f'<p style="font-size:12px;color:#5b6b7f">…and {len(items) - 25} more in '
                         f'<a style="{link}" href="{url}/today">today\'s list</a>.</p>')

    rem = a["reminders"]
    section("Next steps you set", [r for r in rem if r["kind"] == "next_step"])
    section("Warm prospects: send a follow-up", [r for r in rem if r["kind"] == "warm"])
    section("Cold prospects: send a re-engagement message", [r for r in rem if r["kind"] == "cold"])
    queue = []
    if a["drafts"]:
        who = ", ".join(esc(f"{d['first_name']} {d['last_name']}".strip()) for d in a["drafts"][:6])
        more = f" and {len(a['drafts']) - 6} more" if len(a["drafts"]) > 6 else ""
        queue.append(f'<b>{len(a["drafts"])} email draft{"s" if len(a["drafts"]) != 1 else ""}</b> waiting for your approval '
                     f'({who}{more}). <a style="{link}" href="{url}/drafts">Review and send</a>')
    if a["linkedin"]:
        queue.append(f'<b>{a["linkedin"]} LinkedIn task{"s" if a["linkedin"] != 1 else ""}</b> to do. '
                     f'<a style="{link}" href="{url}/tasks">Open the queue</a>')
    if a["whatsapp"]:
        queue.append(f'<b>{a["whatsapp"]} WhatsApp message{"s" if a["whatsapp"] != 1 else ""}</b> ready to send. '
                     f'<a style="{link}" href="{url}/whatsapp">Open the WhatsApp queue</a>')
    if a["comments"]:
        queue.append(f'<b>{a["comments"]} LinkedIn comment{"s" if a["comments"] != 1 else ""}</b> to write or approve. '
                     f'<a style="{link}" href="{url}/tasks">Open the queue</a>')
    if queue:
        parts.append('<h3 style="margin:18px 0 6px;font-size:14px">Waiting in your queues</h3><ul style="margin:0;'
                     'padding-left:20px;font-size:13px;line-height:1.8">' + "".join(f"<li>{q}</li>" for q in queue) + "</ul>")
    if not a["total"]:
        parts.append('<p style="font-size:13px">Nothing is waiting on you today.</p>')
    parts.append(f'<p style="margin-top:22px;color:#8696a8;font-size:11px">Sent by your outreach dashboard at '
                 f'{esc(url)}. Links open on the computer where it runs. Change the time or turn this off in '
                 f'Settings → Reminders.</p></div>')
    n = a["total"]
    return f"Sales actions for {today}: {n} to do" if n else f"Sales actions for {today}: all clear", "".join(parts), n


def maybe_send_digest(conn, boxes, now: datetime, log, tz=None, force: bool = False) -> int:
    """Once a day, at the first run after the reminder hour (8:00 by default, this computer's time): email the
    list of actions to the main mailbox, and to each team member with an email address their own leads' list.
    Nothing is sent when there's nothing to do. Returns how many emails went out."""
    from . import engine

    cfg, tz = settings(conn), tz or local_tz()
    local = now.astimezone(tz)
    today = local.date().isoformat()
    if not force:
        if not cfg["digest_enabled"] or local.hour < int(cfg["digest_hour"]):
            return 0
        if cfg["digest_skip_weekends"] and local.weekday() >= 5:
            return 0
        if db.get_setting(conn, "digest_sent_on") == today:
            return 0
        failed = parse(db.get_setting(conn, "digest_failed_at"))
        if failed and now - failed < timedelta(minutes=15):
            return 0
    graph = boxes.get(engine.default_sender(conn))
    if not graph:
        return 0  # no mailbox signed in: try again on a later run
    sent = 0
    try:
        mine = graph.my_address()
        subject, body, n = digest(conn, tz)
        if n or force:
            graph.send_new(mine, "", subject, body)
            sent += 1
        db.set_setting(conn, "digest_sent_on", today)  # from here on, a failure can't cause a repeat to you
        if cfg["digest_team"] and not force:
            for m in team(conn, active_only=True):
                if not m["email"] or m["email"].lower() == mine:
                    continue
                subject, body, n = digest(conn, tz, m)
                if n:
                    graph.send_new(m["email"], m["name"], subject, body)
                    sent += 1
    except Exception:
        db.set_setting(conn, "digest_failed_at", iso(now))
        raise
    if sent:
        log(f"daily reminder email sent ({sent})")
    return sent


# ---------- communications history ----------

def _scope(alias: str, lead_id: int | None, campaign_id: int | None) -> tuple[str, list]:
    if lead_id is not None:
        return f"{alias}.lead_id = ?", [lead_id]
    if campaign_id is not None:
        return f"{alias}.lead_id IN (SELECT lead_id FROM enrollments WHERE campaign_id = ?)", [campaign_id]
    return "1", []


AUTO_LABEL = {"ooo": "out of office reply", "left": "no longer with the company (auto-reply)", "auto": "automatic reply"}


def communications(conn, *, lead_id: int | None = None, campaign_id: int | None = None) -> list[dict]:
    """Every email, LinkedIn touch, WhatsApp message, call, meeting and note, newest first. campaign_id: every
    communication with the leads in that campaign (from any campaign, labelled)."""
    out: list[dict] = []

    def add(r, channel, direction, label, *, subject="", content="", by="", outcome="", extra="", source="auto",
            activity_id=None):
        out.append({"at": r["at"], "lead_id": r["lead_id"], "campaign": r.get("campaign") or "", "channel": channel,
                    "direction": direction, "label": label, "subject": subject or "", "content": content or "",
                    "by": by or "", "outcome": outcome, "extra": extra, "source": source, "activity_id": activity_id})

    w, a = _scope("ev", lead_id, campaign_id)
    # Automatic replies, by the email they answered: that email's entry says "out of office".
    autos = {}
    for r in rows(conn, f"SELECT ev.enrollment_id, ev.step_pos, ev.detail, ev.content, ev.at FROM events ev "
                        f"WHERE {w} AND ev.type = 'auto_reply' ORDER BY ev.at", *a):
        autos[(r["enrollment_id"], r["step_pos"])] = auto_reply_info(r["detail"], r["content"], r["at"])
    for r in rows(conn, f"""
        SELECT ev.*, c.name campaign, s.body, s.sender, s.opens, s.clicks, s.tracked, s.machine_events
        FROM events ev LEFT JOIN campaigns c ON c.id = ev.campaign_id
        LEFT JOIN sends s ON s.id = (SELECT id FROM sends WHERE enrollment_id = ev.enrollment_id
                                     AND step_pos = ev.step_pos ORDER BY sent_at DESC LIMIT 1)
        WHERE {w} AND ev.type IN ('sent', 'reply', 'auto_reply', 'bounce', 'unsubscribe', 'connected',
                                  'email_in', 'email_out', 'li_withdrawn', 'wa_in', 'wa_out')""", *a):
        t, ch = r["type"], r["channel"]
        if t == "reply" and (r["detail"] or "").startswith(LOGGED):
            continue
        manual = r["detail"] in ("marked manually", "marked from queue")
        if t == "sent":
            seen = []
            if r["tracked"] and r["opens"]:
                seen.append(f"opened {r['opens']}×")
            if r["tracked"] and r["clicks"]:
                seen.append(f"clicked {r['clicks']}×")
            auto = autos.get((r["enrollment_id"], r["step_pos"]))
            if auto:
                seen.append(AUTO_LABEL[auto["kind"]] + (f", back {auto['back'].strftime('%d %b')}" if auto["back"] else ""))
            add(r, "email", "out", f"Email · step {r['step_pos'] + 1}" if r["step_pos"] is not None else "Email",
                subject=r["detail"], content=r["body"] or r["content"] or "", by=r["sender"] or "", extra=", ".join(seen))
        elif t == "email_out":
            add(r, "email", "out", "Email (written in Outlook)", subject=r["detail"], content=r["content"] or "")
        elif t == "email_in":
            add(r, "email", "in", "Email received", subject=r["detail"], content=r["content"] or "")
        elif t == "reply" and ch == "email":
            add(r, "email", "in", "Email reply", subject="" if manual else r["detail"],
                content=r["content"] or ("Marked as replied by hand" if manual else ""))
        elif t == "reply" and ch in ("linkedin", "whatsapp"):
            add(r, ch, "in", "LinkedIn reply" if ch == "linkedin" else "WhatsApp reply",
                content=r["content"] or ("Marked as replied by hand" if manual else r["detail"] or ""))
        elif t == "auto_reply":
            info = auto_reply_info(r["detail"], r["content"], r["at"])
            add(r, "email", "in", AUTO_LABEL[info["kind"]].capitalize(), subject=r["detail"], content=r["content"] or "",
                extra=f"back {info['back'].strftime('%d %b %Y')}" if info["back"] else "")
        elif t == "wa_out":
            add(r, "whatsapp", "out", "WhatsApp message", content=r["content"] or "", by="bot")
        elif t == "wa_in":
            add(r, "whatsapp", "in", "WhatsApp received", content=r["content"] or "")
        elif t == "bounce":
            add(r, "email", "in", "Bounced", subject=r["detail"], content=r["content"] or "")
        elif t == "unsubscribe":
            add(r, "email", "in", "Unsubscribed", content=r["detail"] or "")
        elif t == "connected":
            add(r, "linkedin", "in", "Connected on LinkedIn", content=r["detail"] or "")
        elif t == "li_withdrawn":
            add(r, "linkedin", "out", "LinkedIn invite withdrawn", content=f"Unanswered; {r['detail'].lower()}" if r["detail"] else "", by="bot")

    w, a = _scope("e", lead_id, campaign_id)
    labels = {"li_connect": "LinkedIn invite", "li_message": "LinkedIn message", "whatsapp": "WhatsApp message"}
    for r in rows(conn, f"""
        SELECT t.id, t.channel, t.body, t.done_at at, e.lead_id, c.name campaign,
          (EXISTS (SELECT 1 FROM bot_actions b WHERE b.task_id = t.id AND b.ok = 1)
           OR EXISTS (SELECT 1 FROM wa_actions w WHERE w.task_id = t.id AND w.ok = 1)) by_bot
        FROM tasks t JOIN enrollments e ON e.id = t.enrollment_id JOIN campaigns c ON c.id = e.campaign_id
        WHERE {w} AND t.status = 'done' AND t.channel IN ('li_connect', 'li_message', 'whatsapp')
          AND t.done_at IS NOT NULL""", *a):
        add(r, "whatsapp" if r["channel"] == "whatsapp" else "linkedin", "out", labels[r["channel"]],
            content=r["body"] or ("(no note)" if r["channel"] == "li_connect" else ""), by="bot" if r["by_bot"] else "you")
    for r in rows(conn, f"""
        SELECT b.at, b.detail post_url, e.lead_id, c.name campaign FROM bot_actions b JOIN tasks t ON t.id = b.task_id
        JOIN enrollments e ON e.id = t.enrollment_id JOIN campaigns c ON c.id = e.campaign_id
        WHERE {w} AND b.action = 'like' AND b.ok = 1""", *a):
        add(r, "linkedin", "out", "Liked a post", content=r["post_url"] or "", by="bot")
    for r in rows(conn, f"""
        SELECT g.at, g.liked, g.commented, g.comment, g.post_url, g.task_id, e.lead_id, c.name campaign,
          EXISTS (SELECT 1 FROM bot_actions b WHERE b.task_id = g.task_id AND b.action = 'like' AND b.ok = 1) bot_liked,
          EXISTS (SELECT 1 FROM bot_actions b WHERE b.task_id = g.task_id AND b.action = 'comment' AND b.ok = 1) bot_commented
        FROM engagements g JOIN tasks t ON t.id = g.task_id JOIN enrollments e ON e.id = t.enrollment_id
        JOIN campaigns c ON c.id = e.campaign_id WHERE {w}""", *a):
        if r["commented"]:
            add(r, "linkedin", "out", "Commented on a post", content=r["comment"] or "", extra=r["post_url"] or "",
                by="bot" if r["bot_commented"] else "you")
        if r["liked"] and not r["bot_liked"]:
            add(r, "linkedin", "out", "Liked a post", content=r["post_url"] or "", by="you")

    w, a = _scope("x", lead_id, campaign_id)
    for r in rows(conn, f"""SELECT x.*, c.name campaign, tm.name by_name FROM activities x
                            LEFT JOIN campaigns c ON c.id = x.campaign_id LEFT JOIN team tm ON tm.id = x.by_id
                            WHERE {w}""", *a):
        bits = [r["notes"]] if r["notes"] else []
        if r["next_step"] or r["next_step_at"]:
            bits.append(f"Next: {r['next_step'] or 'follow up'}" + (f" ({r['next_step_at']})" if r["next_step_at"] else ""))
        label = {"whatsapp": "WhatsApp / SMS", "linkedin": "LinkedIn message"}.get(r["kind"], KINDS[r["kind"]])
        add(r, r["kind"], "in" if r["outcome"] == "They replied" else "out",
            label + (f" · {r['duration_min']} min" if r["duration_min"] else ""),
            content="\n".join(bits), by=r["by_name"] or "", outcome=r["outcome"], source="manual", activity_id=r["id"])

    out.sort(key=lambda x: x["at"] or "", reverse=True)
    return out


def summaries(conn, *, lead_id: int | None = None, campaign_id: int | None = None) -> dict[int, dict]:
    """Per lead: counts by channel and direction, last touch, last call, voicemail dates, current next step."""
    out: dict[int, dict] = {}
    for c in communications(conn, lead_id=lead_id, campaign_id=campaign_id):  # newest first
        s = out.setdefault(c["lead_id"], {"email_out": 0, "email_in": 0, "li_out": 0, "li_in": 0, "wa_out": 0,
                                          "wa_in": 0, "wa_dates": [], "calls": 0, "meetings": 0, "last": None,
                                          "last_call": None, "voicemails": [], "next": None, "_latest_manual": None})
        key = {"email": "email", "linkedin": "li", "whatsapp": "wa"}.get(c["channel"])
        if key:
            s[f"{key}_{c['direction']}"] += 1
        if c["channel"] == "whatsapp" and c["direction"] == "out":
            s["wa_dates"].insert(0, c["at"])
        if c["channel"] == "call":
            s["calls"] += 1
            s["last_call"] = s["last_call"] or c
            if c["outcome"] == VOICEMAIL:
                s["voicemails"].insert(0, c["at"])
        if c["channel"] == "meeting":
            s["meetings"] += 1
        if c["label"] not in ("Connected on LinkedIn",) and c["channel"] != "note":
            s["last"] = s["last"] or c
        if c["source"] == "manual" and s["_latest_manual"] is None:
            s["_latest_manual"] = c["activity_id"]
    # The next step is whatever the latest call/meeting/note set (logging a new one replaces it).
    ids = [s["_latest_manual"] for s in out.values() if s["_latest_manual"]]
    if ids:
        nxt = {r["id"]: r for r in rows(conn, f"SELECT id, next_step, next_step_at FROM activities WHERE id IN "
                                              f"({','.join('?' * len(ids))})", *ids)}
        for s in out.values():
            r = nxt.get(s["_latest_manual"])
            if r and (r["next_step"] or r["next_step_at"]):
                s["next"] = {"text": r["next_step"] or "Follow up", "date": r["next_step_at"]}
    return out


def parse_local(value: str, tz) -> datetime | None:
    """<input type=datetime-local> value in the dashboard's zone -> aware datetime."""
    if not value:
        return None
    return datetime.strptime(value[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=tz)


def local_day(s: str | None, tz) -> str:
    return parse(s).astimezone(tz).strftime("%d-%b-%y") if s else ""
