"""WhatsApp bot: sends approved WhatsApp messages through WhatsApp Web and reads the replies, in a Chrome profile of
its own that you link to your phone yourself.

- Linking: the bot's Chrome window shows WhatsApp Web's QR code; you scan it with your phone (WhatsApp → Linked
  devices). The bot never sees a password or a code. If WhatsApp Web logs out or asks for anything, it pauses and
  every message goes back to the Task queue.
- Who: only Warm and Converted leads, and people you've marked as old contacts (crm.wa_blocked). Everyone else's
  WhatsApp steps are skipped. Checked again just before each send.
- What: sequence WhatsApp steps (tasks) once you approve them in the Task queue (the default; can be turned off),
  and one-off messages you write on a lead's page.
- How fast: a low daily cap, a few messages an hour at most, only in its hours, typed at a person's pace with
  minutes between messages. WhatsApp bans numbers that message strangers in bulk; this is for people who know you.
- Reading: chats the app has written to in the last 30 days are opened now and then; a reply stops that person's
  sequences like any other reply and lands in their history.

WhatsApp Web changes its pages often, so everything page-specific is in WhatsAppDriver.
`./oc whatsapp-check <phone>` opens one chat read-only (types and sends nothing) to see what still matches.
"""
from __future__ import annotations

import hashlib
import random
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

from . import crm, db, engine
from .db import event, iso, one, parse, rows, scalar, utcnow
from .linkedin_bot import ActionError, BlockedError, _ProfileLock
from .render import lead_phone, wa_digits

PROFILE_DIR = Path.home() / ".outlook-campaigns" / "whatsapp-profile"
BASE = "https://web.whatsapp.com"
SNAP_DIR = db.DB_PATH.parent / "bot-errors"
DEFAULTS = {
    "wa_mode": "assisted",            # assisted (you send from the queue) | bot
    "wa_approve": True,               # sequence messages wait for your approval before the bot sends them
    "wa_daily_cap": 15,
    "wa_gap": [90, 240],              # seconds between messages
    "wa_hours": ["10:00", "18:30"],
    "wa_days": "mon,tue,wed,thu,fri",
    "wa_read": True,                  # read replies in chats the app has written to
}
CEILING = 30                          # messages a day, whatever the setting
HOUR_CAP = 6                          # messages in any rolling hour
PER_RUN = (1, 4)                      # messages per session, then a rest
REST_BETWEEN_RUNS = (10, 40)          # minutes off after a session that sent something
MAX_ATTEMPTS = 2
READ_EVERY = timedelta(minutes=20)
READ_LOOKBACK = timedelta(days=30)    # chats are watched for replies this long after the app last wrote
READ_PER_PASS = 6


class NotOnWhatsApp(ActionError):
    """The number isn't on WhatsApp (or isn't a valid number): no point retrying."""


class Unconfirmed(ActionError):
    """Enter was pressed but the message didn't show up: it may or may not have gone. Never retried."""


class _WaLock(_ProfileLock):
    path = PROFILE_DIR.parent / "whatsapp-profile.lock"


PROFILE_LOCK = _WaLock()


# ---------- settings & state ----------

def settings(conn) -> dict:
    s = {k: db.get_setting(conn, k, v) for k, v in DEFAULTS.items()}
    s["wa_daily_cap"] = max(0, min(int(s["wa_daily_cap"]), CEILING))
    return s


def status(conn) -> dict:
    return {"mode": settings(conn)["wa_mode"], "paused": db.get_setting(conn, "wa_bot_paused"),
            "signed_in": bool(db.get_setting(conn, "wa_bot_signed_in")), "profile_exists": PROFILE_DIR.exists()}


def pause(conn, reason: str) -> None:
    db.set_setting(conn, "wa_bot_paused", {"reason": reason, "at": iso(utcnow())})


def resume(conn) -> None:
    db.set_setting(conn, "wa_bot_paused", None)


def _local(now: datetime) -> datetime:
    return now.astimezone(datetime.now().astimezone().tzinfo)


def in_hours(cfg: dict, now: datetime) -> bool:
    loc = _local(now)
    return (engine.DAYS[loc.weekday()] in cfg["wa_days"].split(",")
            and cfg["wa_hours"][0] <= loc.strftime("%H:%M") < cfg["wa_hours"][1])


def sent_since(conn, since: datetime) -> int:
    return scalar(conn, "SELECT COUNT(*) FROM wa_actions WHERE ok = 1 AND at >= ?", iso(since))


def sent_today(conn, now: datetime) -> int:
    return sent_since(conn, _local(now).replace(hour=0, minute=0, second=0, microsecond=0))


def resting(conn, now: datetime) -> bool:
    until = parse(db.get_setting(conn, "wa_bot_rest_until"))
    return bool(until and now < until)


# ---------- what to send ----------

TASKS_SQL = """
    SELECT t.id, t.body, t.bot_attempts, t.comment_status, t.created_at, e.lead_id, l.first_name, l.last_name,
           l.data, l.status, l.dnc, l.old_contact, c.name campaign
    FROM tasks t JOIN enrollments e ON e.id = t.enrollment_id JOIN campaigns c ON c.id = e.campaign_id
    JOIN leads l ON l.id = e.lead_id
    WHERE t.status = 'open' AND t.channel = 'whatsapp' AND c.status = 'active' AND t.bot_attempts < ?
"""
OUTBOX_SQL = """
    SELECT o.id, o.body, o.attempts, o.created_at, o.lead_id, l.first_name, l.last_name, l.data, l.status, l.dnc,
           l.old_contact FROM wa_outbox o JOIN leads l ON l.id = o.lead_id
    WHERE o.status = 'queued' AND o.attempts < ?
"""


def due(conn, cfg: dict) -> list[dict]:
    """Messages the bot may send now: approved sequence messages and your one-offs, oldest first."""
    out = []
    for t in rows(conn, TASKS_SQL, MAX_ATTEMPTS):
        if cfg["wa_approve"] and t["comment_status"] != "approved":
            continue
        out.append({**t, "kind": "task"})
    out += [{**o, "kind": "outbox"} for o in rows(conn, OUTBOX_SQL, MAX_ATTEMPTS)]
    out.sort(key=lambda x: x["created_at"])
    return out


def _name(x: dict) -> str:
    return f"{x['first_name']} {x['last_name']}".strip() or f"lead {x['lead_id']}"


def _drop(conn, item: dict, why: str, now: datetime, log) -> None:
    """Not to be sent at all (no longer eligible): the task is skipped / the one-off cancelled."""
    if item["kind"] == "task":
        conn.execute("UPDATE tasks SET bot_note = ? WHERE id = ?", (why, item["id"]))
        engine.complete_task(conn, item["id"], "skip", now=now, actor="bot")
    else:
        conn.execute("UPDATE wa_outbox SET status = 'cancelled', note = ? WHERE id = ?", (why, item["id"]))
    log(f"WhatsApp bot: not messaging {_name(item)}: {why}")


def _failed(conn, item: dict, note: str, now: datetime, log, final: bool = False) -> None:
    conn.execute("INSERT INTO wa_actions (task_id, outbox_id, lead_id, ok, detail, at) VALUES (?, ?, ?, 0, ?, ?)",
                 (item["id"] if item["kind"] == "task" else None, item["id"] if item["kind"] == "outbox" else None,
                  item["lead_id"], note[:300], iso(now)))
    if item["kind"] == "task":
        conn.execute("UPDATE tasks SET bot_attempts = ?, bot_note = ? WHERE id = ?",
                     (MAX_ATTEMPTS if final else item["bot_attempts"] + 1, note[:300], item["id"]))
        left = not final and item["bot_attempts"] + 1 < MAX_ATTEMPTS
    else:
        attempts = MAX_ATTEMPTS if final else item["attempts"] + 1
        conn.execute("UPDATE wa_outbox SET attempts = ?, note = ?, status = ? WHERE id = ?",
                     (attempts, note[:300], "queued" if attempts < MAX_ATTEMPTS else "failed", item["id"]))
        left = attempts < MAX_ATTEMPTS
    log(f"WhatsApp bot: {_name(item)}: {note}" + ("" if left else " (left for you)"))


def record_sent(conn, item: dict, now: datetime) -> None:
    conn.execute("INSERT INTO wa_actions (task_id, outbox_id, lead_id, ok, detail, at) VALUES (?, ?, ?, 1, NULL, ?)",
                 (item["id"] if item["kind"] == "task" else None, item["id"] if item["kind"] == "outbox" else None,
                  item["lead_id"], iso(now)))
    if item["kind"] == "task":
        engine.complete_task(conn, item["id"], "done", item["body"], now=now, actor="bot")
    else:
        conn.execute("UPDATE wa_outbox SET status = 'sent', sent_at = ?, note = NULL WHERE id = ?", (iso(now), item["id"]))
        event(conn, item["lead_id"], "wa_out", channel="whatsapp", detail="sent by the WhatsApp bot", at=now,
              content=item["body"])
    crm.refresh_followups(conn, lead_id=item["lead_id"])


# ---------- reading replies ----------

def read_due(conn, cfg: dict, now: datetime) -> bool:
    last = parse(db.get_setting(conn, "wa_read_at"))
    return bool(cfg["wa_read"]) and (not last or now - last >= READ_EVERY)


def read_candidates(conn, now: datetime) -> list[dict]:
    """People the app has sent a WhatsApp in the last READ_LOOKBACK, least recently read first."""
    since = iso(now - READ_LOOKBACK)
    sent = rows(conn, """
        SELECT lead_id, MAX(at) last_out FROM (
          SELECT e.lead_id, t.done_at at FROM tasks t JOIN enrollments e ON e.id = t.enrollment_id
          WHERE t.channel = 'whatsapp' AND t.status = 'done' AND t.done_at >= ?
          UNION ALL SELECT lead_id, at FROM events WHERE type = 'wa_out' AND at >= ?)
        GROUP BY lead_id""", since, since)
    read_at = db.get_setting(conn, "wa_read_marks", {}) or {}
    out = []
    for s in sent:
        l = one(conn, "SELECT id, first_name, last_name, data, dnc FROM leads WHERE id = ?", s["lead_id"])
        if not l or l["dnc"] or not wa_digits(lead_phone(l)):
            continue
        out.append({**l, "lead_id": l["id"], "last_out": s["last_out"], "read_at": read_at.get(str(l["id"]), "")})
    out.sort(key=lambda x: x["read_at"])
    return out


META_RE = re.compile(r"\[\s*(\d{1,2})[:.](\d{2})(?:\s*([ap])\.?\s*m\.?)?\s*,\s*(\d{1,4})[./-](\d{1,2})[./-](\d{1,4})\s*\]", re.I)


def parse_meta(meta: str, now: datetime) -> datetime | None:
    """WhatsApp's '[10:21 am, 17/11/2026] Name: ' (the date order follows the phone's locale). Day-first or
    month-first, whichever isn't in the future and is closest to now. Local time."""
    m = META_RE.search(meta or "")
    if not m:
        return None
    hh, mm, ap, a, b, c = m.groups()
    hh, mm = int(hh), int(mm)
    if ap:
        hh = hh % 12 + (12 if ap.lower() == "p" else 0)
    a, b, c = int(a), int(b), int(c)
    tz = _local(now).tzinfo
    options = []
    for y, mo, d in ((c, b, a), (c, a, b), (a, b, c)):  # d/m/y, m/d/y, y/m/d
        y = y + 2000 if y < 100 else y
        try:
            options.append(datetime(y, mo, d, hh, mm, tzinfo=tz))
        except ValueError:
            pass
    past = [o for o in options if o <= now + timedelta(minutes=5)]
    return max(past) if past else None


def new_replies(msgs: list[dict]) -> list[dict]:
    """Messages from them after the first message from us in the loaded part of the chat; earlier history is
    the conversation you already had."""
    first_out = next((i for i, m in enumerate(msgs) if m["dir"] == "out"), None)
    if first_out is None:
        return []
    return [m for m in msgs[first_out + 1:] if m["dir"] == "in" and m["text"]]


def _key(lead_id: int, m: dict) -> str:
    raw = m.get("id") or f"{m.get('meta')}|{m.get('text')}"
    return "wa:" + hashlib.sha1(f"{lead_id}|{raw}".encode()).hexdigest()[:20]


def record_replies(conn, lead: dict, msgs: list[dict], now: datetime, log) -> int:
    """Log their new messages (once each). The first one is a reply: it stops their sequences."""
    n = 0
    for m in new_replies(msgs):
        ref = _key(lead["lead_id"], m)
        if scalar(conn, "SELECT 1 FROM events WHERE ref = ?", ref):
            continue
        at = parse_meta(m.get("meta", ""), now) or now
        at = min(at, now)
        if engine.mark_replied(conn, lead["lead_id"], "whatsapp", at, detail="WhatsApp", content=m["text"][:2000]):
            conn.execute("UPDATE events SET ref = ? WHERE id = (SELECT MAX(id) FROM events WHERE lead_id = ? AND type = 'reply')",
                         (ref, lead["lead_id"]))
            log(f"reply (WhatsApp) from {_name(lead)}")
        else:
            event(conn, lead["lead_id"], "wa_in", channel="whatsapp", detail="WhatsApp", at=at, content=m["text"][:2000], ref=ref)
        n += 1
    if n:
        crm.refresh_followups(conn, lead_id=lead["lead_id"])
    return n


def check_replies(conn, driver, candidates: list[dict], now: datetime, log, sleep, rng) -> None:
    marks = db.get_setting(conn, "wa_read_marks", {}) or {}
    for i, lead in enumerate(candidates[:READ_PER_PASS]):
        if i:
            sleep(rng.uniform(4, 12))
        try:
            msgs = driver.read(wa_digits(lead_phone(lead)))
        except BlockedError:
            raise
        except Exception as ex:
            log(f"WhatsApp bot: couldn't read the chat with {_name(lead)}: {ex}")
            continue
        record_replies(conn, lead, msgs, now, log)
        marks[str(lead["lead_id"])] = iso(now)
    keep = {str(c["lead_id"]) for c in candidates}
    db.set_setting(conn, "wa_read_marks", {k: v for k, v in marks.items() if k in keep})
    db.set_setting(conn, "wa_read_at", iso(now))


# ---------- the run ----------

def run(conn, driver_factory, *, now: datetime | None = None, log=print, sleep=None, rng=None) -> None:
    """One WhatsApp session: read replies when due, then send a few approved messages."""
    import time

    real = now is None  # a live run: timestamps follow the clock through the session's pauses
    now = now or utcnow()
    sleep = sleep or time.sleep
    rng = rng or random.Random()
    cfg = settings(conn)
    if cfg["wa_mode"] != "bot" or db.get_setting(conn, "wa_bot_paused"):
        return
    hours = in_hours(cfg, now)
    room = min(cfg["wa_daily_cap"] - sent_today(conn, now), HOUR_CAP - sent_since(conn, now - timedelta(hours=1)))
    work = due(conn, cfg) if hours and room > 0 and not resting(conn, now) else []
    reading = read_candidates(conn, now) if hours and read_due(conn, cfg, now) else []
    if not work and not reading:
        return
    if not PROFILE_LOCK.acquire(blocking=False):
        return
    driver = driver_factory()
    sent = 0
    try:
        driver.open()
        driver.ready()
        if reading:
            check_replies(conn, driver, reading, now, log, sleep, rng)
        k = min(room, rng.randint(*PER_RUN))
        for item in work:
            if sent >= k:
                break
            lead = one(conn, "SELECT * FROM leads WHERE id = ?", item["lead_id"])
            why = crm.wa_blocked(lead) if lead else "lead deleted"
            digits = wa_digits(lead_phone(lead)) if lead else ""
            if not why and not digits:
                why = "no phone number with country code"
            if why:
                _drop(conn, item, why, now, log)
                continue
            if sent:
                gap = rng.uniform(*cfg["wa_gap"]) * rng.uniform(0.9, 1.3)
                log(f"WhatsApp bot: next message in {gap / 60:.1f} min")
                sleep(gap)
            at = utcnow() if real else now
            try:
                driver.send(digits, item["body"])
            except BlockedError:
                raise
            except (NotOnWhatsApp, Unconfirmed) as ex:
                _failed(conn, item, str(ex), at, log, final=True)
                driver.snapshot(f"wa-{item['kind']}-{item['id']}")
                continue
            except Exception as ex:
                _failed(conn, item, str(ex) or type(ex).__name__, at, log)
                driver.snapshot(f"wa-{item['kind']}-{item['id']}")
                break  # something on the page isn't as expected: stop this session
            record_sent(conn, item, at)
            sent += 1
            log(f"WhatsApp bot: sent to {_name(item)}")
    except BlockedError as ex:
        pause(conn, str(ex))
        log(f"WhatsApp bot paused: {ex}. Messages wait in the Task queue.")
        try:
            driver.snapshot("wa-blocked")
        except Exception:
            pass
    finally:
        try:
            driver.close()
        finally:
            PROFILE_LOCK.release()
        if sent:
            db.set_setting(conn, "wa_bot_rest_until", iso(utcnow() + timedelta(minutes=rng.uniform(*REST_BETWEEN_RUNS))))


# ---------- the browser ----------

_READ_JS = r"""() => {
  const out = [];
  const main = document.querySelector('#main') || document;
  for (const el of main.querySelectorAll('[data-pre-plain-text]')) {
    const holder = el.closest('[data-id]');
    const id = holder ? holder.getAttribute('data-id') || '' : '';
    const row = el.closest('.message-in, .message-out');
    const dir = id ? (id.startsWith('true_') ? 'out' : 'in') : (row && row.classList.contains('message-out') ? 'out' : 'in');
    const t = el.querySelector('span.selectable-text, [data-testid="selectable-text"]');
    out.push({id, dir, meta: el.getAttribute('data-pre-plain-text') || '', text: ((t || el).innerText || '').trim()});
  }
  return out.slice(-40);
}"""


class WhatsAppDriver:
    """WhatsApp Web in your installed Chrome, with a profile used only by the bot."""

    COMPOSE = ("footer div[contenteditable='true'][role='textbox'], footer div[contenteditable='true'], "
               "div[aria-label='Type a message'][contenteditable='true']")
    CHAT_LIST = "#pane-side, [aria-label='Chat list'], div[role='grid'][aria-label]"
    QR = "canvas[aria-label*='scan' i], div[data-ref] canvas, [data-testid='qrcode']"

    def __init__(self, headless: bool = False, profile_dir: Path = PROFILE_DIR):
        self.headless, self.profile_dir = headless, profile_dir
        self._pw = self.ctx = self.page = None

    def open(self) -> None:
        from playwright.sync_api import sync_playwright

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self.ctx = self._pw.chromium.launch_persistent_context(
            str(self.profile_dir), channel="chrome", headless=self.headless, chromium_sandbox=True,
            viewport={"width": 1280, "height": 900})
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.page.set_default_timeout(20000)

    def close(self) -> None:
        try:
            if self.ctx:
                self.ctx.close()
        finally:
            if self._pw:
                self._pw.stop()

    def snapshot(self, name: str) -> None:
        try:
            SNAP_DIR.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(SNAP_DIR / f"{name}.png"))
            for old in sorted(SNAP_DIR.glob("*.png"), key=lambda p: p.stat().st_mtime)[:-50]:
                old.unlink()
        except Exception:
            pass

    def _pause(self, lo=1.5, hi=3.5) -> None:
        self.page.wait_for_timeout(int(random.uniform(lo, hi) * 1000))

    def _visible(self, selector: str):
        loc = self.page.locator(selector)
        for i in range(loc.count()):
            if loc.nth(i).is_visible():
                return loc.nth(i)
        return None

    def _state(self) -> str:
        """'chats' | 'qr' | 'elsewhere' (open in another window) | 'loading'."""
        if self._visible(self.QR):
            return "qr"
        if self._visible(self.CHAT_LIST):
            return "chats"
        body = (self.page.locator("body").inner_text(timeout=5000) or "").lower()
        if "open in another window" in body or "use here" in body:
            return "elsewhere"
        if "to use whatsapp on your computer" in body or "log in with phone number" in body:
            return "qr"
        return "loading"

    def ready(self, timeout_s: int = 90) -> None:
        """WhatsApp Web loaded and linked, or BlockedError."""
        if not self.page.url.startswith(BASE):
            self.page.goto(BASE, wait_until="domcontentloaded")
        for _ in range(timeout_s):
            st = self._state()
            if st == "chats":
                self._pause(2, 4)
                return
            if st == "qr":
                raise BlockedError("WhatsApp Web is signed out: link it again in Settings (scan the QR code)")
            if st == "elsewhere":
                raise BlockedError("WhatsApp Web is open in another window of the bot's profile")
            self.page.wait_for_timeout(1000)
        raise BlockedError("WhatsApp Web didn't finish loading")

    def login_interactive(self, timeout_s: int = 600) -> bool:
        """Shows the QR code; you scan it with your phone. True once the chat list appears."""
        self.page.goto(BASE, wait_until="domcontentloaded")
        for _ in range(timeout_s):
            try:
                if self._visible(self.CHAT_LIST):
                    self._pause(3, 5)  # let it finish syncing before the window closes
                    return True
            except Exception:
                pass
            self.page.wait_for_timeout(1000)
        return False

    def _open_chat(self, digits: str):
        """Open the chat with this number (WhatsApp's own click-to-chat link). Returns the message box."""
        self.page.goto(f"{BASE}/send?phone={digits}", wait_until="domcontentloaded")
        for _ in range(60):
            box = self._visible(self.COMPOSE)
            if box:
                self._pause(1.5, 3.5)  # look at the chat before doing anything
                return box
            st = self._state()
            if st == "qr":
                raise BlockedError("WhatsApp Web is signed out: link it again in Settings (scan the QR code)")
            txt = ""
            try:
                txt = (self.page.locator("div[role='dialog'], [data-animate-modal-popup]").first.inner_text(timeout=500) or "").lower()
            except Exception:
                pass
            if "invalid" in txt or "isn't on whatsapp" in txt or "not on whatsapp" in txt:
                ok = self._visible("div[role='dialog'] button")
                if ok:
                    ok.click()
                raise NotOnWhatsApp("this number isn't on WhatsApp")
            self.page.wait_for_timeout(1000)
        raise ActionError("the chat didn't open")

    def _type(self, text: str) -> None:
        lines = text.replace("\r\n", "\n").split("\n")
        for i, line in enumerate(lines):
            if i:
                self.page.keyboard.press("Shift+Enter")
                self._pause(0.2, 0.6)
            for word in re.split(r"(?<= )", line):
                self.page.keyboard.type(word, delay=random.randint(35, 110))
                if word.rstrip()[-1:] in ".,?!:;" and random.random() < 0.5:
                    self._pause(0.3, 1.0)

    @staticmethod
    def _norm(s: str) -> str:
        return " ".join((s or "").split()).lower()

    def send(self, digits: str, text: str) -> None:
        box = self._open_chat(digits)
        before = len([m for m in self.page.evaluate(_READ_JS) if m["dir"] == "out"])
        box.click()
        self._pause(0.5, 1.2)
        if self._norm(box.inner_text()):
            raise ActionError("the message box already had text in it")
        self._type(text)
        self._pause(1.5, 3.5)  # link previews load; a person rereads
        if self._norm(box.inner_text())[:30] != self._norm(text)[:30]:
            self.page.keyboard.press("Meta+A" if sys.platform == "darwin" else "Control+A")
            self.page.keyboard.press("Delete")
            raise ActionError("the typed text didn't match the message; cleared it")
        self.page.keyboard.press("Enter")
        first = self._norm(text.split("\n")[0])[:25]
        for _ in range(20):
            self.page.wait_for_timeout(1000)
            outs = [m for m in self.page.evaluate(_READ_JS) if m["dir"] == "out"]
            if len(outs) > before or (outs and first and first in self._norm(outs[-1]["text"])):
                self._pause(1.5, 3.0)
                return
        raise Unconfirmed("couldn't confirm the message appeared in the chat; check WhatsApp, then mark the task "
                          "Done or send it yourself")

    def read(self, digits: str) -> list[dict]:
        self._open_chat(digits)
        self._pause(1.5, 3.0)
        return self.page.evaluate(_READ_JS)

    def check(self, digits: str) -> dict:
        """Read-only: open the chat, report what the page looks like. Types nothing."""
        self.ready()
        self._open_chat(digits)
        msgs = self.page.evaluate(_READ_JS)
        return {"compose_box": bool(self._visible(self.COMPOSE)), "messages": len(msgs),
                "from_them": sum(m["dir"] == "in" for m in msgs), "from_you": sum(m["dir"] == "out" for m in msgs),
                "last": msgs[-3:]}
