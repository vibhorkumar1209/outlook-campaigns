"""oc: email + LinkedIn outreach from your Outlook, with a local dashboard. See README.md."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

from . import osutil
from .osutil import ROOT, WINDOWS

OC = r".\oc" if WINDOWS else "./oc"


def load_env() -> None:
    f = ROOT / ".env"
    if not f.exists():
        return
    for line in f.read_text(encoding="utf-8-sig").splitlines():
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
            sys.exit(f"blocked: {e}. Run {OC} linkedin-login")
        finally:
            d.close()


def cmd_tracker_setup(a):
    """Generate the shared tracking secrets into .env (once) and print the Cloudflare deploy steps."""
    import secrets

    env = ROOT / ".env"
    text = env.read_text(encoding="utf-8-sig") if env.exists() else ""
    added = []
    for k in ("OC_TRACK_KEY", "OC_TRACK_SECRET"):
        if f"{k}=" not in text:
            text += ("" if text.endswith("\n") or not text else "\n") + f"{k}={secrets.token_urlsafe(32)}\n"
            added.append(k)
    env.write_text(text, encoding="utf-8")
    if not WINDOWS:
        env.chmod(0o600)
    print(f"{'Added ' + ', '.join(added) + ' to' if added else 'Kept existing values in'} {env}\n")
    if WINDOWS:
        secret_steps = ("  npx wrangler secret put SIGNING_SECRET          # paste the OC_TRACK_SECRET value from .env\n"
                        "  npx wrangler secret put API_KEY                 # paste the OC_TRACK_KEY value from .env\n")
    else:
        secret_steps = ("  grep '^OC_TRACK_SECRET=' ../.env | cut -d= -f2- | npx wrangler secret put SIGNING_SECRET\n"
                        "  grep '^OC_TRACK_KEY=' ../.env | cut -d= -f2- | npx wrangler secret put API_KEY\n")
    print(f"""Deploy the tracker (one time, needs a free Cloudflare account; your domain must be on Cloudflare):

  cd "{ROOT / 'tracker'}"
  npm install
  npx wrangler login
  npx wrangler d1 create outreach-tracker        # paste the database_id into wrangler.toml
  npx wrangler d1 execute outreach-tracker --remote --file=schema.sql
{secret_steps}  # in wrangler.toml, uncomment routes and set your subdomain, e.g. links.yourdomain.com
  npx wrangler deploy

Only after the deploy works, add this line to .env and restart {OC} serve:

  OC_TRACK_URL=https://{a.domain or '<your tracking subdomain>'}

(Tracking stays off until then, so no email ever carries links to a tracker that isn't live.)
Settings → Open & click tracking should show "working".""")


def _log_to(path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = f


def cmd_serve(a):
    import uvicorn

    if a.log:
        _log_to(a.log)
    if a.parent_pid:
        osutil.exit_with_parent(a.parent_pid)
    os.environ["OC_PORT"] = str(a.port)
    os.environ["OC_TICKER"] = "0" if a.no_ticker else "1"
    os.environ["OC_TICK_MINUTES"] = str(a.every)
    print(f"Dashboard: http://127.0.0.1:{a.port}", flush=True)
    uvicorn.run("campaigner.web.app:app", host="127.0.0.1", port=a.port, log_level="warning")


def cmd_supervise(a):
    """Windows' stand-in for launchd KeepAlive: run the dashboard, start it again whenever it exits
    (a crash, or a restart after an update)."""
    from pathlib import Path

    log = Path(a.log)
    _log_to(log)
    env = dict(os.environ, OC_SUPERVISED="1", PYTHONPATH=str(ROOT))
    while True:
        started = time.monotonic()
        code = subprocess.call([osutil.python_exe(windowless=True), "-m", "campaigner", "serve", "--port", str(a.port),
                                "--every", str(a.every), "--log", str(log), "--parent-pid", str(os.getpid())],
                               cwd=str(ROOT), env=env)
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} dashboard exited ({code}), starting it again", flush=True)
        time.sleep(2 if time.monotonic() - started > 60 else 30)  # back off if it keeps crashing at startup


def cmd_tick(a):
    from .runner import Runner

    r = Runner()
    if not r.run_once(send=not a.dry_run):
        sys.exit(1)
    if not a.dry_run:
        r.run_bot()


def cmd_install(a):
    log = ROOT / "data" / "dashboard.log"
    osutil.install_service(["--port", str(a.port), "--every", str(a.every)], log)
    print(f"Installed. Dashboard at http://127.0.0.1:{a.port}, starts at login, runs every {a.every} min. Log: {log}")


def cmd_uninstall(a):
    if not osutil.uninstall_service():
        sys.exit("not installed")
    print("Uninstalled")


def _request_restart(port: int) -> bool:
    """Ask the running dashboard to restart once it's idle. False if nothing is listening."""
    import requests

    url = f"http://127.0.0.1:{port}"
    try:
        r = requests.post(f"{url}/api/restart", headers={"Origin": url}, timeout=10)
    except requests.ConnectionError:
        return False
    if r.status_code != 200:
        sys.exit(f"The dashboard refused: {r.text}")
    if not r.json().get("ok"):
        sys.exit(r.json().get("error", "restart refused"))
    return True


def cmd_restart(a):
    if _request_restart(a.port):
        print("Restarting: it waits for the current run and the LinkedIn bot to finish, then comes back on its own.")
    else:
        print(f"Nothing running on port {a.port}. Start it with {OC} serve (or {OC} install).")


def cmd_update(a):
    from . import updater

    st = updater.status()
    if not st["ok"]:
        sys.exit(st["error"])
    if not st["behind"]:
        print(f"Up to date ({st['version']}, branch {st['branch']}).")
        return
    print(f"{st['behind']} new change(s) on {st['branch']}:")
    for c in st["changes"]:
        print(f"  - {c}")
    try:
        print(updater.apply())
    except RuntimeError as e:
        sys.exit(f"Update failed: {e}")
    if _request_restart(a.port):
        print("The dashboard restarts on the new version as soon as it's idle.")
    else:
        print(f"Start the dashboard with {OC} serve to use it.")


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
    for stream in (sys.stdout, sys.stderr):  # Windows consoles and pipes default to cp1252, which can't print "→"
        if stream and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    load_env()
    p = argparse.ArgumentParser(prog="oc", description="Email + LinkedIn outreach from Outlook, with a local dashboard.")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command")

    s = sub.add_parser("login", help="sign in to your M365 mailbox")
    s.add_argument("--device-code", action="store_true", help="use a code instead of a browser popup")
    s.set_defaults(fn=cmd_login)

    for name, fn, help_ in (("serve", cmd_serve, "run the dashboard (and the scheduler) on localhost"),
                            ("install", cmd_install, "start the dashboard at login and keep it running")):
        s = sub.add_parser(name, help=help_)
        s.add_argument("--port", type=int, default=8765)
        s.add_argument("--every", type=int, default=10, help="minutes between runs (default 10)")
        if name == "serve":
            s.add_argument("--no-ticker", action="store_true", help="dashboard only, no automatic runs")
            s.add_argument("--log", help=argparse.SUPPRESS)
            s.add_argument("--parent-pid", type=int, help=argparse.SUPPRESS)
        s.set_defaults(fn=fn)

    s = sub.add_parser("uninstall", help="stop starting the dashboard at login")
    s.set_defaults(fn=cmd_uninstall)

    s = sub.add_parser("supervise")  # internal: what the Windows logon task runs
    s.add_argument("--log", required=True)
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--every", type=int, default=10)
    s.set_defaults(fn=cmd_supervise)

    for name, fn, help_ in (("restart", cmd_restart, "restart the running dashboard once it's idle"),
                            ("update", cmd_update, "get the latest version from GitHub and restart when idle")):
        s = sub.add_parser(name, help=help_)
        s.add_argument("--port", type=int, default=8765)
        s.set_defaults(fn=fn)

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

    s = sub.add_parser("demo", help="seed a draft demo campaign (example.com leads, never sends)")
    s.set_defaults(fn=cmd_demo)

    a = p.parse_args()
    from .graph import AuthError, GraphError

    try:
        a.fn(a)
    except (AuthError, GraphError) as e:
        sys.exit(f"error: {e}")
