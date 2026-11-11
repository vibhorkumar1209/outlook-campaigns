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
    from .runner import add_sender

    print(f"Connected mailbox {add_sender(login(device_code=a.device_code))}. Run again to add another.")


def cmd_linkedin_login(a):
    from . import db, linkedin_bot

    with linkedin_bot.PROFILE_LOCK:
        d = linkedin_bot.ChromeDriver(headless=False)
        d.open()
        try:
            print("A Chrome window opened on LinkedIn. Sign in there yourself (waiting up to 10 minutes)...", flush=True)
            ok = d.login_interactive()
        finally:
            d.close()
    conn = db.connect()
    db.set_setting(conn, "li_bot_signed_in", ok)
    if ok:
        linkedin_bot.resume(conn)
    print("Signed in. Turn the bot on in Settings → LinkedIn automation." if ok else "Not signed in (timed out).")


def cmd_linkedin_check(a):
    """Read-only: open a profile in the bot's window and report what the bot can find. Clicks nothing."""
    import json
    from . import linkedin_bot

    with linkedin_bot.PROFILE_LOCK:
        d = linkedin_bot.ChromeDriver(headless=False)
        d.open()
        try:
            print(json.dumps(d.check(a.profile_url), indent=2))
        except linkedin_bot.BlockedError as e:
            sys.exit(f"blocked: {e}. Run ./oc linkedin-login")
        finally:
            d.close()


def cmd_tracker_setup(a):
    """Generate the shared tracking secrets into .env (once) and print the Cloudflare deploy steps."""
    import secrets

    env = ROOT / ".env"
    text = env.read_text() if env.exists() else ""
    added = []
    for k in ("OC_TRACK_KEY", "OC_TRACK_SECRET"):
        if f"{k}=" not in text:
            text += ("" if text.endswith("\n") or not text else "\n") + f"{k}={secrets.token_urlsafe(32)}\n"
            added.append(k)
    env.write_text(text)
    env.chmod(0o600)
    print(f"{'Added ' + ', '.join(added) + ' to' if added else 'Kept existing values in'} {env}\n")
    print(f"""Deploy the tracker (one time, needs a free Cloudflare account; your domain must be on Cloudflare):

  cd "{ROOT / 'tracker'}"
  npm install
  npx wrangler login
  npx wrangler d1 create outreach-tracker        # paste the database_id into wrangler.toml
  npx wrangler d1 execute outreach-tracker --remote --file=schema.sql
  grep '^OC_TRACK_SECRET=' ../.env | cut -d= -f2- | npx wrangler secret put SIGNING_SECRET
  grep '^OC_TRACK_KEY=' ../.env | cut -d= -f2- | npx wrangler secret put API_KEY
  # in wrangler.toml, uncomment routes and set your subdomain, e.g. links.yourdomain.com
  npx wrangler deploy

Only after the deploy works, add this line to .env and restart ./oc serve:

  OC_TRACK_URL=https://{a.domain or '<your tracking subdomain>'}

(Tracking stays off until then, so no email ever carries links to a tracker that isn't live.)
Settings → Open & click tracking should show "working".""")


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
    if not a.dry_run:
        r.run_bot()


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


def cmd_campaign_export(a):
    from . import db
    from .campaign_file import export_campaign

    data = export_campaign(db.connect(), a.id)
    Path(a.file).write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n")
    print(f"Wrote {data['name']} ({len(data['steps'])} steps) to {a.file}")


def cmd_campaign_import(a):
    from . import db
    from .campaign_file import import_campaign

    data = json.loads(Path(a.file).read_text())
    cid = import_campaign(db.connect(), data, name=a.name, apply_settings=not a.no_settings)
    print(f"Created draft campaign {cid}: {a.name or data.get('name')} ({len(data['steps'])} steps). "
          "Import leads, then Activate it in the dashboard.")


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
        email = f"{f.lower()}.{l.lower()}@example.com"
        cur = conn.execute(
            "INSERT OR IGNORE INTO leads (email, linkedin_url, first_name, last_name, company, title, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (email, f"https://www.linkedin.com/in/{f.lower()}-{l.lower()}-demo", f, l, co, t, iso(now)))
        # lastrowid is stale after an ignored insert, so look the lead up when it already existed.
        lid = cur.lastrowid if cur.rowcount else db.scalar(conn, "SELECT id FROM leads WHERE email = ?", email)
        conn.execute("INSERT OR IGNORE INTO enrollments (campaign_id, lead_id, enrolled_at) VALUES (?, ?, ?)",
                     (cid, lid, iso(now - timedelta(days=6))))
        e = db.one(conn, "SELECT * FROM enrollments WHERE campaign_id = ? AND lead_id = ?", cid, lid)
        at = now - timedelta(days=6 - i % 3)
        event(conn, lid, "enrolled", enrollment=e, at=at)
        event(conn, lid, "task_done", enrollment=e, channel="li_connect", step_pos=0, detail="(demo)", at=at)
        conn.execute("UPDATE leads SET li_state = ? WHERE id = ?", ("connected" if i % 2 else "invited", lid))
        conn.execute("UPDATE enrollments SET step_pos = 0, status = 'active', last_step_at = ? WHERE id = ?", (iso(at), e["id"]))
        if i < 4:
            sent_at = at + timedelta(days=1)
            event(conn, lid, "sent", enrollment=e, channel="email", step_pos=1, detail=f"{co} + quick idea", at=sent_at)
            conn.execute("""INSERT INTO sends (id, enrollment_id, campaign_id, lead_id, step_pos, sender, subject, tracked,
                              sent_at, opened_at, opens, clicked_at, clicks, machine_events) VALUES (?,?,?,?,?,?,?,3,?,?,?,?,?,?)""",
                         (f"demo{cid}x{i}", e["id"], cid, lid, 1, "demo@example.com", f"{co} + quick idea", iso(sent_at),
                          iso(sent_at + timedelta(hours=2)) if i < 3 else None, 2 if i < 3 else 0,
                          iso(sent_at + timedelta(hours=3)) if i == 0 else None, 1 if i == 0 else 0, 1))
            if i < 3:
                event(conn, lid, "open", enrollment=e, channel="email", step_pos=1, detail=f"{co} + quick idea",
                      at=sent_at + timedelta(hours=2))
            if i == 0:
                event(conn, lid, "click", enrollment=e, channel="email", step_pos=1, detail="https://example.com/case-study",
                      at=sent_at + timedelta(hours=3))
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

    s = sub.add_parser("linkedin-login", help="sign the LinkedIn bot's Chrome profile in (you type your password)")
    s.set_defaults(fn=cmd_linkedin_login)

    s = sub.add_parser("linkedin-check", help="read-only test of the bot on one profile (clicks nothing)")
    s.add_argument("profile_url")
    s.set_defaults(fn=cmd_linkedin_check)

    s = sub.add_parser("tracker-setup", help="generate tracking secrets into .env and print Cloudflare deploy steps")
    s.add_argument("--domain", help="tracking subdomain, e.g. links.yourcompany.com")
    s.set_defaults(fn=cmd_tracker_setup)

    s = sub.add_parser("campaign-export", help="save a campaign's sequence and settings to a JSON file")
    s.add_argument("id", type=int, help="campaign id (in its dashboard URL)")
    s.add_argument("file")
    s.set_defaults(fn=cmd_campaign_export)

    s = sub.add_parser("campaign-import", help="recreate a campaign from a JSON file, as a draft")
    s.add_argument("file")
    s.add_argument("--name", help='campaign name (put "Dummy" in it for the 25-minute test mode)')
    s.add_argument("--no-settings", action="store_true", help="don't apply the bot, engagement and mailbox limits")
    s.set_defaults(fn=cmd_campaign_import)

    s = sub.add_parser("demo", help="seed a draft demo campaign (example.com leads, never sends)")
    s.set_defaults(fn=cmd_demo)

    a = p.parse_args()
    from .graph import AuthError, GraphError

    try:
        a.fn(a)
    except (AuthError, GraphError) as e:
        sys.exit(f"error: {e}")
