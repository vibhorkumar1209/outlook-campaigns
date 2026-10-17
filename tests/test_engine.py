"""Orchestration logic against a fake Graph. Run: .venv/bin/python -m unittest discover tests"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from campaigner import db, engine, importer, stats
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

    def send_new(self, to, name, subject, html):
        self.sent.append(("new", to, subject, html))
        return f"conv-{self.me}-{len(self.sent)}"

    def send_in_thread(self, cid, to, name, html, fallback, strip_pixels=None):
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


class RenderTest(unittest.TestCase):
    def test_links(self):
        h = email_html("Read [the guide](https://a.com/x?y=1&z=2), or https://b.com.", "", {})
        self.assertIn('<a href="https://a.com/x?y=1&amp;z=2">the guide</a>', h)
        self.assertIn('<a href="https://b.com">https://b.com</a>.', h)
        self.assertIn('&lt;b&gt;', email_html("{{ x }}", "", {"x": "<b>"}))


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
