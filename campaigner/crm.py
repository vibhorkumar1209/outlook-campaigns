"""CRM layer: lead owners, calls/meetings/notes, and one communications history per lead.

Emails and LinkedIn are never re-entered by hand: they're read from what the app already records (sends, events,
tasks, engagements, bot_actions). Only calls, meetings and notes live in `activities`, logged by whoever works the lead.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from . import db
from .db import iso, one, parse, rows, scalar, utcnow

KINDS = {"call": "Call", "meeting": "Meeting", "note": "Note"}
OUTCOMES = {
    "call": ["Connected – interested", "Connected – not interested", "Connected – call back later", "Meeting booked",
             "Left voicemail", "No answer", "Gatekeeper", "Wrong number"],
    "meeting": ["Held", "No-show", "Rescheduled", "Cancelled"],
    "note": [],
}
# Outcomes where they actually responded: offered as "stop their sequences" (ticked by default).
RESPONDED = {"Connected – interested", "Connected – not interested", "Connected – call back later", "Meeting booked",
             "Held"}
VOICEMAIL = "Left voicemail"
ROLES = ["BDR", "SDR", "AE", "Manager", "Other"]


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


# ---------- calls, meetings, notes ----------

def log_activity(conn, lead_id: int, *, kind: str = "call", at: datetime | None = None, outcome: str = "",
                 duration_min: int | None = None, notes: str = "", next_step: str = "", next_step_at: str | None = None,
                 by_id: int | None = None, campaign_id: int | None = None, stop_sequences: bool = False) -> int:
    from . import engine

    if kind not in KINDS:
        raise ValueError("Unknown activity type")
    if outcome and OUTCOMES[kind] and outcome not in OUTCOMES[kind]:
        raise ValueError("Unknown outcome")
    if kind == "note" and not notes.strip():
        raise ValueError("Write the note")
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
    if outcome == "Meeting booked" and not scalar(conn, "SELECT lead_status FROM leads WHERE id = ?", lead_id):
        conn.execute("UPDATE leads SET lead_status = 'Meeting Booked' WHERE id = ?", (lead_id,))
    if stop_sequences:
        engine.mark_replied(conn, lead_id, kind, at, detail=f"{KINDS[kind]}: {outcome}" if outcome else KINDS[kind],
                            content=notes.strip() or None)
    return aid


def delete_activity(conn, activity_id: int) -> None:
    conn.execute("DELETE FROM activities WHERE id = ?", (activity_id,))


# ---------- communications history ----------

def _scope(alias: str, lead_id: int | None, campaign_id: int | None) -> tuple[str, list]:
    if lead_id is not None:
        return f"{alias}.lead_id = ?", [lead_id]
    if campaign_id is not None:
        return f"{alias}.lead_id IN (SELECT lead_id FROM enrollments WHERE campaign_id = ?)", [campaign_id]
    return "1", []


def communications(conn, *, lead_id: int | None = None, campaign_id: int | None = None) -> list[dict]:
    """Every email, LinkedIn touch, call, meeting and note, newest first. campaign_id: every communication with
    the leads in that campaign (from any campaign, labelled)."""
    out: list[dict] = []

    def add(r, channel, direction, label, *, subject="", content="", by="", outcome="", extra="", source="auto",
            activity_id=None):
        out.append({"at": r["at"], "lead_id": r["lead_id"], "campaign": r.get("campaign") or "", "channel": channel,
                    "direction": direction, "label": label, "subject": subject or "", "content": content or "",
                    "by": by or "", "outcome": outcome, "extra": extra, "source": source, "activity_id": activity_id})

    w, a = _scope("ev", lead_id, campaign_id)
    for r in rows(conn, f"""
        SELECT ev.*, c.name campaign, s.body, s.sender, s.opens, s.clicks, s.tracked, s.machine_events
        FROM events ev LEFT JOIN campaigns c ON c.id = ev.campaign_id
        LEFT JOIN sends s ON s.id = (SELECT id FROM sends WHERE enrollment_id = ev.enrollment_id
                                     AND step_pos = ev.step_pos ORDER BY sent_at DESC LIMIT 1)
        WHERE {w} AND ev.type IN ('sent', 'reply', 'auto_reply', 'bounce', 'unsubscribe', 'connected')""", *a):
        t, ch = r["type"], r["channel"]
        if t == "sent":
            seen = []
            if r["tracked"] and r["opens"]:
                seen.append(f"opened {r['opens']}×")
            if r["tracked"] and r["clicks"]:
                seen.append(f"clicked {r['clicks']}×")
            add(r, "email", "out", f"Email · step {r['step_pos'] + 1}" if r["step_pos"] is not None else "Email",
                subject=r["detail"], content=r["body"] or "", by=r["sender"] or "", extra=", ".join(seen))
        elif t == "reply" and ch == "email":
            add(r, "email", "in", "Email reply", subject=r["detail"] if r["detail"] != "marked manually" else "",
                content=r["content"] or ("Marked as replied by hand" if r["detail"] == "marked manually" else ""))
        elif t == "reply" and ch == "linkedin":
            manual = r["detail"] in ("marked manually", "marked from queue")
            add(r, "linkedin", "in", "LinkedIn reply", content=r["content"] or ("Marked as replied by hand" if manual
                                                                               else r["detail"] or ""))
        elif t == "auto_reply":
            add(r, "email", "in", "Automatic reply", subject=r["detail"], content=r["content"] or "")
        elif t == "bounce":
            add(r, "email", "in", "Bounced", subject=r["detail"], content=r["content"] or "")
        elif t == "unsubscribe":
            add(r, "email", "in", "Unsubscribed", content=r["detail"] or "")
        elif t == "connected":
            add(r, "linkedin", "in", "Connected on LinkedIn", content=r["detail"] or "")

    w, a = _scope("e", lead_id, campaign_id)
    for r in rows(conn, f"""
        SELECT t.id, t.channel, t.body, t.done_at at, e.lead_id, c.name campaign,
          EXISTS (SELECT 1 FROM bot_actions b WHERE b.task_id = t.id AND b.ok = 1) by_bot
        FROM tasks t JOIN enrollments e ON e.id = t.enrollment_id JOIN campaigns c ON c.id = e.campaign_id
        WHERE {w} AND t.status = 'done' AND t.channel IN ('li_connect', 'li_message') AND t.done_at IS NOT NULL""", *a):
        label = "LinkedIn invite" if r["channel"] == "li_connect" else "LinkedIn message"
        add(r, "linkedin", "out", label, content=r["body"] or ("(no note)" if r["channel"] == "li_connect" else ""),
            by="bot" if r["by_bot"] else "you")
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
        add(r, r["kind"], "out", KINDS[r["kind"]] + (f" · {r['duration_min']} min" if r["duration_min"] else ""),
            content="\n".join(bits), by=r["by_name"] or "", outcome=r["outcome"], source="manual", activity_id=r["id"])

    out.sort(key=lambda x: x["at"] or "", reverse=True)
    return out


def summaries(conn, *, lead_id: int | None = None, campaign_id: int | None = None) -> dict[int, dict]:
    """Per lead: counts by channel and direction, last touch, last call, voicemail dates, current next step."""
    out: dict[int, dict] = {}
    for c in communications(conn, lead_id=lead_id, campaign_id=campaign_id):  # newest first
        s = out.setdefault(c["lead_id"], {"email_out": 0, "email_in": 0, "li_out": 0, "li_in": 0, "calls": 0,
                                          "meetings": 0, "last": None, "last_call": None, "voicemails": [],
                                          "next": None, "_latest_manual": None})
        key = {"email": "email", "linkedin": "li"}.get(c["channel"])
        if key:
            s[f"{key}_{c['direction']}"] += 1
        if c["channel"] == "call":
            s["calls"] += 1
            s["last_call"] = s["last_call"] or c
            if c["outcome"] == VOICEMAIL:
                s["voicemails"].insert(0, c["at"])
        if c["channel"] == "meeting":
            s["meetings"] += 1
        if c["label"] not in ("Connected on LinkedIn",):
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


def follow_ups(conn, tz, days: int = 7) -> list[dict]:
    """Next steps due by `days` from today (overdue first), with the lead and owner."""
    last = rows(conn, """SELECT x.lead_id, x.next_step, x.next_step_at, x.kind, x.outcome, x.at,
                           l.first_name, l.last_name, l.company, tm.name owner
                         FROM activities x JOIN leads l ON l.id = x.lead_id LEFT JOIN team tm ON tm.id = l.owner_id
                         WHERE x.id = (SELECT id FROM activities y WHERE y.lead_id = x.lead_id ORDER BY y.at DESC, y.id DESC LIMIT 1)
                         AND x.next_step_at IS NOT NULL AND l.dnc = 0""")
    today = datetime.now(tz).date()
    horizon = (today + timedelta(days=days)).isoformat()
    due = [dict(r, overdue=r["next_step_at"] < today.isoformat(), today=r["next_step_at"] == today.isoformat())
           for r in last if r["next_step_at"] <= horizon]
    return sorted(due, key=lambda r: r["next_step_at"])


def parse_local(value: str, tz) -> datetime | None:
    """<input type=datetime-local> value in the dashboard's zone -> aware datetime."""
    if not value:
        return None
    return datetime.strptime(value[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=tz)


def local_day(s: str | None, tz) -> str:
    return parse(s).astimezone(tz).strftime("%d-%b-%y") if s else ""
