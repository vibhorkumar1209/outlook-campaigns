"""A campaign as a file: its sequence, its settings, and the LinkedIn bot / engagement / mailbox limits it ran
with. `oc campaign-export` writes one; `oc campaign-import` recreates it as a draft (e.g. on a new Mac)."""
from __future__ import annotations

from . import db
from .db import iso, one, rows, utcnow

CAMPAIGN_KEYS = ("window_days", "window_start", "window_end", "email_daily_cap", "li_daily_cap", "gap_min", "gap_max",
                 "signature", "track_opens", "track_clicks", "li_engage")
SETTING_KEYS = ("li_mode", "li_bot_caps", "li_bot_week_invites", "li_bot_gap", "li_bot_hours", "li_bot_days",
                "engage_every_days", "engage_lookback_months")


def export_campaign(conn, cid: int) -> dict:
    c = one(conn, "SELECT * FROM campaigns WHERE id = ?", cid)
    if not c:
        raise ValueError(f"no campaign {cid}")
    steps = []
    for s in rows(conn, "SELECT * FROM steps WHERE campaign_id = ? ORDER BY pos", cid):
        st = {"channel": s["channel"], "delay_days": s["delay_days"], "condition": s["condition"], "body": s["body"]}
        if s["channel"] == "email":
            st.update(subject=s["subject"], in_thread=bool(s["in_thread"]))
        steps.append(st)
    return {"name": c["name"], "campaign": {k: c[k] for k in CAMPAIGN_KEYS}, "steps": steps,
            "settings": {k: db.get_setting(conn, k) for k in SETTING_KEYS if db.get_setting(conn, k) is not None},
            "mailbox_daily_cap": db.scalar(conn, "SELECT daily_cap FROM senders ORDER BY created_at LIMIT 1")}


def import_campaign(conn, data: dict, name: str | None = None, apply_settings: bool = True) -> int:
    """New DRAFT campaign from an exported file (validated like the sequence editor). Returns its id."""
    from .web.app import _save_steps, _validate_steps

    steps, errs = _validate_steps(data["steps"])
    if errs:
        raise ValueError("; ".join(errs))
    c = {k: v for k, v in data.get("campaign", {}).items() if k in CAMPAIGN_KEYS}
    cols = ["name", "status", "created_at", *c]
    cid = conn.execute(f"INSERT INTO campaigns ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                       (name or data.get("name") or "Imported campaign", "draft", iso(utcnow()), *c.values())).lastrowid
    _save_steps(conn, cid, steps)
    if apply_settings:
        for k, v in data.get("settings", {}).items():
            if k in SETTING_KEYS:
                db.set_setting(conn, k, v)
        if data.get("mailbox_daily_cap"):
            conn.execute("UPDATE senders SET daily_cap = ?", (data["mailbox_daily_cap"],))
    return cid
