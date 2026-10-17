"""Runs engine ticks: on a timer inside the dashboard, or once from the CLI. One run at a time across processes."""
from __future__ import annotations

import fcntl
import os
import threading
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime

from . import ai, db, engine, linkedin_bot
from .db import iso, utcnow
from .graph import Graph, accounts, login
from .tracking import Tracker

LOCK_PATH = db.DB_PATH.parent / ".tick.lock"


@contextmanager
def tick_lock():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_PATH, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


class Runner:
    def __init__(self):
        self.logs: deque[str] = deque(maxlen=300)
        self.busy = False
        self.login_state = {"state": "idle", "error": None}
        self.li_login_state = {"state": "idle", "error": None}
        self._lock = threading.Lock()

    def log(self, msg: str) -> None:
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
        self.logs.append(line)
        print(line, flush=True)

    def run_once(self, send: bool = True) -> bool:
        if not self._lock.acquire(blocking=False):
            return False
        self.busy = True
        try:
            with tick_lock() as ok:
                if not ok:
                    self.log("another run in progress, skipped")
                    return False
                factory = Graph if self.accounts() else None
                conn = db.connect()
                try:
                    engine.tick(conn, factory, tracker=Tracker.from_env(), send=send, log=self.log)
                    if send:
                        linkedin_bot.run(conn, linkedin_bot.ChromeDriver, log=self.log, draft=comment_drafter(conn))
                finally:
                    conn.close()
                return True
        except Exception as e:  # keep the loop alive; surface in the dashboard log
            self.log(f"run crashed: {e!r}")
            return False
        finally:
            self.busy = False
            self._lock.release()

    def run_async(self) -> None:
        threading.Thread(target=self.run_once, daemon=True).start()

    def start_loop(self, minutes: int) -> None:
        def loop():
            time.sleep(5)
            while True:
                self.run_once()
                time.sleep(minutes * 60)

        threading.Thread(target=loop, daemon=True, name="ticker").start()

    def start_login(self) -> None:
        def go():
            self.login_state = {"state": "waiting", "error": None}
            try:
                user = add_sender(login())
                self.login_state = {"state": "done", "error": None, "user": user}
                self.log(f"Mailbox connected: {user}")
            except Exception as e:
                self.login_state = {"state": "failed", "error": str(e)}

        threading.Thread(target=go, daemon=True).start()

    def start_li_login(self) -> None:
        """Open the bot's Chrome window on LinkedIn's sign-in page; you sign in there yourself."""
        def go():
            if not linkedin_bot.PROFILE_LOCK.acquire(blocking=False):
                self.li_login_state = {"state": "failed", "error": "the bot is using its browser right now; try again shortly"}
                return
            self.li_login_state = {"state": "waiting", "error": None}
            d = linkedin_bot.ChromeDriver(headless=False)
            try:
                d.open()
                ok = d.login_interactive()
                conn = db.connect()
                db.set_setting(conn, "li_bot_signed_in", ok)
                if ok:
                    linkedin_bot.resume(conn)
                conn.close()
                self.li_login_state = {"state": "done" if ok else "failed",
                                       "error": None if ok else "sign-in not finished within 10 minutes"}
                self.log("LinkedIn bot signed in" if ok else "LinkedIn sign-in timed out")
            except Exception as e:
                self.li_login_state = {"state": "failed", "error": str(e)}
            finally:
                try:
                    d.close()
                finally:
                    linkedin_bot.PROFILE_LOCK.release()

        threading.Thread(target=go, daemon=True).start()

    @staticmethod
    def accounts() -> list[str]:
        try:
            return accounts()
        except Exception:  # offline at startup: MSAL authority discovery fails
            return []


def add_sender(account: str) -> str:
    """Record a freshly signed-in mailbox. First one becomes the default sender and LinkedIn-notification inbox."""
    prof = Graph(account).profile()
    conn = db.connect()
    try:
        conn.execute("""INSERT INTO senders (account, email, name, created_at) VALUES (?, ?, ?, ?)
                        ON CONFLICT(account) DO UPDATE SET email = excluded.email, name = excluded.name""",
                     (account, prof["email"], prof["name"], iso(utcnow())))
        if not db.get_setting(conn, "li_mailbox"):
            db.set_setting(conn, "li_mailbox", account)
        conn.execute("UPDATE campaigns SET sender = ? WHERE sender IS NULL", (account,))
    finally:
        conn.close()
    return prof["email"]


def comment_drafter(conn):
    """Claude drafts for the bot's engage tasks, or None when no Anthropic key is configured."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    voice = db.get_setting(conn, "comment_voice", "")
    return lambda text, lead: ai.draft_comment(text, lead, voice)
