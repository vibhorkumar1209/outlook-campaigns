"""Orchestration logic against a fake Graph. Run: .venv/bin/python -m unittest discover tests"""
import json
import random
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from campaigner import crm, db, engine, importer, linkedin_bot, stats
from campaigner.db import iso, one, parse, rows, scalar
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
        self.drafts, self.outbox = {}, []   # Outlook's Drafts folder by id; emails written by hand in Outlook

    def draft_new(self, to, name, subject, html, attachments=None):
        i = f"draft-{self.me}-{len(self.drafts) + 1}"
        self.drafts[i] = {"state": "draft", "kind": "new", "to": to, "subject": subject, "html": html,
                          "conv": f"conv-{i}", "files": [a["name"] for a in attachments or []]}
        return {"id": i, "conversationId": f"conv-{i}", "webLink": f"https://outlook.example/{i}"}

    def draft_in_thread(self, cid, to, name, html, fallback, strip_pixels=None, attachments=None):
        i = f"draft-{self.me}-{len(self.drafts) + 1}"
        self.drafts[i] = {"state": "draft", "kind": "thread", "to": to, "subject": fallback, "html": html, "conv": cid,
                          "files": [a["name"] for a in attachments or []]}
        return {"id": i, "conversationId": cid, "webLink": f"https://outlook.example/{i}"}

    def draft_state(self, i):
        d = self.drafts.get(i)
        if not d:
            return {"state": "missing"}
        return {"state": d["state"], "subject": d["subject"], "html": d["html"], "sent_at": d.get("sent_at"),
                "conversation_id": d["conv"]}

    def send_draft(self, i):
        d = self.drafts[i]
        d["state"] = "sent"
        self.sent.append((d["kind"], d["to"], d["subject"], d["html"]))

    def delete_draft(self, i):
        self.drafts.pop(i, None)

    def sent_since(self, since):
        return [m for m in self.outbox if datetime.fromisoformat(m["sentDateTime"]) > since]

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

    def last_thread_with(self, email, since):
        return getattr(self, "outside", {}).get(email)  # a thread sent by hand before import

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
        db.set_setting(self.conn, "digest_enabled", False)  # the 8:00 reminder email has its own tests
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
        # Most tests exercise sending itself; drafts-first (the app's default) is covered by DraftTest.
        cols = {"name": name, "status": "active", "timezone": "UTC", "window_days": "mon,tue,wed,thu,fri,sat,sun",
                "window_start": "09:00", "window_end": "17:00", "created_at": iso(MON), "email_mode": "auto", **kw}
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

    def test_keep_awake_only_while_a_campaign_is_active(self):
        from campaigner import runner
        self.assertTrue(runner.wants_awake(self.conn))                     # campaign "c" is active
        db.set_setting(self.conn, "keep_awake", False)
        self.assertFalse(runner.wants_awake(self.conn))
        db.set_setting(self.conn, "keep_awake", True)
        self.conn.execute("UPDATE campaigns SET status = 'paused'")
        self.assertFalse(runner.wants_awake(self.conn))
        from campaigner import osutil
        k = runner.KeepAwake()
        with mock.patch.object(osutil, "WINDOWS", False), mock.patch.object(osutil.subprocess, "Popen") as popen, \
                mock.patch.object(osutil.shutil, "which", return_value="/usr/bin/caffeinate"):
            popen.return_value.poll.return_value = None
            k.update(True)
            k.update(True)                                                  # already holding: no second process
            self.assertEqual(popen.call_count, 1)
            self.assertEqual(popen.call_args[0][0][:2], ["caffeinate", "-i"])
            k.update(False)
            popen.return_value.terminate.assert_called_once()

    def test_keep_awake_windows_holds_a_thread(self):
        from campaigner import osutil
        calls = []
        with mock.patch.object(osutil, "WINDOWS", True), \
                mock.patch.object(osutil, "_hold_awake", lambda stop: (calls.append("on"), stop.wait(), calls.append("off"))):
            k = osutil.KeepAwake()
            k.update(True)
            k.update(True)                                                  # already holding: no second thread
            self.assertTrue(k.on)
            k.update(False)
            self.assertFalse(k.on)
        self.assertEqual(calls, ["on", "off"])


class PlatformTest(unittest.TestCase):
    def test_lock_is_exclusive_across_handles(self):
        from campaigner import osutil
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.lock"
            a, b = osutil.open_lock(path), osutil.open_lock(path)
            try:
                self.assertTrue(osutil.try_lock(a))
                self.assertFalse(osutil.try_lock(b))                        # held elsewhere
                osutil.unlock(a)
                self.assertTrue(osutil.try_lock(b))
                osutil.unlock(b)
            finally:
                a.close(), b.close()

    def test_tick_lock_released_after_use(self):
        from campaigner import runner
        with tempfile.TemporaryDirectory() as d, mock.patch.object(runner, "LOCK_PATH", Path(d) / ".tick.lock"):
            with runner.tick_lock() as ok:
                self.assertTrue(ok)
            with runner.tick_lock() as ok:
                self.assertTrue(ok)

    def test_update_status_reports_missing_git(self):
        from campaigner import updater
        with mock.patch.object(updater.subprocess, "run", side_effect=FileNotFoundError):
            st = updater.status()
        self.assertFalse(st["ok"])
        self.assertIn("Git", st["error"])

    def test_update_status_counts_new_commits(self):
        from campaigner import updater
        out = {"fetch": "", "rev-parse": "main", "log": "abc · Nov 11, 2026", "status": ""}

        def run(cmd, **kw):
            sub = cmd[3]
            text = "fix a\nfeat b" if sub == "log" and "HEAD..@{u}" in cmd else out.get(sub, "")
            return mock.Mock(returncode=0, stdout=text, stderr="")
        with mock.patch.object(updater.subprocess, "run", side_effect=run):
            st = updater.status()
        self.assertEqual((st["ok"], st["behind"], st["changes"], st["dirty"]), (True, 2, ["fix a", "feat b"], False))


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



class StartBranchTest(Base):
    """Connected at the start: email + LinkedIn at once. Needed an invite: email on day 2, message on day 4 if accepted."""
    SEQ = [("li_connect", 0, None, "Hi {{ first_name }}", "if_not_connected"),
           ("email", 0, "Intro", "Hello {{ first_name }}", "if_was_connected"),
           ("li_message", 0, None, "Curious, {{ first_name }}", "if_was_connected"),
           ("email", 2, "Intro", "Hello {{ first_name }}", "if_was_invited"),
           ("li_message", 2, None, "Curious, {{ first_name }}", "if_was_invited"),
           ("email", 2, "Value", "Value {{ first_name }}", "always")]

    def setUp(self):
        super().setUp()
        self.conn.execute("UPDATE campaigns SET status = 'paused' WHERE id = ?", (self.cid,))
        self.cid = self.campaign([s[:4] for s in self.SEQ], name="start")
        for i, s in enumerate(self.SEQ):
            self.conn.execute("UPDATE steps SET condition = ?, in_thread = 0 WHERE campaign_id = ? AND pos = ?", (s[4], self.cid, i))
        lead = one(self.conn, "SELECT id FROM leads WHERE email = 'jane@x.com'")["id"]
        self.conn.execute("INSERT INTO enrollments (campaign_id, lead_id, enrolled_at) VALUES (?, ?, ?)", (self.cid, lead, iso(MON)))

    def subjects(self):
        return [x[2] for x in self.g.sent if x[1] == "jane@x.com"]

    def test_already_connected_gets_email_and_message_at_once(self):
        self.conn.execute("UPDATE leads SET li_state = 'connected' WHERE email = 'jane@x.com'")
        self.tick(MON)
        self.assertEqual(self.subjects(), ["Intro"])
        self.tick(MON + timedelta(minutes=1))                    # next 1-minute pass queues the LinkedIn message
        t = self.task("jane@x.com")
        self.assertEqual((t["channel"], t["body"]), ("li_message", "Curious, Jane"))
        engine.complete_task(self.conn, t["id"], "done", now=MON + timedelta(minutes=5))
        for d in (2, 4, 6):
            self.tick(MON + timedelta(days=d, minutes=10))
        self.assertEqual(self.subjects(), ["Intro", "Value"])   # invite-branch steps skipped, no repeats
        self.assertIsNone(self.task("jane@x.com"))
        self.assertEqual(self.enr("jane@x.com")["was_connected"], 1)

    def test_found_connected_at_invite_counts_as_connected(self):
        self.tick(MON)
        engine.complete_task(self.conn, self.task("jane@x.com")["id"], "connected", now=MON)  # bot: already connected
        self.tick(MON + timedelta(minutes=5))
        self.assertEqual(self.subjects(), ["Intro"])

    def test_invited_lead_who_accepts_stays_on_invite_branch(self):
        self.tick(MON)
        engine.complete_task(self.conn, self.task("jane@x.com")["id"], "done", now=MON)  # invite sent
        self.tick(MON + timedelta(minutes=5))
        self.assertEqual(self.subjects(), [])                   # nothing right away
        engine.set_connected(self.conn, self.enr("jane@x.com")["lead_id"], MON + timedelta(days=1))  # accepts on day 1
        self.tick(MON + timedelta(days=2, minutes=10))
        self.assertEqual(self.subjects(), ["Intro"])             # day 2 email, not skipped because they connected
        self.tick(MON + timedelta(days=4, minutes=20))
        self.assertEqual(self.task("jane@x.com")["channel"], "li_message")
        self.assertEqual(self.enr("jane@x.com")["was_connected"], 0)

    def test_invited_lead_who_never_accepts_skips_message_without_waiting(self):
        self.tick(MON)
        engine.complete_task(self.conn, self.task("jane@x.com")["id"], "done", now=MON)
        self.tick(MON + timedelta(minutes=1))
        for d in (2, 4, 6):
            self.tick(MON + timedelta(days=d, minutes=10))
        self.assertEqual(self.subjects(), ["Intro", "Value"])


class OnAcceptTest(Base):
    SEQ = [("li_connect", 0, None, "Hi {{ first_name }}", "if_not_connected"),
           ("email", 0, "Intro", "Hello {{ first_name }}", "always"),
           ("li_message", 0, None, "Thanks for connecting, {{ first_name }}", "on_accept"),
           ("email", 4, "Value", "Value {{ first_name }}", "always")]

    def setUp(self):
        super().setUp()
        self.conn.execute("UPDATE campaigns SET status = 'paused' WHERE id = ?", (self.cid,))
        self.cid = self.campaign([s[:4] for s in self.SEQ], name="accept")
        for i, s in enumerate(self.SEQ):
            self.conn.execute("UPDATE steps SET condition = ?, in_thread = 0 WHERE campaign_id = ? AND pos = ?", (s[4], self.cid, i))
        lead = one(self.conn, "SELECT id FROM leads WHERE email = 'jane@x.com'")["id"]
        self.conn.execute("INSERT INTO enrollments (campaign_id, lead_id, enrolled_at) VALUES (?, ?, ?)", (self.cid, lead, iso(MON)))
        self.lead = lead

    def subjects(self):
        return [x[2] for x in self.g.sent if x[1] == "jane@x.com"]

    def test_message_goes_out_when_they_accept_and_the_sequence_keeps_its_timing(self):
        self.tick(MON)
        engine.complete_task(self.conn, self.task("jane@x.com")["id"], "done", now=MON)   # invite sent
        self.tick(MON + timedelta(minutes=1))
        self.assertEqual(self.subjects(), ["Intro"])                                       # email right after the invite
        self.tick(MON + timedelta(days=1))
        self.assertIsNone(self.task("jane@x.com"))                                         # not accepted yet: nothing
        engine.set_connected(self.conn, self.lead, MON + timedelta(days=1, hours=1))
        self.tick(MON + timedelta(days=1, hours=1, minutes=1))
        t = self.task("jane@x.com")
        self.assertEqual((t["channel"], t["body"]), ("li_message", "Thanks for connecting, Jane"))
        e = self.enr("jane@x.com")
        engine.complete_task(self.conn, t["id"], "done", now=MON + timedelta(days=1, hours=2))
        self.assertEqual(self.enr("jane@x.com")["step_pos"], e["step_pos"])               # didn't move the sequence
        for d in (2, 3):
            self.tick(MON + timedelta(days=d))
        self.assertEqual(self.subjects(), ["Intro"])
        self.tick(MON + timedelta(days=4, minutes=2))                                      # 4 days after Intro, as set
        self.assertEqual(self.subjects(), ["Intro", "Value"])
        self.assertEqual(scalar(self.conn, "SELECT COUNT(*) FROM tasks WHERE channel = 'li_message'"), 1)  # once only

    def test_full_daily_linkedin_limit_doesnt_hold_back_the_accept_message(self):
        self.tick(MON)
        engine.complete_task(self.conn, self.task("jane@x.com")["id"], "done", now=MON)
        self.conn.execute("UPDATE campaigns SET li_daily_cap = 1 WHERE id = ?", (self.cid,))  # used by the invite
        engine.set_connected(self.conn, self.lead, MON + timedelta(minutes=5))
        self.tick(MON + timedelta(minutes=6))
        self.assertEqual(self.task("jane@x.com")["channel"], "li_message")

    def test_open_on_accept_message_doesnt_hold_up_email(self):
        self.tick(MON)
        engine.complete_task(self.conn, self.task("jane@x.com")["id"], "done", now=MON)
        engine.set_connected(self.conn, self.lead, MON)                                    # accepts at once
        self.tick(MON + timedelta(minutes=1))
        self.assertEqual(self.task("jane@x.com")["channel"], "li_message")                 # still open
        self.assertEqual(self.subjects(), ["Intro"])                                       # email went anyway

    def test_already_connected_lead_doesnt_get_it(self):
        self.conn.execute("UPDATE leads SET li_state = 'connected' WHERE id = ?", (self.lead,))
        for m in (0, 1, 2):
            self.tick(MON + timedelta(minutes=m))
        self.assertEqual(self.subjects(), ["Intro"])
        self.assertIsNone(self.task("jane@x.com"))


class HistoryImportTest(Base):
    """Leads already worked outside the app (tracker sheet) start where they are, with nothing repeated."""
    SEQ = [("li_connect", 0, None, "Hi {{ first_name }}", "if_not_connected"),
           ("email", 0, "Intro", "Hello {{ first_name }}", "if_was_connected"),
           ("email", 0, "Intro", "Hello {{ first_name }}", "if_was_invited"),
           ("li_message", 0, None, "Good to connect, {{ first_name }}", "on_accept"),
           ("email", 4, "Value", "Value {{ first_name }}", "always"),
           ("li_message", 3, None, "Demo, {{ first_name }}?", "if_connected"),
           ("email", 3, "Spend", "Spend {{ first_name }}", "always")]
    COLS = ["Name", "First Name", "Last Name", "Email", "LinkedIn", "Department", "Phone", "Lead Status", "LI Status",
            "LI 1st Msg", "LI 2nd Msg", "EM1", "EM2"]
    OCT9 = datetime(2026, 10, 9, 16, tzinfo=timezone.utc)

    def setUp(self):
        super().setUp()
        self.conn.execute("UPDATE campaigns SET status = 'paused' WHERE id = ?", (self.cid,))
        self.cid = self.campaign([x[:4] for x in self.SEQ], name="hist")
        for i, x in enumerate(self.SEQ):
            self.conn.execute("UPDATE steps SET condition = ?, in_thread = 0 WHERE campaign_id = ? AND pos = ?", (x[4], self.cid, i))

    def load(self, *people):
        rows_ = [dict(zip(self.COLS, p)) for p in people]
        return importer.import_rows(self.conn, rows_, importer.guess_mapping(self.COLS), self.cid)

    def en(self, email):
        return one(self.conn, "SELECT e.*, l.li_state, l.stage, l.email_bounced, l.last_name, l.company FROM enrollments e "
                              "JOIN leads l ON l.id = e.lead_id WHERE l.email = ? AND e.campaign_id = ?", email, self.cid)

    def test_mapping_and_cleanup(self):
        m = importer.guess_mapping(self.COLS)
        self.assertEqual((m["li_status"], m["li_msg_1"], m["li_msg_2"], m["email_1"], m["email_2"], m["lead_status"]),
                         ("LI Status", "LI 1st Msg", "LI 2nd Msg", "EM1", "EM2", "Lead Status"))
        self.assertEqual([importer.clean_last_name(l, f) for l, f in [("KK)", "Krishna Kumaraswamy (KK)"),
                          ("Nats]", "Natarajan Venkataraman [Nats]"), ("PE", "Matt Gautreaux, PE"), ("Gupta", "Somil Gupta")]],
                         ["Kumaraswamy", "Venkataraman", "Gautreaux", "Gupta"])

    def test_invite_pending_after_email_1(self):
        self.load(("Ann Lee", "Ann ", "Lee", "ann@x.com", "linkedin.com/in/annlee", "Sales", "1555", "", "Request Sent", "-", "", "09-Oct-26", ""))
        e = self.en("ann@x.com")
        self.assertEqual((e["li_state"], e["was_connected"], e["step_pos"], e["status"]), ("invited", 0, 2, "active"))  # after Email 1
        self.assertEqual(e["last_step_at"], iso(self.OCT9))
        self.tick(MON + timedelta(days=30))                       # next is Email 2 ("Value"), overdue: goes now
        self.assertEqual([x[2] for x in self.g.sent if x[1] == "ann@x.com"], ["Value"])
        self.assertIsNone(self.task("ann@x.com"))                 # no second invite, no message (not accepted)

    def test_accepted_without_message_gets_the_accept_message(self):
        self.load(("Bo Tan", "Bo", "Tan", "bo@x.com", "linkedin.com/in/botan", "", "", "", "Request Accepted", "-", "", "09-Oct-26", ""))
        self.tick(MON + timedelta(days=30))
        t = self.task("bo@x.com")
        self.assertEqual((t["channel"], t["body"]), ("li_message", "Good to connect, Bo"))

    def test_second_message_already_sent_is_skipped_later(self):
        self.load(("Cy Wu", "Cy", "Wu", "cy@x.com", "linkedin.com/in/cywu", "", "", "", "Request Accepted", "29-Sep-26", "09-Oct-26", "09-Oct-26", ""))
        now = MON + timedelta(days=30)
        self.tick(now)                                             # Email 2 now; the on-accept message was done
        self.assertIsNone(self.task("cy@x.com"))
        for d in (1, 3, 4, 7):
            self.tick(now + timedelta(days=d, minutes=1))
        self.assertIsNone(self.task("cy@x.com"))                   # "Demo" message counted as LI 2nd Msg: not resent
        self.assertEqual([x[2] for x in self.g.sent if x[1] == "cy@x.com"], ["Value", "Spend"])

    def test_undelivered_ooo_and_lead_status(self):
        s = self.load(("Di Ko", "Di", "Ko", "di@x.com", "linkedin.com/in/diko", "", "", "", "Request Accepted", "09-Oct-26", "", "Undelivered", ""),
                      ("Ed Po", "Ed", "Po", "ed@x.com", "linkedin.com/in/edpo", "", "", "", "Request Sent", "-", "", "12-Oct-26", "OOO"),
                      ("Fa Ru", "Fa", "Ru", "fa@x.com", "linkedin.com/in/faru", "", "", "Demo Scheduled", "Request Sent", "-", "", "09-Oct-26", ""))
        self.assertEqual(s["with_history"], 3)
        self.assertEqual(self.en("di@x.com")["email_bounced"], 1)  # LinkedIn goes on; emails skip
        self.assertEqual(scalar(self.conn, "SELECT COUNT(*) FROM events WHERE type = 'auto_reply'"), 1)
        fa = self.en("fa@x.com")
        self.assertEqual((fa["stage"], fa["status"]), ("lead", "stopped"))
        cols, rows_ = stats.campaign_status(self.conn, self.cid, timezone.utc)
        cells = {r["cells"]["Email"]: r["cells"] for r in rows_}
        self.assertEqual((cells["di@x.com"]["LI Status"], cells["di@x.com"]["LI 1st Msg"], cells["di@x.com"]["EM1"]),
                         ("Request Accepted", "09-Oct-26", "Undelivered"))
        self.assertEqual((cells["ed@x.com"]["LI 1st Msg"], cells["ed@x.com"]["EM1"]), ("-", "12-Oct-26 (OOO)"))
        self.assertEqual((cells["fa@x.com"]["Status Note"], cells["fa@x.com"]["Lead Status"]), ("Demo Scheduled", "New"))
        self.assertEqual(cols[-11:], ["WhatsApp", "VM1", "VM2", "VM3", "Calls", "Last Call", "Last Contacted",
                                      "Next Follow-up", "Next Step", "Last Touch", "Touches"])
        all_cols, all_rows = stats.campaign_status(self.conn, None, timezone.utc)   # every campaign
        self.assertEqual(all_cols[:3], ["#", "Campaign", "Name"])
        self.assertEqual({r["cells"]["Campaign"] for r in all_rows}, {"c", "hist"})

    def test_follow_up_replies_on_top_of_the_email_sent_by_hand(self):
        self.load(("Ann Lee", "Ann", "Lee", "ann@x.com", "linkedin.com/in/annlee", "", "", "", "Request Sent", "-", "", "09-Oct-26", ""))
        self.conn.execute("UPDATE steps SET in_thread = 1 WHERE campaign_id = ? AND pos > 2 AND channel = 'email'", (self.cid,))
        self.g.outside = {"ann@x.com": "conv-by-hand"}
        self.tick(MON + timedelta(days=30))
        self.assertEqual([x[:2] for x in self.g.sent], [("thread", "ann@x.com")])   # on top of the hand-sent Email 1
        self.assertEqual(self.en("ann@x.com")["conversation_id"], "conv-by-hand")

    def test_follow_up_without_an_earlier_thread_starts_one(self):
        self.load(("Bo Tan", "Bo", "Tan", "bo@x.com", "linkedin.com/in/botan", "", "", "", "Request Sent", "-", "", "09-Oct-26", ""))
        self.conn.execute("UPDATE steps SET in_thread = 1 WHERE campaign_id = ? AND pos > 2 AND channel = 'email'", (self.cid,))
        self.tick(MON + timedelta(days=30))
        self.assertEqual([x[:3] for x in self.g.sent], [("new", "bo@x.com", "Value")])

    def test_resume_after_bounce_still_resends(self):
        self.assertFalse(engine._done_before(self.conn, {"id": 999}, 2))

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


class Steady(random.Random):
    """Predictable pacing for bot tests: keeps task order, never wanders, takes no breaks."""
    def shuffle(self, x): pass
    def random(self): return 0.99
    def uniform(self, a, b): return a
    def randint(self, a, b): return 10 ** 6


class RhythmTest(Base):
    def setUp(self):
        super().setUp()
        db.set_setting(self.conn, "li_bot_hours", ["09:00", "17:00"])
        self.cfg = linkedin_bot.settings(self.conn)

    def plans(self, n=60):
        return [linkedin_bot.day_plan(self.conn, self.cfg, MON + timedelta(days=d)) for d in range(n)]

    def test_each_day_has_its_own_plan_inside_the_settings(self):
        plans = self.plans()
        caps = self.cfg["li_bot_caps"]
        for p in plans:
            self.assertGreaterEqual(p["start"], 9 * 60)
            self.assertLessEqual(p["end"], 17 * 60)
            for a, n in p["caps"].items():
                self.assertLessEqual(n, caps[a])
            if p["lunch"]:
                self.assertTrue(p["start"] < p["lunch"][0] < p["lunch"][1] < p["end"])
        self.assertGreater(len({p["hours"] for p in plans}), 30)
        self.assertGreater(len({p["caps"]["connect"] for p in plans}), 3)
        self.assertTrue(any(p["light"] for p in plans))
        self.assertTrue(any(p["lunch"] for p in plans))
        # stable within a day
        again = linkedin_bot.day_plan(self.conn, self.cfg, MON + timedelta(hours=3))
        self.assertEqual((again["hours"], again["caps"]), (plans[0]["hours"], plans[0]["caps"]))

    def test_new_account_ramps_up(self):
        self.assertEqual(self.plans(1)[0]["ramp"], 0.5)                # no history yet
        self.conn.execute("INSERT INTO bot_actions (action, task_id, lead_id, ok, at) VALUES ('view', 0, 0, 1, ?)",
                          (iso(MON - timedelta(days=10)),))
        self.assertEqual(self.plans(1)[0]["ramp"], 0.75)
        self.conn.execute("UPDATE bot_actions SET at = ?", (iso(MON - timedelta(days=30)),))
        self.assertEqual(self.plans(1)[0]["ramp"], 1.0)

    def test_fixed_rhythm_when_turned_off(self):
        db.set_setting(self.conn, "li_bot_vary", False)
        p = linkedin_bot.day_plan(self.conn, linkedin_bot.settings(self.conn), MON)
        self.assertEqual((p["hours"], p["lunch"], p["caps"]), ("09:00–17:00", None, self.cfg["li_bot_caps"]))

    def test_lunch_and_rest_between_sessions(self):
        lock = linkedin_bot._ProfileLock()
        lock.path = Path(self.tmp.name) / "profile.lock"
        patcher = mock.patch.object(linkedin_bot, "PROFILE_LOCK", lock)
        patcher.start()
        self.addCleanup(patcher.stop)
        db.set_setting(self.conn, "li_mode", "bot")
        db.set_setting(self.conn, "li_bot_hours", ["00:00", "23:59"])
        db.set_setting(self.conn, "li_bot_days", "mon,tue,wed,thu,fri,sat,sun")
        cfg = linkedin_bot.settings(self.conn)
        plan = linkedin_bot.day_plan(self.conn, cfg, MON)
        local = MON.astimezone(datetime.now().astimezone().tzinfo)
        day0 = local.replace(hour=0, minute=0, second=0, microsecond=0)
        if plan["lunch"]:
            self.assertFalse(linkedin_bot.in_plan(cfg, plan, day0 + timedelta(minutes=plan["lunch"][0] + 5)))
        self.assertTrue(linkedin_bot.in_plan(cfg, plan, day0 + timedelta(minutes=plan["start"] + 5)))
        at = next(MON + timedelta(minutes=m) for m in range(0, 600, 5)
                  if linkedin_bot.in_plan(cfg, plan, MON + timedelta(minutes=m)))
        self.tick(at)
        run = lambda when: linkedin_bot.run(self.conn, lambda: d, now=when, log=self.logs.append, sleep=lambda s: None,
                                            rng=Steady())
        d = FakeDriver()
        run(at)
        self.assertIn("open", d.calls)
        until = parse(db.get_setting(self.conn, "li_bot_rest_until"))
        self.assertTrue(at + timedelta(minutes=8) <= until <= at + timedelta(minutes=36))
        self.conn.execute("UPDATE leads SET linkedin_url = 'https://www.linkedin.com/in/bob' WHERE email = 'bob@x.com'")
        self.tick(at + timedelta(minutes=1))
        d = FakeDriver()
        run(at + timedelta(minutes=2))
        self.assertEqual(d.calls, [])                                    # resting
        d = FakeDriver()
        run(until + timedelta(minutes=1))
        self.assertIn("open", d.calls)


class WithdrawTest(Base):
    class Sent(object):
        pass

    def setUp(self):
        super().setUp()
        lock = linkedin_bot._ProfileLock()
        lock.path = Path(self.tmp.name) / "profile.lock"
        patcher = mock.patch.object(linkedin_bot, "PROFILE_LOCK", lock)
        patcher.start()
        self.addCleanup(patcher.stop)
        for k, v in [("li_mode", "bot"), ("li_bot_hours", ["00:00", "23:59"]), ("li_bot_vary", False),
                     ("li_bot_days", "mon,tue,wed,thu,fri,sat,sun"), ("li_bot_caps", {a: 0 for a in linkedin_bot.CEILINGS} | {"withdraw": 3})]:
            db.set_setting(self.conn, k, v)
        self.conn.execute("UPDATE leads SET li_state = 'invited' WHERE email = 'jane@x.com'")

    def driver(self, sent, total=512):
        class D(FakeDriver):
            def sent_invites(self, min_days, want):
                self.calls.append(("sent_invites", min_days, want))
                return [x for x in sent if linkedin_bot.sent_age_days(x["age"]) >= min_days], total
            def withdraw(self, inv):
                self.calls.append(("withdraw", inv["slug"]))
                if inv["slug"] == "broken":
                    raise linkedin_bot.ActionError("still listed after Withdraw")
        return D()

    def run_bot(self, d, at=MON):
        linkedin_bot.run(self.conn, lambda: d, now=at, log=self.logs.append, sleep=lambda s: None, rng=Steady())
        return d

    def test_sent_age(self):
        f = linkedin_bot.sent_age_days
        self.assertEqual([f("Sent 3 weeks ago"), f("Sent 1 month ago"), f("Sent a week ago"), f("Sent yesterday"),
                          f("Sent today"), f("Sent 5 hours ago"), f("whatever")],
                         [21, 30, 7, 1.0, 0.0, 5 * (1 / 24), None])

    def test_withdraws_old_invites_and_frees_the_lead(self):
        sent = [{"slug": "janedoe", "name": "Jane Doe", "age": "Sent 1 month ago"},
                {"slug": "stranger", "name": "Not In App", "age": "Sent 3 weeks ago"},
                {"slug": "fresh", "name": "New One", "age": "Sent 2 weeks ago"}]
        d = self.run_bot(self.driver(sent))
        self.assertIn(("sent_invites", 21, 3), d.calls)
        self.assertEqual([c[1] for c in d.calls if c[0] == "withdraw"], ["janedoe", "stranger"])
        jane = one(self.conn, "SELECT * FROM leads WHERE email = 'jane@x.com'")
        self.assertEqual(jane["li_state"], "none")
        self.assertIsNotNone(one(self.conn, "SELECT * FROM events WHERE lead_id = ? AND type = 'li_withdrawn'", jane["id"]))
        self.assertEqual(scalar(self.conn, "SELECT COUNT(*) FROM bot_actions WHERE action = 'withdraw' AND ok = 1"), 2)
        self.assertEqual(db.get_setting(self.conn, "li_pending_total")["n"], 512)
        # LinkedIn's count (512) beats the app's own: new invites stop
        plan = linkedin_bot.day_plan(self.conn, linkedin_bot.settings(self.conn), MON)
        self.assertEqual(plan["caps"]["connect"], 0)
        comms = crm.communications(self.conn, lead_id=jane["id"])
        self.assertTrue(any(c["label"] == "LinkedIn invite withdrawn" for c in comms))
        # daily cap: 3 a day
        sent2 = [{"slug": f"p{i}", "name": f"P{i}", "age": "Sent 2 months ago"} for i in range(5)]
        d = self.run_bot(self.driver(sent2), at=MON + timedelta(minutes=30))
        self.assertEqual(len([c for c in d.calls if c[0] == "withdraw"]), 1)

    def test_nothing_old_waits_before_looking_again(self):
        d = self.run_bot(self.driver([{"slug": "fresh", "name": "New", "age": "Sent 2 days ago"}], total=30))
        self.assertIn("open", d.calls)
        looked = lambda d: any(c[0] == "sent_invites" for c in d.calls if isinstance(c, tuple))  # noqa: E731
        self.assertFalse(looked(self.run_bot(self.driver([]), at=MON + timedelta(hours=1))))
        self.assertTrue(looked(self.run_bot(self.driver([]), at=MON + timedelta(hours=13))))

    def test_failure_stops_withdrawing_for_the_session(self):
        sent = [{"slug": "broken", "name": "B", "age": "Sent 1 month ago"},
                {"slug": "stranger", "name": "S", "age": "Sent 1 month ago"}]
        d = self.run_bot(self.driver(sent))
        self.assertEqual([c[1] for c in d.calls if c[0] == "withdraw"], ["broken"])
        self.assertEqual(scalar(self.conn, "SELECT COUNT(*) FROM bot_actions WHERE action = 'withdraw' AND ok = 0"), 1)
        self.assertTrue(any("couldn't withdraw" in l for l in self.logs))


class FakeDriver:
    def __init__(self, posts=(), connect="sent", message="sent", blocked=False, connections=()):
        self.calls, self.posts, self.connect_result, self.message_result, self.blocked = [], list(posts), connect, message, blocked
        self.connections = list(connections)

    def recent_connections(self, limit=40):
        self._gate(); self.calls.append(("connections", limit)); return self.connections

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
        db.set_setting(self.conn, "li_bot_vary", False)  # RhythmTest covers the daily variation
        db.set_setting(self.conn, "li_bot_withdraw_weeks", 0)  # WithdrawTest covers withdrawals
        self.drivers = []

    def bot(self, driver=None, at=MON, draft=None):
        d = driver or FakeDriver()
        self.drivers.append(d)
        linkedin_bot.run(self.conn, lambda: d, now=at, log=self.logs.append, sleep=lambda s: None, draft=draft,
                        rng=Steady())
        return d

    def test_assisted_mode_never_opens_browser(self):
        db.set_setting(self.conn, "li_mode", "assisted")
        self.tick(MON)
        self.assertEqual(self.bot().calls, [])
        self.assertIsNotNone(self.task("jane@x.com"))

    def test_page_error_uses_an_attempt_and_the_queue_moves_on(self):
        importer.import_rows(self.conn, [{"Email": "kim@x.com", "Name": "Kim Lee", "LinkedIn": "linkedin.com/in/kimlee"}],
                             importer.guess_mapping(["Email", "Name", "LinkedIn"]), self.cid)
        self.conn.execute("UPDATE enrollments SET enrolled_at = ?", (iso(MON),))
        self.tick(MON)

        class Flaky(FakeDriver):
            def connect(self, url, note):
                if "janedoe" in url:
                    raise TimeoutError("Locator.click: Timeout 15000ms exceeded.\n<svg> intercepts pointer events")
                return super().connect(url, note)

        d = self.bot(Flaky())
        self.assertIn(("connect", "https://www.linkedin.com/in/kimlee", "Hi Kim"), d.calls)  # not stuck behind Jane
        jane = self.task("jane@x.com")
        self.assertEqual(jane["bot_attempts"], 1)
        self.assertTrue(jane["bot_note"].startswith("TimeoutError: Locator.click: Timeout 15000ms"))
        self.bot(Flaky(), at=MON + timedelta(minutes=5))
        self.assertEqual(self.task("jane@x.com")["bot_attempts"], 2)       # limit reached: left for you
        self.assertNotIn("open", self.bot(Flaky(), at=MON + timedelta(minutes=10)).calls)

    def test_bot_sees_accepted_invites(self):
        self.tick(MON)
        self.bot()                                                          # invites Jane
        self.assertEqual(one(self.conn, "SELECT li_state FROM leads WHERE email = 'jane@x.com'")["li_state"], "invited")
        d = self.bot(FakeDriver(connections=["someone-else"]), at=MON + timedelta(minutes=20))
        self.assertIn(("connections", 300), d.calls)                     # first look of the day goes deep
        d = self.bot(FakeDriver(connections=["someone-else", "janedoe"]), at=MON + timedelta(minutes=40))
        self.assertIn(("connections", 40), d.calls)                      # then the newest 40
        self.assertEqual(one(self.conn, "SELECT li_state FROM leads WHERE email = 'jane@x.com'")["li_state"], "connected")
        d = self.bot(FakeDriver(), at=MON + timedelta(minutes=60))      # nobody invited left: no scan
        self.assertFalse([c for c in d.calls if c[0] == "connections"])

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

    def test_human_order_hops_between_people_and_puts_messages_first(self):
        t = lambda lead, i: {"lead_id": lead, "id": i}
        todo = [(t(1, 1), "view"), (t(1, 2), "like"), (t(2, 3), "connect"), (t(3, 4), "message"), (t(2, 5), "view")]
        for seed in range(30):
            out = linkedin_bot.human_order(list(todo), random.Random(seed))
            self.assertEqual(out[0][1], "message")
            self.assertEqual(sorted(x[0]["id"] for x in out), [1, 2, 3, 4, 5])
            leads = [x[0]["lead_id"] for x in out]
            self.assertFalse(any(a == b for a, b in zip(leads, leads[1:])), leads)
        orders = {tuple(x[0]["id"] for x in linkedin_bot.human_order(list(todo), random.Random(s))) for s in range(30)}
        self.assertGreater(len(orders), 1)  # actually shuffled

    def test_pacing_wanders_and_takes_breaks(self):
        class Wanders(FakeDriver):
            def wander(self): self.calls.append("wander")
        class Rng(random.Random):
            def random(self): return 0.0          # always wander
            def randint(self, a, b): return a     # break after 3 actions
            def uniform(self, a, b): return b
        sleeps, logs = [], []
        d = Wanders()
        cfg = {"li_bot_gap": [100, 200]}
        nb = 3
        for done in range(1, 5):
            nb = linkedin_bot._between(d, cfg, done, nb, Rng(), sleeps.append, logs.append)
        self.assertEqual(d.calls.count("wander"), 4)
        self.assertTrue(any("break" in l for l in logs))
        self.assertIn(linkedin_bot.BREAK_SECONDS[1], sleeps)
        self.assertEqual(nb, 6)
        gap = 200 * 1.3 * linkedin_bot.DISTRACTED[2]  # random() 0 is also always distracted
        self.assertAlmostEqual(sum(x for x in sleeps if x != linkedin_bot.BREAK_SECONDS[1]), gap * 4)

    def test_wander_failure_never_costs_a_task(self):
        class Broken(FakeDriver):
            def wander(self): raise TimeoutError("feed slow")
        class Rng(Steady):
            def random(self): return 0.0
        linkedin_bot._between(Broken(), {"li_bot_gap": [1, 2]}, 1, 99, Rng(), lambda s: None, print)

    def test_task_you_handled_mid_run_is_not_redone(self):
        self.conn.execute("UPDATE leads SET linkedin_url = 'https://www.linkedin.com/in/bob' WHERE email = 'bob@x.com'")
        self.tick(MON)
        bob = self.task("bob@x.com")
        d = FakeDriver()
        def sleep(_):  # you click Done on Bob's card while the bot pauses between leads
            if self.task("bob@x.com"):
                engine.complete_task(self.conn, bob["id"], "done", now=MON)
        linkedin_bot.run(self.conn, lambda: d, now=MON, log=self.logs.append, sleep=sleep, rng=Steady())
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
        linkedin_bot.run(self.conn, lambda: d, now=MON, log=self.logs.append, sleep=lambda s: None, rng=Steady())
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

    def test_caps_never_exceed_the_ceilings(self):
        db.set_setting(self.conn, "li_bot_caps", {"view": 500, "connect": 100, "message": 0})
        db.set_setting(self.conn, "li_bot_week_invites", 400)
        cfg = linkedin_bot.settings(self.conn)
        self.assertEqual(cfg["li_bot_caps"]["view"], linkedin_bot.CEILINGS["view"])
        self.assertEqual(cfg["li_bot_caps"]["connect"], linkedin_bot.CEILINGS["connect"])
        self.assertEqual(cfg["li_bot_caps"]["message"], 0)
        self.assertEqual(cfg["li_bot_week_invites"], 100)

    def test_hourly_burst_guard(self):
        self.tick(MON)
        for i in range(linkedin_bot.HOUR_CAP):
            self.conn.execute("INSERT INTO bot_actions (action, task_id, lead_id, ok, at) VALUES ('view', 0, 0, 1, ?)",
                              (iso(MON - timedelta(minutes=50 - i)),))
        self.assertEqual(self.bot().calls, [])                         # a full hour already
        self.assertIn("open", self.bot(at=MON + timedelta(minutes=15)).calls)  # older ones aged out

    def test_open_invites_and_low_acceptance_hold_invites_back(self):
        cfg = linkedin_bot.settings(self.conn)
        self.conn.execute("UPDATE leads SET li_state = 'none'")
        lid = self.enr("jane@x.com")["lead_id"]
        base = linkedin_bot.day_plan(self.conn, cfg, MON)["caps"]["connect"]
        # 40 invites 10 days ago, 4 accepted: under 20%
        for i in range(40):
            self.conn.execute("INSERT INTO leads (email, first_name, li_state, created_at) VALUES (?, 'X', ?, ?)",
                              (f"p{i}@y.com", "connected" if i < 4 else "invited", iso(MON)))
            new = scalar(self.conn, "SELECT MAX(id) FROM leads")
            self.conn.execute("INSERT INTO bot_actions (action, task_id, lead_id, ok, at) VALUES ('connect', 0, ?, 1, ?)",
                              (new, iso(MON - timedelta(days=10))))
        plan = linkedin_bot.day_plan(self.conn, cfg, MON)
        self.assertEqual(plan["caps"]["connect"], base // 2)
        self.assertTrue(any("halved" in n for n in plan["notes"]))
        self.conn.execute("UPDATE leads SET li_state = 'invited' WHERE email LIKE 'p%' OR id = ?", (lid,))
        for i in range(linkedin_bot.PENDING_MAX):
            self.conn.execute("INSERT INTO leads (email, first_name, li_state, created_at) VALUES (?, 'Y', 'invited', ?)",
                              (f"q{i}@y.com", iso(MON)))
        plan = linkedin_bot.day_plan(self.conn, cfg, MON)
        self.assertEqual(plan["caps"]["connect"], 0)
        self.assertTrue(any("withdraw" in n for n in plan["notes"]))

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
        self.assertEqual(self.tracker("jane@x.com", "LI Likes", "LI Comments"), ("19-Oct-26", "Waiting for you"))
        db.set_setting(self.conn, "li_accept_checked_at", iso(MON + timedelta(hours=3)))  # acceptance scan not due
        self.assertEqual(self.bot(at=MON + timedelta(hours=3)).calls, [])  # waits for you; nothing else due
        self.conn.execute("UPDATE tasks SET body = 'Edited comment', comment_status = 'approved' WHERE id = ?", (t["id"],))
        d2 = self.bot(at=MON + timedelta(hours=4))
        self.assertIn(("comment", "https://li/p/mid", "Edited comment"), d2.calls)
        g = one(self.conn, "SELECT * FROM engagements WHERE task_id = ?", t["id"])
        self.assertEqual((g["post_url"], g["liked"], g["commented"], g["comment"]), ("https://li/p/mid", 1, 1, "Edited comment"))
        self.assertEqual(self.tracker("jane@x.com", "LI Likes", "LI Comments"), ("19-Oct-26", "19-Oct-26"))

    def tracker(self, email, *cols):
        _, items = stats.campaign_status(self.conn, self.cid, timezone.utc)
        cells = next(r["cells"] for r in items if r["cells"]["Email"] == email)
        return tuple(cells[c] for c in cols)

    def test_no_posts_in_window_and_expiry(self):
        t = self.engage_task()
        self.bot(FakeDriver(posts=[linkedin_bot.Post("a", "https://li/p/old", 400, "old", False)]), at=MON + timedelta(hours=2))
        self.assertEqual(one(self.conn, "SELECT status FROM tasks WHERE id = ?", t["id"])["status"], "skipped")
        self.assertEqual(self.tracker("jane@x.com", "LI Likes"), ("No recent posts",))
        # A draft nobody approves expires into a like-only engagement.
        self.tick(MON + timedelta(days=8))
        t2 = self.task("jane@x.com", "li_engage")
        self.bot(FakeDriver(posts=[linkedin_bot.Post("b", "https://li/p/x", 1, "text", True)]), at=MON + timedelta(days=8))
        self.bot(at=MON + timedelta(days=12))                              # still waiting for you after 4 days
        self.assertIsNone(one(self.conn, "SELECT * FROM engagements WHERE task_id = ?", t2["id"]))
        self.bot(at=MON + timedelta(days=16))                              # expired after 7
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

    def test_first_name_token(self):
        F = linkedin_bot.first_name_token
        self.assertEqual([F("Jenny (Cox) Garms"), F("Dr. Ana Ruiz"), F("(Maiden) Lee Kim"), F("")], ["jenny", "ana", "lee", ""])

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


class CrmTest(Base):
    def lead(self, email):
        return one(self.conn, "SELECT * FROM leads WHERE email = ?", email)

    def test_communications_merge_email_linkedin_and_calls(self):
        from campaigner import crm
        self.tick(MON)
        t = self.task("jane@x.com")
        engine.complete_task(self.conn, t["id"], "done", "Hi Jane!", now=MON)
        self.tick(MON + timedelta(days=1))                                   # emails go out
        jane = self.lead("jane@x.com")
        sent = one(self.conn, "SELECT body FROM sends WHERE lead_id = ?", jane["id"])
        self.assertEqual(sent["body"], "Hello Jane")                         # plain-text copy kept for the log
        self.g.inbox.append(mail("jane@x.com", "Re: Hi Acme", cid=self.conv("jane@x.com"), at=MON + timedelta(days=2),
                                 preview="Sounds good, call me Thursday"))
        self.tick(MON + timedelta(days=2, hours=1))
        crm.log_activity(self.conn, jane["id"], kind="call", at=MON + timedelta(days=3), outcome="Left voicemail",
                         notes="rang twice")
        comms = crm.communications(self.conn, lead_id=jane["id"])
        got = [(c["channel"], c["direction"], c["label"]) for c in comms]
        self.assertEqual(got[0], ("call", "out", "Call"))
        self.assertIn(("email", "in", "Email reply"), got)
        self.assertIn(("linkedin", "out", "LinkedIn invite"), got)
        self.assertIn(("email", "out", "Email · step 2"), got)
        reply = next(c for c in comms if c["label"] == "Email reply")
        self.assertEqual(reply["content"], "Sounds good, call me Thursday")
        invite = next(c for c in comms if c["label"] == "LinkedIn invite")
        self.assertEqual((invite["content"], invite["by"]), ("Hi Jane!", "you"))
        s = crm.summaries(self.conn, lead_id=jane["id"])[jane["id"]]
        self.assertEqual((s["email_out"], s["email_in"], s["li_out"], s["calls"]), (1, 1, 1, 1))
        self.assertEqual(len(s["voicemails"]), 1)

    def test_call_that_connects_stops_sequences_and_sets_next_step(self):
        from campaigner import crm
        bdr = crm.add_member(self.conn, "Priya", "priya@co.com", "BDR")
        bob = self.lead("bob@x.com")
        crm.log_activity(self.conn, bob["id"], kind="call", outcome="Meeting booked", notes="Demo on Tue",
                         next_step="Send deck", next_step_at="2026-10-21", by_id=bdr, stop_sequences=True, at=MON)
        e = self.enr("bob@x.com")
        self.assertEqual((e["status"], e["reply_channel"]), ("replied", "call"))
        lead = self.lead("bob@x.com")
        self.assertEqual((lead["stage"], lead["lead_status"]), ("lead", "Meeting Booked"))
        s = crm.summaries(self.conn, lead_id=bob["id"])[bob["id"]]
        self.assertEqual(s["next"], {"text": "Send deck", "date": "2026-10-21"})
        crm.log_activity(self.conn, bob["id"], kind="note", notes="Deck sent", at=MON + timedelta(hours=1))
        self.assertIsNone(crm.summaries(self.conn, lead_id=bob["id"])[bob["id"]]["next"])  # latest log replaces it
        with self.assertRaises(ValueError):
            crm.log_activity(self.conn, bob["id"], kind="call", outcome="Telepathy")

    def test_next_step_reminder_due_then_rescheduled(self):
        from campaigner import crm
        jane = self.lead("jane@x.com")
        today = datetime.now(timezone.utc).date()
        crm.log_activity(self.conn, jane["id"], kind="call", outcome="No answer", next_step="Try again",
                         next_step_at=(today - timedelta(days=1)).isoformat(), at=MON)
        due = crm.reminders(self.conn, timezone.utc)
        self.assertEqual([(d["lead_id"], d["overdue_days"], d["kind"], d["reason"]) for d in due],
                         [(jane["id"], 1, "next_step", "Try again")])
        crm.log_activity(self.conn, jane["id"], kind="call", outcome="No answer",
                         next_step_at=(today + timedelta(days=30)).isoformat(), at=MON + timedelta(hours=1))
        self.assertEqual(crm.reminders(self.conn, timezone.utc), [])              # rescheduled beyond today

    def test_owners_campaign_default_and_import_column(self):
        from campaigner import crm
        a = crm.add_member(self.conn, "Alex")
        self.conn.execute("UPDATE campaigns SET owner_id = ? WHERE id = ?", (a, self.cid))
        self.assertEqual(crm.assign_campaign_owner(self.conn, self.cid), 2)
        self.assertEqual(self.lead("jane@x.com")["owner_id"], a)
        importer.import_rows(self.conn, [{"Email": "cy@x.com", "Name": "Cy Z", "Owner": "Sam Lee"},
                                         {"Email": "di@x.com", "Name": "Di Q", "Owner": "sam lee"}],
                             importer.guess_mapping(["Email", "Name", "Owner"]), self.cid)
        sam = one(self.conn, "SELECT id FROM team WHERE name = 'Sam Lee'")["id"]
        self.assertEqual({self.lead("cy@x.com")["owner_id"], self.lead("di@x.com")["owner_id"]}, {sam})
        self.assertEqual(scalar(self.conn, "SELECT COUNT(*) FROM team"), 2)     # matched case-insensitively
        cols, items = stats.campaign_status(self.conn, self.cid, timezone.utc)
        owners = {r["cells"]["Name"]: r["cells"]["Owner"] for r in items}
        self.assertEqual((owners["Jane Doe"], owners["Cy Z"]), ("Alex", "Sam Lee"))

    def test_old_voicemail_cells_become_calls(self):
        e = self.enr("jane@x.com")
        self.conn.execute("UPDATE enrollments SET voicemails = ? WHERE id = ?", (json.dumps(["03-Nov-26", "left msg"]), e["id"]))
        db.set_setting(self.conn, "voicemails_to_calls", False)
        db._voicemails_to_calls(self.conn)
        got = rows(self.conn, "SELECT outcome, notes, at FROM activities ORDER BY id")
        self.assertEqual([g["outcome"] for g in got], ["Left voicemail"] * 2)
        self.assertTrue(got[0]["at"].startswith("2026-11-03"))
        self.assertEqual(got[1]["notes"], "logged as: left msg")

    def test_status_export_has_communications_sheet(self):
        from fastapi.testclient import TestClient
        from campaigner.web import app as webapp
        from openpyxl import load_workbook
        import io
        from campaigner import crm
        crm.log_activity(self.conn, self.lead("jane@x.com")["id"], kind="call", outcome="No answer")
        real, dbfile, opened = db.connect, Path(self.tmp.name) / "t.db", []

        def connect(*a, **k):
            opened.append(real(dbfile))
            return opened[-1]
        with mock.patch.object(webapp.db, "connect", connect):
            c = TestClient(webapp.app, base_url=f"http://127.0.0.1:{webapp.PORT}")
            r = c.get(f"/status/export.xlsx?campaign={self.cid}")
            self.assertEqual(r.status_code, 200)
            wb = load_workbook(io.BytesIO(r.content))
            self.assertEqual(wb.sheetnames, ["Campaign Status", "Communications"])
            ws = wb["Communications"]
            self.assertEqual([x.value for x in ws[1]][:3], ["Date", "Time", "Lead"])
            self.assertEqual(ws.cell(2, 7).value, "Call")
            r = c.post(f"/api/leads/{self.lead('bob@x.com')['id']}/activities",
                       json={"kind": "call", "outcome": "No answer", "at": ""},
                       headers={"Origin": f"http://127.0.0.1:{webapp.PORT}"})
            self.assertEqual(r.status_code, 200, r.text)
            for path in ("/status", f"/leads/{self.lead('jane@x.com')['id']}", "/", "/settings", "/leads", "/prospects",
                         "/today", "/drafts", "/tasks", "/reminders/email", "/campaigns", f"/campaigns/{self.cid}?tab=sequence",
                         f"/leads/{self.lead('jane@x.com')['id']}/history", f"/campaigns/{self.cid}?tab=status",
                         f"/campaigns/{self.cid}?tab=settings"):
                self.assertEqual(c.get(path).status_code, 200, path)
            # Compose from a lead page: saved as an Outlook draft, sent only when asked; approvals go through the API.
            jane, origin = self.lead("jane@x.com"), {"Origin": f"http://127.0.0.1:{webapp.PORT}"}
            kicks = []
            with mock.patch.object(webapp, "mail_client", lambda acct: self.graphs[acct]), \
                    mock.patch.object(webapp, "_kick", lambda: kicks.append(1)):
                r = c.post(f"/api/leads/{jane['id']}/compose", json={"subject": "", "body": "Hi"}, headers=origin)
                self.assertEqual(r.status_code, 422)                       # no thread yet: a subject is needed
                r = c.post(f"/api/leads/{jane['id']}/compose", headers=origin,
                           json={"subject": "Quick one", "body": "Hi {{ first_name }}", "send": False})
                self.assertEqual((r.status_code, r.json()["sent"], kicks), (200, False, []))
                d = one(self.conn, "SELECT * FROM email_drafts WHERE lead_id = ?", jane["id"])
                self.assertEqual((d["status"], d["step_pos"], d["subject"], "Hi Jane" in d["html"]), ("pending", None, "Quick one", True))
                self.assertEqual(self.g.sent, [])
                r = c.post("/api/drafts/approve", json={"ids": [d["id"]]}, headers=origin)
                self.assertEqual((r.json()["approved"], kicks), (1, [1]))
                r = c.post(f"/api/drafts/{d['id']}", json={"action": "hold"}, headers=origin)
                self.assertEqual(one(self.conn, "SELECT status FROM email_drafts WHERE id = ?", d["id"])["status"], "pending")
                self.assertEqual(c.get(f"/drafts/{d['id']}/preview").status_code, 200)
                r = c.post(f"/api/leads/{jane['id']}", json={"action": "status", "status": "Cold"}, headers=origin)
                self.assertEqual((r.status_code, self.lead("jane@x.com")["status"]), (200, "Cold"))
                r = c.post(f"/api/leads/{jane['id']}", json={"action": "update", "email": "jane@x.com", "phone": "+44 20 7946 0000",
                                                             "linkedin_url": "https://www.linkedin.com/in/janedoe"}, headers=origin)
                self.assertEqual(r.status_code, 200, r.text)
                self.assertIn("442079460000", c.get(f"/leads/{jane['id']}").text)
        # The app's request threads opened these; drop them so Windows can delete the temp database.
        import gc
        opened.clear()
        gc.collect()


class DraftTest(Base):
    """The default: sequence emails are saved in Outlook's Drafts and go out only after you approve them."""

    def setUp(self):
        super().setUp()
        self.conn.execute("UPDATE campaigns SET email_mode = 'draft'")
        self.tick(MON)                                                        # LinkedIn step first
        engine.complete_task(self.conn, self.task("jane@x.com")["id"], "done", now=MON)

    def drafts(self, status=None):
        return rows(self.conn, "SELECT d.*, l.email FROM email_drafts d JOIN leads l ON l.id = d.lead_id "
                               + ("WHERE d.status = ? " if status else "WHERE ? IS NULL ") + "ORDER BY d.id", status)

    def test_email_is_drafted_not_sent_until_approved(self):
        day1 = MON + timedelta(days=1)
        self.tick(day1)
        self.assertEqual(self.g.sent, [])                                     # nothing left the mailbox
        ds = self.drafts("pending")
        self.assertEqual(sorted(d["email"] for d in ds), ["bob@x.com", "jane@x.com"])
        self.assertEqual(len(self.g.drafts), 2)                               # both sit in Outlook's Drafts
        self.assertEqual(self.enr("jane@x.com")["step_pos"], 0)               # the sequence waits on you
        self.tick(day1 + timedelta(minutes=1))
        self.assertEqual(len(self.drafts()), 2)                               # no second draft for the same step
        jane = next(d for d in ds if d["email"] == "jane@x.com")
        self.assertEqual(engine.approve_drafts(self.conn, [jane["id"]]), 1)
        self.tick(day1 + timedelta(minutes=2))
        self.assertEqual([(k, to, subj) for k, to, subj, _ in self.g.sent], [("new", "jane@x.com", "Hi Acme")])
        e = self.enr("jane@x.com")
        self.assertEqual((e["step_pos"], e["conversation_id"] is not None), (1, True))
        self.assertEqual(one(self.conn, "SELECT body FROM sends WHERE enrollment_id = ?", e["id"])["body"], "Hello Jane")
        self.assertEqual(self.enr("bob@x.com")["step_pos"], 0)                # Bob's still waits

    def test_approved_draft_waits_for_the_send_window(self):
        self.tick(MON + timedelta(days=1))
        engine.approve_drafts(self.conn, [d["id"] for d in self.drafts()])
        night = MON + timedelta(days=1, hours=12)                             # 22:00 UTC: outside 09:00-17:00
        self.tick(night)
        self.assertEqual(self.g.sent, [])
        self.assertTrue(any("approved emails waiting" in l for l in self.logs))
        self.tick(MON + timedelta(days=2))
        self.assertEqual(len(self.g.sent), 2)

    def test_drafts_are_written_outside_the_window_for_review(self):
        self.tick(MON + timedelta(days=1, hours=12))
        self.assertEqual(len(self.drafts("pending")), 2)

    def test_skip_moves_the_sequence_on(self):
        self.tick(MON + timedelta(days=1))
        d = next(x for x in self.drafts() if x["email"] == "bob@x.com")
        engine.skip_draft(self.conn, d, "skipped by you", MON + timedelta(days=1))
        self.assertEqual(self.enr("bob@x.com")["step_pos"], 1)
        self.tick(MON + timedelta(days=1, minutes=1))
        self.assertEqual(self.g.sent, [])
        self.assertEqual(len(self.drafts("pending")), 1)                      # no re-draft of the skipped step

    def test_sent_or_deleted_in_outlook_is_noticed(self):
        self.tick(MON + timedelta(days=1))
        jane = next(d for d in self.drafts() if d["email"] == "jane@x.com")
        bob = next(d for d in self.drafts() if d["email"] == "bob@x.com")
        sent_at = MON + timedelta(days=1, hours=1)
        self.g.drafts[jane["outlook_id"]].update(state="sent", subject="Hi Acme (edited)", sent_at=iso(sent_at))
        self.g.drafts[bob["outlook_id"]]["state"] = "deleted"
        self.tick(MON + timedelta(days=1, hours=2))                            # full pass checks Outlook
        self.assertEqual(self.g.sent, [])                                      # the app sent nothing itself
        s = one(self.conn, "SELECT subject, sent_at FROM sends WHERE lead_id = ?", jane["lead_id"])
        self.assertEqual((s["subject"], s["sent_at"]), ("Hi Acme (edited)", iso(sent_at)))
        self.assertEqual(self.enr("jane@x.com")["step_pos"], 1)
        self.assertEqual((self.drafts("skipped")[0]["email"], self.enr("bob@x.com")["step_pos"]), ("bob@x.com", 1))

    def test_reply_before_approval_drops_the_draft(self):
        self.conn.execute("UPDATE campaigns SET email_mode = 'auto'")
        self.tick(MON + timedelta(days=1))                                     # email 1 goes out
        self.conn.execute("UPDATE campaigns SET email_mode = 'draft'")
        self.tick(MON + timedelta(days=6))                                     # Bob's LinkedIn step skips (no URL)
        self.tick(MON + timedelta(days=9))                                     # follow-up drafted (reply in thread)
        ds = [d for d in self.drafts("pending") if d["email"] == "bob@x.com"]
        self.assertEqual((len(ds), ds[0]["threaded"]), (1, 1))
        self.g.inbox.append(mail("bob@x.com", "Re: Hi Beta", cid=self.conv("bob@x.com"), at=MON + timedelta(days=9, hours=1)))
        engine.approve_drafts(self.conn, [ds[0]["id"]])
        n = len(self.g.sent)
        self.tick(MON + timedelta(days=9, hours=2))
        self.assertEqual(len(self.g.sent), n)                                  # never nudges someone who answered
        self.assertEqual(self.enr("bob@x.com")["status"], "replied")
        self.assertEqual(one(self.conn, "SELECT status FROM email_drafts WHERE id = ?", ds[0]["id"])["status"], "discarded")
        self.assertNotIn(ds[0]["outlook_id"], self.g.drafts)                   # and the Outlook copy is removed

    def test_auto_mode_sends_pending_drafts(self):
        self.tick(MON + timedelta(days=1))
        self.conn.execute("UPDATE campaigns SET email_mode = 'auto'")
        self.tick(MON + timedelta(days=1, minutes=1))
        self.assertEqual(len(self.g.sent), 2)

    def test_one_off_email_sends_when_approved_and_is_logged(self):
        from campaigner import crm
        jane = one(self.conn, "SELECT * FROM leads WHERE email = 'jane@x.com'")
        dr = self.g.draft_new("jane@x.com", "Jane Doe", "Quick question", "<p>Hi Jane</p>")
        self.conn.execute("""INSERT INTO email_drafts (lead_id, sender, to_email, subject, html, outlook_id, status, created_at)
                             VALUES (?, ?, 'jane@x.com', 'Quick question', '<p>Hi Jane</p>', ?, 'approved', ?)""",
                          (jane["id"], ME, dr["id"], iso(MON)))
        engine.tick(self.conn, self.factory, now=MON + timedelta(hours=12), sleep=lambda s: None, log=self.logs.append,
                    checks=False)                                              # quick pass, outside any window
        self.assertEqual([(to, subj) for _, to, subj, _ in self.g.sent], [("jane@x.com", "Quick question")])
        c = next(x for x in crm.communications(self.conn, lead_id=jane["id"]) if x["channel"] == "email")
        self.assertEqual((c["label"], c["subject"], c["content"]), ("Email", "Quick question", "Hi Jane"))


class StatusTest(Base):
    def lead(self, email):
        return one(self.conn, "SELECT * FROM leads WHERE email = ?", email)

    def test_default_new_reply_makes_warm_and_unsubscribe_closes(self):
        self.assertEqual(self.lead("jane@x.com")["status"], "New")
        engine.mark_replied(self.conn, self.lead("jane@x.com")["id"], "email", MON)
        self.assertEqual(self.lead("jane@x.com")["status"], "Warm")
        engine.unsubscribe(self.conn, self.lead("bob@x.com")["id"], MON)
        self.assertEqual(self.lead("bob@x.com")["status"], "Closed/Lost")

    def test_warm_and_cold_cadence_from_last_contact(self):
        from campaigner import crm
        jane, bob = self.lead("jane@x.com"), self.lead("bob@x.com")
        now = datetime.now(timezone.utc)
        crm.log_activity(self.conn, jane["id"], kind="call", outcome="Connected – interested", status="Warm",
                         at=now - timedelta(days=3))
        crm.log_activity(self.conn, bob["id"], kind="call", outcome="Connected – not interested", status="Cold",
                         at=now - timedelta(days=10))
        crm.refresh_followups(self.conn, tz=timezone.utc)
        j, b = self.lead("jane@x.com"), self.lead("bob@x.com")
        self.assertEqual((j["status"], j["next_followup_at"]), ("Warm", now.date().isoformat()))       # 3 days on
        self.assertEqual((b["status"], b["next_followup_at"]), ("Cold", (now.date() + timedelta(days=80)).isoformat()))
        due = crm.reminders(self.conn, timezone.utc)
        self.assertEqual([(d["lead_id"], d["kind"], d["status"]) for d in due], [(jane["id"], "warm", "Warm")])
        self.assertEqual(due[0]["last_contacted_at"], iso(now - timedelta(days=3)))
        # A new contact restarts the clock; so does a snooze.
        crm.log_activity(self.conn, jane["id"], kind="whatsapp", outcome="Sent", at=now)
        crm.refresh_followups(self.conn, tz=timezone.utc)
        self.assertEqual(crm.reminders(self.conn, timezone.utc), [])
        self.conn.execute("UPDATE activities SET at = ? WHERE lead_id = ? AND kind = 'whatsapp'", (iso(now - timedelta(days=4)), jane["id"]))
        crm.refresh_followups(self.conn, tz=timezone.utc)
        self.assertEqual(len(crm.reminders(self.conn, timezone.utc)), 1)
        until = crm.snooze(self.conn, jane["id"], tz=timezone.utc)
        self.assertEqual(until, (now.date() + timedelta(days=3)).isoformat())
        self.assertEqual(crm.reminders(self.conn, timezone.utc), [])

    def test_cold_recurs_every_90_days_until_status_changes(self):
        from campaigner import crm
        bob = self.lead("bob@x.com")
        now = datetime.now(timezone.utc)
        crm.log_activity(self.conn, bob["id"], kind="call", outcome="No answer", status="Cold", at=now - timedelta(days=91))
        crm.refresh_followups(self.conn, tz=timezone.utc)
        self.assertEqual([d["kind"] for d in crm.reminders(self.conn, timezone.utc)], ["cold"])
        crm.snooze(self.conn, bob["id"], tz=timezone.utc)                        # reminded again in 90 days
        self.assertEqual(self.lead("bob@x.com")["next_followup_at"], (now.date() + timedelta(days=90)).isoformat())
        crm.set_status(self.conn, bob["id"], "Closed/Lost")
        self.assertIsNone(self.lead("bob@x.com")["next_followup_at"])
        self.assertEqual(self.enr("bob@x.com")["status"], "stopped")             # closing ends the sequence

    def test_sequence_email_updates_last_contacted(self):
        self.tick(MON)
        engine.complete_task(self.conn, self.task("jane@x.com")["id"], "done", now=MON)
        self.tick(MON + timedelta(days=1))
        self.assertEqual(self.lead("bob@x.com")["last_contacted_at"], iso(MON + timedelta(days=1)))
        self.assertEqual(self.lead("jane@x.com")["last_contacted_at"], iso(MON + timedelta(days=1)))

    def test_import_status_column_and_closed_not_enrolled(self):
        s = importer.import_rows(self.conn, [{"Email": "w@x.com", "Name": "Wa Rm", "Status": "warm"},
                                             {"Email": "l@x.com", "Name": "Lo St", "Status": "Closed-Lost"}],
                                 importer.guess_mapping(["Email", "Name", "Status"]), self.cid)
        self.assertEqual((self.lead("w@x.com")["status"], self.lead("l@x.com")["status"]), ("Warm", "Closed/Lost"))
        self.assertEqual((s["enrolled"], s["not_enrolled"]), (1, 1))

    def test_daily_reminder_email(self):
        from campaigner import crm
        db.set_setting(self.conn, "digest_enabled", True)
        priya = crm.add_member(self.conn, "Priya", "priya@co.com")
        jane = self.lead("jane@x.com")
        crm.set_owner(self.conn, jane["id"], priya)
        now = datetime.now(timezone.utc).replace(hour=9, minute=0, second=0, microsecond=0)
        crm.log_activity(self.conn, jane["id"], kind="call", outcome="Connected – interested", status="Warm",
                         at=now - timedelta(days=5))
        crm.refresh_followups(self.conn, tz=timezone.utc)
        boxes = engine.Mailboxes(self.factory, self.logs.append)
        early = now.replace(hour=7)
        self.assertEqual(crm.maybe_send_digest(self.conn, boxes, early, self.logs.append, tz=timezone.utc), 0)   # before 8:00
        self.assertEqual(crm.maybe_send_digest(self.conn, boxes, now, self.logs.append, tz=timezone.utc), 2)     # you + Priya
        to = [x[1] for x in self.g.sent]
        self.assertEqual(to, [ME, "priya@co.com"])
        html = self.g.sent[0][3]
        for needle in ("Jane Doe", "Warm", f"/leads/{jane['id']}?compose=1", "Send email / message",
                       (now - timedelta(days=5)).strftime("%d %b %Y")):
            self.assertIn(needle, html)
        self.assertEqual(crm.maybe_send_digest(self.conn, boxes, now + timedelta(hours=1), self.logs.append, tz=timezone.utc), 0)  # once a day
        self.assertEqual(scalar(self.conn, "SELECT COUNT(*) FROM sends"), 0)       # a reminder isn't outreach

    def test_nothing_due_sends_nothing(self):
        from campaigner import crm
        db.set_setting(self.conn, "digest_enabled", True)
        now = datetime.now(timezone.utc).replace(hour=9)
        self.assertEqual(crm.maybe_send_digest(self.conn, engine.Mailboxes(self.factory, self.logs.append), now,
                                               self.logs.append, tz=timezone.utc), 0)
        self.assertEqual(self.g.sent, [])


class WhatsAppTest(Base):
    def setUp(self):
        super().setUp()
        self.wid = self.campaign([("whatsapp", 0, None, "Hi {{ first_name }}, **quick** one"),
                                  ("email", 1, "Hi", "Hello")], name="wa")
        importer.import_rows(self.conn, [{"Email": "pat@x.com", "Name": "Pat Kim", "Mobile": "+1 (440) 555-0100", "Old contact": "yes"},
                                         {"Email": "nop@x.com", "Name": "No Phone", "Old contact": "yes"},
                                         {"Email": "new@x.com", "Name": "New Person", "Mobile": "+1 440 555 0199"}],
                             importer.guess_mapping(["Email", "Name", "Mobile", "Old contact"]), self.wid)
        self.conn.execute("UPDATE enrollments SET enrolled_at = ? WHERE campaign_id = ?", (iso(MON), self.wid))

    def test_whatsapp_step_is_a_task_and_skips_without_a_phone(self):
        from campaigner import crm
        self.tick(MON)
        t = one(self.conn, """SELECT t.*, e.lead_id FROM tasks t JOIN enrollments e ON e.id = t.enrollment_id
                              WHERE e.campaign_id = ? AND t.status = 'open'""", self.wid)
        self.assertEqual((t["channel"], t["body"]), ("whatsapp", "Hi Pat, *quick* one"))
        nop = one(self.conn, "SELECT e.* FROM enrollments e JOIN leads l ON l.id = e.lead_id WHERE l.email = 'nop@x.com'")
        self.assertEqual(nop["step_pos"], 0)                                       # skipped: no number to message
        new = one(self.conn, "SELECT e.* FROM enrollments e JOIN leads l ON l.id = e.lead_id WHERE l.email = 'new@x.com'")
        self.assertEqual(new["step_pos"], 0)                                       # skipped: a stranger, not a warm lead
        self.assertEqual(scalar(self.conn, "SELECT detail FROM events WHERE enrollment_id = ? AND type = 'step_skipped'", new["id"]),
                         "WhatsApp is only for Warm leads and old contacts")
        d = FakeDriver()
        db.set_setting(self.conn, "li_mode", "bot")
        db.set_setting(self.conn, "li_bot_hours", ["00:00", "23:59"])
        db.set_setting(self.conn, "li_bot_days", "mon,tue,wed,thu,fri,sat,sun")
        linkedin_bot.run(self.conn, lambda: d, now=MON, log=self.logs.append, sleep=lambda s: None, rng=Steady())
        self.assertEqual(one(self.conn, "SELECT status FROM tasks WHERE id = ?", t["id"])["status"], "open")  # never the bot's
        engine.complete_task(self.conn, t["id"], "done", now=MON)
        c = crm.communications(self.conn, lead_id=t["lead_id"])[0]
        self.assertEqual((c["channel"], c["direction"], c["label"]), ("whatsapp", "out", "WhatsApp message"))
        crm.refresh_followups(self.conn)
        self.assertEqual(one(self.conn, "SELECT last_contacted_at FROM leads WHERE id = ?", t["lead_id"])["last_contacted_at"],
                         iso(MON))

    def test_phone_helpers(self):
        from campaigner.render import lead_phone, wa_digits, whatsapp_text
        self.assertEqual(wa_digits("+1 (440) 555-0100"), "14405550100")
        self.assertEqual(wa_digits("0044 20 7946 0000"), "442079460000")
        self.assertEqual(wa_digits("555-0100"), "")
        self.assertEqual(lead_phone({"data": json.dumps({"phone": "111", "mobile": "222"})}), "222")
        self.assertEqual(whatsapp_text("**bold** and *it*\n- one"), "*bold* and _it_\n• one")


class MailSyncTest(Base):
    def test_hand_written_and_later_emails_are_logged_once(self):
        from campaigner import crm
        jane = one(self.conn, "SELECT * FROM leads WHERE email = 'jane@x.com'")
        t0 = MON + timedelta(hours=1)
        self.g.outbox.append({"id": "o1", "subject": "Following up", "bodyPreview": "As discussed…",
                              "sentDateTime": iso(t0), "toRecipients": [{"emailAddress": {"address": "Jane@x.com"}}]})
        self.g.inbox.append(mail("jane@x.com", "Thanks!", at=t0 + timedelta(hours=1), preview="Great, see you then"))
        self.g.inbox.append(mail("stranger@y.com", "Hello", at=t0))
        self.tick(MON + timedelta(hours=3))
        self.tick(MON + timedelta(hours=4))                                        # second pass adds nothing
        got = [(c["label"], c["subject"], c["content"]) for c in crm.communications(self.conn, lead_id=jane["id"])
               if c["channel"] == "email"]
        self.assertEqual(got, [("Email received", "Thanks!", "Great, see you then"),
                               ("Email (written in Outlook)", "Following up", "As discussed…")])
        self.assertEqual(one(self.conn, "SELECT last_contacted_at FROM leads WHERE id = ?", jane["id"])["last_contacted_at"],
                         iso(t0 + timedelta(hours=1)))
        self.assertEqual(self.enr("jane@x.com")["status"], "pending")              # an unrelated email stops nothing

    def test_app_sends_are_not_logged_twice(self):
        from campaigner import crm
        self.tick(MON)
        engine.complete_task(self.conn, self.task("jane@x.com")["id"], "done", now=MON)
        at = MON + timedelta(days=1)
        self.tick(at)                                                              # the app sends email 1
        self.g.outbox.append({"id": "o9", "subject": "Hi Beta", "bodyPreview": "Hello Bob", "sentDateTime": iso(at + timedelta(seconds=20)),
                              "toRecipients": [{"emailAddress": {"address": "bob@x.com"}}]})
        self.tick(at + timedelta(minutes=30))
        bob = one(self.conn, "SELECT id FROM leads WHERE email = 'bob@x.com'")["id"]
        self.assertEqual([c["label"] for c in crm.communications(self.conn, lead_id=bob) if c["channel"] == "email"],
                         ["Email · step 2"])


class TagsTest(Base):
    def lid(self, email):
        return one(self.conn, "SELECT id FROM leads WHERE email = ?", email)["id"]

    def test_tags_are_capped_and_match_any_case(self):
        jane, bob = self.lid("jane@x.com"), self.lid("bob@x.com")
        self.assertEqual(crm.set_tags(self.conn, jane, "Fintech, P1; 2025, fintech"), ["Fintech", "P1", "2025"])
        with self.assertRaises(ValueError):
            crm.set_tags(self.conn, jane, ["a", "b", "c", "d", "e", "f", "g"])
        self.assertEqual(crm.set_tags(self.conn, bob, ["FINTECH"]), ["Fintech"])   # joins the spelling in use
        r = crm.add_tags(self.conn, [jane, bob], "Q4, p1")
        self.assertEqual(r, {"tagged": 2, "full": 0})
        self.assertEqual(crm.tags_of(self.conn, [bob])[bob], ["Fintech", "Q4", "P1"])
        crm.set_tags(self.conn, jane, ["1", "2", "3", "4", "5", "6"])
        self.assertEqual(crm.add_tags(self.conn, [jane, bob], "Seven"), {"tagged": 1, "full": 1})
        self.assertEqual(crm.remove_tag(self.conn, [jane, bob], "seven"), 1)
        self.assertEqual({t["tag"]: t["n"] for t in crm.all_tags(self.conn)}["Fintech"], 1)
        sql, args = crm.tag_filter_sql(["fintech", "q4"])
        got = [r["id"] for r in rows(self.conn, "SELECT l.id FROM leads l WHERE 1" + sql, *args)]
        self.assertEqual(got, [bob])

    def test_import_tags_and_old_contacts(self):
        s = importer.import_rows(self.conn, [
            {"Email": "jane@x.com", "Tags": "Fintech, P1", "Old contact": "Yes"},
            {"Email": "new@x.com", "Name": "New One", "Tags": "a,b,c,d,e,f,g"}],
            importer.guess_mapping(["Email", "Name", "Tags", "Old contact"]), None)
        jane, new = self.lid("jane@x.com"), self.lid("new@x.com")
        self.assertEqual(crm.tags_of(self.conn, [jane])[jane], ["Fintech", "P1"])
        self.assertEqual(len(crm.tags_of(self.conn, [new])[new]), crm.MAX_TAGS)
        self.assertTrue(any("only 6 tags" in x for x in s["skipped"]))
        self.assertEqual(one(self.conn, "SELECT old_contact FROM leads WHERE id = ?", jane)["old_contact"], 1)

    def test_warm_campaign_takes_people_who_replied(self):
        jane = self.lid("jane@x.com")
        engine.mark_replied(self.conn, jane, "email", MON, detail="hi")
        cold = self.campaign([("email", 0, "Hi", "Hi")], name="cold")
        warm = self.campaign([("whatsapp", 0, None, "Hi {{ first_name }}")], name="nurture", audience="warm")
        self.assertEqual(importer.enroll_leads(self.conn, [jane], cold)["not_enrolled"], 1)
        self.assertEqual(importer.enroll_leads(self.conn, [jane], warm)["enrolled"], 1)
        self.assertEqual(importer.enroll_leads(self.conn, [jane], warm)["already_enrolled"], 1)


class OutOfOfficeTest(Base):
    def test_return_dates(self):
        at = datetime(2026, 11, 17, 9, 0, tzinfo=timezone.utc)   # a Tuesday
        cases = {
            "I am out of the office until Monday 24 November with limited access to email.": "2026-11-24",
            "I'm on annual leave and will be back on 24th Nov.": "2026-11-24",
            "Out of office. Returning December 2, 2026.": "2026-12-02",
            "I will return on 01/12/2026": "2026-12-01",
            "Back in the office 11/24": "2026-11-24",
            "Away at a conference, back tomorrow": "2026-11-18",
            "I'm travelling and back on Friday": "2026-11-20",
            "On leave from 10 Nov to 5 Jan 2027": "2027-01-05",
            "I am currently out of the office with limited access to email.": None,
            "Back at 9.30 am": None,
        }
        for text, want in cases.items():
            got = crm.ooo_return(text, at)
            self.assertEqual(got.isoformat() if got else None, want, text)
        self.assertEqual(crm.auto_reply_kind("Jane is no longer with Acme. Please contact sales@"), "left")
        self.assertEqual(crm.auto_reply_kind("Automatic reply: thanks for your email"), "auto")

    def test_ooo_shows_on_the_lead_the_email_and_the_draft(self):
        self.tick(MON)
        engine.complete_task(self.conn, self.task("jane@x.com")["id"], "done", now=MON)
        at = MON + timedelta(days=1)
        self.tick(at)                                                              # email 1 to Jane
        e = self.enr("jane@x.com")
        self.g.inbox.append(mail("jane@x.com", "Automatic reply: Hi Acme", cid=e["conversation_id"], at=at + timedelta(minutes=5),
                                 preview="I am out of the office until 3 November."))
        self.tick(at + timedelta(minutes=30))
        jane = one(self.conn, "SELECT id FROM leads WHERE email = 'jane@x.com'")["id"]
        self.assertEqual(self.enr("jane@x.com")["status"], "active")             # the sequence carries on
        now = at + timedelta(hours=1)
        ooo = crm.out_of_office(self.conn, [jane], now=now)[jane]
        self.assertEqual((ooo["kind"], ooo["back"].isoformat(), ooo["label"]), ("ooo", "2026-11-03", "Out of office · back 03 Nov"))
        sent = [c for c in crm.communications(self.conn, lead_id=jane) if c["label"].startswith("Email ·")][0]
        self.assertIn("out of office reply, back 03 Nov", sent["extra"])
        auto = [c for c in crm.communications(self.conn, lead_id=jane) if c["direction"] == "in"][0]
        self.assertEqual((auto["label"], auto["extra"]), ("Out of office reply", "back 03 Nov 2026"))
        self.assertEqual(crm.out_of_office(self.conn, [jane], now=datetime(2026, 11, 5, tzinfo=timezone.utc)), {})  # back now
        cols, out = stats.campaign_status(self.conn, self.cid, timezone.utc)
        self.assertIn("(OOO, back 03-Nov)", [r for r in out if r["lead_id"] == jane][0]["cells"]["EM1"])


class FakeWA:
    """WhatsApp Web stand-in: records sends; chats map digits -> messages read back."""
    def __init__(self):
        self.sent, self.reads, self.chats = [], [], {}
        self.fail = self.blocked = None

    def open(self): pass
    def close(self): pass
    def snapshot(self, name): pass

    def ready(self):
        if self.blocked:
            raise linkedin_bot.BlockedError(self.blocked)

    def send(self, digits, text):
        if self.fail:
            raise self.fail
        self.sent.append((digits, text))
        self.chats.setdefault(digits, []).append({"id": f"true_{len(self.sent)}", "dir": "out", "meta": "", "text": text})

    def read(self, digits):
        self.reads.append(digits)
        return list(self.chats.get(digits, []))


class WhatsAppBotTest(Base):
    def setUp(self):
        super().setUp()
        from campaigner import whatsapp_bot as wb
        self.wb = wb
        self.wid = self.campaign([("whatsapp", 0, None, "Hi {{ first_name }}"), ("email", 3, "Hi", "Hello")], name="wa")
        importer.import_rows(self.conn, [
            {"Email": "pat@x.com", "Name": "Pat Kim", "Mobile": "+1 440 555 0100", "Old contact": "y"},
            {"Email": "sam@x.com", "Name": "Sam Lee", "Mobile": "+44 20 7946 0000", "Old contact": "y"}],
            importer.guess_mapping(["Email", "Name", "Mobile", "Old contact"]), self.wid)
        self.conn.execute("UPDATE enrollments SET enrolled_at = ? WHERE campaign_id = ?", (iso(MON), self.wid))
        for k, v in (("wa_mode", "bot"), ("wa_hours", ["00:00", "23:59"]), ("wa_days", "mon,tue,wed,thu,fri,sat,sun")):
            db.set_setting(self.conn, k, v)
        self.d = FakeWA()
        self.tick(MON)

    def run_wa(self, at):
        self.wb.run(self.conn, lambda: self.d, now=at, log=self.logs.append, sleep=lambda s: None, rng=Steady())

    def wtask(self, email):
        return one(self.conn, """SELECT t.* FROM tasks t JOIN enrollments e ON e.id = t.enrollment_id JOIN leads l ON l.id = e.lead_id
                                 WHERE l.email = ? AND t.channel = 'whatsapp'""", email)

    def lid(self, email):
        return one(self.conn, "SELECT id FROM leads WHERE email = ?", email)["id"]

    def test_sends_only_what_you_approved(self):
        self.run_wa(MON + timedelta(minutes=1))
        self.assertEqual(self.d.sent, [])                                          # waiting for approval
        pat = self.wtask("pat@x.com")
        self.conn.execute("UPDATE tasks SET comment_status = 'approved', body = 'Hi Pat!' WHERE id = ?", (pat["id"],))
        self.run_wa(MON + timedelta(minutes=2))
        self.assertEqual(self.d.sent, [("14405550100", "Hi Pat!")])
        self.assertEqual(self.wtask("pat@x.com")["status"], "done")
        self.assertEqual(self.wtask("sam@x.com")["status"], "open")
        c = [x for x in crm.communications(self.conn, lead_id=self.lid("pat@x.com")) if x["channel"] == "whatsapp"][0]
        self.assertEqual((c["label"], c["by"], c["content"]), ("WhatsApp message", "bot", "Hi Pat!"))
        self.assertEqual(self.wb.sent_today(self.conn, MON + timedelta(minutes=3)), 1)

    def test_rechecks_who_it_may_message_before_sending(self):
        db.set_setting(self.conn, "wa_approve", False)
        self.conn.execute("UPDATE leads SET old_contact = 0, status = 'Cold' WHERE email = 'sam@x.com'")
        self.run_wa(MON + timedelta(minutes=1))
        self.assertEqual([d for d, _ in self.d.sent], ["14405550100"])
        sam = self.wtask("sam@x.com")
        self.assertEqual((sam["status"], sam["bot_note"]), ("skipped", "WhatsApp is only for Warm leads and old contacts"))

    def test_caps_and_rest(self):
        db.set_setting(self.conn, "wa_approve", False)
        db.set_setting(self.conn, "wa_daily_cap", 1)
        self.run_wa(MON + timedelta(minutes=1))
        self.run_wa(MON + timedelta(hours=2))
        self.assertEqual(len(self.d.sent), 1)
        self.assertIsNotNone(db.get_setting(self.conn, "wa_bot_rest_until"))
        db.set_setting(self.conn, "wa_daily_cap", 99)
        self.assertEqual(self.wb.settings(self.conn)["wa_daily_cap"], self.wb.CEILING)

    def test_one_off_and_reading_replies(self):
        pat = self.lid("pat@x.com")
        self.conn.execute("UPDATE tasks SET status = 'skipped' WHERE channel = 'whatsapp'")
        self.conn.execute("INSERT INTO wa_outbox (lead_id, body, status, created_at) VALUES (?, 'Coffee next week?', 'queued', ?)",
                          (pat, iso(MON)))
        self.run_wa(MON + timedelta(minutes=1))
        self.assertEqual(self.d.sent, [("14405550100", "Coffee next week?")])
        self.assertEqual(one(self.conn, "SELECT status FROM wa_outbox")["status"], "sent")
        self.assertEqual(crm.communications(self.conn, lead_id=pat)[0]["label"], "WhatsApp message")
        self.d.chats["14405550100"].insert(0, {"id": "false_old", "dir": "in", "meta": "", "text": "an old chat"})
        self.d.chats["14405550100"].append({"id": "false_1", "dir": "in", "meta": "[10:21, 19/10/2026] Pat: ", "text": "Sure, Tuesday?"})
        db.set_setting(self.conn, "wa_bot_rest_until", None)
        self.run_wa(MON + timedelta(hours=1))
        self.assertEqual(self.d.reads, ["14405550100"])
        e = one(self.conn, "SELECT * FROM enrollments WHERE lead_id = ? AND campaign_id = ?", pat, self.wid)
        self.assertEqual((e["status"], e["reply_channel"]), ("replied", "whatsapp"))
        self.assertEqual(one(self.conn, "SELECT status FROM leads WHERE id = ?", pat)["status"], "Warm")
        self.d.chats["14405550100"].append({"id": "false_2", "dir": "in", "meta": "", "text": "Or Wednesday"})
        self.run_wa(MON + timedelta(hours=2))                                      # next read: logged once each
        self.run_wa(MON + timedelta(hours=3))
        got = [(c["label"], c["content"]) for c in crm.communications(self.conn, lead_id=pat) if c["direction"] == "in"]
        self.assertEqual(sorted(got), [("WhatsApp received", "Or Wednesday"), ("WhatsApp reply", "Sure, Tuesday?")])

    def test_signed_out_pauses_and_unconfirmed_is_never_retried(self):
        db.set_setting(self.conn, "wa_approve", False)
        self.d.blocked = "WhatsApp Web is signed out"
        self.run_wa(MON + timedelta(minutes=1))
        self.assertIn("signed out", db.get_setting(self.conn, "wa_bot_paused")["reason"])
        self.assertEqual(crm.actions(self.conn)["alerts"][-1][:28], "The WhatsApp bot is paused: ")
        self.wb.resume(self.conn)
        self.d.blocked, self.d.fail = None, self.wb.Unconfirmed("couldn't confirm")
        self.run_wa(MON + timedelta(minutes=2))
        t = self.wtask("pat@x.com")
        self.assertEqual((t["status"], t["bot_attempts"]), ("open", self.wb.MAX_ATTEMPTS))  # left for you
        self.d.fail = None
        self.run_wa(MON + timedelta(hours=3))
        self.assertNotIn("14405550100", [d for d, _ in self.d.sent])

    def test_parse_meta(self):
        now = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
        got = self.wb.parse_meta("[10:21 pm, 16/11/2026] Pat: ", now)
        self.assertEqual((got.day, got.month, got.hour), (16, 11, 22))
        got = self.wb.parse_meta("[09:05, 11/3/26] Pat: ", now)                  # 3 Nov (m/d) beats 11 Mar? both past: newest
        self.assertEqual((got.month, got.day), (11, 3))
        self.assertIsNone(self.wb.parse_meta("no date", now))

    def test_web_pages_and_bulk_actions(self):
        from fastapi.testclient import TestClient
        from campaigner.web import app as webapp
        real, dbfile, opened = db.connect, Path(self.tmp.name) / "t.db", []

        def connect(*a, **k):
            opened.append(real(dbfile))
            return opened[-1]
        origin = {"Origin": f"http://127.0.0.1:{webapp.PORT}"}
        pat, jane = self.lid("pat@x.com"), self.lid("jane@x.com")
        with mock.patch.object(webapp.db, "connect", connect), \
                mock.patch.object(webapp.runner, "run_wa", lambda: None):
            c = TestClient(webapp.app, base_url=f"http://127.0.0.1:{webapp.PORT}")
            r = c.post("/api/leads/bulk", json={"ids": [pat, jane], "action": "tag", "tag": "Fintech, P1"}, headers=origin)
            self.assertEqual((r.status_code, r.json()["msg"]), (200, "Tagged 2"))
            self.assertIn("Pat", c.get("/prospects?tag=fintech&tag=P1").text)
            self.assertNotIn("Pat Kim", c.get("/prospects?tag=Nope").text)
            r = c.post(f"/api/leads/{jane}/whatsapp", json={"body": "Hi", "send": True}, headers=origin)
            self.assertEqual(r.status_code, 409)                                   # Jane is New, not an old contact
            r = c.post("/api/leads/bulk", json={"ids": [jane], "action": "old_contact", "value": True}, headers=origin)
            self.assertEqual(r.status_code, 200)
            r = c.post(f"/api/leads/{jane}/whatsapp", json={"body": "Hi {{ first_name }}", "send": True}, headers=origin)
            self.assertEqual(r.status_code, 422)                                   # no phone number
            r = c.post(f"/api/leads/{pat}/whatsapp", json={"body": "Hi {{ first_name }}", "send": False}, headers=origin)
            self.assertEqual(r.status_code, 200)
            o = one(self.conn, "SELECT * FROM wa_outbox WHERE lead_id = ?", pat)
            self.assertEqual((o["status"], o["body"]), ("draft", "Hi Pat"))
            self.assertEqual(c.post(f"/api/wa-outbox/{o['id']}", json={"action": "send"}, headers=origin).status_code, 200)
            self.assertEqual(one(self.conn, "SELECT status FROM wa_outbox WHERE id = ?", o["id"])["status"], "queued")
            t = self.wtask("sam@x.com")
            self.assertNotIn("Approve &amp; send via bot", c.get("/tasks").text)   # LinkedIn queue only
            page = c.get("/whatsapp").text
            self.assertIn("Approve &amp; send via bot", page)                     # sequence message
            self.assertIn("Hi Pat", page)                                          # queued one-off
            self.assertIn("bot will send", page)
            d = c.post(f"/api/leads/{pat}/whatsapp", json={"body": "Draft two", "send": False}, headers=origin)
            self.assertEqual(d.status_code, 200)
            d = one(self.conn, "SELECT id FROM wa_outbox WHERE body = 'Draft two'")
            r = c.post(f"/api/wa-outbox/{d['id']}", json={"action": "sent"}, headers=origin)
            self.assertEqual(r.status_code, 200)
            self.assertIn("wa_open", r.json())
            self.assertEqual(one(self.conn, "SELECT status FROM wa_outbox WHERE id = ?", d["id"])["status"], "sent")
            self.assertEqual(c.post(f"/api/wa-outbox/{o['id']}", json={"action": "sent"}, headers=origin).status_code, 409)
            page = c.get("/whatsapp").text
            self.assertIn("Last 14 days", page)
            self.assertIn("by you", page)                                          # the one you sent yourself
            r = c.post(f"/api/tasks/{t['id']}/wa-approve", json={"body": "Hi Sam, edited"}, headers=origin)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(self.wtask("sam@x.com")["comment_status"], "approved")
            r = c.post(f"/api/leads/{pat}", json={"action": "tags", "tags": list("abcdefg")}, headers=origin)
            self.assertEqual(r.status_code, 422)
            warm = self.campaign([("whatsapp", 0, None, "Hi")], name="nurture", audience="warm")
            r = c.post("/api/leads/bulk", json={"ids": [pat], "action": "enroll", "campaign_id": warm}, headers=origin)
            self.assertIn("not added", r.json()["msg"])                            # still in the live "wa" campaign
            r = c.post("/settings/whatsapp-bot", data={"wa_mode": "bot", "wa_daily_cap": "500", "gap_min": "10",
                                                       "gap_max": "20", "hours_start": "10:00", "hours_end": "18:00",
                                                       "days": ["mon"]}, headers=origin, follow_redirects=False)
            self.assertEqual(r.status_code, 303)
            cfg = self.wb.settings(self.conn)
            self.assertEqual((cfg["wa_daily_cap"], cfg["wa_gap"], cfg["wa_approve"]), (self.wb.CEILING, [30, 30], False))
            for path in (f"/leads/{pat}", "/settings", "/prospects?old=1", "/leads", "/drafts", "/today", "/status",
                         f"/campaigns/{self.wid}?tab=settings"):
                self.assertEqual(c.get(path).status_code, 200, path)
        import gc
        opened.clear()
        gc.collect()


class ReplyCheckTest(Base):
    """The LinkedIn bot's inbox check: likely replies first, a broken profile is skipped, the rest rotate."""

    def lead(self, i, first, last="X"):
        return {"lead_id": i, "first_name": first, "last_name": last, "linkedin_url": f"https://www.linkedin.com/in/{first.lower()}",
                "sent_text": "Hi"}

    def driver(self, inbox, replies, broken=()):
        d = FakeDriver()
        d.inbox = lambda: inbox

        def thread(url, ours):
            d.calls.append(("thread", url))
            if any(b in url for b in broken):
                raise TimeoutError("Locator.inner_text: Timeout 10000ms exceeded.")
            return replies.get(url)
        d.thread_reply = thread
        return d

    def test_likely_reply_first_and_a_broken_profile_does_not_stop_the_check(self):
        jane = one(self.conn, "SELECT id FROM leads WHERE email = 'jane@x.com'")["id"]
        self.conn.execute("UPDATE leads SET stage = 'prospect' WHERE id = ?", (jane,))
        cands = [self.lead(9001, "Ann"), self.lead(9002, "Ben"), {**self.lead(jane, "Jane", "Doe")}]
        d = self.driver([{"name": "Jane Doe", "snippet": "Jane: sounds good", "last_from_me": False}],
                        {"https://www.linkedin.com/in/jane": "sounds good"}, broken=("ann",))
        linkedin_bot.check_replies(self.conn, d, cands, MON, self.logs.append)
        opened = [c[1] for c in d.calls if c[0] == "thread"]
        self.assertEqual(opened[0], "https://www.linkedin.com/in/jane")              # the likely reply first
        self.assertIn("https://www.linkedin.com/in/ben", opened)                       # not stopped by Ann's page
        self.assertEqual(one(self.conn, "SELECT stage FROM leads WHERE id = ?", jane)["stage"], "lead")
        self.assertTrue(any("couldn't open Ann X" in l for l in self.logs))

    def test_others_rotate_and_repeated_failures_stop_early(self):
        cands = [self.lead(9000 + i, f"P{i}") for i in range(10)]
        d = self.driver([], {})
        linkedin_bot.check_replies(self.conn, d, cands, MON, self.logs.append)
        first = [c[1] for c in d.calls if c[0] == "thread"]
        self.assertEqual(len(first), linkedin_bot.REPLY_ROTATE)
        d = self.driver([], {})
        linkedin_bot.check_replies(self.conn, d, cands, MON + timedelta(minutes=10), self.logs.append)
        second = [c[1] for c in d.calls if c[0] == "thread"]
        self.assertFalse(set(first) & set(second))                                     # the next ones in line
        d = self.driver([], {}, broken=("linkedin.com",))
        linkedin_bot.check_replies(self.conn, d, cands, MON + timedelta(minutes=20), self.logs.append)
        self.assertEqual(len([c for c in d.calls if c[0] == "thread"]), linkedin_bot.REPLY_FAILS_IN_A_ROW)
        self.assertIn("stopped early", self.logs[-1])


class AttuneTest(Base):
    """After an out-of-office the prospect stays in the sequence; the next message waits and is adjusted."""
    FOLLOW = "Hi {{ first_name }},\n\nFollowing up on my note."

    def setUp(self):
        super().setUp()
        self.kc = self.campaign([("email", 0, "Hello", "Hi {{ first_name }},\n\nIntro."), ("email", 2, None, self.FOLLOW)],
                                name="ooo")
        importer.import_rows(self.conn, [{"Email": "kim@x.com", "Name": "Kim Lee", "Company": "Acme"}],
                             importer.guess_mapping(["Email", "Name", "Company"]), self.kc)
        self.conn.execute("UPDATE enrollments SET enrolled_at = ?", (iso(MON),))
        self.tick(MON)                                                                 # intro email to Kim

    def kim_mails(self):
        return [s for s in self.g.sent if s[1] == "kim@x.com"]

    def auto(self, text, subject="Automatic reply: Hello"):
        e = one(self.conn, "SELECT e.* FROM enrollments e JOIN leads l ON l.id = e.lead_id WHERE l.email = 'kim@x.com'")
        self.g.inbox.append(mail("kim@x.com", subject, cid=e["conversation_id"], at=MON + timedelta(minutes=5), preview=text))
        self.tick(MON + timedelta(minutes=30))

    def test_waits_until_the_day_after_their_return_then_opens_with_the_line(self):
        self.assertEqual(len(self.kim_mails()), 1)
        self.auto("I am out of the office until 26 October with limited access to email.")
        self.tick(MON + timedelta(days=2, hours=1))                                    # step 2 due: held
        self.assertEqual(len(self.kim_mails()), 1)
        held = one(self.conn, "SELECT detail FROM events WHERE type = 'step_held'")
        self.assertEqual(held["detail"], "out of office: waits until 27 Oct")
        self.tick(datetime(2026, 10, 27, 8, 0, tzinfo=timezone.utc))                   # before the window opens
        self.assertEqual(len(self.kim_mails()), 1)
        self.tick(datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc))
        html = self.kim_mails()[-1][3]
        self.assertLess(html.index("Hi Kim,"), html.index("Hope you had a good break."))
        self.assertLess(html.index("Hope you had a good break."), html.index("Following up"))
        self.assertEqual(one(self.conn, "SELECT stage FROM leads WHERE email = 'kim@x.com'")["stage"], "prospect")

    def test_no_return_date_waits_a_week_and_templates_can_word_it_themselves(self):
        self.conn.execute("UPDATE steps SET body = ? WHERE campaign_id = ? AND pos = 1",
                          ("Hi {{ first_name }},\n\n{% if ooo %}Welcome back.{% endif %} Following up.", self.kc))
        self.auto("I am currently out of the office with limited access to email.")
        self.tick(MON + timedelta(days=6))
        self.assertEqual(len(self.kim_mails()), 1)
        self.tick(MON + timedelta(days=7, hours=1))
        html = self.kim_mails()[-1][3]
        self.assertIn("Welcome back.", html)
        self.assertNotIn("Hope you had a good break", html)

    def test_left_the_company_skips_email_but_keeps_the_prospect(self):
        self.auto("Kim Lee is no longer with Acme. Please contact sales@acme.com.")
        self.tick(MON + timedelta(days=2, hours=1))
        self.assertEqual(len(self.kim_mails()), 1)
        ev = one(self.conn, "SELECT detail FROM events WHERE type = 'step_skipped' AND channel = 'email' ORDER BY id DESC LIMIT 1")
        self.assertIn("left the company", ev["detail"])
        self.assertEqual(one(self.conn, "SELECT stage FROM leads WHERE email = 'kim@x.com'")["stage"], "prospect")

    def test_plain_acknowledgement_changes_nothing_and_opener_can_be_off(self):
        self.auto("Thanks for your email. This is an automated response.", subject="Automatic reply: Hello")
        self.tick(MON + timedelta(days=2, hours=1))
        self.assertEqual(len(self.kim_mails()), 2)
        self.assertNotIn("Hope you had", self.kim_mails()[-1][3])
        ctx = {}
        db.set_setting(self.conn, "ooo_opener", "")
        info = {"kind": "ooo", "back": None, "at": iso(MON)}
        self.assertEqual(engine.attune(self.conn, self.FOLLOW, ctx, info), self.FOLLOW)
        self.assertTrue(ctx["ooo"])
