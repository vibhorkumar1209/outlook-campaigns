"""Orchestration logic against a fake Graph. Run: .venv/bin/python -m unittest discover tests"""
import json
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from campaigner import db, engine, importer, linkedin_bot, stats
from campaigner.db import iso, one, rows, scalar
from campaigner.graph import AuthError
from campaigner.render import email_html
from campaigner.tracking import Tracker

MON = datetime(2026, 10, 19, 10, 0, tzinfo=timezone.utc)
ME = "me@co.com"
OTHER = "sales@co.com"
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
    def __init__(self, me=ME):
        self.me, self.sent, self.inbox = me, [], []

    def my_address(self):
        return self.me

    def send_new(self, to, name, subject, html, attachments=None):
        self.sent.append(("new", to, subject, html))
        self.files = [a["name"] for a in attachments or []]
        return f"conv-{self.me}-{len(self.sent)}"

    def send_in_thread(self, cid, to, name, html, fallback, strip_pixels=None, attachments=None):
        self.sent.append(("thread", to, fallback, html))
        self.files = [a["name"] for a in attachments or []]
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
        self.graphs = {ME: FakeGraph(ME), OTHER: FakeGraph(OTHER)}
        self.g = self.graphs[ME]
        for i, acct in enumerate(self.graphs):  # ME is the default sender (registered first)
            self.conn.execute("INSERT INTO senders (account, email, created_at) VALUES (?, ?, ?)",
                              (acct, acct, iso(MON + timedelta(seconds=i))))
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

    def factory(self, acct):
        if acct not in self.graphs:
            raise AuthError(f"{acct} is not signed in.")
        return self.graphs[acct]

    def tick(self, at, graph=..., tracker=None):
        factory = self.factory if graph is ... else (None if graph is None else (lambda a: graph))
        engine.tick(self.conn, factory, tracker=tracker, now=at, sleep=lambda s: None, log=self.logs.append)

    def enr(self, email):
        return one(self.conn, "SELECT e.* FROM enrollments e JOIN leads l ON l.id = e.lead_id "
                              "WHERE l.email = ? AND e.campaign_id = ?", email, self.cid)

    def conv(self, email):
        return self.enr(email)["conversation_id"]

    def task(self, email, channel=None):
        """Open sequence task (or open task of a given channel, e.g. li_engage)."""
        return one(self.conn, """SELECT t.* FROM tasks t JOIN enrollments e ON e.id = t.enrollment_id
                                 JOIN leads l ON l.id = e.lead_id WHERE l.email = ? AND t.status = 'open'
                                 AND (? IS NULL AND t.channel != 'li_engage' OR t.channel = ?)""", email, channel, channel)


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


    def test_import_keeps_out_leads_dnc_and_people_in_another_sequence(self):
        jane, bob = (one(self.conn, "SELECT id FROM leads WHERE email = ?", e)["id"] for e in ("jane@x.com", "bob@x.com"))
        self.conn.execute("UPDATE leads SET stage = 'lead' WHERE id = ?", (jane,))
        importer.import_rows(self.conn, [{"Email": "carl@x.com", "Name": "Carl Fox"}],
                             importer.guess_mapping(["Email", "Name"]), None)
        self.conn.execute("UPDATE leads SET dnc = 1 WHERE email = 'carl@x.com'")
        other = self.campaign(name="other")
        cols = ["Email", "Name"]
        s = importer.import_rows(self.conn, [{"Email": e, "Name": n} for e, n in
                                             [("jane@x.com", "Jane Doe"), ("bob@x.com", "Bob Ray"), ("carl@x.com", "Carl Fox"),
                                              ("dee@x.com", "Dee Lin")]], importer.guess_mapping(cols), other)
        self.assertEqual((s["enrolled"], s["not_enrolled"]), (1, 3))           # only the new person joins
        self.assertTrue(any("Bob Ray is still in another sequence (c)" in m for m in s["skipped"]))
        self.assertTrue(any("Jane Doe already replied" in m for m in s["skipped"]))
        self.assertTrue(any("Carl Fox unsubscribed" in m for m in s["skipped"]))
        # Once Bob's first sequence ends, he can join the next one; re-importing into his own campaign is a no-op.
        self.conn.execute("UPDATE enrollments SET status = 'done' WHERE lead_id = ? AND campaign_id = ?", (bob, self.cid))
        s = importer.import_rows(self.conn, [{"Email": "bob@x.com", "Name": "Bob Ray"}], importer.guess_mapping(cols), other)
        self.assertEqual(s["enrolled"], 1)
        s = importer.import_rows(self.conn, [{"Email": "bob@x.com", "Name": "Bob Ray"}], importer.guess_mapping(cols), self.cid)
        self.assertEqual((s["already_enrolled"], s["not_enrolled"]), (1, 0))

    def test_copy_campaign(self):
        self.conn.execute("UPDATE campaigns SET signature = 'Best, V', track_opens = 0, status = 'active' WHERE id = ?", (self.cid,))
        self.conn.execute("UPDATE steps SET attachments = '[{\"id\": \"abc\"}]' WHERE campaign_id = ? AND pos = 1", (self.cid,))
        new = engine.copy_campaign(self.conn, self.cid)
        c = one(self.conn, "SELECT * FROM campaigns WHERE id = ?", new)
        self.assertEqual((c["name"], c["status"], c["signature"], c["track_opens"]), ("c (copy)", "draft", "Best, V", 0))
        cols = "pos, channel, delay_days, condition, subject, body, in_thread, attachments"
        self.assertEqual([tuple(r.values()) for r in rows(self.conn, f"SELECT {cols} FROM steps WHERE campaign_id = ? ORDER BY pos", new)],
                         [tuple(r.values()) for r in rows(self.conn, f"SELECT {cols} FROM steps WHERE campaign_id = ? ORDER BY pos", self.cid)])
        self.assertEqual(scalar(self.conn, "SELECT COUNT(*) FROM enrollments WHERE campaign_id = ?", new), 0)
        self.assertEqual(one(self.conn, "SELECT name FROM campaigns WHERE id = ?", engine.copy_campaign(self.conn, self.cid))["name"],
                         "c (copy 2)")

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

    def test_bounce_flags_address_and_linkedin_continues(self):
        self.tick(MON)
        self.tick(MON + timedelta(days=1))
        self.g.inbox.append(mail("postmaster@x.com", "Undeliverable: Hi Beta", cid=self.conv("bob@x.com"),
                                 at=MON + timedelta(days=1, hours=1), preview="Your message to bob@x.com couldn't be delivered"))
        self.tick(MON + timedelta(days=1, hours=2))
        self.assertEqual(one(self.conn, "SELECT email_bounced FROM leads WHERE email = 'bob@x.com'")["email_bounced"], 1)
        self.assertEqual(self.enr("bob@x.com")["status"], "active")       # sequence keeps going
        self.assertEqual(scalar(self.conn, "SELECT COUNT(*) FROM events WHERE type = 'bounce'"), 1)
        self.tick(MON + timedelta(days=1, hours=3))                       # rescans don't double-count
        self.assertEqual(scalar(self.conn, "SELECT COUNT(*) FROM events WHERE type = 'bounce'"), 1)
        self.tick(MON + timedelta(days=20))
        self.assertEqual([x for x in self.g.sent if x[1] == "bob@x.com" and x[0] == "thread"], [])  # email steps skipped

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


class ResumeStageTest(Base):
    EMAIL = [("email", 0, "Hi {{ company }}", "Hello"), ("email", 2, None, "Bump")]

    def setUp(self):
        super().setUp()
        self.conn.execute("UPDATE campaigns SET status = 'paused' WHERE id = ?", (self.cid,))
        self.cid = self.campaign(self.EMAIL, name="email-only")
        for l in rows(self.conn, "SELECT id FROM leads"):
            self.conn.execute("INSERT INTO enrollments (campaign_id, lead_id, enrolled_at) VALUES (?, ?, ?)",
                              (self.cid, l["id"], iso(MON)))

    def test_fixing_a_bounced_address_resumes_email(self):
        self.conn.execute("INSERT INTO steps (campaign_id, pos, channel, delay_days, subject, body) VALUES (?, 2, 'email', 2, NULL, 'Last')",
                          (self.cid,))
        self.tick(MON)
        self.g.inbox.append(mail("postmaster@x.com", "Undeliverable: Hi Beta", cid=self.conv("bob@x.com"),
                                 at=MON + timedelta(minutes=2), preview="Your message to bob@x.com couldn't be delivered"))
        self.tick(MON + timedelta(minutes=5))
        lead = one(self.conn, "SELECT id FROM leads WHERE email = 'bob@x.com'")["id"]
        self.tick(MON + timedelta(days=2))                                  # step 2 skipped: still bounced
        engine.set_lead_contact(self.conn, lead, "bob.ray@x.com", None, MON + timedelta(days=2, hours=1))
        self.tick(MON + timedelta(days=2, hours=2))                         # old notice doesn't re-flag the new address
        self.assertEqual(one(self.conn, "SELECT email_bounced FROM leads WHERE id = ?", lead)["email_bounced"], 0)
        self.tick(MON + timedelta(days=4, hours=1))
        self.assertIn("bob.ray@x.com", [x[1] for x in self.g.sent])         # step 3 goes to the fixed address

    def test_resume_error_and_refuse_replied(self):
        self.conn.execute("UPDATE steps SET body = '{{ nope }}' WHERE campaign_id = ? AND pos = 0", (self.cid,))
        self.tick(MON)
        e = self.enr("jane@x.com")
        self.assertEqual(e["status"], "error")
        self.conn.execute("UPDATE steps SET body = 'Hello' WHERE campaign_id = ? AND pos = 0", (self.cid,))
        engine.resume_enrollment(self.conn, e["id"], now=MON + timedelta(minutes=5))
        self.tick(MON + timedelta(minutes=6))
        self.assertIn("jane@x.com", [x[1] for x in self.g.sent])
        engine.mark_replied(self.conn, e["lead_id"], "email", MON + timedelta(hours=1))
        with self.assertRaises(ValueError):
            engine.resume_enrollment(self.conn, e["id"])

    def test_reply_moves_to_leads_and_out_of_sequences(self):
        self.tick(MON)
        self.g.inbox.append(mail("jane@x.com", cid=self.conv("jane@x.com"), at=MON + timedelta(minutes=30)))
        self.tick(MON + timedelta(hours=1))
        l = one(self.conn, "SELECT stage, became_lead_at FROM leads WHERE email = 'jane@x.com'")
        self.assertEqual(l["stage"], "lead")
        self.assertIsNotNone(l["became_lead_at"])
        statuses = {r["status"] for r in rows(self.conn, "SELECT e.status FROM enrollments e JOIN leads l ON l.id = e.lead_id WHERE l.email = 'jane@x.com'")}
        self.assertTrue(statuses <= {"replied", "stopped"})
        self.tick(MON + timedelta(days=3))
        self.assertEqual([x for x in self.g.sent if x[1] == "jane@x.com" and x[0] == "thread"], [])
        self.assertEqual(one(self.conn, "SELECT stage FROM leads WHERE email = 'bob@x.com'")["stage"], "prospect")

    def test_quick_pass_sends_without_inbox_scans(self):
        calls = []
        orig = self.g.conversation
        self.g.conversation = lambda cid: calls.append(cid) or orig(cid)
        engine.tick(self.conn, self.factory, now=MON, sleep=lambda s: None, log=self.logs.append, checks=False)
        self.assertEqual(len(self.g.sent), 2)
        self.assertEqual(calls, [])


class NewFeaturesTest(Base):
    def test_dummy_campaign_runs_in_30_minutes(self):
        self.conn.execute("UPDATE campaigns SET status = 'paused' WHERE id = ?", (self.cid,))
        cid = self.campaign([("email", 0, "Hi {{ company }}", "Hello"), ("email", 2, None, "Bump"), ("email", 7, None, "Last")],
                            name="Dummy run", window_days="sat", window_start="09:00", window_end="09:01")  # window ignored
        lead = one(self.conn, "SELECT id FROM leads WHERE email = 'bob@x.com'")["id"]
        self.conn.execute("INSERT INTO enrollments (campaign_id, lead_id, enrolled_at) VALUES (?, ?, ?)", (cid, lead, iso(MON)))
        c = one(self.conn, "SELECT * FROM campaigns WHERE id = ?", cid)
        steps = rows(self.conn, "SELECT * FROM steps WHERE campaign_id = ? ORDER BY pos", cid)
        total = sum((engine.step_delay(c, steps, k) for k in range(3)), timedelta())
        self.assertEqual(round(total.total_seconds() / 60), 25)
        for m in range(0, 31):
            self.tick(MON + timedelta(minutes=m))
        self.assertEqual(len([x for x in self.g.sent if x[1] == "bob@x.com"]), 3)
        self.assertEqual(one(self.conn, "SELECT status FROM enrollments WHERE campaign_id = ?", cid)["status"], "done")

    def test_linkedin_step_runs_after_an_email_bounce(self):
        self.conn.execute("UPDATE campaigns SET status = 'paused' WHERE id = ?", (self.cid,))
        cid = self.campaign([("email", 0, "Hi {{ company }}", "Hello"), ("li_message", 0.01, None, "Follow-up **now**")],
                            name="mixed")
        jane = one(self.conn, "SELECT id FROM leads WHERE email = 'jane@x.com'")["id"]
        self.conn.execute("UPDATE leads SET li_state = 'connected' WHERE id = ?", (jane,))
        self.conn.execute("INSERT INTO enrollments (campaign_id, lead_id, enrolled_at) VALUES (?, ?, ?)", (cid, jane, iso(MON)))
        self.tick(MON)
        conv = one(self.conn, "SELECT conversation_id FROM enrollments WHERE campaign_id = ?", cid)["conversation_id"]
        self.g.inbox.append(mail("postmaster@x.com", "Undeliverable: Hi Acme", cid=conv, at=MON + timedelta(minutes=2),
                                 preview="Your message to jane@x.com couldn't be delivered"))
        self.tick(MON + timedelta(hours=1))
        t = one(self.conn, "SELECT t.* FROM tasks t JOIN enrollments e ON e.id = t.enrollment_id WHERE e.campaign_id = ?", cid)
        self.assertEqual((t["channel"], t["body"]), ("li_message", "Follow-up 𝗻𝗼𝘄"))

    def test_attachments_go_with_the_email(self):
        from campaigner import attachments
        with tempfile.TemporaryDirectory() as d, mock.patch.object(attachments, "DIR", Path(d)):
            meta = attachments.save("Deck Q4.pdf", b"%PDF-1.4 test")
            self.conn.execute("UPDATE steps SET attachments = ? WHERE campaign_id = ? AND pos = 1",
                              (json.dumps([meta]), self.cid))
            self.tick(MON)
            self.tick(MON + timedelta(days=1))
            self.assertEqual(self.g.files, ["Deck Q4.pdf"])
            with self.assertRaises(attachments.AttachmentError):
                attachments.save("big.bin", b"x" * (attachments.MAX_BYTES + 1))


class BranchTest(Base):
    BRANCH = [("li_connect", 0, None, "Hi {{ first_name }}"),
              ("li_message", 0, None, "Great to be connected, {{ first_name }}"),
              ("email", 0, "Hi {{ company }}", "Hello {{ first_name }}")]

    def setUp(self):
        super().setUp()
        self.conn.execute("UPDATE campaigns SET status = 'paused' WHERE id = ?", (self.cid,))
        self.cid = self.campaign(self.BRANCH, name="branch")
        self.conn.execute("UPDATE steps SET condition = 'if_not_connected' WHERE campaign_id = ? AND pos = 0", (self.cid,))
        self.conn.execute("UPDATE steps SET condition = 'if_connected' WHERE campaign_id = ? AND pos = 1", (self.cid,))
        for l in rows(self.conn, "SELECT id FROM leads WHERE linkedin_url IS NOT NULL"):
            self.conn.execute("INSERT INTO enrollments (campaign_id, lead_id, enrolled_at) VALUES (?, ?, ?)",
                              (self.cid, l["id"], iso(MON)))

    def test_not_connected_gets_invite_then_email_without_waiting(self):
        self.tick(MON)
        t = self.task("jane@x.com")
        self.assertEqual(t["channel"], "li_connect")
        engine.complete_task(self.conn, t["id"], "done", now=MON)
        self.tick(MON + timedelta(minutes=10))
        self.assertIsNone(self.task("jane@x.com"))          # message step skipped at once (not connected)
        self.assertEqual([s[1] for s in self.g.sent], ["jane@x.com"])  # email went out, no 7-day wait

    def test_already_connected_gets_message_not_invite(self):
        self.conn.execute("UPDATE leads SET li_state = 'connected' WHERE email = 'jane@x.com'")
        self.tick(MON)
        t = self.task("jane@x.com")
        self.assertEqual((t["channel"], t["body"]), ("li_message", "Great to be connected, Jane"))

    def test_discovered_connection_at_invite_switches_to_message(self):
        self.tick(MON)
        engine.complete_task(self.conn, self.task("jane@x.com")["id"], "connected", now=MON)  # "Already connected"
        self.tick(MON + timedelta(minutes=10))
        self.assertEqual(self.task("jane@x.com")["channel"], "li_message")


class MailboxTest(Base):
    EMAIL_ONLY = [("email", 0, "Hi {{ company }}", "Hello {{ first_name }}"), ("email", 2, None, "Bump")]

    def other_campaign(self, sender=OTHER, **kw):
        cid = self.campaign(self.EMAIL_ONLY, name="sales", sender=sender, **kw)
        for l in rows(self.conn, "SELECT id FROM leads"):
            self.conn.execute("INSERT INTO enrollments (campaign_id, lead_id, enrolled_at) VALUES (?, ?, ?)",
                              (cid, l["id"], iso(MON)))
        return cid

    def test_campaign_sends_from_its_mailbox_and_threads_stay_put(self):
        self.conn.execute("UPDATE campaigns SET status = 'paused' WHERE id = ?", (self.cid,))
        cid = self.other_campaign()
        self.tick(MON)
        self.assertEqual({s[1] for s in self.graphs[OTHER].sent}, {"jane@x.com", "bob@x.com"})
        self.assertEqual(self.g.sent, [])
        self.assertEqual(scalar(self.conn, "SELECT COUNT(*) FROM sends WHERE sender = ?", OTHER), 2)
        # Switching the campaign's sender doesn't move existing threads.
        self.conn.execute("UPDATE campaigns SET sender = ? WHERE id = ?", (ME, cid))
        self.tick(MON + timedelta(days=2))
        self.assertEqual([s[0] for s in self.graphs[OTHER].sent[2:]], ["thread", "thread"])
        self.assertEqual(self.g.sent, [])

    def test_reply_checked_in_the_sending_mailbox(self):
        self.conn.execute("UPDATE campaigns SET status = 'paused' WHERE id = ?", (self.cid,))
        cid = self.other_campaign()
        self.tick(MON)
        e = one(self.conn, "SELECT e.* FROM enrollments e JOIN leads l ON l.id = e.lead_id "
                           "WHERE l.email = 'bob@x.com' AND e.campaign_id = ?", cid)
        self.graphs[OTHER].inbox.append(mail("bob@x.com", cid=e["conversation_id"], at=MON + timedelta(hours=1)))
        self.tick(MON + timedelta(hours=2))
        self.assertEqual(one(self.conn, "SELECT status FROM enrollments WHERE id = ?", e["id"])["status"], "replied")

    def test_signed_out_mailbox_holds_only_its_campaign(self):
        del self.graphs[OTHER]
        self.other_campaign()
        self.tick(MON)
        self.tick(MON + timedelta(days=1))
        self.assertEqual(len(self.g.sent), 1)  # main campaign: bob's email (jane waits on her connect task)
        self.assertEqual(scalar(self.conn, "SELECT COUNT(*) FROM sends WHERE sender = ?", OTHER), 0)
        self.assertTrue(any("not signed in" in l for l in self.logs))

    def test_mailbox_daily_cap_spans_campaigns(self):
        self.conn.execute("UPDATE senders SET daily_cap = 1 WHERE account = ?", (OTHER,))
        self.other_campaign()
        self.other_campaign()
        self.tick(MON)
        self.assertEqual(len(self.graphs[OTHER].sent), 1)
        self.tick(MON + timedelta(days=1))
        self.assertEqual(len(self.graphs[OTHER].sent), 2)


class NetworkFailTest(MailboxTest):
    def test_network_error_holds_mailbox_without_failing_leads(self):
        from campaigner.graph import NetworkError
        self.conn.execute("UPDATE campaigns SET status = 'paused' WHERE id = ?", (self.cid,))
        self.other_campaign()
        def boom(*a, **k):
            raise NetworkError("POST /me/messages: network error (ConnectionError)")
        self.graphs[OTHER].send_new = boom
        self.tick(MON)
        self.assertEqual(scalar(self.conn, "SELECT MAX(failures) FROM enrollments"), 0)
        self.assertEqual(scalar(self.conn, "SELECT COUNT(*) FROM enrollments WHERE status = 'error'"), 0)
        self.assertTrue(any("network problem" in l for l in self.logs))


class RemoveSenderTest(MailboxTest):
    def test_remove_moves_campaigns_and_restarts_threads(self):
        from campaigner.runner import remove_sender
        from unittest import mock
        self.conn.execute("UPDATE campaigns SET status = 'paused' WHERE id = ?", (self.cid,))
        cid = self.other_campaign()
        self.tick(MON)
        db.set_setting(self.conn, "li_mailbox", OTHER)
        with mock.patch("campaigner.runner.logout"):
            r = remove_sender(self.conn, OTHER)
        self.assertEqual((r["replacement"], r["campaigns"], r["threads"]), (ME, 1, 2))
        self.assertEqual(one(self.conn, "SELECT sender FROM campaigns WHERE id = ?", cid)["sender"], ME)
        self.assertEqual(db.get_setting(self.conn, "li_mailbox"), ME)
        self.tick(MON + timedelta(days=2))  # follow-ups continue from ME as new emails
        self.assertEqual({(s[0], s[1]) for s in self.g.sent}, {("new", "jane@x.com"), ("new", "bob@x.com")})


class FakeTracker(Tracker):
    def __init__(self, down=False):
        super().__init__("https://links.co.com", "key", "secret")
        self.events, self.down = [], down

    def pull(self, since):
        return [e for e in self.events if e["id"] > since]

    def health(self):
        return "unreachable" if self.down else None

    def hit(self, type, sid, at, ua="Mozilla/5.0", url=None, head=0):
        self.events.append({"id": len(self.events) + 1, "type": type, "sid": sid, "url": url, "ua": ua, "head": head,
                            "ts": int(at.timestamp() * 1000)})


class TrackingTest(Base):
    STEPS = [("email", 0, "Hi {{ company }}", "See https://example.com/case.\n\n[Opt out]({{ unsubscribe_url }})"),
             ("email", 2, None, "Bump")]

    def setUp(self):
        super().setUp()
        self.conn.execute("UPDATE campaigns SET status = 'paused' WHERE id = ?", (self.cid,))
        self.cid = self.campaign(self.STEPS, name="tracked")
        for l in rows(self.conn, "SELECT id FROM leads"):
            self.conn.execute("INSERT INTO enrollments (campaign_id, lead_id, enrolled_at) VALUES (?, ?, ?)",
                              (self.cid, l["id"], iso(MON)))
        self.t = FakeTracker()

    def sid(self, email):
        return one(self.conn, """SELECT s.id FROM sends s JOIN leads l ON l.id = s.lead_id
                                 WHERE l.email = ? AND s.campaign_id = ? ORDER BY s.sent_at LIMIT 1""", email, self.cid)["id"]

    def test_links_pixel_and_unsubscribe_are_tracked(self):
        self.tick(MON, tracker=self.t)
        html = next(s[3] for s in self.g.sent if s[1] == "jane@x.com")
        sid = self.sid("jane@x.com")
        self.assertIn(self.t.open_url(sid), html)
        self.assertIn(f'href="{self.t.click_url(sid, "https://example.com/case")}"', html)
        self.assertIn(">https://example.com/case</a>.", html)  # sentence period stays outside the link
        self.assertIn(f'href="{self.t.unsub_url(sid)}">Opt out</a>', html)  # not wrapped in a click redirect

    def test_events_update_metrics_and_filter_machines(self):
        self.tick(MON, tracker=self.t)
        jane, bob = self.sid("jane@x.com"), self.sid("bob@x.com")
        self.t.hit("open", jane, MON + timedelta(seconds=5))                     # scanner: too soon
        self.t.hit("click", bob, MON + timedelta(hours=1), ua="Mimecast")        # scanner: UA
        self.t.hit("open", jane, MON + timedelta(hours=1))
        self.t.hit("open", jane, MON + timedelta(hours=2))
        self.t.hit("click", bob, MON + timedelta(hours=3), url="https://example.com/case")
        self.tick(MON + timedelta(hours=4), tracker=self.t)
        j, b = (one(self.conn, "SELECT * FROM sends WHERE id = ?", x) for x in (jane, bob))
        self.assertEqual((j["opens"], j["machine_events"], j["clicked_at"]), (2, 1, None))
        self.assertEqual((b["clicks"], b["machine_events"]), (1, 1))
        self.assertIsNotNone(b["opened_at"])  # click implies open
        em = stats.email_stats(self.conn, campaign_id=self.cid)
        self.assertEqual((em["sent"], em["opened"], em["clicked"], em["open_rate"], em["click_rate"]), (2, 2, 1, 100.0, 50.0))
        kinds = [e["type"] for e in rows(self.conn, "SELECT type FROM events WHERE type IN ('open', 'click') ORDER BY id")]
        self.assertEqual(kinds, ["open", "click"])  # first open only, every click

    def test_unsubscribe_stops_everything(self):
        self.tick(MON, tracker=self.t)
        self.t.hit("unsubscribe", self.sid("bob@x.com"), MON + timedelta(hours=1))
        self.tick(MON + timedelta(days=3), tracker=self.t)
        self.assertEqual(one(self.conn, "SELECT dnc FROM leads WHERE email = 'bob@x.com'")["dnc"], 1)
        self.assertEqual({s[1] for s in self.g.sent if s[0] == "thread"}, {"jane@x.com"})
        self.assertEqual(stats.email_stats(self.conn, campaign_id=self.cid)["unsubscribed"], 1)
        # Re-enrolling elsewhere doesn't resurrect them.
        other = self.campaign(self.STEPS, name="again")
        lead = one(self.conn, "SELECT id FROM leads WHERE email = 'bob@x.com'")["id"]
        self.conn.execute("INSERT INTO enrollments (campaign_id, lead_id, enrolled_at) VALUES (?, ?, ?)", (other, lead, iso(MON)))
        self.tick(MON + timedelta(days=3, hours=1), tracker=self.t)
        self.assertEqual(one(self.conn, "SELECT status FROM enrollments WHERE campaign_id = ?", other)["status"], "stopped")

    def test_tracker_down_sends_plain_links(self):
        self.tick(MON, tracker=FakeTracker(down=True))
        html = self.g.sent[0][3]
        self.assertNotIn("links.co.com", html)
        self.assertIn('href="https://example.com/case"', html)
        self.assertIn("mailto:", html)

    def test_tracking_off_per_campaign(self):
        self.conn.execute("UPDATE campaigns SET track_opens = 0, track_clicks = 0 WHERE id = ?", (self.cid,))
        self.tick(MON, tracker=self.t)
        html = self.g.sent[0][3]
        self.assertNotIn("/o/", html)
        self.assertNotIn("/c/", html)
        self.assertIn(self.t.unsub_url(self.sid(self.g.sent[0][1])), html)  # opt-out link still works


class EngageTest(Base):
    def engage(self, email="jane@x.com"):
        return self.task(email, "li_engage")

    def start(self):
        self.tick(MON)
        engine.complete_task(self.conn, self.task("jane@x.com")["id"], "done", now=MON)  # invite sent

    def test_weekly_engage_task_after_invite_or_email(self):
        self.tick(MON)
        self.assertIsNone(self.engage())                 # nothing sent to jane yet
        engine.complete_task(self.conn, self.task("jane@x.com")["id"], "done", now=MON)
        self.tick(MON + timedelta(hours=1))
        t = self.engage()
        self.assertIsNotNone(t)
        self.assertEqual((t["step_pos"], t["body"]), (-1, ""))
        engine.complete_task(self.conn, t["id"], "done", "Sharp point on vendor sprawl.", now=MON + timedelta(hours=2),
                             engage={"post_url": "https://www.linkedin.com/posts/jane-1", "liked": 1, "commented": 1})
        self.tick(MON + timedelta(days=3))
        self.assertIsNone(self.engage())                 # cadence: not again within 7 days
        self.tick(MON + timedelta(days=7, hours=2))
        self.assertIsNotNone(self.engage())
        self.assertIsNone(self.engage("bob@x.com"))      # emailed, but no LinkedIn URL

    def test_engage_does_not_block_or_advance_sequence(self):
        self.start()
        self.tick(MON + timedelta(hours=1))              # engage task opens
        self.tick(MON + timedelta(days=1))               # email step still goes out despite the open engage task
        self.assertIn("jane@x.com", {s[1] for s in self.g.sent})
        before = self.enr("jane@x.com")["step_pos"]
        engine.complete_task(self.conn, self.engage()["id"], "skip", now=MON + timedelta(days=1))
        self.assertEqual(self.enr("jane@x.com")["step_pos"], before)

    def test_repeat_post_rejected_and_nothing_ticked_rejected(self):
        self.start()
        self.tick(MON + timedelta(hours=1))
        url = "https://www.linkedin.com/posts/jane-1"
        engine.complete_task(self.conn, self.engage()["id"], "done", now=MON, engage={"post_url": url, "liked": 1})
        self.tick(MON + timedelta(days=8))
        t = self.engage()
        with self.assertRaises(ValueError):
            engine.complete_task(self.conn, t["id"], "done", now=MON + timedelta(days=8), engage={"post_url": url, "liked": 1})
        with self.assertRaises(ValueError):
            engine.complete_task(self.conn, t["id"], "done", now=MON + timedelta(days=8), engage={})
        self.assertEqual(self.engage()["id"], t["id"])  # still open after rejected attempts

    def test_reply_stops_engagement(self):
        self.start()
        self.tick(MON + timedelta(hours=1))
        self.assertIsNotNone(self.engage())
        self.g.inbox.append(mail("messages-noreply@linkedin.com", "Jane Doe sent you a new message", at=MON + timedelta(hours=2)))
        self.tick(MON + timedelta(hours=3))
        self.assertIsNone(self.engage())
        self.tick(MON + timedelta(days=10))
        self.assertIsNone(self.engage())

    def test_toggle_off_and_dnc(self):
        self.conn.execute("UPDATE campaigns SET li_engage = 0")
        self.start()
        self.tick(MON + timedelta(hours=1))
        self.assertIsNone(self.engage())
        self.conn.execute("UPDATE campaigns SET li_engage = 1")
        self.conn.execute("UPDATE leads SET dnc = 1 WHERE email = 'jane@x.com'")
        self.tick(MON + timedelta(hours=2))
        self.assertIsNone(self.engage())

    def test_one_engage_per_person_across_campaigns(self):
        other = self.campaign(name="other")
        lead = self.enr("jane@x.com")["lead_id"]
        self.conn.execute("INSERT INTO enrollments (campaign_id, lead_id, status, step_pos, enrolled_at, last_step_at) "
                          "VALUES (?, ?, 'active', 0, ?, ?)", (other, lead, iso(MON), iso(MON)))
        self.start()
        self.tick(MON + timedelta(hours=1))
        n = scalar(self.conn, "SELECT COUNT(*) FROM tasks WHERE channel = 'li_engage' AND status = 'open'")
        self.assertEqual(n, 1)


class DraftTest(unittest.TestCase):
    class FakeClient:
        def __init__(self, text="Good point on X.", stop="end_turn"):
            self.calls, outer = [], self
            class Msgs:
                def create(self_inner, **kw):
                    outer.calls.append(kw)
                    block = type("B", (), {"type": "text", "text": text})()
                    return type("R", (), {"stop_reason": stop, "content": [block]})()
            self.beta = type("Beta", (), {"messages": Msgs()})()

    def test_draft(self):
        from campaigner.ai import DraftError, draft_comment
        c = self.FakeClient()
        out = draft_comment("We cut cloud spend 30% by...", {"first_name": "Jane", "company": "Acme"}, "I run IT ops", client=c)
        self.assertEqual(out, "Good point on X.")
        kw = c.calls[0]
        self.assertEqual((kw["model"], kw["fallbacks"]), ("claude-opus-5-5", "default"))
        self.assertIn("<post>\nWe cut cloud spend", kw["messages"][0]["content"])
        with self.assertRaises(DraftError):
            draft_comment("  ", {}, client=c)
        with self.assertRaises(DraftError):
            draft_comment("post", {}, client=self.FakeClient(stop="refusal"))


class FakeDriver:
    def __init__(self, posts=(), connect="sent", message="sent", blocked=False):
        self.calls, self.posts, self.connect_result, self.message_result, self.blocked = [], list(posts), connect, message, blocked

    def _gate(self):
        if self.blocked:
            raise linkedin_bot.BlockedError("LinkedIn showed a security check")

    def open(self): self.calls.append("open")
    def close(self): self.calls.append("close")

    def view(self, url):
        self._gate(); self.calls.append(("view", url))
        return linkedin_bot.Profile(degree="2nd", pending=False)

    def connect(self, url, note):
        self._gate(); self.calls.append(("connect", url, note)); return self.connect_result

    def message(self, url, text):
        self._gate(); self.calls.append(("message", url, text)); return self.message_result

    def recent_posts(self, url):
        self._gate(); self.calls.append(("posts", url)); return self.posts

    def like(self, post): self.calls.append(("like", post.url))
    def comment(self, post, text): self.calls.append(("comment", post.url, text))


class BotTest(Base):
    def setUp(self):
        super().setUp()
        # A private profile lock: the real one may be held by the dashboard's live bot while tests run.
        lock = linkedin_bot._ProfileLock()
        lock.path = Path(self.tmp.name) / "profile.lock"
        patcher = mock.patch.object(linkedin_bot, "PROFILE_LOCK", lock)
        patcher.start()
        self.addCleanup(patcher.stop)
        db.set_setting(self.conn, "li_mode", "bot")
        db.set_setting(self.conn, "li_bot_hours", ["00:00", "23:59"])
        db.set_setting(self.conn, "li_bot_days", "mon,tue,wed,thu,fri,sat,sun")
        self.drivers = []

    def bot(self, driver=None, at=MON, draft=None):
        d = driver or FakeDriver()
        self.drivers.append(d)
        linkedin_bot.run(self.conn, lambda: d, now=at, log=self.logs.append, sleep=lambda s: None, draft=draft)
        return d

    def test_assisted_mode_never_opens_browser(self):
        db.set_setting(self.conn, "li_mode", "assisted")
        self.tick(MON)
        self.assertEqual(self.bot().calls, [])
        self.assertIsNotNone(self.task("jane@x.com"))

    def test_connect_with_note(self):
        self.tick(MON)
        d = self.bot()
        self.assertIn(("connect", "https://www.linkedin.com/in/janedoe", "Hi Jane"), d.calls)
        self.assertIsNone(self.task("jane@x.com"))
        self.assertEqual(one(self.conn, "SELECT li_state FROM leads WHERE email = 'jane@x.com'")["li_state"], "invited")
        self.assertEqual(self.enr("jane@x.com")["step_pos"], 0)
        ev = one(self.conn, "SELECT detail FROM events WHERE type = 'task_done' ORDER BY id DESC LIMIT 1")
        self.assertTrue(ev["detail"].startswith("by bot"))

    def test_already_connected(self):
        self.tick(MON)
        self.bot(FakeDriver(connect="already_connected"))
        self.assertEqual(one(self.conn, "SELECT li_state FROM leads WHERE email = 'jane@x.com'")["li_state"], "connected")
        self.assertEqual(scalar(self.conn, "SELECT COUNT(*) FROM bot_actions WHERE action = 'connect'"), 0)  # no invite spent

    def test_failures_fall_back_to_you(self):
        self.conn.execute("UPDATE steps SET channel = 'li_message', condition = 'always' WHERE pos = 0")
        self.conn.execute("UPDATE leads SET li_state = 'connected'")
        self.tick(MON)
        for _ in range(3):
            self.bot(FakeDriver(message="not_connected"))
        t = self.task("jane@x.com")
        self.assertEqual((t["bot_attempts"], t["bot_note"]), (2, "not a 1st-degree connection yet"))
        self.assertEqual(self.drivers[2].calls, [])  # third run had nothing left to try

    def test_task_you_handled_mid_run_is_not_redone(self):
        self.conn.execute("UPDATE leads SET linkedin_url = 'https://www.linkedin.com/in/bob' WHERE email = 'bob@x.com'")
        self.tick(MON)
        bob = self.task("bob@x.com")
        d = FakeDriver()
        def sleep(_):  # you click Done on Bob's card while the bot pauses between leads
            if self.task("bob@x.com"):
                engine.complete_task(self.conn, bob["id"], "done", now=MON)
        linkedin_bot.run(self.conn, lambda: d, now=MON, log=self.logs.append, sleep=sleep)
        self.assertEqual([c[1] for c in d.calls if c[0] == "connect"], ["https://www.linkedin.com/in/janedoe"])

    def test_task_completed_during_action_does_not_crash(self):
        self.tick(MON)
        jane = self.task("jane@x.com")
        d = FakeDriver(connect="already_connected")
        orig = d.connect
        def connect(url, note):
            engine.complete_task(self.conn, jane["id"], "connected", now=MON)  # you, mid-action
            return orig(url, note)
        d.connect = connect
        linkedin_bot.run(self.conn, lambda: d, now=MON, log=self.logs.append, sleep=lambda s: None)
        self.assertTrue(any("already handled" in l for l in self.logs))
        self.assertIn("close", d.calls)

    def test_block_pauses_until_resumed(self):
        self.tick(MON)
        self.bot(FakeDriver(blocked=True))
        self.assertIn("security check", db.get_setting(self.conn, "li_bot_paused")["reason"])
        self.assertEqual(self.bot().calls, [])
        self.assertIsNotNone(self.task("jane@x.com"))  # still there for you
        linkedin_bot.resume(self.conn)
        self.bot()
        self.assertIsNone(self.task("jane@x.com"))

    def test_caps_and_hours(self):
        self.tick(MON)
        db.set_setting(self.conn, "li_bot_caps", {"connect": 0})
        self.assertEqual(self.bot().calls, [])
        db.set_setting(self.conn, "li_bot_caps", {"connect": 15})
        db.set_setting(self.conn, "li_bot_days", "sat")  # MON is outside
        self.assertEqual(self.bot().calls, [])

    def engage_task(self):
        self.tick(MON)
        self.bot()                                   # invite sent -> eligible for engagement
        self.tick(MON + timedelta(hours=1))
        return self.task("jane@x.com", "li_engage")

    def test_like_then_approved_comment(self):
        t = self.engage_task()
        Post = linkedin_bot.Post
        self.conn.execute("INSERT INTO engagements (lead_id, post_url, liked, at) VALUES (?, 'https://li/p/new', 1, ?)",
                          (self.enr("jane@x.com")["lead_id"], iso(MON)))
        d = FakeDriver(posts=[Post("a", "https://li/p/old", 300, "old", False),
                              Post("b", "https://li/p/mid", 10, "We cut SaaS spend 22%.", False),
                              Post("c", "https://li/p/new", 2, "already done", False)])
        self.bot(d, at=MON + timedelta(hours=2), draft=lambda text, lead: f"Draft about: {text[:12]}")
        self.assertIn(("like", "https://li/p/mid"), d.calls)
        t = one(self.conn, "SELECT * FROM tasks WHERE id = ?", t["id"])
        self.assertEqual((t["status"], t["comment_status"], t["body"]), ("open", "needs_approval", "Draft about: We cut SaaS "))
        self.assertEqual(self.bot(at=MON + timedelta(hours=3)).calls, [])  # waits for you; nothing else due
        self.conn.execute("UPDATE tasks SET body = 'Edited comment', comment_status = 'approved' WHERE id = ?", (t["id"],))
        d2 = self.bot(at=MON + timedelta(hours=4))
        self.assertIn(("comment", "https://li/p/mid", "Edited comment"), d2.calls)
        g = one(self.conn, "SELECT * FROM engagements WHERE task_id = ?", t["id"])
        self.assertEqual((g["post_url"], g["liked"], g["commented"], g["comment"]), ("https://li/p/mid", 1, 1, "Edited comment"))

    def test_no_posts_in_window_and_expiry(self):
        t = self.engage_task()
        self.bot(FakeDriver(posts=[linkedin_bot.Post("a", "https://li/p/old", 400, "old", False)]), at=MON + timedelta(hours=2))
        self.assertEqual(one(self.conn, "SELECT status FROM tasks WHERE id = ?", t["id"])["status"], "skipped")
        # A draft nobody approves expires into a like-only engagement.
        self.tick(MON + timedelta(days=8))
        t2 = self.task("jane@x.com", "li_engage")
        self.bot(FakeDriver(posts=[linkedin_bot.Post("b", "https://li/p/x", 1, "text", True)]), at=MON + timedelta(days=8))
        self.bot(at=MON + timedelta(days=12))
        g = one(self.conn, "SELECT * FROM engagements WHERE task_id = ?", t2["id"])
        self.assertEqual((g["liked"], g["commented"]), (1, 0))

    def messaged(self):
        """Jane (already connected) gets the sequence's LinkedIn message from the bot."""
        self.conn.execute("UPDATE steps SET channel = 'li_message', condition = 'always' WHERE campaign_id = ? AND pos = 0", (self.cid,))
        self.conn.execute("UPDATE leads SET li_state = 'connected' WHERE email = 'jane@x.com'")
        self.tick(MON)
        self.bot()
        self.assertEqual(one(self.conn, "SELECT status FROM tasks WHERE channel = 'li_message'")["status"], "done")

    def reply_driver(self, inbox, thread):
        d = FakeDriver()
        d.inbox = lambda: inbox
        d.thread_reply = lambda url, ours: (d.calls.append(("thread", url)), thread)[1]
        return d

    def test_linkedin_reply_moves_to_leads(self):
        self.messaged()
        d = self.reply_driver([{"name": "Jane (JD)", "snippet": "Jane: yes", "last_from_me": False}], "yes")
        self.bot(d, at=MON + timedelta(minutes=20))
        self.assertEqual(self.enr("jane@x.com")["status"], "replied")
        self.assertEqual(one(self.conn, "SELECT stage FROM leads WHERE email = 'jane@x.com'")["stage"], "lead")
        ev = one(self.conn, "SELECT channel, detail FROM events WHERE type = 'reply'")
        self.assertEqual((ev["channel"], ev["detail"]), ("linkedin", "yes"))

    def test_reply_after_bounce_still_makes_a_lead(self):
        self.messaged()
        self.conn.execute("UPDATE enrollments SET status = 'bounced' WHERE lead_id = (SELECT id FROM leads WHERE email = 'jane@x.com')")
        self.bot(self.reply_driver([], "Sure, call me"), at=MON + timedelta(minutes=20))
        self.assertEqual(one(self.conn, "SELECT stage FROM leads WHERE email = 'jane@x.com'")["stage"], "lead")
        self.assertEqual(self.enr("jane@x.com")["status"], "bounced")  # history kept

    def test_inbox_showing_we_spoke_last_skips_thread(self):
        self.messaged()
        d = self.reply_driver([{"name": "Jane Doe", "snippet": "You: Hi Jane", "last_from_me": True}], "should not be read")
        self.bot(d, at=MON + timedelta(minutes=20))
        self.assertNotIn("thread", [c[0] for c in d.calls if isinstance(c, tuple)])
        self.assertEqual(self.enr("jane@x.com")["status"], "active")

    def test_old_message_from_them_is_not_a_reply_and_check_is_throttled(self):
        self.messaged()
        d = self.reply_driver([], None)            # not in the list -> open the thread; nothing after ours
        self.bot(d, at=MON + timedelta(minutes=20))
        self.assertEqual(self.enr("jane@x.com")["status"], "active")
        d2 = self.reply_driver([], "late reply")
        self.bot(d2, at=MON + timedelta(minutes=25))  # within 10 minutes: no new check
        self.assertEqual(d2.calls, [])
        self.bot(d2, at=MON + timedelta(minutes=31))
        self.assertEqual(self.enr("jane@x.com")["status"], "replied")

    def test_parse_age(self):
        P = linkedin_bot.parse_age_days
        self.assertEqual([P("3h •"), P("2d • Edited"), P("1w"), P("5mo •"), P("1yr"), P("Just now")],
                         [3 / 24, 2, 7, 5 * 30.4, 365, None])


class RenderTest(unittest.TestCase):
    def test_links(self):
        h = email_html("Read [the guide](https://a.com/x?y=1&z=2), or https://b.com.", "", {})
        self.assertIn('<a href="https://a.com/x?y=1&amp;z=2">the guide</a>', h)
        self.assertIn('<a href="https://b.com">https://b.com</a>.', h)
        self.assertIn('&lt;b&gt;', email_html("{{ x }}", "", {"x": "<b>"}))


class RepliesTest(Base):
    def bob_emailed(self):
        self.tick(MON)
        self.tick(MON + timedelta(days=1))
        self.assertEqual(self.g.sent[0][1:3], ("bob@x.com", "Hi Beta"))
        return self.conv("bob@x.com")

    def test_unrelated_mail_from_them_is_not_a_reply(self):
        self.bob_emailed()
        self.g.inbox.append(mail("bob@x.com", "RE: Interview invitation", cid="elsewhere", at=MON + timedelta(days=1, hours=1)))
        self.tick(MON + timedelta(days=1, hours=2))
        self.assertEqual(self.enr("bob@x.com")["status"], "active")
        # ...but a reply or forward of our email counts even when Outlook files it in another thread.
        self.g.inbox.append(mail("bob@x.com", "FW: Hi Beta", cid="forwarded", at=MON + timedelta(days=1, hours=3)))
        self.tick(MON + timedelta(days=1, hours=4))
        self.assertEqual(self.enr("bob@x.com")["status"], "replied")

    def test_out_of_office_keeps_the_sequence_going(self):
        cid = self.bob_emailed()
        self.g.inbox += [
            mail("bob@x.com", "Re: Hi Beta", cid=cid, at=MON + timedelta(days=1, hours=1),
                 preview="Thanks for your email. I am currently out of the office until Monday with limited access."),
            mail("bob@x.com", "Out of Office: Hi Beta", cid=cid, at=MON + timedelta(days=1, hours=2)),
        ]
        self.tick(MON + timedelta(days=1, hours=3))
        self.tick(MON + timedelta(days=1, hours=4))  # rescans don't log them again
        self.assertEqual(self.enr("bob@x.com")["status"], "active")
        self.assertEqual(scalar(self.conn, "SELECT COUNT(*) FROM events WHERE type = 'auto_reply'"), 2)
        for d in (4, 10):
            self.tick(MON + timedelta(days=d))
        self.assertIn(("thread", "bob@x.com"), [x[:2] for x in self.g.sent])  # the follow-up still went out

    def test_not_a_reply_puts_them_back(self):
        cid = self.bob_emailed()
        self.g.inbox.append(mail("bob@x.com", "Re: Hi Beta", cid=cid, at=MON + timedelta(days=1, hours=1)))
        self.tick(MON + timedelta(days=1, hours=2))
        lead = self.enr("bob@x.com")["lead_id"]
        self.assertEqual(self.enr("bob@x.com")["status"], "replied")
        self.assertEqual(engine.undo_reply(self.conn, lead, MON + timedelta(days=1, hours=3)), 1)
        self.assertEqual((self.enr("bob@x.com")["status"], scalar(self.conn, "SELECT stage FROM leads WHERE id = ?", lead)),
                         ("active", "prospect"))
        self.tick(MON + timedelta(days=1, hours=4))  # the same message doesn't count again
        self.assertEqual(self.enr("bob@x.com")["status"], "active")
        for d in (4, 10):
            self.tick(MON + timedelta(days=d))
        self.assertIn(("thread", "bob@x.com"), [x[:2] for x in self.g.sent])
        self.g.inbox.append(mail("bob@x.com", "Re: Hi Beta", cid=cid, at=MON + timedelta(days=11)))
        self.tick(MON + timedelta(days=11, hours=1))  # a newer reply still counts
        self.assertEqual(self.enr("bob@x.com")["status"], "replied")
        with self.assertRaises(ValueError):
            engine.undo_reply(self.conn, self.enr("jane@x.com")["lead_id"])


class ClassifyTest(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(engine.classify(mail("a@x.com"), ME), "reply")
        self.assertEqual(engine.classify(mail(ME), ME), None)
        self.assertEqual(engine.classify(mail("a@x.com", "Hi", headers=[("Auto-Submitted", "auto-replied")]), ME), "auto")
        self.assertEqual(engine.classify(mail("mailer-daemon@x.com", "Hi"), ME), "bounce")
        self.assertEqual(engine.classify(mail("a@x.com", "RE: Automatic reply: Hi"), ME), "auto")
        self.assertEqual(engine.classify(mail("a@x.com", "Réponse automatique : Hi"), ME), "auto")
        self.assertEqual(engine.classify(mail("a@x.com", preview="I’m on annual leave and will reply when I return."), ME), "auto")
        self.assertEqual(engine.classify(mail("a@x.com", headers=[("Precedence", "auto_reply")]), ME), "auto")
        self.assertEqual(engine.classify(mail("a@x.com", preview="Sounds good, Tuesday works. Send the invite."), ME), "reply")
        self.assertEqual(engine.topic("RE: Fwd: AW: Pick an account"), "pick an account")
        self.assertEqual(engine.li_kind(mail("invitations@linkedin.com", "Jane, start a conversation with your new connection")), "connected")
        self.assertEqual(engine.li_kind(mail("notifications-noreply@linkedin.com", "Jane sent you a new message")), "message")
        self.assertEqual(engine.li_kind(mail("invitations@linkedin.com", "Jane wants to connect")), None)


if __name__ == "__main__":
    unittest.main()
