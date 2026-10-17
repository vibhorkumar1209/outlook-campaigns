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
