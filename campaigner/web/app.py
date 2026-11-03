"""Local dashboard. FastAPI + server-rendered Jinja. Listens on 127.0.0.1 only."""
from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, available_timezones

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import TemplateError, TemplateSyntaxError

from .. import ai, db, domain_health, engine, importer, linkedin_bot, stats
from ..db import iso, one, parse, rows, scalar, utcnow
from ..render import ENV, email_html, lead_context, render_text
from ..runner import Runner, remove_sender
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
    peak = max([d["email"] + d["linkedin"] for d in act] + [1])
    camps = stats.campaign_rows(conn)
    for c in camps:
        c["email"] = stats.email_stats(conn, campaign_id=c["id"])
    return page(request, "overview.html", conn, o=stats.overview(conn, tz), act=act, peak=peak, campaigns=camps,
                replies=stats.recent_replies(conn), em=stats.email_stats(conn, since=utcnow() - timedelta(days=30)))


# ---------- LinkedIn queue ----------

@app.get("/tasks")
def tasks(request: Request, campaign: int | None = None):
    conn = db.connect()
    sql = """SELECT t.*, c.id campaign_id, c.name campaign, l.id lead_id, l.first_name, l.last_name, l.company,
                    l.title, l.linkedin_url, l.li_state,
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
        if t["channel"] == engine.ENGAGE:
            t["posts_url"] = t["linkedin_url"].rstrip("/") + "/recent-activity/all/"
            t["engaged"] = rows(conn, "SELECT * FROM engagements WHERE lead_id = ? ORDER BY at DESC LIMIT 10", t["lead_id"])
    done_today = scalar(conn, "SELECT COUNT(*) FROM events WHERE type = 'task_done' AND at >= ?",
                        iso(utcnow().astimezone(local_tz()).replace(hour=0, minute=0, second=0)))
    camps = rows(conn, "SELECT id, name FROM campaigns WHERE status = 'active' ORDER BY name")
    bot = linkedin_bot.settings(conn)
    for t in items:
        t["bot_will"] = (bot["li_mode"] == "bot" and t["bot_attempts"] < linkedin_bot.MAX_ATTEMPTS
                         and t["comment_status"] != "needs_approval")
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
                      track_opens: str = Form(""), track_clicks: str = Form(""), li_engage: str = Form("")):
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
    return back(url, tab="settings", msg="Saved")


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
        subject = ((s.get("subject") or "").strip() or None) if ch == "email" else None
        body = (s.get("body") or "") if ch != "li_view" else ""
        in_thread = bool(s.get("in_thread", True))
        if ch == "email":
            if not subject and (not seen_email or not in_thread):
                errs.append(f"{n}: subject needed ({'first email starts the thread' if not seen_email else 'new thread'})")
            seen_email = True
        if ch in ("email", "li_message") and not body.strip():
            errs.append(f"{n}: message is empty")
        if ch == "li_connect" and len(body) > 300:
            errs.append(f"{n}: LinkedIn invite notes max 300 characters")
        for label, t in (("subject", subject), ("message", body)):
            try:
                ENV.parse(t or "")
            except TemplateSyntaxError as e:
                errs.append(f"{n} {label}: template syntax: {e.message}")
        out.append({"pos": i, "channel": ch, "delay_days": max(delay, 0), "condition": cond, "subject": subject,
                    "body": body, "in_thread": 1 if in_thread else 0})
    if not raw:
        errs.append("Add at least one step")
    return out, errs


def _save_steps(conn, cid: int, steps: list[dict]) -> None:
    conn.execute("BEGIN")
    conn.execute("DELETE FROM steps WHERE campaign_id = ?", (cid,))
    for s in steps:
        conn.execute("INSERT INTO steps (campaign_id, pos, channel, delay_days, condition, subject, body, in_thread) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (cid, s["pos"], s["channel"], s["delay_days"], s["condition"], s["subject"], s["body"], s["in_thread"]))
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
        text = render_text(st.get("body") or "", ctx)
        return {"text": text, "chars": len(text)}
    except TemplateError as e:
        return {"error": str(e)}


@app.post("/api/enrollments/{eid}/stop")
def stop(eid: int):
    conn = db.connect()
    engine.stop_enrollment(conn, eid)
    return {"ok": True}


# ---------- leads ----------

@app.get("/leads")
def leads(request: Request, q: str = ""):
    conn = db.connect()
    like = f"%{q.strip()}%"
    items = rows(conn, """
        SELECT l.*, (SELECT COUNT(*) FROM enrollments e WHERE e.lead_id = l.id) n_campaigns,
          (SELECT status FROM enrollments e WHERE e.lead_id = l.id ORDER BY e.id DESC LIMIT 1) last_status,
          (SELECT MAX(at) FROM events ev WHERE ev.lead_id = l.id AND ev.type IN ('sent', 'task_done', 'reply')) last_touch
        FROM leads l
        WHERE ? = '' OR l.first_name || ' ' || l.last_name LIKE ? OR l.email LIKE ? OR l.company LIKE ?
        ORDER BY l.id DESC LIMIT 500""", q.strip(), like, like, like)
    return page(request, "leads.html", conn, leads=items, q=q, total=scalar(conn, "SELECT COUNT(*) FROM leads"))


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
    return page(request, "lead.html", conn, l=l, enrollments=ens, events=events, extras=extras)


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
    elif act == "connected":
        engine.set_connected(conn, lid, now, source="marked manually")
    elif act == "dnc":
        if not engine.unsubscribe(conn, lid, now, detail="marked do-not-contact"):
            raise HTTPException(409, "already do-not-contact")
    elif act == "stop_all":
        for e in rows(conn, "SELECT id FROM enrollments WHERE lead_id = ?", lid):
            engine.stop_enrollment(conn, e["id"], now=now)
    elif act == "update":
        email = (p.get("email") or "").strip().lower() or None
        li = importer.normalize_linkedin(p.get("linkedin_url") or "") if p.get("linkedin_url") else None
        if p.get("linkedin_url") and not li:
            raise HTTPException(422, "Not a linkedin.com/in/ URL")
        if email and not importer.EMAIL_RE.match(email):
            raise HTTPException(422, "Invalid email")
        try:
            conn.execute("UPDATE leads SET email = ?, linkedin_url = ? WHERE id = ?", (email, li, lid))
        except Exception:
            raise HTTPException(409, "That email or URL belongs to another lead")
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
                tick_minutes=os.environ.get("OC_TICK_MINUTES", "10"))


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
    db.set_setting(conn, "li_bot_week_invites", num("week_invites", 80, 100))
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
