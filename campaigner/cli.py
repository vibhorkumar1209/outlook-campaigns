"""oc: email + LinkedIn outreach from your Outlook, with a local dashboard. See README.md."""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LABEL = "com.outlook-campaigns.dashboard"
PLIST = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"


def load_env() -> None:
    f = ROOT / ".env"
    if not f.exists():
        return
    for line in f.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def cmd_login(a):
    from .graph import login

    print(f"Signed in as {login(device_code=a.device_code)}")


def cmd_serve(a):
    import uvicorn

    os.environ["OC_PORT"] = str(a.port)
    os.environ["OC_TICKER"] = "0" if a.no_ticker else "1"
    os.environ["OC_TICK_MINUTES"] = str(a.every)
    print(f"Dashboard: http://127.0.0.1:{a.port}", flush=True)
    uvicorn.run("campaigner.web.app:app", host="127.0.0.1", port=a.port, log_level="warning")


def cmd_tick(a):
    from .runner import Runner

    r = Runner()
    if not r.run_once(send=not a.dry_run):
        sys.exit(1)


def cmd_install(a):
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    log = ROOT / "data" / "dashboard.log"
    log.parent.mkdir(exist_ok=True)
    PLIST.write_bytes(plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [str(ROOT / "oc"), "serve", "--port", str(a.port), "--every", str(a.every)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }))
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(PLIST)], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(PLIST)], check=True)
    print(f"Installed. Dashboard at http://127.0.0.1:{a.port}, starts at login, runs every {a.every} min. Log: {log}")


def cmd_uninstall(a):
    if not PLIST.exists():
        sys.exit("not installed")
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(PLIST)], capture_output=True)
    PLIST.unlink()
    print("Uninstalled")


def cmd_demo(a):
    """Seed a DRAFT campaign with example.com leads so the dashboard has something to show. Never sends."""
    from . import db, engine
    from .db import event, iso, utcnow
    from datetime import timedelta
    from .web.app import DEFAULT_SEQUENCE, _save_steps, _validate_steps

    conn = db.connect()
    now = utcnow()
    cid = conn.execute("INSERT INTO campaigns (name, status, created_at) VALUES (?, 'draft', ?)",
                       ("Demo · IT leaders Q4", iso(now))).lastrowid
    _save_steps(conn, cid, _validate_steps(DEFAULT_SEQUENCE)[0])
    people = [("Jane", "Doe", "Acme Corp", "Head of IT"), ("Raj", "Mehta", "Globex", "CTO"),
              ("Mei", "Lin", "Initech", "VP Infrastructure"), ("Tom", "Berg", "Umbrella", "IT Director"),
              ("Ana", "Silva", "Hooli", "CIO"), ("Sam", "Okafor", "Stark Ind", "Head of Platform")]
    for i, (f, l, co, t) in enumerate(people):
        lid = conn.execute(
            "INSERT OR IGNORE INTO leads (email, linkedin_url, first_name, last_name, company, title, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"{f.lower()}.{l.lower()}@example.com", f"https://www.linkedin.com/in/{f.lower()}-{l.lower()}-demo",
             f, l, co, t, iso(now))).lastrowid or db.scalar(conn, "SELECT id FROM leads WHERE first_name = ? AND last_name = ?", f, l)
        conn.execute("INSERT OR IGNORE INTO enrollments (campaign_id, lead_id, enrolled_at) VALUES (?, ?, ?)",
                     (cid, lid, iso(now - timedelta(days=6))))
        e = db.one(conn, "SELECT * FROM enrollments WHERE campaign_id = ? AND lead_id = ?", cid, lid)
        at = now - timedelta(days=6 - i % 3)
        event(conn, lid, "enrolled", enrollment=e, at=at)
        event(conn, lid, "task_done", enrollment=e, channel="li_connect", step_pos=0, detail="(demo)", at=at)
        conn.execute("UPDATE leads SET li_state = ? WHERE id = ?", ("connected" if i % 2 else "invited", lid))
        conn.execute("UPDATE enrollments SET step_pos = 0, status = 'active', last_step_at = ? WHERE id = ?", (iso(at), e["id"]))
        if i < 4:
            event(conn, lid, "sent", enrollment=e, channel="email", step_pos=1, detail=f"{co} + quick idea",
                  at=at + timedelta(days=1))
            conn.execute("UPDATE enrollments SET step_pos = 1 WHERE id = ?", (e["id"],))
        if i == 1:
            engine.mark_replied(conn, lid, "linkedin", now - timedelta(days=1), detail="(demo)")
        if i in (4, 5):
            conn.execute("INSERT INTO tasks (enrollment_id, step_pos, channel, body, created_at) VALUES (?, 1, 'li_message', ?, ?)",
                         (e["id"], f"Thanks for connecting, {f}! Sent you a short note by email.", iso(now)))
    print(f"Demo campaign #{cid} created (draft; example.com leads, never sends). Delete it from its Settings tab.")


def main() -> None:
    load_env()
    p = argparse.ArgumentParser(prog="oc", description="Email + LinkedIn outreach from Outlook, with a local dashboard.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("login", help="sign in to your M365 mailbox")
    s.add_argument("--device-code", action="store_true", help="use a code instead of a browser popup")
    s.set_defaults(fn=cmd_login)

    for name, fn, help_ in (("serve", cmd_serve, "run the dashboard (and the scheduler) on localhost"),
                            ("install", cmd_install, "run the dashboard at login via launchd")):
        s = sub.add_parser(name, help=help_)
        s.add_argument("--port", type=int, default=8765)
        s.add_argument("--every", type=int, default=10, help="minutes between runs (default 10)")
        if name == "serve":
            s.add_argument("--no-ticker", action="store_true", help="dashboard only, no automatic runs")
        s.set_defaults(fn=fn)

    s = sub.add_parser("uninstall", help="remove the launchd job")
    s.set_defaults(fn=cmd_uninstall)

    s = sub.add_parser("tick", help="one run now: check replies, queue LinkedIn tasks, send due emails")
    s.add_argument("--dry-run", action="store_true", help="log emails instead of sending")
    s.set_defaults(fn=cmd_tick)

    s = sub.add_parser("demo", help="seed a draft demo campaign (example.com leads, never sends)")
    s.set_defaults(fn=cmd_demo)

    a = p.parse_args()
    from .graph import AuthError, GraphError

    try:
        a.fn(a)
    except (AuthError, GraphError) as e:
        sys.exit(f"error: {e}")
