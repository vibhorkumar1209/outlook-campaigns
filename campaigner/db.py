"""SQLite store for campaigns, sequences, leads, enrollments, LinkedIn tasks and the event log."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("OC_DB") or ROOT / "data" / "app.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',          -- draft|active|paused
    timezone TEXT,                                  -- IANA; NULL = this Mac's zone
    start_at TEXT,                                  -- UTC ISO; NULL = as soon as enrolled
    window_days TEXT NOT NULL DEFAULT 'mon,tue,wed,thu,fri',
    window_start TEXT NOT NULL DEFAULT '09:30',
    window_end TEXT NOT NULL DEFAULT '17:00',
    email_daily_cap INTEGER NOT NULL DEFAULT 40,
    li_daily_cap INTEGER NOT NULL DEFAULT 20,
    gap_min INTEGER NOT NULL DEFAULT 45,
    gap_max INTEGER NOT NULL DEFAULT 120,
    signature TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS steps (
    id INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    pos INTEGER NOT NULL,
    channel TEXT NOT NULL,                          -- email|li_view|li_connect|li_message
    delay_days REAL NOT NULL DEFAULT 0,             -- after the previous step
    condition TEXT NOT NULL DEFAULT 'always',       -- always|if_connected|if_not_connected
    subject TEXT,
    body TEXT NOT NULL DEFAULT '',
    in_thread INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY,
    email TEXT UNIQUE,
    linkedin_url TEXT UNIQUE,
    first_name TEXT NOT NULL DEFAULT '',
    last_name TEXT NOT NULL DEFAULT '',
    company TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    data TEXT NOT NULL DEFAULT '{}',                -- extra import columns, usable in templates
    li_state TEXT NOT NULL DEFAULT 'none',          -- none|invited|connected
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS enrollments (
    id INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending',         -- pending|active|done|replied|bounced|stopped|error
    step_pos INTEGER NOT NULL DEFAULT -1,           -- last step completed or skipped
    conversation_id TEXT,
    first_email_at TEXT,
    last_step_at TEXT,
    replied_at TEXT,
    reply_channel TEXT,
    failures INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    enrolled_at TEXT NOT NULL,
    UNIQUE (campaign_id, lead_id)
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    enrollment_id INTEGER NOT NULL REFERENCES enrollments(id) ON DELETE CASCADE,
    step_pos INTEGER NOT NULL,
    channel TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',            -- open|done|skipped
    created_at TEXT NOT NULL,
    done_at TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    enrollment_id INTEGER REFERENCES enrollments(id) ON DELETE SET NULL,
    campaign_id INTEGER REFERENCES campaigns(id) ON DELETE SET NULL,
    step_pos INTEGER,
    type TEXT NOT NULL,
    channel TEXT,
    detail TEXT,
    at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS senders (
    account TEXT PRIMARY KEY,                       -- M365 sign-in name
    email TEXT NOT NULL,                            -- address mail goes out from
    name TEXT NOT NULL DEFAULT '',
    daily_cap INTEGER NOT NULL DEFAULT 150,         -- across all campaigns using this mailbox
    created_at TEXT NOT NULL
);

-- One row per email sent; carries the tracking id (sid) used in pixel/click/unsubscribe URLs.
CREATE TABLE IF NOT EXISTS sends (
    id TEXT PRIMARY KEY,
    enrollment_id INTEGER REFERENCES enrollments(id) ON DELETE SET NULL,
    campaign_id INTEGER REFERENCES campaigns(id) ON DELETE SET NULL,
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    step_pos INTEGER NOT NULL,
    sender TEXT NOT NULL,
    subject TEXT,
    tracked INTEGER NOT NULL DEFAULT 0,             -- bit 1 = opens, bit 2 = clicks
    sent_at TEXT NOT NULL,
    opened_at TEXT,
    opens INTEGER NOT NULL DEFAULT 0,
    clicked_at TEXT,
    clicks INTEGER NOT NULL DEFAULT 0,
    machine_events INTEGER NOT NULL DEFAULT 0       -- scanner/prefetch hits, excluded from rates
);

-- LinkedIn likes/comments you did from an Engage task; used to avoid repeating a post.
CREATE TABLE IF NOT EXISTS engagements (
    id INTEGER PRIMARY KEY,
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    post_url TEXT,
    liked INTEGER NOT NULL DEFAULT 0,
    commented INTEGER NOT NULL DEFAULT 0,
    comment TEXT,
    at TEXT NOT NULL
);

-- Every action the LinkedIn bot takes; drives its daily/weekly caps.
CREATE TABLE IF NOT EXISTS bot_actions (
    id INTEGER PRIMARY KEY,
    action TEXT NOT NULL,                           -- view|connect|message|like|comment
    task_id INTEGER,
    lead_id INTEGER,
    ok INTEGER NOT NULL,
    detail TEXT,
    at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_sends_campaign ON sends (campaign_id, step_pos);
CREATE INDEX IF NOT EXISTS ix_sends_sender ON sends (sender, sent_at);
CREATE INDEX IF NOT EXISTS ix_events_lead ON events (lead_id, at);
CREATE INDEX IF NOT EXISTS ix_events_campaign ON events (campaign_id, type, at);
CREATE INDEX IF NOT EXISTS ix_enroll_campaign ON enrollments (campaign_id, status);
CREATE INDEX IF NOT EXISTS ix_tasks_status ON tasks (status);
"""


# Columns added after the first release; applied to existing databases on connect.
MIGRATIONS = [
    ("campaigns", "sender", "TEXT"),                               # senders.account; NULL = default mailbox
    ("campaigns", "track_opens", "INTEGER NOT NULL DEFAULT 1"),
    ("campaigns", "track_clicks", "INTEGER NOT NULL DEFAULT 1"),
    ("enrollments", "sender", "TEXT"),                             # mailbox that started the thread
    ("leads", "dnc", "INTEGER NOT NULL DEFAULT 0"),                # do not contact (unsubscribed)
    ("campaigns", "li_engage", "INTEGER NOT NULL DEFAULT 1"),      # like/comment on recent posts during the sequence
    ("tasks", "bot_attempts", "INTEGER NOT NULL DEFAULT 0"),       # LinkedIn bot tries; at the limit it's left to you
    ("tasks", "bot_note", "TEXT"),
    ("tasks", "post_url", "TEXT"),                                 # engage: the post the bot picked
    ("tasks", "post_text", "TEXT"),
    ("tasks", "bot_liked", "INTEGER NOT NULL DEFAULT 0"),
    ("tasks", "comment_status", "TEXT NOT NULL DEFAULT ''"),       # ''|needs_approval|approved
    ("leads", "stage", "TEXT NOT NULL DEFAULT 'prospect'"),        # prospect | lead (they replied)
    ("leads", "became_lead_at", "TEXT"),
    ("leads", "email_bounced", "INTEGER NOT NULL DEFAULT 0"),      # address bounced: email steps skip until fixed
    ("leads", "email_updated_at", "TEXT"),
    ("steps", "attachments", "TEXT NOT NULL DEFAULT '[]'"),        # [{id, name, size, type}] for email steps
    ("leads", "reply_ignored_before", "TEXT"),                     # 'Not a reply': messages up to here don't count
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


def parse(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    for table, col, decl in MIGRATIONS:
        if col not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    if not get_setting(conn, "bounce_model_v2"):
        # Bounces used to stop the whole sequence; now they only flag the address and LinkedIn steps carry on.
        conn.execute("UPDATE leads SET email_bounced = 1 WHERE id IN (SELECT lead_id FROM enrollments WHERE status = 'bounced')")
        conn.execute("UPDATE enrollments SET status = 'active', note = 'email bounced' WHERE status = 'bounced'")
        set_setting(conn, "bounce_model_v2", True)
    if not get_setting(conn, "stage_backfilled"):
        # Everyone who replied before stages existed becomes a lead (once; later manual moves are kept).
        conn.execute("""UPDATE leads SET stage = 'lead', became_lead_at = (
                          SELECT MIN(replied_at) FROM enrollments e WHERE e.lead_id = leads.id AND e.status = 'replied')
                        WHERE id IN (SELECT lead_id FROM enrollments WHERE status = 'replied')""")
        set_setting(conn, "stage_backfilled", True)
    return conn


def rows(conn, sql: str, *args) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, args)]


def one(conn, sql: str, *args) -> dict | None:
    r = conn.execute(sql, args).fetchone()
    return dict(r) if r else None


def scalar(conn, sql: str, *args):
    r = conn.execute(sql, args).fetchone()
    return r[0] if r else None


def event(conn, lead_id: int, type: str, *, enrollment: dict | None = None, channel: str | None = None,
          step_pos: int | None = None, detail: str | None = None, at: datetime | None = None) -> None:
    conn.execute(
        "INSERT INTO events (lead_id, enrollment_id, campaign_id, step_pos, type, channel, detail, at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (lead_id, enrollment and enrollment["id"], enrollment and enrollment["campaign_id"],
         step_pos, type, channel, detail, iso(at or utcnow())),
    )


def get_setting(conn, key: str, default=None):
    v = scalar(conn, "SELECT value FROM settings WHERE key = ?", key)
    return json.loads(v) if v is not None else default


def set_setting(conn, key: str, value) -> None:
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, json.dumps(value)))
