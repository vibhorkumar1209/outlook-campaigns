CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT NOT NULL,          -- open | click | unsubscribe
  sid TEXT NOT NULL,           -- send id from the local app
  url TEXT,                    -- click target
  ua TEXT,
  country TEXT,
  head INTEGER NOT NULL DEFAULT 0,
  ts INTEGER NOT NULL          -- epoch ms
);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events (ts);
