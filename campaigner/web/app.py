"""Local dashboard. FastAPI + server-rendered Jinja. Listens on 127.0.0.1 only."""
from __future__ import annotations

import csv
import io
import json
import os
import re
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, available_timezones

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import TemplateError, TemplateSyntaxError

from .. import ai, attachments, crm, db, domain_health, engine, importer, linkedin_bot, osutil, stats, updater
from ..db import iso, one, parse, rows, scalar, utcnow
from ..graph import AuthError, Graph, GraphError
from ..render import (ENV, WRAP, email_html, lead_context, lead_phone, linkedin_text, render_text, text_to_html,
                      wa_digits, whatsapp_text)
from ..runner import Runner, remove_sender, wants_awake
from ..tracking import Tracker

HERE = Path(__file__).parent
PORT = int(os.environ.get("OC_PORT", "8765"))
HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
STATUS_TONE = {
    "active": "info", "open": "info", "pending": "muted", "done": "muted", "draft": "muted",
    "replied": "good", "connected": "good", "paused": "warning", "stopped": "warning", "invited": "info",
    "bounced": "critical", "error": "critical", "none": "muted", "good": "good", "warning": "warning",
    "critical": "critical", "muted": "muted", "unsubscribed": "warning",
}
SAMPLE_LEAD = {"first_name": "Jane", "last_name": "Doe", "company": "Acme Corp", "title": "Head of IT",
               "email": "jane@example.com", "linkedin_url": "https://www.linkedin.com/in/janedoe", "data": "{}"}
DEFAULT_SEQUENCE = [
    {"channel": "li_connect", "delay_days": 0, "body":
        "Hi {{ first_name }}, I work with IT leaders at companies like {{ company }}. Would be good to connect."},
    {"channel": "email", "delay_days": 1, "subject": "{{ company }} + quick idea", "body":
        "Hi {{ first_name }},\n\nSaw {{ company }} is growing its IT team. As {{ title | default('a leader there', true) }}, "
        "you might find this useful: ...\n\nWorth a 15-minute call next week?"},
    {"channel": "li_message", "delay_days": 3, "body":
        "Thanks for connecting, {{ first_name }}! I sent you a short note by email. Happy to share more here if easier."},
    {"channel": "email", "delay_days": 4, "body": "Hi {{ first_name }}, bumping this up in case it got buried."},
    {"channel": "email", "delay_days": 7, "body":
        "Hi {{ first_name }}, last note from me. If it's not a priority for {{ company }} right now, "
        "just reply \"no\" and I won't follow up again."},
]

runner = Runner()
app = FastAPI(docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


templates = Jinja2Templates(directory=HERE / "templates")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(HERE / "static" / "favicon.ico", media_type="image/x-icon")


def mail_client(account: str):
    """The Outlook client for one mailbox (its own function so tests can swap it)."""
    return Graph(account)
UPLOADS: dict[str, dict] = {}


def local_tz():
    return datetime.now().astimezone().tzinfo


def _ago(s: str | None) -> str:
    dt = parse(s) if isinstance(s, str) else s
    if not dt:
        return "never"
    secs = (utcnow() - dt).total_seconds()
    future, secs = secs < 0, abs(secs)
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= n:
            v = f"{int(secs // n)}{unit}"
            return f"in {v}" if future else f"{v} ago"
    return "just now"


def _local(s: str | None, fmt: str = "%b %d %H:%M") -> str:
    dt = parse(s) if isinstance(s, str) else s
    return dt.astimezone(local_tz()).strftime(fmt) if dt else "-"


templates.env.filters.update(ago=_ago, local=_local)
templates.env.globals.update(
    tone=lambda s: STATUS_TONE.get(s, "muted"),
    channel_label=lambda c: engine.TASK_LABELS.get(c, c),
    pct=lambda v: "–" if v is None else (f"{v:g}%"),
    # Versioned static URLs so browsers pick up CSS/JS changes immediately.
    asset=lambda name: f"/static/{name}?v={int((HERE / 'static' / name).stat().st_mtime)}",
    CHANNELS=engine.CHANNELS, CONDITIONS=engine.CONDITIONS, FIELDS=importer.FIELDS,
    MACHINE=osutil.MACHINE, WINDOWS=osutil.WINDOWS, OC=".\\oc" if osutil.WINDOWS else "./oc",
    STATUSES=crm.STATUSES, LEAD_TONE=crm.STATUS_TONE, KINDS=crm.KINDS, SUGGESTED_STATUS=crm.SUGGESTED_STATUS,
)

OPEN_TASKS_SQL = """SELECT COUNT(*) FROM tasks t JOIN enrollments e ON e.id = t.enrollment_id
                    JOIN campaigns c ON c.id = e.campaign_id WHERE t.status = 'open' AND c.status = 'active'"""


@app.middleware("http")
async def local_only(request: Request, call_next):
    # Block DNS rebinding and cross-site form posts: this app can send email as you.
    if request.headers.get("host", "") not in HOSTS:
        return PlainTextResponse("bad host", 400)
    if request.method not in ("GET", "HEAD"):
        origin = request.headers.get("origin") or request.headers.get("referer") or ""
        if not any(origin.startswith(f"http://{h}") for h in HOSTS):
            return PlainTextResponse("cross-origin request blocked", 403)
    return await call_next(request)


@app.on_event("startup")
def _start():
    if os.environ.get("OC_TICKER", "1") == "1":
        runner.start_loop(int(os.environ.get("OC_TICK_MINUTES", "10")))


def connected_senders(conn) -> list[dict]:
    """Senders with a live token in the cache, flagged; unconnected ones need a re-sign-in."""
    live = set(runner.accounts())
    return [{**s, "connected": s["account"] in live} for s in rows(conn, "SELECT * FROM senders ORDER BY created_at")]


def page(request: Request, name: str, conn, **ctx):
    senders = connected_senders(conn)
    base = {
        "senders_nav": senders,
        "account": next((s["email"] for s in senders if s["connected"]), None),
        "n_connected": sum(s["connected"] for s in senders),
        "li_bot": linkedin_bot.status(conn),
        "last_tick": db.get_setting(conn, "last_tick"),
        "open_tasks": scalar(conn, OPEN_TASKS_SQL),
        "updates_behind": (runner.update_info or {}).get("behind", 0),
        "team": crm.team(conn, active_only=True),
        "me": crm.me(conn),
        "OUTCOMES": crm.OUTCOMES, "RESPONDED": sorted(crm.RESPONDED),
        "today_count": crm.actions(conn, local_tz())["total"],
        "drafts_pending": scalar(conn, "SELECT COUNT(*) FROM email_drafts WHERE status = 'pending'"),
        "path": request.url.path,
        "msg": request.query_params.get("msg"),
        "err": request.query_params.get("err"),
    }
    return templates.TemplateResponse(request, name, {**base, **ctx})


def back(url: str, **q) -> RedirectResponse:
    from urllib.parse import urlencode

    return RedirectResponse(url + ("?" + urlencode(q) if q else ""), status_code=303)


def get_campaign(conn, cid: int) -> dict:
    c = one(conn, "SELECT * FROM campaigns WHERE id = ?", cid)
    if not c:
        raise HTTPException(404, "campaign not found")
    return c


# ---------- overview ----------

@app.get("/")
def overview(request: Request):
    conn = db.connect()
    tz = local_tz()
    act = stats.activity(conn, tz)
    peak = max([d["email"] + d["linkedin"] + d["whatsapp"] for d in act] + [1])
    camps = stats.campaign_rows(conn)
    for c in camps:
        c["email"] = stats.email_stats(conn, campaign_id=c["id"])
    return page(request, "overview.html", conn, o=stats.overview(conn, tz), act=act, peak=peak, campaigns=camps,
                replies=stats.recent_replies(conn), em=stats.email_stats(conn, since=utcnow() - timedelta(days=30)))


# ---------- Today: everything waiting on a person ----------

@app.get("/today")
def today(request: Request, owner: str = ""):
    conn = db.connect()
    tz = local_tz()
    crm.refresh_followups(conn, tz=tz)
    oid = int(owner) if owner.isdigit() else None
    a = crm.actions(conn, tz, oid)
    upcoming = [r for r in crm.reminders(conn, tz, oid, ahead=7) if not r["today"] and not r["overdue_days"]]
    return page(request, "today.html", conn, a=a, upcoming=upcoming, owner=owner, cadence=crm.cadence(conn),
                cfg=crm.settings(conn))


@app.get("/reminders/email")
def reminder_email_preview():
    """Today's reminder email, as it would be sent."""
    return HTMLResponse(crm.digest(db.connect(), local_tz())[1])


# ---------- email drafts: written by the sequences, sent when you approve ----------

DRAFTS_SQL = """SELECT d.*, l.first_name, l.last_name, l.company, l.title, c.name campaign, c.window_start, c.window_end,
                       c.status campaign_status, (SELECT COUNT(*) FROM steps s WHERE s.campaign_id = d.campaign_id) n_steps
                FROM email_drafts d JOIN leads l ON l.id = d.lead_id LEFT JOIN campaigns c ON c.id = d.campaign_id"""


def _kick() -> None:
    """A quick pass now, so something you just approved doesn't wait for the next minute's run."""
    import threading

    threading.Thread(target=runner.run_once, kwargs={"checks": False}, daemon=True).start()


@app.get("/drafts")
def drafts_page(request: Request, campaign: int | None = None):
    conn = db.connect()
    where, args = (" AND d.campaign_id = ?", [campaign]) if campaign else ("", [])
    items = rows(conn, DRAFTS_SQL + f" WHERE d.status IN ('pending', 'approved'){where} ORDER BY d.status = 'approved', "
                                    "d.created_at, d.id", *args)
    recent = rows(conn, DRAFTS_SQL + " WHERE d.status NOT IN ('pending', 'approved') ORDER BY d.done_at DESC LIMIT 15")
    camps = rows(conn, """SELECT c.id, c.name, c.email_mode, COUNT(d.id) n FROM campaigns c
                          LEFT JOIN email_drafts d ON d.campaign_id = c.id AND d.status = 'pending'
                          WHERE c.status != 'draft' GROUP BY c.id ORDER BY c.status = 'active' DESC, c.id DESC""")
    return page(request, "drafts.html", conn, drafts=items, recent=recent, campaigns=camps, campaign=campaign,
                n_pending=sum(1 for d in items if d["status"] == "pending"))


@app.get("/drafts/{did}/preview")
def draft_preview(did: int):
    d = one(db.connect(), "SELECT subject, html FROM email_drafts WHERE id = ?", did)
    if not d:
        raise HTTPException(404)
    body = re.sub(r"<img[^>]*(width=\"1\"|/o/)[^>]*>", "", d["html"], flags=re.I)  # looking mustn't count as an open
    return HTMLResponse('<!doctype html><meta charset="utf-8"><base target="_blank"><body style="margin:12px;'
                        'font-family:Aptos,Calibri,Arial,sans-serif">' + body,
                        headers={"Content-Security-Policy": "sandbox allow-popups; default-src 'none'; style-src 'unsafe-inline'"})


@app.post("/api/drafts/approve")
async def drafts_approve(request: Request):
    p = await request.json()
    conn = db.connect()
    if p.get("all"):
        where, args = (" AND campaign_id = ?", [int(p["campaign_id"])]) if str(p.get("campaign_id") or "").isdigit() else ("", [])
        ids = [r["id"] for r in rows(conn, f"SELECT id FROM email_drafts WHERE status = 'pending'{where}", *args)]
    else:
        ids = [int(i) for i in p.get("ids") or [] if str(i).isdigit()]
    n = engine.approve_drafts(conn, ids)
    if n:
        _kick()
    return {"ok": True, "approved": n, "pending": scalar(conn, "SELECT COUNT(*) FROM email_drafts WHERE status = 'pending'")}


@app.post("/api/drafts/{did}")
async def draft_action(did: int, request: Request):
    act = (await request.json()).get("action")
    conn = db.connect()
    d = one(conn, "SELECT * FROM email_drafts WHERE id = ?", did)
    if not d or d["status"] not in ("pending", "approved"):
        raise HTTPException(409, "This draft was already sent or dropped")
    if act == "skip":      # don't send this one; the sequence carries on. The Outlook copy is removed on the next full run.
        engine.skip_draft(conn, d, "skipped by you", utcnow())
    elif act == "hold":    # take back an approval that hasn't gone out yet
        conn.execute("UPDATE email_drafts SET status = 'pending', approved_at = NULL WHERE id = ? AND status = 'approved'", (did,))
    else:
        raise HTTPException(400, "bad action")
    return {"ok": True, "pending": scalar(conn, "SELECT COUNT(*) FROM email_drafts WHERE status = 'pending'")}


@app.post("/api/leads/{lid}/compose")
async def compose(lid: int, request: Request):
    """A one-off email to a lead: saved in Outlook's Drafts (in their existing thread when there is one), and
    sent only if you say so."""
    p = await request.json()
    conn = db.connect()
    l = one(conn, "SELECT * FROM leads WHERE id = ?", lid)
    if not l:
        raise HTTPException(404)
    if l["dnc"]:
        raise HTTPException(409, "This lead is marked do-not-contact")
    if not l["email"]:
        raise HTTPException(422, "No email address for this lead. Add one under Details.")
    subject, text = (p.get("subject") or "").strip(), (p.get("body") or "").strip()
    if not text:
        raise HTTPException(422, "Write the message")
    acct, conv, signature = engine.thread_for(conn, lid)
    if not acct:
        raise HTTPException(409, "Connect a mailbox first (Senders)")
    threaded = bool(conv) and not subject
    if not threaded and not subject:
        raise HTTPException(422, "Add a subject (there's no earlier email thread with them to reply in)")
    ctx = lead_context(l)
    try:
        html = WRAP.format(text_to_html(render_text(text, ctx)) + (text_to_html(render_text(signature, ctx)) if signature else ""))
        subject = render_text(subject, ctx)
    except TemplateError as ex:
        raise HTTPException(422, f"Template: {ex}")

    def save():
        g = mail_client(acct)
        return (g.draft_in_thread(conv, l["email"], ctx["name"], html, "Re:") if threaded
                else g.draft_new(l["email"], ctx["name"], subject, html))
    try:
        from starlette.concurrency import run_in_threadpool
        dr = await run_in_threadpool(save)
    except (AuthError, GraphError) as ex:
        raise HTTPException(409, f"Couldn't save the draft in Outlook: {ex}")
    send = bool(p.get("send"))
    conn.execute("""INSERT INTO email_drafts (lead_id, sender, to_email, subject, html, outlook_id, web_link, conversation_id,
                      threaded, status, created_at, approved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                 (lid, acct, l["email"], subject or "(reply in thread)", html, dr.get("id"), dr.get("webLink"),
                  dr.get("conversationId"), int(threaded), "approved" if send else "pending", iso(utcnow()),
                  iso(utcnow()) if send else None))
    if send:
        _kick()
    return {"ok": True, "web_link": dr.get("webLink"), "sent": send}


# ---------- task queue: LinkedIn and WhatsApp ----------

@app.get("/tasks")
def tasks(request: Request, campaign: int | None = None):
    conn = db.connect()
    sql = """SELECT t.*, c.id campaign_id, c.name campaign, l.id lead_id, l.first_name, l.last_name, l.company,
                    l.title, l.linkedin_url, l.li_state, l.data,
                    (SELECT COUNT(*) FROM steps s WHERE s.campaign_id = c.id) n_steps
             FROM tasks t JOIN enrollments e ON e.id = t.enrollment_id
             JOIN campaigns c ON c.id = e.campaign_id JOIN leads l ON l.id = e.lead_id
             WHERE t.status = 'open' AND c.status = 'active'"""
    args = ()
    if campaign:
        sql += " AND c.id = ?"
        args = (campaign,)
    items = rows(conn, sql + " ORDER BY t.created_at, t.id", *args)
    eng = engine.engage_settings(conn)
    since = (utcnow() - timedelta(days=round(30.4 * eng["engage_lookback_months"]))).astimezone(local_tz())
    for t in items:
        t["phone"] = lead_phone(t)
        t["wa"] = wa_digits(t["phone"])
        if t["channel"] == engine.ENGAGE:
            t["posts_url"] = t["linkedin_url"].rstrip("/") + "/recent-activity/all/"
            t["engaged"] = rows(conn, "SELECT * FROM engagements WHERE lead_id = ? ORDER BY at DESC LIMIT 10", t["lead_id"])
    done_today = scalar(conn, "SELECT COUNT(*) FROM events WHERE type = 'task_done' AND at >= ?",
                        iso(utcnow().astimezone(local_tz()).replace(hour=0, minute=0, second=0)))
    camps = rows(conn, "SELECT id, name FROM campaigns WHERE status = 'active' ORDER BY name")
    bot = linkedin_bot.settings(conn)
    for t in items:
        t["bot_will"] = (bot["li_mode"] == "bot" and t["bot_attempts"] < linkedin_bot.MAX_ATTEMPTS
                         and t["comment_status"] != "needs_approval" and t["channel"] != engine.WHATSAPP)
    return page(request, "tasks.html", conn, tasks=items, campaigns=camps, campaign=campaign, done_today=done_today,
                since=since, ai_ready=bool(os.environ.get("ANTHROPIC_API_KEY")))


@app.post("/api/tasks/{task_id}")
async def task_action(task_id: int, request: Request):
    body = await request.json()
    if body.get("action") not in ("done", "skip", "connected", "replied"):
        raise HTTPException(400, "bad action")
    conn = db.connect()
    try:
        engine.complete_task(conn, task_id, body["action"], body.get("body"), engage=body.get("engage"))
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"ok": True, "open": scalar(conn, OPEN_TASKS_SQL)}


@app.post("/api/tasks/{task_id}/approve")
async def task_approve(task_id: int, request: Request):
    """Approve the bot's comment draft (edited or not); the bot posts it on its next run."""
    comment = ((await request.json()).get("comment") or "").strip()
    if not comment:
        raise HTTPException(422, "Write a comment first, or use Like only")
    conn = db.connect()
    cur = conn.execute("UPDATE tasks SET body = ?, comment_status = 'approved' WHERE id = ? AND status = 'open' "
                       "AND comment_status = 'needs_approval'", (comment, task_id))
    if not cur.rowcount:
        raise HTTPException(409, "task already handled")
    return {"ok": True, "open": scalar(conn, OPEN_TASKS_SQL)}


@app.post("/api/tasks/{task_id}/draft")
async def task_draft(task_id: int, request: Request):
    p = await request.json()
    conn = db.connect()
    lead = one(conn, """SELECT l.* FROM tasks t JOIN enrollments e ON e.id = t.enrollment_id
                        JOIN leads l ON l.id = e.lead_id WHERE t.id = ?""", task_id)
    if not lead:
        raise HTTPException(404)
    try:
        # Runs in FastAPI's threadpool (sync SDK call inside an async route would block the loop).
        from starlette.concurrency import run_in_threadpool
        comment = await run_in_threadpool(ai.draft_comment, p.get("post") or "", lead,
                                          db.get_setting(conn, "comment_voice", ""))
    except ai.DraftError as e:
        raise HTTPException(422, str(e))
    return {"comment": comment}


# ---------- campaigns ----------

@app.get("/campaigns")
def campaigns(request: Request):
    conn = db.connect()
    camps = stats.campaign_rows(conn)
    for c in camps:
        c["email"] = stats.email_stats(conn, campaign_id=c["id"])
    return page(request, "campaigns.html", conn, campaigns=camps)


@app.post("/campaigns")
def create_campaign(name: str = Form(...)):
    conn = db.connect()
    cur = conn.execute("INSERT INTO campaigns (name, sender, created_at) VALUES (?, ?, ?)",
                       (name.strip() or "Untitled", engine.default_sender(conn), iso(utcnow())))
    _save_steps(conn, cur.lastrowid, _validate_steps(DEFAULT_SEQUENCE)[0])
    return back(f"/campaigns/{cur.lastrowid}", tab="sequence", msg="Campaign created with a starter sequence. Edit it, import leads, then activate.")


@app.post("/campaigns/{cid}/copy")
def campaign_copy(cid: int):
    conn = db.connect()
    get_campaign(conn, cid)
    new = engine.copy_campaign(conn, cid)
    return back(f"/campaigns/{new}", tab="sequence",
                msg="Copied as a draft with the same sequence and settings, no leads. Rename it, import leads, then activate.")


@app.get("/campaigns/{cid}")
def campaign(request: Request, cid: int, tab: str = "overview", status: str | None = None):
    conn = db.connect()
    c = get_campaign(conn, cid)
    steps = rows(conn, "SELECT * FROM steps WHERE campaign_id = ? ORDER BY pos", cid)
    has_li = any(s["channel"] in engine.LI for s in steps)
    sender = one(conn, "SELECT * FROM senders WHERE account = ?", c["sender"] or engine.default_sender(conn))
    ctx = {"c": c, "tab": tab, "steps": steps, "counts": stats.status_counts(conn, cid), "sender": sender,
           "tracker": Tracker.from_env()}
    if tab == "overview":
        ctx.update(funnel=stats.funnel(conn, cid, has_li), step_stats=stats.step_stats(conn, cid, steps),
                   em=stats.email_stats(conn, campaign_id=cid), step_em=stats.step_email_stats(conn, cid),
                   has_email=any(s["channel"] == "email" for s in steps))
    elif tab == "sequence":
        leads = rows(conn, """SELECT l.id, l.first_name, l.last_name, l.company FROM leads l
            JOIN enrollments e ON e.lead_id = l.id WHERE e.campaign_id = ? ORDER BY e.id LIMIT 50""", cid)
        ctx["preview_leads"] = [{"id": l["id"], "label": " · ".join(x for x in (
            f"{l['first_name']} {l['last_name']}".strip(), l["company"]) if x)} for l in leads]
        # Steps carry their live counts so they stay attached when nodes are reordered in the builder.
        ctx["seq_steps"] = stats.step_stats(conn, cid, steps)
    elif tab == "leads":
        sql = """SELECT e.*, l.first_name, l.last_name, l.company, l.title, l.email, l.linkedin_url, l.li_state
                 FROM enrollments e JOIN leads l ON l.id = e.lead_id WHERE e.campaign_id = ?"""
        args = [cid]
        if status:
            sql += " AND e.status = ?"
            args.append(status)
        ctx.update(enrollments=rows(conn, sql + " ORDER BY e.id LIMIT 1000", *args), status=status)
    elif tab == "status":
        ctx["status_cols"], ctx["status_rows"] = stats.campaign_status(conn, cid, local_tz())
    elif tab == "settings":
        tz = engine.tz_of(c)
        start = parse(c["start_at"])
        ctx.update(start_local=start.astimezone(tz).strftime("%Y-%m-%dT%H:%M") if start else "",
                   days=c["window_days"].split(","), zones=sorted(available_timezones()),
                   senders=connected_senders(conn))
    return page(request, "campaign.html", conn, **ctx)


@app.post("/campaigns/{cid}/settings")
def campaign_settings(cid: int, name: str = Form(...), timezone: str = Form(""), start_at: str = Form(""),
                      window_days: list[str] = Form([]), window_start: str = Form("09:30"), window_end: str = Form("17:00"),
                      email_daily_cap: int = Form(40), li_daily_cap: int = Form(20), gap_min: int = Form(45),
                      gap_max: int = Form(120), signature: str = Form(""), sender: str = Form(""),
                      track_opens: str = Form(""), track_clicks: str = Form(""), li_engage: str = Form(""),
                      owner_id: str = Form(""), email_mode: str = Form("draft")):
    conn = db.connect()
    get_campaign(conn, cid)
    url = f"/campaigns/{cid}"
    timezone = timezone.strip()
    if timezone and timezone not in available_timezones():
        return back(url, tab="settings", err=f"Unknown timezone {timezone}")
    if not window_days:
        return back(url, tab="settings", err="Pick at least one send day")
    if window_start >= window_end:
        return back(url, tab="settings", err="Send window must end after it starts")
    if sender and not one(conn, "SELECT 1 FROM senders WHERE account = ?", sender):
        return back(url, tab="settings", err="Unknown mailbox")
    tz = ZoneInfo(timezone) if timezone else local_tz()
    start = iso(datetime.fromisoformat(start_at).replace(tzinfo=tz)) if start_at else None
    gap_min, gap_max = max(0, gap_min), max(gap_min, gap_max)
    conn.execute("""UPDATE campaigns SET name = ?, timezone = ?, start_at = ?, window_days = ?, window_start = ?,
                    window_end = ?, email_daily_cap = ?, li_daily_cap = ?, gap_min = ?, gap_max = ?, signature = ?,
                    sender = ?, track_opens = ?, track_clicks = ?, li_engage = ? WHERE id = ?""",
                 (name.strip() or "Untitled", timezone or None, start, ",".join(window_days), window_start, window_end,
                  max(0, email_daily_cap), max(0, li_daily_cap), gap_min, gap_max, signature, sender or None,
                  1 if track_opens else 0, 1 if track_clicks else 0, 1 if li_engage else 0, cid))
    owner = int(owner_id) if owner_id.isdigit() and one(conn, "SELECT 1 FROM team WHERE id = ?", int(owner_id)) else None
    conn.execute("UPDATE campaigns SET owner_id = ?, email_mode = ? WHERE id = ?",
                 (owner, "auto" if email_mode == "auto" else "draft", cid))
    n = crm.assign_campaign_owner(conn, cid)
    return back(url, tab="settings", msg="Saved" + (f". {n} unassigned lead(s) given to the campaign owner." if n else ""))


@app.post("/api/campaigns/{cid}/rename")
async def campaign_rename(cid: int, request: Request):
    name = ((await request.json()).get("name") or "").strip()
    if not name:
        raise HTTPException(422, "Name can't be empty")
    if len(name) > 120:
        raise HTTPException(422, "Keep the name under 120 characters")
    conn = db.connect()
    get_campaign(conn, cid)
    conn.execute("UPDATE campaigns SET name = ? WHERE id = ?", (name, cid))
    return {"ok": True, "name": name, "dummy": "dummy" in name.lower()}


@app.post("/campaigns/{cid}/status")
def campaign_status(cid: int, status: str = Form(...)):
    conn = db.connect()
    get_campaign(conn, cid)
    url = f"/campaigns/{cid}"
    if status not in ("draft", "active", "paused"):
        raise HTTPException(400)
    if status == "active":
        if not scalar(conn, "SELECT COUNT(*) FROM steps WHERE campaign_id = ?", cid):
            return back(url, err="Add at least one step first")
        if not scalar(conn, "SELECT COUNT(*) FROM enrollments WHERE campaign_id = ?", cid):
            return back(url, err="Import leads into this campaign first")
        c = get_campaign(conn, cid)
        has_email = scalar(conn, "SELECT COUNT(*) FROM steps WHERE campaign_id = ? AND channel = 'email'", cid)
        if has_email and not (c["sender"] or engine.default_sender(conn)):
            return back(url, err="Connect a mailbox on the Senders page first; this sequence has email steps")
    conn.execute("UPDATE campaigns SET status = ? WHERE id = ?", (status, cid))
    if status == "active":
        runner.run_async()
    return back(url, msg={"active": "Live. First run started.", "paused": "Paused. Replies are still tracked.",
                          "draft": "Back to draft."}[status])


@app.post("/campaigns/{cid}/delete")
def campaign_delete(cid: int):
    conn = db.connect()
    conn.execute("DELETE FROM campaigns WHERE id = ?", (cid,))
    return back("/campaigns", msg="Campaign deleted")


def _validate_steps(raw: list[dict]) -> tuple[list[dict], list[str]]:
    out, errs, seen_email = [], [], False
    for i, s in enumerate(raw):
        n = f"Step {i + 1}"
        ch = s.get("channel")
        if ch not in engine.CHANNELS:
            errs.append(f"{n}: unknown channel")
            continue
        cond = s.get("condition") or "always"
        if cond not in engine.CONDITIONS:
            errs.append(f"{n}: unknown condition")
        try:
            delay = float(s.get("delay_days") or 0)
        except (TypeError, ValueError):
            delay = -1
        if delay < 0:
            errs.append(f"{n}: wait must be 0 or more days")
        if cond == engine.ON_ACCEPT:
            if ch != "li_message":
                errs.append(f"{n}: “as soon as they accept” is only for LinkedIn messages")
            delay = 0  # it fires on acceptance, not after a wait
        subject = ((s.get("subject") or "").strip() or None) if ch == "email" else None
        body = (s.get("body") or "") if ch != "li_view" else ""
        in_thread = bool(s.get("in_thread", True))
        if ch == "email":
            if not subject and (not seen_email or not in_thread):
                errs.append(f"{n}: subject needed ({'first email starts the thread' if not seen_email else 'new thread'})")
            seen_email = True
        if ch in ("email", "li_message", "whatsapp") and not body.strip():
            errs.append(f"{n}: message is empty")
        if ch == "li_connect" and len(body) > 300:
            errs.append(f"{n}: LinkedIn invite notes max 300 characters")
        for label, t in (("subject", subject), ("message", body)):
            try:
                ENV.parse(t or "")
            except TemplateSyntaxError as e:
                errs.append(f"{n} {label}: template syntax: {e.message}")
        files = []
        if ch == "email":
            for m in s.get("attachments") or []:
                try:
                    ok = attachments.path(m).exists()
                except attachments.AttachmentError:
                    ok = False
                if not ok:
                    errs.append(f"{n}: attachment {m.get('name', '?')} is missing; remove and re-upload it")
                else:
                    files.append({k: m[k] for k in ("id", "name", "size", "type") if k in m})
        out.append({"pos": i, "channel": ch, "delay_days": max(delay, 0), "condition": cond, "subject": subject,
                    "body": body, "in_thread": 1 if in_thread else 0, "attachments": files})
    if not raw:
        errs.append("Add at least one step")
    return out, errs


def _save_steps(conn, cid: int, steps: list[dict]) -> None:
    conn.execute("BEGIN")
    conn.execute("DELETE FROM steps WHERE campaign_id = ?", (cid,))
    for s in steps:
        conn.execute("INSERT INTO steps (campaign_id, pos, channel, delay_days, condition, subject, body, in_thread, "
                     "attachments) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (cid, s["pos"], s["channel"], s["delay_days"], s["condition"], s["subject"], s["body"], s["in_thread"],
                      json.dumps(s.get("attachments") or [])))
    conn.execute("COMMIT")


@app.post("/api/campaigns/{cid}/steps")
async def save_steps(cid: int, request: Request):
    conn = db.connect()
    get_campaign(conn, cid)
    steps, errs = _validate_steps((await request.json()).get("steps") or [])
    if errs:
        return JSONResponse({"ok": False, "errors": errs}, status_code=422)
    _save_steps(conn, cid, steps)
    return {"ok": True}


@app.post("/api/attachments")
async def upload_attachment(file: UploadFile = File(...)):
    data = await file.read(attachments.MAX_BYTES + 1)
    try:
        return attachments.save(file.filename or "file", data)
    except attachments.AttachmentError as e:
        raise HTTPException(413 if len(data) > attachments.MAX_BYTES else 422, str(e))


@app.get("/api/attachments/{aid}")
def get_attachment(aid: str, name: str):
    from fastapi.responses import FileResponse
    try:
        p = attachments.path({"id": aid, "name": name})
    except attachments.AttachmentError:
        raise HTTPException(404)
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, filename=p.name.split("__", 1)[1])


@app.post("/api/preview")
async def preview(request: Request):
    p = await request.json()
    conn = db.connect()
    c = get_campaign(conn, int(p["campaign_id"]))
    lead = one(conn, "SELECT * FROM leads WHERE id = ?", p.get("lead_id")) if p.get("lead_id") else None
    ctx = lead_context(lead or SAMPLE_LEAD)
    tracker = Tracker.from_env()
    ctx["unsubscribe_url"] = tracker.unsub_url("preview") if tracker else "mailto:you?subject=Unsubscribe"
    st = p.get("step") or {}
    try:
        if st.get("channel") == "email":
            subject = render_text(st.get("subject") or p.get("first_subject") or "", ctx)
            if not st.get("subject") and p.get("in_thread_followup"):
                subject = "Re: " + subject
            return {"subject": subject, "html": email_html(st.get("body") or "", c["signature"], ctx)}
        fmt = whatsapp_text if st.get("channel") == engine.WHATSAPP else linkedin_text
        text = fmt(render_text(st.get("body") or "", ctx))
        return {"text": text, "chars": len(text)}
    except TemplateError as e:
        return {"error": str(e)}


@app.post("/api/enrollments/{eid}/resume")
def resume(eid: int):
    conn = db.connect()
    try:
        how = engine.resume_enrollment(conn, eid)
    except ValueError as e:
        raise HTTPException(409, str(e))
    runner.run_async()
    return {"ok": True, "how": how}


@app.post("/api/enrollments/{eid}/stop")
def stop(eid: int):
    conn = db.connect()
    engine.stop_enrollment(conn, eid)
    return {"ok": True}


@app.post("/api/enrollments/{eid}/tracker")
async def tracker_fields(eid: int, request: Request):
    """Campaign Status columns you fill in yourself: Lead Status (a label) and voicemails VM1..VM3."""
    p = await request.json()
    conn = db.connect()
    e = one(conn, "SELECT id, lead_id FROM enrollments WHERE id = ?", eid)
    if not e:
        raise HTTPException(404)
    if "lead_status" in p:
        conn.execute("UPDATE leads SET lead_status = ? WHERE id = ?", (str(p["lead_status"] or "").strip()[:80], e["lead_id"]))
    if "voicemails" in p:
        vms = [str(v or "").strip()[:40] for v in (p["voicemails"] or [])][:stats.N_VOICEMAILS]
        conn.execute("UPDATE enrollments SET voicemails = ? WHERE id = ?", (json.dumps(vms), eid))
    return {"ok": True}


@app.get("/status")
def status_page(request: Request, campaign: str = "", owner: str = ""):
    conn = db.connect()
    campaigns = rows(conn, "SELECT id, name, status FROM campaigns ORDER BY status = 'active' DESC, id DESC")
    if campaign == "all":
        selected = None
    elif campaign.isdigit():
        selected = int(get_campaign(conn, int(campaign))["id"])
    else:  # default: the newest active campaign
        selected = campaigns[0]["id"] if campaigns else None
    cols, items = stats.campaign_status(conn, selected, local_tz())
    if owner:
        items = [r for r in items if (r["owner_id"] is None if owner == "none" else str(r["owner_id"]) == owner)]
    return page(request, "status.html", conn, campaigns=campaigns, selected=selected, status_cols=cols, status_rows=items,
                owner=owner)


@app.get("/status/export.{fmt}")
def status_export(fmt: str, campaign: str = "all"):
    return campaign_status_export(int(campaign) if campaign.isdigit() else None, fmt)


COMM_COLS = ["Date", "Time", "Lead", "Company", "Owner", "Campaign", "Channel", "Direction", "Type", "Subject",
             "Content", "Outcome", "By", "Seen"]


def communication_rows(conn, cid: int | None) -> list[list]:
    tz = local_tz()
    leads = {r["id"]: r for r in rows(conn, """SELECT l.id, l.first_name, l.last_name, l.company, tm.name owner
                                              FROM leads l LEFT JOIN team tm ON tm.id = l.owner_id""")}
    out = []
    for c in crm.communications(conn, campaign_id=cid):
        l, at = leads.get(c["lead_id"], {}), parse(c["at"]).astimezone(tz)
        out.append([at.strftime("%d-%b-%y"), at.strftime("%H:%M"), f"{l.get('first_name', '')} {l.get('last_name', '')}".strip(),
                    l.get("company", ""), l.get("owner") or "", c["campaign"], c["channel"].capitalize(),
                    "Outbound" if c["direction"] == "out" else "Inbound", c["label"], c["subject"],
                    c["content"][:32000], c["outcome"], c["by"], c["extra"]])
    return out


@app.get("/status/communications.csv")
def communications_export(campaign: str = "all"):
    conn = db.connect()
    cid = int(campaign) if campaign.isdigit() else None
    name = re.sub(r"[^\w\- ]+", "", get_campaign(conn, cid)["name"]).strip() if cid else "All campaigns"
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(COMM_COLS)
    w.writerows(communication_rows(conn, cid))
    return Response("\ufeff" + buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name} - Communications.csv"'})


@app.get("/campaigns/{cid}/status.{fmt}")
def campaign_status_export(cid: int | None, fmt: str):
    conn = db.connect()
    c = get_campaign(conn, cid) if cid else {"name": "All campaigns"}
    cols, items = stats.campaign_status(conn, cid, local_tz())
    name = re.sub(r"[^\w\- ]+", "", c["name"]).strip() or "campaign"
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(cols)
        w.writerows([r["cells"][k] for k in cols] for r in items)
        return Response("\ufeff" + buf.getvalue(), media_type="text/csv",  # BOM: Excel reads it as UTF-8
                        headers={"Content-Disposition": f'attachment; filename="{name} - Campaign Status.csv"'})
    if fmt != "xlsx":
        raise HTTPException(404)
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "Campaign Status"
    ws.append(cols)
    for cell in ws[1]:
        cell.font, cell.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="1F4E79")
    for r in items:
        ws.append([r["cells"][k] for k in cols])
    ws.freeze_panes = "C2"
    for col in ws.columns:
        ws.column_dimensions[col[0].column_letter].width = min(40, max(6, *(len(str(x.value or "")) + 2 for x in col)))
    # Second sheet: every email, LinkedIn touch, call, meeting and note with these leads.
    from openpyxl.styles import Alignment

    cs = wb.create_sheet("Communications")
    cs.append(COMM_COLS)
    for cell in cs[1]:
        cell.font, cell.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="1F4E79")
    for r in communication_rows(conn, cid):
        cs.append(r)
    cs.freeze_panes = "D2"
    for letter, width in zip("ABCDEFGHIJKLMN", (10, 7, 22, 22, 14, 26, 10, 10, 20, 34, 70, 22, 22, 16)):
        cs.column_dimensions[letter].width = width
    for cell in cs["K"][1:]:
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    buf = io.BytesIO()
    wb.save(buf)
    return Response(buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{name} - Campaign Status.xlsx"'})


# ---------- leads ----------

def contacts_page(request: Request, stage: str, q: str, owner: str = "", status: str = ""):
    conn = db.connect()
    like = f"%{q.strip()}%"
    owner_sql = "" if not owner else " AND l.owner_id IS NULL" if owner == "none" else " AND l.owner_id = ?"
    owner_arg = [int(owner)] if owner.isdigit() else []
    if status in crm.STATUSES:
        owner_sql, owner_arg = owner_sql + " AND l.status = ?", owner_arg + [status]
    items = rows(conn, """
        SELECT l.*, tm.name owner, (SELECT COUNT(*) FROM enrollments e WHERE e.lead_id = l.id) n_campaigns,
          (SELECT status FROM enrollments e WHERE e.lead_id = l.id ORDER BY e.id DESC LIMIT 1) last_status,
          (SELECT MAX(at) FROM events ev WHERE ev.lead_id = l.id AND ev.type IN ('sent', 'task_done', 'reply')) last_touch,
          r.channel reply_channel, r.at reply_at, r.detail reply_detail, rc.name reply_campaign
        FROM leads l
        LEFT JOIN events r ON r.id = (SELECT MAX(id) FROM events WHERE lead_id = l.id AND type = 'reply')
        LEFT JOIN campaigns rc ON rc.id = r.campaign_id
        LEFT JOIN team tm ON tm.id = l.owner_id
        WHERE l.stage = ? AND (? = '' OR l.first_name || ' ' || l.last_name LIKE ? OR l.email LIKE ? OR l.company LIKE ?)
        """ + owner_sql + """
        ORDER BY COALESCE(l.became_lead_at, '') DESC, l.id DESC LIMIT 500""", stage, q.strip(), like, like, like, *owner_arg)
    counts = {r["stage"]: r["n"] for r in rows(conn, "SELECT stage, COUNT(*) n FROM leads GROUP BY stage")}
    return page(request, "leads.html", conn, leads=items, q=q, stage=stage, counts=counts, owner=owner, status=status,
                today=datetime.now(local_tz()).date().isoformat())


@app.get("/leads")
def leads(request: Request, q: str = "", owner: str = "", status: str = ""):
    """People who responded: dropped from every sequence when they replied."""
    return contacts_page(request, "lead", q, owner, status)


@app.get("/prospects")
def prospects(request: Request, q: str = "", owner: str = "", status: str = ""):
    return contacts_page(request, "prospect", q, owner, status)


@app.get("/leads/{lid}")
def lead(request: Request, lid: int):
    conn = db.connect()
    l = one(conn, "SELECT * FROM leads WHERE id = ?", lid)
    if not l:
        raise HTTPException(404)
    ens = rows(conn, """SELECT e.*, c.name campaign, c.status campaign_status,
                          (SELECT COUNT(*) FROM steps s WHERE s.campaign_id = c.id) n_steps
                        FROM enrollments e JOIN campaigns c ON c.id = e.campaign_id WHERE e.lead_id = ? ORDER BY e.id""", lid)
    events = rows(conn, """SELECT ev.*, c.name campaign FROM events ev LEFT JOIN campaigns c ON c.id = ev.campaign_id
                           WHERE ev.lead_id = ? ORDER BY ev.at DESC, ev.id DESC""", lid)
    extras = json.loads(l["data"] or "{}")
    crm.refresh_followups(conn, lead_id=lid, tz=local_tz())
    l = one(conn, "SELECT * FROM leads WHERE id = ?", lid)
    summary = crm.summaries(conn, lead_id=lid).get(lid, {})
    phone = lead_phone(l)
    acct, conv, _ = engine.thread_for(conn, lid)
    return page(request, "lead.html", conn, l=l, enrollments=ens, events=events, extras=extras,
                comms=crm.communications(conn, lead_id=lid), summary=summary, phone=phone, wa=wa_digits(phone),
                has_thread=bool(conv), compose_from=acct, today=datetime.now(local_tz()).date().isoformat(),
                lead_drafts=rows(conn, "SELECT * FROM email_drafts WHERE lead_id = ? AND status IN ('pending', 'approved') "
                                       "ORDER BY id", lid),
                active="/leads" if l["stage"] == "lead" else "/prospects")


@app.get("/leads/{lid}/history")
def lead_history(request: Request, lid: int):
    """The communications list alone, for the Campaign Status row expander."""
    conn = db.connect()
    return templates.TemplateResponse(request, "_comms.html", {"comms": crm.communications(conn, lead_id=lid), "compact": True})


@app.post("/api/leads/{lid}/activities")
async def log_activity(lid: int, request: Request):
    p = await request.json()
    conn = db.connect()
    if not one(conn, "SELECT id FROM leads WHERE id = ?", lid):
        raise HTTPException(404)
    try:
        at = crm.parse_local(p.get("at") or "", local_tz())
        if at and at > utcnow() + timedelta(minutes=5):
            raise ValueError("That time is in the future; use Next step for planned calls")
        dur = str(p.get("duration_min") or "").strip()
        aid = crm.log_activity(
            conn, lid, kind=p.get("kind") or "call", at=at, outcome=p.get("outcome") or "",
            duration_min=int(dur) if dur.isdigit() else None, notes=p.get("notes") or "",
            next_step=p.get("next_step") or "", next_step_at=p.get("next_step_at") or None,
            by_id=int(p["by_id"]) if str(p.get("by_id") or "").isdigit() else None,
            campaign_id=int(p["campaign_id"]) if str(p.get("campaign_id") or "").isdigit() else None,
            stop_sequences=bool(p.get("stop_sequences")), status=p.get("status") or None)
    except ValueError as ex:
        raise HTTPException(422, str(ex))
    return {"ok": True, "id": aid}


@app.post("/api/activities/{aid}/delete")
def delete_activity(aid: int):
    crm.delete_activity(db.connect(), aid)
    return {"ok": True}


@app.post("/api/leads/{lid}")
async def lead_action(lid: int, request: Request):
    p = await request.json()
    conn = db.connect()
    if not one(conn, "SELECT id FROM leads WHERE id = ?", lid):
        raise HTTPException(404)
    act, now = p.get("action"), utcnow()
    if act == "replied":
        if not engine.mark_replied(conn, lid, p.get("channel") or "linkedin", now, detail="marked manually"):
            raise HTTPException(409, "lead is not in an open sequence")
    elif act == "not_reply":
        try:
            engine.undo_reply(conn, lid, now)
        except ValueError as ex:
            raise HTTPException(409, str(ex))
    elif act == "connected":
        engine.set_connected(conn, lid, now, source="marked manually")
    elif act == "stage":
        stage = "lead" if p.get("stage") == "lead" else "prospect"
        conn.execute("UPDATE leads SET stage = ?, became_lead_at = CASE WHEN ? = 'lead' THEN COALESCE(became_lead_at, ?) "
                     "ELSE NULL END WHERE id = ?", (stage, stage, iso(now), lid))
        if stage == "lead":  # a lead is out of sequences, same as a reply
            for e in rows(conn, "SELECT id FROM enrollments WHERE lead_id = ? AND status IN ('pending', 'active', 'done')", lid):
                engine.stop_enrollment(conn, e["id"], "moved to leads", now=now)
    elif act == "dnc":
        if not engine.unsubscribe(conn, lid, now, detail="marked do-not-contact"):
            raise HTTPException(409, "already do-not-contact")
    elif act == "stop_all":
        for e in rows(conn, "SELECT id FROM enrollments WHERE lead_id = ?", lid):
            engine.stop_enrollment(conn, e["id"], now=now)
    elif act == "owner":
        try:
            crm.set_owner(conn, lid, int(p["owner_id"]) if str(p.get("owner_id") or "").isdigit() else None)
        except ValueError as ex:
            raise HTTPException(422, str(ex))
    elif act == "status":
        try:
            crm.set_status(conn, lid, p.get("status") or "", now)
        except ValueError as ex:
            raise HTTPException(422, str(ex))
    elif act == "snooze":
        days = int(p["days"]) if str(p.get("days") or "").isdigit() else None
        return {"ok": True, "until": crm.snooze(conn, lid, days, local_tz())}
    elif act == "update":
        email = (p.get("email") or "").strip().lower() or None
        li = importer.normalize_linkedin(p.get("linkedin_url") or "") if p.get("linkedin_url") else None
        if p.get("linkedin_url") and not li:
            raise HTTPException(422, "Not a linkedin.com/in/ URL")
        if email and not importer.EMAIL_RE.match(email):
            raise HTTPException(422, "Invalid email")
        try:
            engine.set_lead_contact(conn, lid, email, li, now)
        except Exception:
            raise HTTPException(409, "That email or URL belongs to another lead")
        if "phone" in p:
            data = json.loads(scalar(conn, "SELECT data FROM leads WHERE id = ?", lid) or "{}")
            key = next((k for k in ("whatsapp", "mobile", "mobile_phone", "phone") if str(data.get(k) or "").strip()), "phone")
            data[key] = str(p.get("phone") or "").strip()[:40]
            conn.execute("UPDATE leads SET data = ? WHERE id = ?", (json.dumps(data), lid))
    else:
        raise HTTPException(400, "bad action")
    return {"ok": True}


# ---------- import ----------

@app.get("/import")
def import_page(request: Request, campaign: int | None = None):
    conn = db.connect()
    return page(request, "import.html", conn, campaigns=rows(conn, "SELECT id, name, status FROM campaigns ORDER BY id DESC"),
                campaign=campaign)


@app.post("/import/upload")
async def import_upload(request: Request, file: UploadFile = File(...), campaign_id: str = Form("")):
    conn = db.connect()
    content = await file.read()
    try:
        headers, data = importer.read_table(file.filename or "", content)
    except Exception as e:
        return back("/import", err=f"Could not read {file.filename}: {e}")
    if not data:
        return back("/import", err="No rows found")
    token = secrets.token_urlsafe(12)
    UPLOADS.clear()  # one pending upload at a time; keeps memory bounded
    UPLOADS[token] = {"filename": file.filename, "headers": headers, "rows": data}
    return page(request, "import_map.html", conn, token=token, filename=file.filename, headers=headers,
                sample=data[:5], total=len(data), mapping=importer.guess_mapping(headers),
                campaigns=rows(conn, "SELECT id, name, status FROM campaigns ORDER BY id DESC"),
                campaign_id=int(campaign_id) if campaign_id.isdigit() else None)


@app.post("/import/commit")
async def import_commit(request: Request):
    form = await request.form()
    up = UPLOADS.pop(str(form.get("token")), None)
    if not up:
        return back("/import", err="Upload expired. Please upload again.")
    mapping = {f: str(form.get(f"map_{f}") or "") for f in importer.FIELDS}
    if not mapping["email"] and not mapping["linkedin_url"]:
        return back("/import", err="Map at least an Email or LinkedIn URL column")
    conn = db.connect()
    cid = str(form.get("campaign_id") or "")
    if cid == "new":
        cur = conn.execute("INSERT INTO campaigns (name, created_at) VALUES (?, ?)",
                           (str(form.get("new_campaign") or "").strip() or Path(up["filename"]).stem, iso(utcnow())))
        cid = cur.lastrowid
        _save_steps(conn, cid, _validate_steps(DEFAULT_SEQUENCE)[0])
    cid = int(cid) if str(cid).isdigit() else None
    summary = importer.import_rows(conn, up["rows"], mapping, cid)
    c = get_campaign(conn, cid) if cid else None
    return page(request, "import_done.html", conn, s=summary, c=c, filename=up["filename"])


# ---------- senders / deliverability ----------

@app.get("/senders")
def senders(request: Request, refresh: str | None = None):
    conn = db.connect()
    live = set(runner.accounts())
    items = stats.sender_rows(conn, local_tz())
    for s in items:
        s["connected"] = s["account"] in live
        s["domain"] = s["email"].split("@")[-1]
    health = {}
    for d in sorted({s["domain"] for s in items}):
        try:
            health[d] = domain_health.cached(conn, d, refresh=refresh == d)
        except Exception as e:  # offline: show the reason instead of failing the page
            health[d] = {"checked_at": None, "results": [{"check": "DNS", "status": "warning", "detail": str(e)}]}
    return page(request, "senders.html", conn, senders=items, health=health,
                li_mailbox=db.get_setting(conn, "li_mailbox") or engine.default_sender(conn),
                login_state=runner.login_state)


@app.post("/senders/{account}/cap")
def sender_cap(account: str, daily_cap: int = Form(...)):
    conn = db.connect()
    conn.execute("UPDATE senders SET daily_cap = ? WHERE account = ?", (max(0, daily_cap), account))
    return back("/senders", msg=f"Daily cap for {account} set to {max(0, daily_cap)}")


@app.post("/senders/li-mailbox")
def sender_li_mailbox(account: str = Form(...)):
    conn = db.connect()
    if not one(conn, "SELECT 1 FROM senders WHERE account = ?", account):
        return back("/senders", err="Unknown mailbox")
    db.set_setting(conn, "li_mailbox", account)
    db.set_setting(conn, "li_cursor", None)  # rescan recent LinkedIn mails in the new inbox
    return back("/senders", msg=f"LinkedIn notifications are read from {account}")


@app.post("/senders/{account}/remove")
def sender_remove(account: str):
    conn = db.connect()
    if not one(conn, "SELECT 1 FROM senders WHERE account = ?", account):
        return back("/senders", err="Unknown mailbox")
    r = remove_sender(conn, account)
    msg = f"Removed {account}."
    if r["campaigns"]:
        msg += (f" {r['campaigns']} campaign(s) now send from {r['replacement']}." if r["replacement"]
                else f" {r['campaigns']} campaign(s) have no mailbox until you connect one.")
    if r["threads"]:
        msg += f" {r['threads']} open thread(s) will continue as new emails."
    return back("/senders", msg=msg)


# ---------- settings / runner ----------

@app.get("/settings")
def settings(request: Request):
    conn = db.connect()
    tracker = Tracker.from_env()
    return page(request, "settings.html", conn, logs=list(runner.logs)[-120:], busy=runner.busy,
                eng=engine.engage_settings(conn), voice=db.get_setting(conn, "comment_voice", ""),
                bot=linkedin_bot.settings(conn), bot_status=linkedin_bot.status(conn),
                bot_used=linkedin_bot.used_today(conn, utcnow()), li_login_state=runner.li_login_state,
                ai_ready=bool(os.environ.get("ANTHROPIC_API_KEY")),
                tracker=tracker, tracker_error=tracker.health() if tracker else None,
                track_cursor=db.get_setting(conn, "track_cursor", 0),
                tick_minutes=os.environ.get("OC_TICK_MINUTES", "10"),
                keep_awake=db.get_setting(conn, "keep_awake", True), awake_on=runner.awake.on,
                update=runner.update_info or updater.status(fetch=False), supervised=osutil.supervised(),
                restarting=runner.restarting, team_all=crm.team(conn), roles=crm.ROLES, crm_cfg=crm.settings(conn),
                digest_sent_on=db.get_setting(conn, "digest_sent_on"),
                owned={r["owner_id"]: r["n"] for r in rows(conn, "SELECT owner_id, COUNT(*) n FROM leads GROUP BY owner_id")})


@app.post("/settings/reminders")
def settings_reminders(crm_warm_days: int = Form(3), crm_cold_days: int = Form(90), digest_enabled: str = Form(""),
                       digest_hour: int = Form(8), digest_team: str = Form(""), digest_skip_weekends: str = Form("")):
    conn = db.connect()
    db.set_setting(conn, "crm_warm_days", min(60, max(1, crm_warm_days)))
    db.set_setting(conn, "crm_cold_days", min(730, max(7, crm_cold_days)))
    db.set_setting(conn, "digest_enabled", bool(digest_enabled))
    db.set_setting(conn, "digest_hour", min(23, max(0, digest_hour)))
    db.set_setting(conn, "digest_team", bool(digest_team))
    db.set_setting(conn, "digest_skip_weekends", bool(digest_skip_weekends))
    crm.refresh_followups(conn, tz=local_tz())
    return back("/settings", msg="Saved")


@app.post("/settings/reminders/test")
def settings_reminders_test():
    """Send today's reminder email to your own mailbox now (not to the team)."""
    conn = db.connect()
    try:
        n = crm.maybe_send_digest(conn, engine.Mailboxes(mail_client, runner.log), utcnow(), runner.log,
                                  tz=local_tz(), force=True)
    except (AuthError, GraphError) as ex:
        return back("/settings", err=f"Couldn't send it: {ex}")
    return back("/settings", msg="Sent to your mailbox." if n else "No mailbox is connected, so nothing was sent.")


@app.post("/settings/team")
def settings_team_add(name: str = Form(""), email: str = Form(""), role: str = Form("BDR")):
    conn = db.connect()
    try:
        mid = crm.add_member(conn, name, email, role)
    except ValueError as ex:
        return back("/settings", err=str(ex))
    if not crm.me(conn):
        db.set_setting(conn, "crm_me", mid)
    return back("/settings", msg=f"Added {name.strip()} to the team")


@app.post("/settings/team/{mid}")
def settings_team_update(mid: int, action: str = Form(...)):
    conn = db.connect()
    m = one(conn, "SELECT * FROM team WHERE id = ?", mid)
    if not m:
        raise HTTPException(404)
    if action == "me":
        db.set_setting(conn, "crm_me", mid)
        return back("/settings", msg=f"This computer is used by {m['name']}")
    if action in ("deactivate", "activate"):
        conn.execute("UPDATE team SET active = ? WHERE id = ?", (1 if action == "activate" else 0, mid))
        return back("/settings", msg=f"{m['name']} {'is active again' if action == 'activate' else 'is inactive: kept on past calls, not offered for new ones'}")
    raise HTTPException(400)


@app.post("/settings/update")
def settings_update(action: str = Form("check")):
    if action == "check":
        runner.update_info = updater.status(fetch=True)
        st = runner.update_info
        if not st["ok"]:
            return back("/settings", err=st["error"])
        return back("/settings", msg=f"{st['behind']} update(s) available." if st["behind"] else "Up to date.")
    try:
        msg = updater.apply()
    except RuntimeError as e:
        return back("/settings", err=f"Update failed: {e}")
    runner.update_info = updater.status(fetch=False)
    if osutil.supervised():
        runner.request_restart()
        msg += " Restarting once the current run and the LinkedIn bot are idle; refresh in a minute."
    else:
        msg += f" Restart the dashboard (stop it, then run {'.\\oc' if osutil.WINDOWS else './oc'} serve) to use it."
    return back("/settings", msg=msg)


@app.post("/api/restart")
def restart():
    if not osutil.supervised():
        return {"ok": False, "error": "This dashboard was started by hand with `oc serve`, so nothing would start it "
                                      "again. Stop it (Ctrl+C) and run it again."}
    runner.request_restart()
    return {"ok": True}


@app.post("/settings/keep-awake")
def settings_keep_awake(keep_awake: str = Form("")):
    conn = db.connect()
    db.set_setting(conn, "keep_awake", bool(keep_awake))
    runner.awake.update(wants_awake(conn))
    return back("/settings", msg="Saved")


@app.post("/settings/engagement")
def settings_engagement(engage_every_days: int = Form(7), engage_lookback_months: int = Form(6),
                        comment_voice: str = Form("")):
    conn = db.connect()
    db.set_setting(conn, "engage_every_days", min(max(engage_every_days, 1), 60))
    db.set_setting(conn, "engage_lookback_months", min(max(engage_lookback_months, 1), 12))
    db.set_setting(conn, "comment_voice", comment_voice.strip()[:1000])
    return back("/settings", msg="Engagement settings saved")


@app.post("/settings/linkedin-bot")
async def settings_linkedin_bot(request: Request):
    f = await request.form()
    conn = db.connect()
    num = lambda k, d, hi: min(max(int(f.get(k) or d), 0), hi)  # noqa: E731
    db.set_setting(conn, "li_mode", "bot" if f.get("li_mode") == "bot" else "assisted")
    db.set_setting(conn, "li_bot_caps", {a: num(f"cap_{a}", 0, 200) for a in ("view", "connect", "message", "like", "comment")})
    db.set_setting(conn, "li_bot_week_invites", num("week_invites", 90, 100))
    gmin = num("gap_min", 45, 3600)
    db.set_setting(conn, "li_bot_gap", [gmin, max(gmin, num("gap_max", 120, 3600))])
    start, end = str(f.get("hours_start") or "09:00"), str(f.get("hours_end") or "18:00")
    if start >= end:
        return back("/settings", err="Bot hours must end after they start")
    db.set_setting(conn, "li_bot_hours", [start, end])
    days = [d for d in f.getlist("days") if d in engine.DAYS]
    db.set_setting(conn, "li_bot_days", ",".join(days) or "mon,tue,wed,thu,fri")
    return back("/settings", msg="LinkedIn bot settings saved")


@app.post("/api/linkedin/login")
def linkedin_login():
    runner.start_li_login()
    return {"ok": True}


@app.post("/api/linkedin/resume")
def linkedin_resume():
    linkedin_bot.resume(db.connect())
    runner.run_async()
    return {"ok": True}


@app.post("/api/run")
def run_now():
    runner.run_async()
    return {"ok": True}


@app.post("/api/login")
def start_login():
    runner.start_login()
    return {"ok": True}


@app.get("/api/state")
def state():
    return {"busy": runner.busy, "login": runner.login_state, "li_login": runner.li_login_state,
            "logs": list(runner.logs)[-120:]}
