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
APPROVAL_TIMEOUT = timedelta(days=3)  # unapproved comment drafts expire; the like still counts
# One Chrome profile can only be open once: the login window and the bot must not overlap.
PROFILE_LOCK = threading.Lock()


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
    urn: str
    url: str
    age_days: float | None
    text: str
    liked: bool


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
    if not todo:
        return
    if not PROFILE_LOCK.acquire(blocking=False):
        log("LinkedIn bot: browser profile busy (sign-in window open?), skipping this run")
        return
    driver = driver_factory()
    done = 0
    try:
        driver.open()
        for t, action in todo:
            if done >= cfg["li_bot_per_run"]:
                break
            if used.get(action, 0) >= caps.get(action, 0):
                continue
            if done:
                sleep(random.uniform(*cfg["li_bot_gap"]))
            try:
                if handle(conn, driver, t, action, now=now, log=log, draft=draft):
                    used[action] = used.get(action, 0) + 1
                    week_invites += action == "connect"
                done += 1
            except BlockedError as e:
                pause(conn, str(e))
                log(f"LinkedIn bot paused: {e}. Resolve it in the bot's Chrome window, then Resume in Settings.")
                return
            except ActionError as e:
                _fail(conn, t, str(e), log)
                _record(conn, action, t, False, str(e))
                done += 1
    finally:
        try:
            driver.close()
        finally:
            PROFILE_LOCK.release()


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
        driver.comment(Post(urn="", url=t["post_url"], age_days=None, text="", liked=True), t["body"])
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
        if self.page.locator("iframe[src*='captcha'], #captcha-internal, iframe[title*='challenge' i]").count():
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

    def view(self, url: str) -> Profile:
        self._goto(url)
        head = self._main().inner_text(timeout=10000)[:2500]
        # "· 1st" in the top card (inline elements can run together: "· 1stMessage"), or the screen-reader text.
        m = re.search(r"·\s*(1st|2nd|3rd)(?!\d)", head) or re.search(r"\b(1st|2nd|3rd) degree connection", head)
        degree = m.group(1) if m else None
        pending = self._main().get_by_role("button", name=re.compile(r"^Pending", re.I)).count() > 0
        return Profile(degree=degree, pending=pending)

    def _connect_button(self):
        main = self._main()
        btn = main.get_by_role("button", name=re.compile(r"^Invite .+ to connect$", re.I))
        if btn.count():
            return btn.first
        more = main.get_by_role("button", name=re.compile(r"^More actions$", re.I))
        if more.count():
            more.first.click()
            self._pause(0.8, 1.6)
            item = self.page.get_by_role("button", name=re.compile(r"^Invite .+ to connect$", re.I))
            if not item.count():
                item = self.page.locator("[aria-label^='Invite'][aria-label$='to connect']")
            if item.count():
                return item.first
        return None

    def connect(self, url: str, note: str) -> str:
        p = self.view(url)
        if p.degree == "1st":
            return "already_connected"
        if p.pending:
            return "pending"
        btn = self._connect_button()
        if not btn:
            raise ActionError("no Connect button (follow-only profile, or LinkedIn changed the page)")
        btn.click()
        dialog = self.page.get_by_role("dialog").last
        dialog.wait_for()
        self._pause(0.8, 1.6)
        if dialog.locator("input[type=email]").count():
            raise ActionError("LinkedIn wants their email address to connect")
        if note.strip():
            add = dialog.get_by_role("button", name=re.compile(r"Add a (free )?note", re.I))
            if add.count():
                add.first.click()
                self._pause(0.8, 1.6)
                box = self.page.get_by_role("dialog").last.locator("textarea")
                if box.count():
                    box.first.click()
                    box.first.type(note[:300], delay=random.randint(25, 60))
                    self._pause(0.6, 1.2)
                    send = self.page.get_by_role("dialog").last.get_by_role(
                        "button", name=re.compile(r"^Send( invitation| now)?$", re.I))
                    if not send.count():
                        raise ActionError("invite dialog changed (no Send button)")
                    send.first.click()
                    self._pause()
                    return "sent"
                # Note blocked (free-account limit upsell): close it and send the invite without one.
                self.page.keyboard.press("Escape")
                self._pause(0.8, 1.6)
                btn = self._connect_button()
                if not btn:
                    raise ActionError("couldn't reopen the invite after the note was refused")
                btn.click()
                dialog = self.page.get_by_role("dialog").last
                dialog.wait_for()
        send = dialog.get_by_role("button", name=re.compile(r"^Send without a note$|^Send( now)?$", re.I))
        if not send.count():
            raise ActionError("invite dialog changed (no Send button)")
        send.first.click()
        self._pause()
        return "sent" if not note.strip() else "sent_without_note"

    def message(self, url: str, text: str) -> str:
        p = self.view(url)
        if p.degree != "1st":
            return "not_connected"
        btn = self._main().get_by_role("button", name=re.compile(r"^Message", re.I))
        if not btn.count():
            raise ActionError("no Message button")
        btn.first.click()
        box = self.page.locator("div.msg-form__contenteditable[contenteditable='true']").last
        box.wait_for()
        box.click()
        self._type_multiline(text)
        self._pause(0.8, 1.6)
        send = self.page.locator("button.msg-form__send-button").last
        if not send.count():
            send = self.page.get_by_role("button", name=re.compile(r"^Send$", re.I)).last
        send.click()
        self._pause()
        return "sent"

    def _type_multiline(self, text: str) -> None:
        # Shift+Enter: a bare Enter can send the message in LinkedIn's composer.
        for i, line in enumerate(text.split("\n")):
            if i:
                self.page.keyboard.press("Shift+Enter")
            if line:
                self.page.keyboard.type(line, delay=random.randint(20, 50))

    def recent_posts(self, url: str, limit: int = 8) -> list[Post]:
        self._goto(url.rstrip("/") + "/recent-activity/shares/")
        items = self.page.locator("[data-urn^='urn:li:activity']")
        out = []
        for i in range(min(items.count(), limit)):
            it = items.nth(i)
            urn = it.get_attribute("data-urn") or ""
            header = it.locator(".update-components-header")
            if header.count() and re.search(r"repost|likes|commented|celebrates|loves|supports|finds|reacted",
                                            header.first.inner_text(), re.I):
                continue  # not their own post
            sub = it.locator(".update-components-actor__sub-description")
            body = it.locator(".update-components-text, .feed-shared-update-v2__description")
            like = it.get_by_role("button", name=re.compile(r"React Like|^Like$", re.I))
            out.append(Post(
                urn=urn, url=f"{self.base}/feed/update/{urn}/",
                age_days=parse_age_days(sub.first.inner_text() if sub.count() else ""),
                text=(body.first.inner_text() if body.count() else "")[:4000],
                liked=bool(like.count() and like.first.get_attribute("aria-pressed") == "true"),
            ))
        return out

    def _like_button(self):
        btn = self._main().get_by_role("button", name=re.compile(r"React Like|^Like$", re.I))
        return btn.first if btn.count() else None

    def like(self, post: Post) -> None:
        self._goto(post.url)
        btn = self._like_button()
        if not btn:
            raise ActionError("no Like button on the post")
        if btn.get_attribute("aria-pressed") != "true":
            btn.click()
            self._pause(1, 2)

    def comment(self, post: Post, text: str) -> None:
        self._goto(post.url)
        open_btn = self._main().get_by_role("button", name=re.compile(r"^Comment$", re.I))
        if open_btn.count():
            open_btn.first.click()
            self._pause(0.8, 1.6)
        box = self._main().locator("div.ql-editor[contenteditable='true']").first
        box.wait_for()
        box.click()
        self._type_multiline(text)
        self._pause(0.8, 1.6)
        submit = self._main().locator("button.comments-comment-box__submit-button, button.comments-comment-box__submit-button--cr")
        if not submit.count():
            submit = self._main().get_by_role("button", name=re.compile(r"^(Comment|Post)$", re.I))
        if not submit.count():
            raise ActionError("no button to post the comment")
        submit.last.click()
        self._pause()

    def check(self, url: str) -> dict:
        """Read-only diagnostic: what the bot can see on a profile. Clicks nothing except reading pages."""
        p = self.view(url)
        main = self._main()
        report = {
            "degree": p.degree, "pending": p.pending,
            "connect_button": main.get_by_role("button", name=re.compile(r"^Invite .+ to connect$", re.I)).count() > 0,
            "more_menu": main.get_by_role("button", name=re.compile(r"^More actions$", re.I)).count() > 0,
            "message_button": main.get_by_role("button", name=re.compile(r"^Message", re.I)).count() > 0,
        }
        posts = self.recent_posts(url)
        report["posts"] = [{"age_days": round(x.age_days, 1) if x.age_days is not None else None, "liked": x.liked,
                            "text": x.text[:80], "url": x.url} for x in posts]
        return report
