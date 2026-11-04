"""Read-side queries for the dashboard."""
from __future__ import annotations

from datetime import datetime, timedelta

from .db import iso, parse, rows, scalar, utcnow

TOUCH_TYPES = ("sent", "task_done")


def funnel(conn, campaign_id: int, has_li: bool) -> list[dict]:
    enrolled = scalar(conn, "SELECT COUNT(*) FROM enrollments WHERE campaign_id = ?", campaign_id)
    contacted = scalar(conn, f"""SELECT COUNT(DISTINCT enrollment_id) FROM events
                                 WHERE campaign_id = ? AND type IN {TOUCH_TYPES}""", campaign_id)
    replied = scalar(conn, "SELECT COUNT(*) FROM enrollments WHERE campaign_id = ? AND status = 'replied'", campaign_id)
    stages = [("Enrolled", enrolled), ("Contacted", contacted)]
    if has_li:
        connected = scalar(conn, """SELECT COUNT(*) FROM enrollments e JOIN leads l ON l.id = e.lead_id
                                    WHERE e.campaign_id = ? AND l.li_state = 'connected'""", campaign_id)
        stages.append(("Connected", connected))
    stages.append(("Replied", replied))
    return [{"label": s, "n": n, "pct": round(100 * n / enrolled) if enrolled else 0} for s, n in stages]


def status_counts(conn, campaign_id: int | None = None) -> dict[str, int]:
    where, args = ("WHERE campaign_id = ?", (campaign_id,)) if campaign_id else ("", ())
    return {r["status"]: r["n"] for r in rows(conn, f"SELECT status, COUNT(*) n FROM enrollments {where} GROUP BY status", *args)}


def step_stats(conn, campaign_id: int, steps: list[dict]) -> list[dict]:
    ev = {(r["step_pos"], r["type"]): r["n"] for r in rows(conn, """
        SELECT step_pos, type, COUNT(*) n FROM events WHERE campaign_id = ? AND step_pos IS NOT NULL
        GROUP BY step_pos, type""", campaign_id)}
    waiting = {r["pos"]: r["n"] for r in rows(conn, """
        SELECT e.step_pos + 1 pos, COUNT(*) n FROM enrollments e
        WHERE e.campaign_id = ? AND e.status IN ('pending', 'active')
          AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.enrollment_id = e.id AND t.status = 'open')
        GROUP BY e.step_pos""", campaign_id)}
    queued = {r["step_pos"]: r["n"] for r in rows(conn, """
        SELECT t.step_pos, COUNT(*) n FROM tasks t JOIN enrollments e ON e.id = t.enrollment_id
        WHERE e.campaign_id = ? AND t.status = 'open' GROUP BY t.step_pos""", campaign_id)}
    out = []
    for s in steps:
        p = s["pos"]
        done = ev.get((p, "sent"), 0) + ev.get((p, "task_done"), 0)
        out.append({
            **s,
            "done": done,
            "skipped": ev.get((p, "step_skipped"), 0) + ev.get((p, "task_skipped"), 0),
            "queued": queued.get(p, 0),
            "waiting": waiting.get(p, 0),
            "replies": ev.get((p, "reply"), 0),
            "rate": round(100 * ev.get((p, "reply"), 0) / done) if done else None,
        })
    return out


def activity(conn, tz, days: int = 14) -> list[dict]:
    """Daily touches by channel, local days, oldest first."""
    today = utcnow().astimezone(tz).date()
    start = datetime.combine(today - timedelta(days=days - 1), datetime.min.time(), tz)
    buckets = {today - timedelta(days=i): {"email": 0, "linkedin": 0, "replies": 0} for i in range(days)}
    for r in rows(conn, f"SELECT type, channel, at FROM events WHERE at >= ? AND type IN ('sent', 'task_done', 'reply')",
                  iso(start)):
        d = parse(r["at"]).astimezone(tz).date()
        if d in buckets:
            key = "replies" if r["type"] == "reply" else ("email" if r["channel"] == "email" else "linkedin")
            buckets[d][key] += 1
    return [{"day": d, **buckets[d]} for d in sorted(buckets)]


def overview(conn, tz) -> dict:
    now = utcnow()
    day0 = iso(now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0))
    week = iso(now - timedelta(days=7))
    contacted = scalar(conn, f"SELECT COUNT(DISTINCT enrollment_id) FROM events WHERE type IN {TOUCH_TYPES}")
    replied = scalar(conn, "SELECT COUNT(*) FROM enrollments WHERE status = 'replied'")
    return {
        "active_campaigns": scalar(conn, "SELECT COUNT(*) FROM campaigns WHERE status = 'active'"),
        "in_sequence": scalar(conn, """SELECT COUNT(*) FROM enrollments e JOIN campaigns c ON c.id = e.campaign_id
                                       WHERE c.status = 'active' AND e.status IN ('pending', 'active')"""),
        "emails_today": scalar(conn, "SELECT COUNT(*) FROM events WHERE type = 'sent' AND at >= ?", day0),
        "tasks_open": scalar(conn, "SELECT COUNT(*) FROM tasks WHERE status = 'open'"),
        "replies_week": scalar(conn, "SELECT COUNT(*) FROM events WHERE type = 'reply' AND at >= ?", week),
        "reply_rate": round(100 * replied / contacted) if contacted else None,
    }


def recent_replies(conn, limit: int = 8) -> list[dict]:
    return rows(conn, """
        SELECT ev.at, ev.channel, ev.detail, l.id lead_id, l.first_name, l.last_name, l.company, c.name campaign
        FROM events ev JOIN leads l ON l.id = ev.lead_id LEFT JOIN campaigns c ON c.id = ev.campaign_id
        WHERE ev.type = 'reply' ORDER BY ev.at DESC LIMIT ?""", limit)


def campaign_rows(conn) -> list[dict]:
    return rows(conn, f"""
        SELECT c.*,
          (SELECT COUNT(*) FROM enrollments e WHERE e.campaign_id = c.id) enrolled,
          (SELECT COUNT(*) FROM enrollments e WHERE e.campaign_id = c.id AND e.status IN ('pending', 'active')) in_sequence,
          (SELECT COUNT(DISTINCT enrollment_id) FROM events ev WHERE ev.campaign_id = c.id AND ev.type IN {TOUCH_TYPES}) contacted,
          (SELECT COUNT(*) FROM enrollments e WHERE e.campaign_id = c.id AND e.status = 'replied') replied,
          (SELECT COUNT(*) FROM steps s WHERE s.campaign_id = c.id) n_steps,
          (SELECT COUNT(*) FROM tasks t JOIN enrollments e ON e.id = t.enrollment_id
             WHERE e.campaign_id = c.id AND t.status = 'open') tasks_open
        FROM campaigns c ORDER BY c.status = 'active' DESC, c.id DESC""")


# ---------- email performance & deliverability ----------

def _pct(n, d):
    return round(100 * n / d, 1) if d else None


def email_stats(conn, *, campaign_id: int | None = None, sender: str | None = None, since: datetime | None = None) -> dict:
    """Unique-per-lead email metrics. Rates exclude scanner/prefetch hits and use only tracked sends as the base."""
    where, args = ["1 = 1"], []
    if campaign_id:
        where.append("campaign_id = ?"); args.append(campaign_id)
    if sender:
        where.append("sender = ?"); args.append(sender)
    if since:
        where.append("sent_at >= ?"); args.append(iso(since))
    w = " AND ".join(where)
    r = rows(conn, f"""
        SELECT COUNT(*) sent, COUNT(DISTINCT lead_id) leads,
          COUNT(DISTINCT CASE WHEN (tracked & 1) > 0 THEN lead_id END) open_base,
          COUNT(DISTINCT CASE WHEN (tracked & 2) > 0 THEN lead_id END) click_base,
          COUNT(DISTINCT CASE WHEN (tracked & 1) > 0 AND opened_at IS NOT NULL THEN lead_id END) opened,
          COUNT(DISTINCT CASE WHEN clicked_at IS NOT NULL THEN lead_id END) clicked,
          COALESCE(SUM(machine_events), 0) machine
        FROM sends WHERE {w}""", *args)[0]
    emailed = f"SELECT enrollment_id FROM sends WHERE {w}"
    bounced = scalar(conn, f"SELECT COUNT(DISTINCT enrollment_id) FROM events WHERE type = 'bounce' "
                           f"AND enrollment_id IN ({emailed})", *args)
    replied = scalar(conn, f"SELECT COUNT(*) FROM enrollments WHERE status = 'replied' AND id IN ({emailed})", *args)
    unsub = scalar(conn, f"""SELECT COUNT(DISTINCT lead_id) FROM events WHERE type = 'unsubscribe'
                             AND lead_id IN (SELECT lead_id FROM sends WHERE {w})""", *args)
    delivered = max(r["leads"] - bounced, 0)
    return {
        **r, "bounced": bounced, "delivered": delivered, "replied": replied, "unsubscribed": unsub,
        "delivery_rate": _pct(delivered, r["leads"]),
        "bounce_rate": _pct(bounced, r["leads"]),
        "open_rate": _pct(r["opened"], max(r["open_base"] - bounced, 0)) if r["open_base"] else None,
        "click_rate": _pct(r["clicked"], max(r["click_base"] - bounced, 0)) if r["click_base"] else None,
        "click_to_open": _pct(r["clicked"], r["opened"]) if r["open_base"] and r["click_base"] else None,
        "reply_rate": _pct(replied, delivered),
        "unsub_rate": _pct(unsub, delivered),
    }


def step_email_stats(conn, campaign_id: int) -> dict[int, dict]:
    out = {}
    for r in rows(conn, """
        SELECT step_pos, COUNT(*) sent,
          SUM((tracked & 1) > 0) open_base, SUM((tracked & 1) > 0 AND opened_at IS NOT NULL) opened,
          SUM((tracked & 2) > 0) click_base, SUM(clicked_at IS NOT NULL) clicked
        FROM sends WHERE campaign_id = ? GROUP BY step_pos""", campaign_id):
        out[r["step_pos"]] = {**r, "open_rate": _pct(r["opened"], r["open_base"]) if r["open_base"] else None,
                              "click_rate": _pct(r["clicked"], r["click_base"]) if r["click_base"] else None}
    return out


def bounce_health(rate: float | None, sent: int) -> str:
    """Status tone for a bounce rate. Under ~20 sends the rate is noise."""
    if rate is None or sent < 20:
        return "muted"
    return "critical" if rate >= 5 else "warning" if rate >= 2 else "good"


def sender_rows(conn, tz) -> list[dict]:
    now = utcnow()
    today = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    out = []
    for s in rows(conn, "SELECT * FROM senders ORDER BY created_at, account"):
        st = email_stats(conn, sender=s["account"], since=now - timedelta(days=30))
        out.append({
            **s, "stats": st,
            "today": scalar(conn, "SELECT COUNT(*) FROM sends WHERE sender = ? AND sent_at >= ?", s["account"], iso(today)),
            "campaigns": rows(conn, "SELECT id, name, status FROM campaigns WHERE sender = ? ORDER BY id DESC", s["account"]),
            "health": bounce_health(st["bounce_rate"], st["sent"]),
        })
    return out
