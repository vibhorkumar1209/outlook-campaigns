"""Orchestration logic against a fake Graph. Run: .venv/bin/python -m unittest discover tests"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from campaigner import db, engine, importer
from campaigner.db import iso, one, rows, scalar

MON = datetime(2026, 10, 19, 10, 0, tzinfo=timezone.utc)
ME = "me@co.com"
STEPS = [
    ("li_connect", 0, None, "Hi {{ first_name }}"),
    ("email", 1, "Hi {{ company }}", "Hello {{ first_name }}"),
    ("li_message", 2, None, "Thanks {{ first_name }}"),
    ("email", 2, None, "Bump {{ first_name }}"),
]


def mail(sender, subject="Re: Hi", cid=None, at=MON, preview="", headers=()):
    return {"id": f"{sender}|{subject}|{at}", "conversationId": cid, "subject": subject, "bodyPreview": preview,
            "from": {"emailAddress": {"address": sender}}, "receivedDateTime": at.isoformat().replace("+00:00", "Z"),
            "internetMessageHeaders": [{"name": n, "value": v} for n, v in headers]}


class FakeGraph:
    def __init__(self):
        self.sent, self.inbox = [], []

    def my_address(self):
        return ME

    def send_new(self, to, name, subject, html):
        self.sent.append(("new", to, subject, html))
        return f"conv-{len(self.sent)}"

    def send_in_thread(self, cid, to, name, html, fallback):
        self.sent.append(("thread", to, fallback, html))
        return cid

    def conversation(self, cid):
        return [m for m in self.inbox if m["conversationId"] == cid]

    def inbound_from(self, email, since):
        return [m for m in self.inbox if m["from"]["emailAddress"]["address"] == email]

    def messages_since(self, since):
        return [m for m in self.inbox if datetime.fromisoformat(m["receivedDateTime"]) > since]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "t.db")
        self.g = FakeGraph()
        self.logs = []
        self.cid = self.campaign()
        importer.import_rows(self.conn, [
            {"Email": "jane@x.com", "Name": "Jane Doe", "Company": "Acme", "LinkedIn": "https://linkedin.com/in/JaneDoe?x=1"},
            {"Email": "bob@x.com", "Name": "Bob Ray", "Company": "Beta", "LinkedIn": ""},
        ], importer.guess_mapping(["Email", "Name", "Company", "LinkedIn"]), self.cid)
        for e in rows(self.conn, "SELECT id FROM enrollments"):
            self.conn.execute("UPDATE enrollments SET enrolled_at = ? WHERE id = ?", (iso(MON), e["id"]))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def campaign(self, steps=STEPS, name="c", **kw):
        cols = {"name": name, "status": "active", "timezone": "UTC", "window_days": "mon,tue,wed,thu,fri,sat,sun",
                "window_start": "09:00", "window_end": "17:00", "created_at": iso(MON), **kw}
        cid = self.conn.execute(f"INSERT INTO campaigns ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                                tuple(cols.values())).lastrowid
        for i, (ch, delay, subj, body) in enumerate(steps):
            self.conn.execute("INSERT INTO steps (campaign_id, pos, channel, delay_days, subject, body) VALUES (?,?,?,?,?,?)",
                              (cid, i, ch, delay, subj, body))
        return cid

    def tick(self, at, graph=...):
        engine.tick(self.conn, self.g if graph is ... else graph, now=at, sleep=lambda s: None, log=self.logs.append)

    def enr(self, email):
        return one(self.conn, "SELECT e.* FROM enrollments e JOIN leads l ON l.id = e.lead_id "
                              "WHERE l.email = ? AND e.campaign_id = ?", email, self.cid)

    def conv(self, email):
        return self.enr(email)["conversation_id"]

    def task(self, email):
        return one(self.conn, """SELECT t.* FROM tasks t JOIN enrollments e ON e.id = t.enrollment_id
                                 JOIN leads l ON l.id = e.lead_id WHERE l.email = ? AND t.status = 'open'""", email)


class ImportTest(Base):
    def test_normalizes_and_dedupes(self):
        lead = one(self.conn, "SELECT * FROM leads WHERE email = 'jane@x.com'")
        self.assertEqual(lead["linkedin_url"], "https://www.linkedin.com/in/janedoe")
        self.assertEqual((lead["first_name"], lead["last_name"]), ("Jane", "Doe"))
        s = importer.import_rows(self.conn, [{"li": "linkedin.com/in/janedoe", "title": "CIO"}],
                                 {"email": "", "first_name": "", "last_name": "", "name": "", "company": "",
                                  "title": "title", "linkedin_url": "li"}, self.cid)
        self.assertEqual((s["created"], s["updated"], s["already_enrolled"]), (0, 1, 1))
        self.assertEqual(one(self.conn, "SELECT title FROM leads WHERE email = 'jane@x.com'")["title"], "CIO")


class FlowTest(Base):
    def test_full_cross_channel_sequence(self):
        self.tick(MON)
        t = self.task("jane@x.com")
        self.assertEqual((t["channel"], t["body"]), ("li_connect", "Hi Jane"))
        self.assertEqual(self.enr("bob@x.com")["step_pos"], 0)  # no LinkedIn URL: connect skipped
        self.assertEqual(self.g.sent, [])

        engine.complete_task(self.conn, t["id"], "done", "Hi Jane!", now=MON)
        self.assertEqual(one(self.conn, "SELECT li_state FROM leads WHERE email = 'jane@x.com'")["li_state"], "invited")

        self.tick(MON + timedelta(days=1))
        self.assertEqual(sorted((k, to, s) for k, to, s, _ in self.g.sent),
                         [("new", "bob@x.com", "Hi Beta"), ("new", "jane@x.com", "Hi Acme")])

        # Jane accepts: picked up from LinkedIn's notification mail.
        self.g.inbox.append(mail("invitations@linkedin.com", "Jane Doe accepted your invitation",
                                 at=MON + timedelta(days=2)))
        self.tick(MON + timedelta(days=3))
        self.assertEqual(one(self.conn, "SELECT li_state FROM leads WHERE email = 'jane@x.com'")["li_state"], "connected")
        self.assertEqual(self.task("jane@x.com")["body"], "Thanks Jane")
        self.assertEqual(self.enr("bob@x.com")["step_pos"], 2)  # no LinkedIn URL: li_message skipped at once

        # Bob replies by email; an out-of-office from Jane is ignored.
        self.g.inbox += [mail("bob@x.com", cid=self.conv("bob@x.com"), at=MON + timedelta(days=3, hours=1)),
                         mail("jane@x.com", "Automatic reply: Hi Acme", cid=self.conv("jane@x.com"), at=MON + timedelta(days=3))]
        self.tick(MON + timedelta(days=3, hours=2))
        self.assertEqual(self.enr("bob@x.com")["status"], "replied")
        self.assertEqual(self.enr("jane@x.com")["status"], "active")

        # Jane messages back on LinkedIn: sequence stops, open task closes.
        self.g.inbox.append(mail("messages-noreply@linkedin.com", "Jane Doe just messaged you", at=MON + timedelta(days=4)))
        self.tick(MON + timedelta(days=4, hours=1))
        self.assertEqual((self.enr("jane@x.com")["status"], self.enr("jane@x.com")["reply_channel"]), ("replied", "linkedin"))
        self.assertIsNone(self.task("jane@x.com"))
        self.assertEqual(len(self.g.sent), 2)

    def test_follow_up_threads_and_waits_for_connection(self):
        self.tick(MON)
        engine.complete_task(self.conn, self.task("jane@x.com")["id"], "done", now=MON)
        self.tick(MON + timedelta(days=1))
        self.tick(MON + timedelta(days=3))  # jane not connected: li_message waits
        self.assertIsNone(self.task("jane@x.com"))
        self.tick(MON + timedelta(days=5))
        self.assertEqual([(s[0], s[1], s[2]) for s in self.g.sent[2:]], [("thread", "bob@x.com", "Re: Hi Beta")])
        self.tick(MON + timedelta(days=10))  # due day 3 + 7 days grace: skipped
        self.assertEqual(self.enr("jane@x.com")["step_pos"], 2)
        self.tick(MON + timedelta(days=12))
        self.assertEqual((self.g.sent[-1][0], self.g.sent[-1][1], self.g.sent[-1][2]), ("thread", "jane@x.com", "Re: Hi Acme"))
        self.tick(MON + timedelta(days=13))
        self.assertEqual({self.enr("jane@x.com")["status"], self.enr("bob@x.com")["status"]}, {"done"})

    def test_bounce(self):
        self.tick(MON)
        self.tick(MON + timedelta(days=1))
        self.g.inbox.append(mail("postmaster@x.com", "Undeliverable: Hi Beta", cid=self.conv("bob@x.com"),
                                 at=MON + timedelta(days=1, hours=1)))
        self.tick(MON + timedelta(days=1, hours=2))
        self.assertEqual(self.enr("bob@x.com")["status"], "bounced")

    def test_reply_stops_other_campaigns(self):
        other = self.campaign(name="other")
        lead = self.enr("bob@x.com")["lead_id"]
        self.conn.execute("INSERT INTO enrollments (campaign_id, lead_id, enrolled_at) VALUES (?, ?, ?)", (other, lead, iso(MON)))
        self.tick(MON)
        self.tick(MON + timedelta(days=1))
        self.g.inbox.append(mail("bob@x.com", cid=self.conv("bob@x.com"), at=MON + timedelta(days=1, hours=1)))
        self.tick(MON + timedelta(days=1, hours=2))
        statuses = {r["campaign_id"]: r["status"] for r in rows(self.conn, "SELECT * FROM enrollments WHERE lead_id = ?", lead)}
        self.assertEqual(statuses, {self.cid: "replied", other: "stopped"})

    def test_window_holds_email_not_linkedin(self):
        self.tick(MON.replace(hour=20))
        self.assertIsNotNone(self.task("jane@x.com"))
        self.tick(MON.replace(hour=20) + timedelta(days=1))
        self.assertEqual(self.g.sent, [])
        self.tick(MON + timedelta(days=2))
        self.assertEqual(len(self.g.sent), 1)  # bob (jane still has an open connect task)

    def test_caps(self):
        self.conn.execute("UPDATE campaigns SET email_daily_cap = 1, li_daily_cap = 0 WHERE id = ?", (self.cid,))
        self.tick(MON)
        self.assertIsNone(self.task("jane@x.com"))
        self.tick(MON + timedelta(days=1))
        self.tick(MON + timedelta(days=1, hours=1))
        self.assertEqual(len(self.g.sent), 1)

    def test_no_outlook_queues_linkedin_only(self):
        self.tick(MON, graph=None)
        self.tick(MON + timedelta(days=1), graph=None)
        self.assertIsNotNone(self.task("jane@x.com"))
        self.assertEqual(self.enr("bob@x.com")["status"], "active")
        self.assertEqual(self.g.sent, [])

    def test_template_error(self):
        self.conn.execute("UPDATE steps SET body = '{{ nope }}' WHERE campaign_id = ? AND pos = 1", (self.cid,))
        self.tick(MON)
        self.tick(MON + timedelta(days=1))
        self.assertEqual(self.enr("bob@x.com")["status"], "error")
        self.assertIn("nope", self.enr("bob@x.com")["note"])

    def test_draft_and_paused_do_nothing(self):
        self.conn.execute("UPDATE campaigns SET status = 'paused'")
        self.tick(MON)
        self.assertIsNone(self.task("jane@x.com"))


class ClassifyTest(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(engine.classify(mail("a@x.com"), ME), "reply")
        self.assertEqual(engine.classify(mail(ME), ME), None)
        self.assertEqual(engine.classify(mail("a@x.com", "Hi", headers=[("Auto-Submitted", "auto-replied")]), ME), "auto")
        self.assertEqual(engine.classify(mail("mailer-daemon@x.com", "Hi"), ME), "bounce")
        self.assertEqual(engine.li_kind(mail("invitations@linkedin.com", "Jane, start a conversation with your new connection")), "connected")
        self.assertEqual(engine.li_kind(mail("notifications-noreply@linkedin.com", "Jane sent you a new message")), "message")
        self.assertEqual(engine.li_kind(mail("invitations@linkedin.com", "Jane wants to connect")), None)


if __name__ == "__main__":
    unittest.main()
