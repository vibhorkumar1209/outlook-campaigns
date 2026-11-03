"""LinkedIn browser bot: works through the LinkedIn queue in a dedicated Chrome profile you sign into yourself.

- It never sees your password: you sign in inside the bot's own Chrome window (`./oc linkedin-login`).
- It never solves CAPTCHAs or security checks. If LinkedIn shows a login wall, checkpoint or verification page,
  the bot pauses and every task falls back to the manual queue until you resume it in Settings.
- It acts slowly and under daily/weekly caps you set, only inside its working hours.
- Comments are drafted, then wait for your approval in the queue; only approved text is ever posted.

LinkedIn changes its pages often, so the page-specific code is confined to ChromeDriver.
`./oc linkedin-check <profile-url>` exercises it read-only (no clicks) to see what still matches.
"""
from __future__ import annotations

import hashlib
import random
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import db, engine
from .db import iso, one, parse, rows, scalar, utcnow

PROFILE_DIR = Path.home() / ".outlook-campaigns" / "linkedin-profile"
BASE = "https://www.linkedin.com"
DEFAULTS = {
    "li_mode": "assisted",                                  # assisted | bot
    "li_bot_caps": {"view": 40, "connect": 15, "message": 20, "like": 25, "comment": 8},  # per day; 0 = off
    "li_bot_week_invites": 80,                              # LinkedIn's own limit is ~100/week
    "li_bot_gap": [45, 120],                                # seconds between actions
    "li_bot_hours": ["09:00", "18:00"],
    "li_bot_days": "mon,tue,wed,thu,fri",
    "li_bot_per_run": 12,
}
ACTION_OF = {"li_view": "view", "li_connect": "connect", "li_message": "message", "li_engage": "like"}
MAX_ATTEMPTS = 2
REPLY_CHECK_EVERY = timedelta(minutes=10)  # LinkedIn inbox scan for replies to the bot's messages
APPROVAL_TIMEOUT = timedelta(days=3)  # unapproved comment drafts expire; the like still counts
class _ProfileLock:
    """One Chrome profile can only be open once. A file lock, so the dashboard's runs, its sign-in window and
    CLI commands (`linkedin-login`, `linkedin-check`) in other processes never open it at the same time."""

    path = PROFILE_DIR.parent / "linkedin-profile.lock"

    def __init__(self):
        self._f = None
        self._t = threading.Lock()

    def acquire(self, blocking: bool = True) -> bool:
        import fcntl
        import time

        if not self._t.acquire(blocking=blocking):
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self.path, "w")
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._f = f
                return True
            except BlockingIOError:
                if not blocking:
                    f.close()
                    self._t.release()
                    return False
                time.sleep(1)

    def release(self) -> None:
        import fcntl

        if self._f:
            fcntl.flock(self._f, fcntl.LOCK_UN)
            self._f.close()
            self._f = None
        self._t.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


PROFILE_LOCK = _ProfileLock()


class BlockedError(RuntimeError):
    """LinkedIn wants a person: login wall, checkpoint, CAPTCHA or restriction notice. Pause everything."""


class ActionError(RuntimeError):
    """This action didn't work (button missing, dialog changed). Leave the task for a person."""


@dataclass
class Profile:
    degree: str | None   # "1st" | "2nd" | "3rd" | None
    pending: bool        # invite already sent


@dataclass
class Post:
    urn: str             # stable key (activity URN, or a hash of the post text)
    url: str
    age_days: float | None
    text: str
    liked: bool
    ref: int | None = None  # position on the currently loaded activity page


AGE_RE = re.compile(r"(\d+)\s*(mo|yr|min|hr|m|h|d|w|y)\b")
UNIT_DAYS = {"min": 1 / 1440, "m": 1 / 1440, "h": 1 / 24, "hr": 1 / 24, "d": 1, "w": 7, "mo": 30.4, "y": 365, "yr": 365}


def parse_age_days(text: str) -> float | None:
    """LinkedIn's relative stamps: 45m, 3h, 2d, 1w, 5mo, 1yr."""
    m = AGE_RE.search(text or "")
    return int(m[1]) * UNIT_DAYS[m[2]] if m else None


# ---------- settings & state ----------

def settings(conn) -> dict:
    out = {k: db.get_setting(conn, k, v) for k, v in DEFAULTS.items()}
    out["li_bot_caps"] = {**DEFAULTS["li_bot_caps"], **out["li_bot_caps"]}
    return out


def pause(conn, reason: str) -> None:
    db.set_setting(conn, "li_bot_paused", {"reason": reason, "at": iso(utcnow())})


def resume(conn) -> None:
    db.set_setting(conn, "li_bot_paused", None)


def status(conn) -> dict:
    return {"mode": settings(conn)["li_mode"], "paused": db.get_setting(conn, "li_bot_paused"),
            "signed_in": bool(db.get_setting(conn, "li_bot_signed_in")), "profile_exists": PROFILE_DIR.exists()}


def in_hours(cfg: dict, now: datetime) -> bool:
    local = now.astimezone(datetime.now().astimezone().tzinfo)
    days = {engine.DAYS.index(d.strip()[:3]) for d in cfg["li_bot_days"].split(",") if d.strip()[:3] in engine.DAYS}
    start, end = cfg["li_bot_hours"]
    return local.weekday() in days and start <= local.strftime("%H:%M") < end


def used_today(conn, now: datetime) -> dict[str, int]:
    day0 = now.astimezone(datetime.now().astimezone().tzinfo).replace(hour=0, minute=0, second=0, microsecond=0)
    return {r["action"]: r["n"] for r in rows(conn, "SELECT action, COUNT(*) n FROM bot_actions "
                                                   "WHERE ok = 1 AND at >= ? GROUP BY action", iso(day0))}


def _record(conn, action: str, t: dict, ok: bool, detail: str | None = None, now: datetime | None = None) -> None:
    conn.execute("INSERT INTO bot_actions (action, task_id, lead_id, ok, detail, at) VALUES (?, ?, ?, ?, ?, ?)",
                 (action, t["id"], t["lead_id"], int(ok), detail, iso(now or utcnow())))


def _fail(conn, t: dict, note: str, log) -> None:
    conn.execute("UPDATE tasks SET bot_attempts = bot_attempts + 1, bot_note = ? WHERE id = ?", (note[:300], t["id"]))
    left = MAX_ATTEMPTS - t["bot_attempts"] - 1
    log(f"LinkedIn bot: {t['first_name']} {t['last_name']}: {note}" + ("" if left > 0 else " (left for you in the queue)"))


TASKS_SQL = """
    SELECT t.*, e.lead_id, l.linkedin_url, l.first_name, l.last_name, l.company, l.title, l.li_state, l.data
    FROM tasks t JOIN enrollments e ON e.id = t.enrollment_id
    JOIN campaigns c ON c.id = e.campaign_id JOIN leads l ON l.id = e.lead_id
    WHERE t.status = 'open' AND c.status = 'active' AND l.linkedin_url IS NOT NULL
"""


def expire_unapproved(conn, now: datetime, log) -> None:
    """Comment drafts nobody approved within APPROVAL_TIMEOUT: close as like-only."""
    for t in rows(conn, TASKS_SQL + " AND t.comment_status = 'needs_approval' AND t.created_at < ?",
                  iso(now - APPROVAL_TIMEOUT)):
        engine.complete_task(conn, t["id"], "done", now=now, actor="bot",
                             engage={"post_url": t["post_url"], "liked": t["bot_liked"]})
        log(f"LinkedIn bot: comment for {t['first_name']} {t['last_name']} not approved in time; kept the like")


# ---------- the run ----------

def run(conn, driver_factory, *, now: datetime | None = None, log=print, sleep=None, draft=None) -> None:
    """One pass over due LinkedIn tasks. draft(post_text, lead) -> comment text, or None to skip drafting."""
    import time

    now = now or utcnow()
    sleep = sleep or time.sleep
    cfg = settings(conn)
    if cfg["li_mode"] != "bot":
        return
    if db.get_setting(conn, "li_bot_paused"):
        return
    expire_unapproved(conn, now, log)
    if not in_hours(cfg, now):
        return
    caps, used = cfg["li_bot_caps"], used_today(conn, now)
    week_invites = scalar(conn, "SELECT COUNT(*) FROM bot_actions WHERE ok = 1 AND action = 'connect' AND at >= ?",
                          iso(now - timedelta(days=7)))

    todo = []
    for t in rows(conn, TASKS_SQL + """ AND t.bot_attempts < ? AND t.comment_status != 'needs_approval'
                  ORDER BY t.comment_status = 'approved' DESC, t.created_at, t.id""", MAX_ATTEMPTS):
        action = "comment" if t["comment_status"] == "approved" else ACTION_OF.get(t["channel"])
        if not action or used.get(action, 0) >= caps.get(action, 0):
            continue
        if action == "connect" and week_invites >= cfg["li_bot_week_invites"]:
            continue
        todo.append((t, action))
    candidates = reply_candidates(conn) if reply_check_due(conn, now) else []
    if not todo and not candidates:
        return
    if not PROFILE_LOCK.acquire(blocking=False):
        log("LinkedIn bot: browser profile busy (sign-in window open?), skipping this run")
        return
    driver = driver_factory()
    done = 0
    try:
        driver.open()
        if candidates:
            try:
                check_replies(conn, driver, candidates, now, log)
            except BlockedError:
                raise
            except Exception as e:  # never let a read-only check stop the outreach work
                log(f"LinkedIn bot: reply check failed: {e}")
            db.set_setting(conn, "li_reply_checked_at", iso(now))
            # Anyone who just replied is out of the sequence: drop their pending actions from this pass.
            todo = [(t, a) for t, a in todo
                    if (one(conn, "SELECT status FROM tasks WHERE id = ?", t["id"]) or {}).get("status") == "open"]
        for t, action in todo:
            if done >= cfg["li_bot_per_run"]:
                break
            if used.get(action, 0) >= caps.get(action, 0):
                continue
            if done:
                sleep(random.uniform(*cfg["li_bot_gap"]))
            # You may have handled it in the queue while the bot was busy: never redo it on LinkedIn.
            fresh = one(conn, "SELECT status, comment_status FROM tasks WHERE id = ?", t["id"])
            if not fresh or fresh["status"] != "open" or fresh["comment_status"] != t["comment_status"]:
                continue
            try:
                if handle(conn, driver, t, action, now=now, log=log, draft=draft):
                    used[action] = used.get(action, 0) + 1
                    week_invites += action == "connect"
                done += 1
            except ActionError as e:
                _fail(conn, t, str(e), log)
                _record(conn, action, t, False, str(e))
                done += 1
            except ValueError as e:  # completed by you between our check and the action finishing
                log(f"LinkedIn bot: {t['first_name']} {t['last_name']}: {e}, skipped")
                done += 1
    except BlockedError as e:  # from the reply check or any action: LinkedIn wants a person
        pause(conn, str(e))
        log(f"LinkedIn bot paused: {e}. Resolve it in the bot's Chrome window, then Resume in Settings.")
    finally:
        try:
            driver.close()
        finally:
            PROFILE_LOCK.release()


# ---------- replies on LinkedIn ----------

def reply_check_due(conn, now: datetime) -> bool:
    last = parse(db.get_setting(conn, "li_reply_checked_at"))
    return not last or now - last >= REPLY_CHECK_EVERY


REPLY_CANDIDATES_SQL = """
    SELECT l.id lead_id, l.first_name, l.last_name, l.linkedin_url, t.body sent_text, t.done_at sent_at
    FROM tasks t JOIN enrollments e ON e.id = t.enrollment_id
    JOIN campaigns c ON c.id = e.campaign_id JOIN leads l ON l.id = e.lead_id
    WHERE t.channel = 'li_message' AND t.status = 'done' AND t.body != ''
      AND e.status NOT IN ('replied') AND c.status != 'draft' AND l.stage = 'prospect' AND l.dnc = 0
      AND l.linkedin_url IS NOT NULL AND t.done_at >= ?
      AND t.id = (SELECT MAX(t2.id) FROM tasks t2 JOIN enrollments e2 ON e2.id = t2.enrollment_id
                  WHERE e2.lead_id = l.id AND t2.channel = 'li_message' AND t2.status = 'done')
"""


def reply_candidates(conn) -> list[dict]:
    """Prospects who got a LinkedIn message from us in the last 30 days, even if their sequence has since
    ended (bounced, stopped): a reply still makes them a lead."""
    return rows(conn, REPLY_CANDIDATES_SQL, iso(utcnow() - timedelta(days=30)))


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).split())


def inbox_says_no_reply(items: list[dict], lead: dict) -> bool:
    """True only when the inbox list shows, unambiguously, that we spoke last in this lead's thread."""
    first, last = _norm(lead["first_name"]), _norm(lead["last_name"])
    named = [i for i in items if _norm(i["name"]).split()[:1] == first.split()[:1]]
    strong = [i for i in named if last and last in _norm(i["name"])]
    match = strong[0] if strong else (named[0] if len(named) == 1 else None)
    return bool(match and match["last_from_me"])


def check_replies(conn, driver, candidates: list[dict], now: datetime, log) -> None:
    items = driver.inbox()
    for lead in candidates:
        if inbox_says_no_reply(items, lead):
            continue
        reply = driver.thread_reply(lead["linkedin_url"], lead["sent_text"])
        if reply:
            if engine.mark_replied(conn, lead["lead_id"], "linkedin", now, detail=reply[:200]):
                log(f"reply (LinkedIn) from {lead['first_name']} {lead['last_name']}: moved to Leads")


def handle(conn, driver, t: dict, action: str, *, now: datetime, log, draft) -> bool:
    """Do one task. True if a capped action was spent."""
    url, who = t["linkedin_url"], f"{t['first_name']} {t['last_name']}".strip()
    if action == "view":
        p = driver.view(url)
        if p.degree == "1st":
            engine.set_connected(conn, t["lead_id"], now, source="seen by bot")
        engine.complete_task(conn, t["id"], "done", now=now, actor="bot")
        _record(conn, "view", t, True, now=now)
        log(f"LinkedIn bot: viewed {who}")
        return True

    if action == "connect":
        result = driver.connect(url, t["body"] or "")
        if result == "already_connected":
            engine.complete_task(conn, t["id"], "connected", now=now, actor="bot")
            log(f"LinkedIn bot: {who} is already a connection")
            return False
        if result == "pending":
            engine.complete_task(conn, t["id"], "done", t["body"], now=now, actor="bot")
            log(f"LinkedIn bot: invite to {who} was already pending")
            return False
        note = None if result == "sent" else "sent without note (LinkedIn didn't allow one)"
        engine.complete_task(conn, t["id"], "done", t["body"] if result == "sent" else "", now=now, actor="bot")
        _record(conn, "connect", t, True, note, now=now)
        log(f"LinkedIn bot: invited {who}" + (f" ({note})" if note else ""))
        return True

    if action == "message":
        result = driver.message(url, t["body"] or "")
        if result == "not_connected":
            raise ActionError("not a 1st-degree connection yet")
        engine.complete_task(conn, t["id"], "done", t["body"], now=now, actor="bot")
        _record(conn, "message", t, True, now=now)
        log(f"LinkedIn bot: messaged {who}")
        return True

    if action == "comment":
        driver.comment(Post(urn=t["post_url"].split("#")[-1], url=t["post_url"], age_days=None, text="", liked=True),
                       t["body"])
        engine.complete_task(conn, t["id"], "done", t["body"], now=now, actor="bot",
                             engage={"post_url": t["post_url"], "liked": t["bot_liked"], "commented": 1})
        _record(conn, "comment", t, True, now=now)
        log(f"LinkedIn bot: commented on {who}'s post")
        return True

    # engage: find a fresh post, like it, then queue a comment draft for approval
    lookback = engine.engage_settings(conn)["engage_lookback_months"] * 30.4
    seen = {r["post_url"] for r in rows(conn, "SELECT post_url FROM engagements WHERE lead_id = ? AND post_url IS NOT NULL",
                                        t["lead_id"])}
    posts = [p for p in driver.recent_posts(url) if p.age_days is not None and p.age_days <= lookback and p.url not in seen]
    if not posts:
        engine.complete_task(conn, t["id"], "skip", now=now, actor="bot")
        log(f"LinkedIn bot: {who} has no new posts in the window")
        return False
    post = min(posts, key=lambda p: p.age_days)
    if not post.liked:
        driver.like(post)
    _record(conn, "like", t, True, post.url, now=now)
    settings_caps = settings(conn)["li_bot_caps"]
    if not settings_caps.get("comment"):
        engine.complete_task(conn, t["id"], "done", now=now, actor="bot", engage={"post_url": post.url, "liked": 1})
        log(f"LinkedIn bot: liked {who}'s post")
        return True
    text = ""
    if draft and post.text.strip():
        try:
            text = draft(post.text, t) or ""
        except Exception as e:  # drafting is optional; the approval card lets you write it
            log(f"LinkedIn bot: couldn't draft a comment for {who}: {e}")
    conn.execute("""UPDATE tasks SET post_url = ?, post_text = ?, bot_liked = 1, body = ?, comment_status = 'needs_approval'
                    WHERE id = ?""", (post.url, post.text[:4000], text, t["id"]))
    log(f"LinkedIn bot: liked {who}'s post; comment waiting for your approval")
    return True


# ---------- Chrome (Playwright) ----------

class ChromeDriver:
    """Drives LinkedIn in your installed Chrome, with a profile used only by the bot."""

    def __init__(self, headless: bool = False, base: str = BASE, profile_dir: Path = PROFILE_DIR):
        self.headless, self.base, self.profile_dir = headless, base.rstrip("/"), profile_dir
        self._pw = self.ctx = self.page = None

    def open(self) -> None:
        from playwright.sync_api import sync_playwright

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self.ctx = self._pw.chromium.launch_persistent_context(
            str(self.profile_dir), channel="chrome", headless=self.headless, viewport={"width": 1280, "height": 900})
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.page.set_default_timeout(15000)

    def close(self) -> None:
        try:
            if self.ctx:
                self.ctx.close()
        finally:
            if self._pw:
                self._pw.stop()

    def _pause(self, lo=1.5, hi=3.5) -> None:
        self.page.wait_for_timeout(int(random.uniform(lo, hi) * 1000))

    def _goto(self, url: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded")
        self._pause(2.5, 4.5)  # LinkedIn renders client-side after load
        self._check_blocked()

    def _check_blocked(self) -> None:
        u = self.page.url
        if any(x in u for x in ("/checkpoint", "/authwall", "/login", "/uas/", "/signup")):
            raise BlockedError(f"LinkedIn asked for sign-in or verification ({u.split('?')[0]})")
        # LinkedIn embeds an invisible reCAPTCHA for background scoring; only a visible challenge means "stop".
        challenge = self.page.locator("iframe[src*='captcha'], #captcha-internal, iframe[title*='challenge' i]")
        if any(challenge.nth(i).is_visible() for i in range(challenge.count())):
            raise BlockedError("LinkedIn showed a security check")

    def login_interactive(self, timeout_s: int = 600) -> bool:
        """You sign in yourself in the window; returns True once LinkedIn shows the feed."""
        self.page.goto(self.base + "/login")
        deadline = timeout_s * 1000
        while deadline > 0:
            if re.search(r"/feed|/in/|/mynetwork", self.page.url):
                return True
            self.page.wait_for_timeout(1000)
            deadline -= 1000
        return False

    def _main(self):
        return self.page.locator("main").first

    @staticmethod
    def _slug(url: str) -> str:
        m = re.search(r"/in/([^/?#]+)", url)
        return m.group(1).lower() if m else ""

    def _visible(self, locator):
        for i in range(locator.count()):
            if locator.nth(i).is_visible():
                return locator.nth(i)
        return None

    # Profile pages also list other people ("People also viewed", "People you may know") with their own
    # Message/Connect controls. Every control below is tied to the profile being worked on.

    def view(self, url: str) -> Profile:
        self._goto(url)
        head = self._main().inner_text(timeout=10000)[:2500]
        self.profile_name = head.strip().split("\n", 1)[0].strip()
        # "· 1st" in the top card (inline elements can run together: "· 1stMessage"), or the screen-reader text.
        m = re.search(r"·\s*(1st|2nd|3rd)(?!\d)", head) or re.search(r"\b(1st|2nd|3rd) degree connection", head)
        pending = self._visible(self._main().get_by_role("button", name=re.compile(r"^Pending\b", re.I)))
        return Profile(degree=m.group(1) if m else None, pending=pending is not None)

    def _invite_control(self, url: str):
        """The invite link/button for THIS profile: LinkedIn links it as /preload/custom-invite/?vanityName=<slug>."""
        slug = self._slug(url)
        links = self.page.locator("a[href*='custom-invite']")
        for i in range(links.count()):
            a = links.nth(i)
            m = re.search(r"vanityName=([^&]+)", a.get_attribute("href") or "")
            if m and m.group(1).lower() == slug and a.is_visible():
                return a
        return None

    def _connect_button(self, url: str):
        c = self._invite_control(url)
        if c:
            return c
        more = self._visible(self._main().get_by_role("button", name=re.compile(r"^More( actions)?$", re.I)))
        if more:
            more.click()
            self._pause(0.8, 1.6)
            return self._invite_control(url)
        return None

    def _button_named(self, pattern):
        return self._visible(self.page.get_by_role("button", name=re.compile(pattern, re.I)))

    def connect(self, url: str, note: str) -> str:
        p = self.view(url)
        if p.degree == "1st":
            return "already_connected"
        if p.pending:
            return "pending"
        btn = self._connect_button(url)
        if not btn:
            raise ActionError("no Connect control for this profile (follow-only, or LinkedIn changed the page)")
        btn.click()
        self._pause(1.5, 2.5)
        if self._visible(self.page.locator("input[type=email]")):
            raise ActionError("LinkedIn wants their email address to connect")
        if note.strip():
            add = self._button_named(r"^Add a (free )?note$")
            if add:
                add.click()
                self._pause(0.8, 1.6)
                box = self._visible(self.page.locator("textarea"))
                if box:
                    box.click()
                    box.type(note[:300], delay=random.randint(25, 60))
                    self._pause(0.6, 1.2)
                    send = self._button_named(r"^Send( invitation| now)?$")
                    if not send or not send.is_enabled():
                        raise ActionError("invite dialog changed (no Send button)")
                    send.click()
                    self._pause()
                    return "sent"
                # Note refused (free-account limit upsell): close it and send the invite without one.
                self.page.keyboard.press("Escape")
                self._pause(0.8, 1.6)
                btn = self._connect_button(url)
                if not btn:
                    raise ActionError("couldn't reopen the invite after the note was refused")
                btn.click()
                self._pause(1.5, 2.5)
        send = self._button_named(r"^Send without a note$|^Send( now)?$")
        if not send:
            raise ActionError("invite dialog changed (no Send button)")
        send.click()
        self._pause()
        return "sent" if not note.strip() else "sent_without_note"

    def _message_control(self):
        """This profile's own Message control is labelled exactly "Message" (others say "Message <name>")."""
        for role in ("link", "button"):
            c = self._visible(self._main().get_by_role(role, name="Message", exact=True))
            if c:
                return c
        return None

    def _close_chats(self) -> None:
        """Chat windows persist across page loads; close them so the composer we type into is unambiguous."""
        for _ in range(6):
            close = self._button_named(r"^Close your (conversation|draft conversation) with")
            if not close:
                return
            close.click()
            self._pause(0.5, 1.0)

    def message(self, url: str, text: str) -> str:
        p = self.view(url)
        if p.degree != "1st":
            return "not_connected"
        btn = self._message_control()
        if not btn:
            raise ActionError("no Message control on the profile")
        self._close_chats()
        btn.click()
        self._pause(2.5, 4)
        editors = self.page.locator("[contenteditable='true'][aria-label^='Write a message']")
        vis = [editors.nth(i) for i in range(editors.count()) if editors.nth(i).is_visible()]
        if len(vis) != 1:
            raise ActionError(f"expected one message window, found {len(vis)}")
        # The window's controls are named "... your conversation with <names>" (as hidden text, not aria-label).
        first = (self.profile_name.split() or [""])[0].lower()
        ctl = self.page.get_by_role("button", name=re.compile(r"your (draft )?conversation with", re.I))
        names = [(ctl.nth(i).get_attribute("aria-label") or ctl.nth(i).inner_text() or "").lower()
                 for i in range(ctl.count()) if ctl.nth(i).is_visible()]
        if not first or not any(first in n for n in names):
            raise ActionError(f"couldn't confirm the message window is addressed to {self.profile_name}")
        box = vis[0]
        box.click()
        self._type_multiline(text)
        self._pause(0.8, 1.6)
        send = self._button_named(r"^Send$")
        if not send or not send.is_enabled():
            raise ActionError("Send button not available")
        send.click()
        self._pause(2, 3)
        if (box.inner_text() or "").strip():
            raise ActionError("message may not have sent (text still in the box)")
        self._close_chats()
        return "sent"

    _INBOX_JS = r"""() => {
      const roots = [document];
      const collect = (r) => { for (const el of r.querySelectorAll('*')) if (el.shadowRoot) { roots.push(el.shadowRoot); collect(el.shadowRoot); } };
      collect(document);
      const out = [];
      for (const r of roots) for (const li of r.querySelectorAll('li')) {
        const t = li.innerText || '';
        if (!/conversation details/i.test(t) || t.length > 2000) continue;
        const lines = t.split('\n').map(s => s.trim()).filter(Boolean).filter(s => !/^Status is /.test(s));
        out.push(lines);
      }
      return out;
    }"""

    def inbox(self, limit: int = 40) -> list[dict]:
        """Recent conversations: [{name, snippet, last_from_me}]. Read-only (LinkedIn may mark the top thread read)."""
        self._goto(self.base + "/messaging/")
        self.page.wait_for_timeout(2500)
        out = []
        for lines in self.page.evaluate(self._INBOX_JS)[:limit]:
            if len(lines) < 3:
                continue
            snippet = next((l for l in lines[1:] if re.match(r"^[^:]{1,40}: ", l) or " sent " in l), "")
            out.append({"name": lines[0], "snippet": snippet, "last_from_me": snippet.startswith("You:")})
        return out

    _THREAD_JS = r"""() => {
      const roots = [document];
      const collect = (r) => { for (const el of r.querySelectorAll('*')) if (el.shadowRoot) { roots.push(el.shadowRoot); collect(el.shadowRoot); } };
      collect(document);
      const out = [];
      for (const r of roots) for (const el of r.querySelectorAll('[aria-label^="Options for the message from"]'))
        out.push(el.getAttribute('aria-label').replace(/^Options for the message from /, ''));
      return out;
    }"""

    def thread_reply(self, url: str, our_text: str) -> str | None:
        """Open this profile's conversation and return the first thing they wrote AFTER our message, if any."""
        p = self.view(url)
        btn = self._message_control()
        if not btn or p.degree != "1st":
            return None
        self._close_chats()
        btn.click()
        self._pause(3, 4.5)
        msgs = []
        for m in self.page.evaluate(self._THREAD_JS):  # "Name: text"; LinkedIn renders each message twice
            if not msgs or msgs[-1] != m:
                msgs.append(m)
        self._close_chats()
        key = re.sub(r"\s+", " ", our_text).strip()[:40].lower()
        ours = [i for i, m in enumerate(msgs) if key and key in re.sub(r"\s+", " ", m).lower()]
        if not ours:
            return None
        me = msgs[ours[-1]].split(":", 1)[0]
        later = [m for m in msgs[ours[-1] + 1:] if m.split(":", 1)[0] != me]
        return later[0].split(":", 1)[1].strip() if later else None

    def _type_multiline(self, text: str) -> None:
        # Shift+Enter: a bare Enter can send the message in LinkedIn's composer.
        for i, line in enumerate(text.split("\n")):
            if i:
                self.page.keyboard.press("Shift+Enter")
            if line:
                self.page.keyboard.type(line, delay=random.randint(20, 50))

    # Activity page: class names are randomized, so posts are found through their accessibility labels.
    # Each post has "Open control menu for post by <name>" and a "Reaction button state: ..." button.
    _POSTS_JS = r"""() => {
      const ctl = [...document.querySelectorAll('main button[aria-label^="Open control menu for post"]')];
      const seen = new Set(), out = [];
      ctl.forEach((b) => {
        let c = b;
        while (c && c !== document.body && !c.querySelector('button[aria-label^="Reaction button state"]')) c = c.parentElement;
        if (!c || c === document.body || seen.has(c)) return;
        seen.add(c);
        const idx = out.length;
        c.setAttribute('data-oc-post', String(idx));
        const stamp = [...c.querySelectorAll('*')].find(e => e.children.length === 0 && /^\s*\d+\s*(mo|yr|min|hr|m|h|d|w|y)\b\s*•?/.test(e.textContent));
        const react = c.querySelector('button[aria-label^="Reaction button state"]');
        const text = c.innerText;
        out.push({idx, stamp: stamp ? stamp.textContent.trim() : '', reaction: react ? react.getAttribute('aria-label') : '',
                  repost: /reposted this|likes this|commented on this|celebrates this|loves this|supports this|finds this/i.test(text.slice(0, 300)),
                  text});
      });
      return out;
    }"""

    def _parse_posts(self, activity: str) -> list[Post]:
        out = []
        for r in self.page.evaluate(self._POSTS_JS):
            if r["repost"]:
                continue
            body = r["text"]
            # Drop the header (name, headline, stamp) and the action bar; keep the post's own words.
            if r["stamp"] and r["stamp"] in body:
                body = body.split(r["stamp"], 1)[1]
            body = re.split(r"\n(?:Like|Comment|Repost|Send)\n", body)[0]
            body = re.sub(r"(?:…|\.\.\.)\s*more\s*$", "", body.strip()).strip()
            key = "post:" + hashlib.sha1(re.sub(r"\s+", " ", body[:400]).lower().encode()).hexdigest()[:16]
            out.append(Post(urn=key, url=f"{activity}#{key}", age_days=parse_age_days(r["stamp"]), text=body[:4000],
                            liked="no reaction" not in r["reaction"].lower(), ref=r["idx"]))
        return out

    def recent_posts(self, url: str, limit: int = 8) -> list[Post]:
        activity = url.split("#")[0].split("/recent-activity/")[0].rstrip("/") + "/recent-activity/all/"
        self._goto(activity)
        return self._parse_posts(activity)[:limit]

    def _find_post(self, post: Post):
        """This post's container on the activity page; reloads the page if needed (comments run on a later pass)."""
        activity = post.url.split("#")[0]
        here = self.page.url.split("#")[0] == activity
        posts = self._parse_posts(activity) if here else self.recent_posts(activity, limit=20)
        found = next((p for p in posts if p.urn == post.urn), None)
        if not found:
            raise ActionError("couldn't find that post on their activity page any more")
        return self.page.locator(f"[data-oc-post='{found.ref}']"), found

    def like(self, post: Post) -> None:
        box, found = self._find_post(post)
        if found.liked:
            return
        react = box.locator("button[aria-label^='Reaction button state']").first
        react.click()
        self._pause(1, 2)
        if "no reaction" in (react.get_attribute("aria-label") or "").lower():
            raise ActionError("like didn't register")

    def comment(self, post: Post, text: str) -> None:
        box, _ = self._find_post(post)
        toggle = box.get_by_role("button", name="Comment", exact=True).first
        toggle.click()
        self._pause(1, 2)
        editor = box.locator("[contenteditable='true'][aria-label^='Text editor for creating comment']").first
        editor.wait_for()
        before = {box.locator("button").nth(i).evaluate("e => e.outerHTML.length + ':' + (e.getAttribute('componentkey') || '')")
                  for i in range(box.locator("button").count())}
        editor.click()
        self._type_multiline(text)
        self._pause(1, 2)
        # The post button only appears once there's text. Use it only if exactly one new one showed up.
        buttons = box.locator("button")
        new = []
        for i in range(buttons.count()):
            b = buttons.nth(i)
            sig = b.evaluate("e => e.outerHTML.length + ':' + (e.getAttribute('componentkey') || '')")
            label = (b.get_attribute("aria-label") or b.inner_text() or "").strip()
            if sig not in before and b.is_visible() and b.is_enabled() and re.search(r"comment|post|submit", label, re.I):
                new.append(b)
        if len(new) != 1:
            raise ActionError("couldn't identify the button to post the comment (left typed, not posted)")
        new[0].click()
        self._pause(2, 3)

    def check(self, url: str) -> dict:
        """Read-only diagnostic: what the bot can see on a profile. Clicks nothing except "More" if present."""
        p = self.view(url)
        report = {
            "name": self.profile_name, "degree": p.degree, "pending": p.pending,
            "message_control": self._message_control() is not None,
            "invite_control_for_this_profile": self._invite_control(url) is not None,
        }
        posts = self.recent_posts(url)
        report["posts"] = [{"age_days": round(x.age_days, 1) if x.age_days is not None else None, "liked": x.liked,
                            "text": x.text[:90].replace("\n", " "), "key": x.urn} for x in posts]
        return report
