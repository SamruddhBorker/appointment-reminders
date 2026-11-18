import csv
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import engine
import remind
import senders
from store import Store

CFG = json.loads((Path(__file__).parent / "config.json").read_text(encoding="utf-8"))


def at(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M")


def make(start="2026-11-20 15:30", status="booked", now="2026-11-19 10:00"):
    s = Store()
    aid = s.add("Priya Naik", "919876543210", "p@example.com", "Checkup", at(start), now=at(now))
    if status != "booked":
        s.set_status(aid, status)
    return s, aid


class Collector:
    def __init__(self, ok=True):
        self.sent, self.ok = [], ok

    def __call__(self, channel, appt, text):
        self.sent.append((channel, appt["id"], text))
        return self.ok, "test"


class ParsingTests(unittest.TestCase):
    def test_phone_normalisation(self):
        n = engine.normalize_phone
        self.assertEqual(n("98765 43210"), "919876543210")
        self.assertEqual(n("+91 98765-43210"), "919876543210")
        self.assertEqual(n("09876543210"), "919876543210")
        self.assertEqual(n("919876543210"), "919876543210")
        self.assertEqual(n("0044 7911 123456"), "447911123456")
        self.assertIsNone(n("12345"))
        self.assertIsNone(n(""))
        self.assertIsNone(n(None))

    def test_email_and_clean_text(self):
        self.assertTrue(engine.valid_email("a@b.co"))
        self.assertFalse(engine.valid_email("not an email"))
        self.assertEqual(engine.clean_text("Sam\r\nBcc: x@y.z"), "Sam  Bcc: x@y.z")

    def test_parse_dt(self):
        self.assertEqual(engine.parse_dt("2026-11-20 15:30"), at("2026-11-20 15:30"))
        self.assertEqual(engine.parse_dt("2026-11-20T15:30"), at("2026-11-20 15:30"))
        with self.assertRaises(ValueError):
            engine.parse_dt("tomorrow")

    def test_template_cannot_execute_code(self):
        out = engine.render("Hi $first_name {__class__} ${name.__class__}", {"first_name": "Sam", "name": "X"})
        self.assertIn("Sam", out)
        self.assertNotIn("class 'str'", out)

    def test_quiet_hours_cross_midnight(self):
        self.assertTrue(engine.in_quiet_hours(at("2026-11-19 22:00"), CFG))
        self.assertTrue(engine.in_quiet_hours(at("2026-11-19 07:59"), CFG))
        self.assertFalse(engine.in_quiet_hours(at("2026-11-19 08:00"), CFG))
        self.assertFalse(engine.in_quiet_hours(at("2026-11-19 20:59"), CFG))


class SchedulingTests(unittest.TestCase):
    def test_nothing_due_too_early(self):
        s, _ = make()
        c = Collector()
        engine.run(s, CFG, at("2026-11-19 10:00"), c)   # 29.5h before
        self.assertEqual(c.sent, [])

    def test_day_before_sent_once(self):
        s, aid = make()
        c = Collector()
        res = engine.run(s, CFG, at("2026-11-19 15:30"), c)  # exactly 24h before
        self.assertEqual(res["sent"], 1)
        self.assertIn("tomorrow", c.sent[0][2])
        engine.run(s, CFG, at("2026-11-19 16:00"), c)
        self.assertEqual(len(c.sent), 1, "must not send the same reminder twice")

    def test_two_hour_reminder_after_day_before(self):
        s, _ = make()
        c = Collector()
        engine.run(s, CFG, at("2026-11-19 15:30"), c)
        engine.run(s, CFG, at("2026-11-20 13:30"), c)
        self.assertEqual(len(c.sent), 2)
        self.assertIn("today", c.sent[1][2])

    def test_late_booking_gets_only_nearest_reminder(self):
        s, aid = make(start="2026-11-20 15:30", now="2026-11-20 12:00")
        c = Collector()
        res = engine.run(s, CFG, at("2026-11-20 12:30"), c)  # 3h before: day_before window open, two_hours not yet
        self.assertEqual([x[2] for x in c.sent].__len__(), 1)
        self.assertIn("today", c.sent[0][2])  # when = today even for day_before template
        # now the 2h window opens: day_before is done, so two_hours goes out too
        engine.run(s, CFG, at("2026-11-20 13:30"), c)
        self.assertEqual(len(c.sent), 2)

    def test_missed_window_is_skipped_not_sent(self):
        s, aid = make(start="2026-11-20 15:30")
        c = Collector()
        res = engine.run(s, CFG, at("2026-11-20 14:00"), c)   # both windows open; only nearest sends
        self.assertEqual(res["sent"], 1)
        self.assertEqual(res["skipped"], 1)
        self.assertIn("see you", c.sent[0][2].lower())

    def test_quiet_hours_defer_then_send(self):
        s, _ = make(start="2026-11-20 08:30")
        c = Collector()
        engine.run(s, CFG, at("2026-11-20 06:30"), c)   # 2h before but quiet hours
        self.assertEqual(c.sent, [])
        engine.run(s, CFG, at("2026-11-20 08:00"), c)   # quiet hours over, still 30 min left
        self.assertEqual(len(c.sent), 1)

    def test_no_reminder_too_close_to_start(self):
        s, _ = make(start="2026-11-20 15:30")
        c = Collector()
        res = engine.run(s, CFG, at("2026-11-20 15:25"), c)
        self.assertEqual(c.sent, [])
        self.assertEqual(res["skipped"], 2)

    def test_cancelled_and_past_never_reminded(self):
        s, _ = make(status="cancelled")
        c = Collector()
        engine.run(s, CFG, at("2026-11-20 14:00"), c)
        self.assertEqual(c.sent, [])
        s2, _ = make(start="2026-11-19 09:00")
        engine.run(s2, CFG, at("2026-11-19 12:00"), c)
        self.assertEqual(c.sent, [])

    def test_failed_send_is_retried(self):
        s, aid = make()
        bad = Collector(ok=False)
        res = engine.run(s, CFG, at("2026-11-19 16:00"), bad)
        self.assertEqual(res["failed"], 1)
        good = Collector()
        res = engine.run(s, CFG, at("2026-11-19 16:15"), good)
        self.assertEqual(res["sent"], 1)

    def test_channel_fallback(self):
        cfg = dict(CFG, channels=["whatsapp", "email"])
        s, _ = make()
        calls = []

        def send(ch, appt, text):
            calls.append(ch)
            return (ch == "email"), ch
        engine.run(s, cfg, at("2026-11-19 16:00"), send)
        self.assertEqual(calls, ["whatsapp", "email"])

    def test_dry_run_changes_nothing(self):
        s, aid = make()
        c = Collector()
        res = engine.run(s, CFG, at("2026-11-19 16:00"), c, dry_run=True)
        self.assertEqual(len(res["preview"]), 1)
        self.assertEqual(c.sent, [])
        self.assertEqual(s.reminders_for(aid), [])


class ReportTests(unittest.TestCase):
    def test_no_show_rates_split_by_reminder(self):
        s = Store()
        for i in range(4):
            aid = s.add(f"A{i}", "919876543210", None, "Checkup", at("2026-11-10 10:00"))
            s.record(aid, "day_before", "sent", "console", at("2026-11-09 10:00"))
            s.set_status(aid, "attended" if i else "no_show")
        for i in range(2):
            aid = s.add(f"B{i}", "919876543210", None, "Checkup", at("2026-11-11 10:00"))
            s.set_status(aid, "no_show")
        rep = engine.report(s)
        self.assertEqual(rep["reminded"], {"n": 4, "no_show": 1})
        self.assertEqual(rep["not_reminded"], {"n": 2, "no_show": 2})


class CliTests(unittest.TestCase):
    def test_add_list_mark_import(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / "a.db")
            base = ["--db", db]
            self.assertEqual(remind.main(base + ["add", "--name", "Sam", "--phone", "98765", "43210",
                                                  "--service", "Cut", "--at", "2099-01-01 10:00"]), 0)
            self.assertEqual(remind.main(base + ["add", "--name", "Bad", "--at", "2099-01-01 10:00"]), 1)
            self.assertEqual(remind.main(base + ["mark", "1", "attended"]), 0)
            f = Path(d) / "c.csv"
            with open(f, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["name", "phone", "email", "service", "start"])
                w.writerow(["Ok One", "9876543211", "", "Cut", "2099-01-02 11:00"])
                w.writerow(["Bad Date", "9876543212", "", "Cut", "nonsense"])
                w.writerow(["No Contact", "", "", "Cut", "2099-01-02 12:00"])
            self.assertEqual(remind.main(base + ["import", str(f)]), 0)
            s = Store(db)
            self.assertEqual(len(s.all()), 2)  # Sam + Ok One
            s.close()

    def test_name_with_newline_is_sanitised(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / "a.db")
            remind.main(["--db", db, "add", "--name", "Eve\nBcc: x@y.z", "--phone", "9876543210",
                         "--at", "2099-01-01 10:00"])
            s = Store(db)
            self.assertNotIn("\n", s.get(1)["name"])
            s.close()


class SenderTests(unittest.TestCase):
    def test_email_needs_credentials(self):
        appt = {"email": "a@b.co", "name": "x", "phone": None}
        with mock.patch.dict("os.environ", {}, clear=True):
            ok, detail = senders.send("email", appt, "hi", CFG)
        self.assertFalse(ok)
        self.assertIn("SMTP", detail)

    def test_whatsapp_rejects_bad_phone(self):
        ok, detail = senders.send("whatsapp", {"phone": "123", "name": "x", "email": None}, "hi", CFG)
        self.assertFalse(ok)

    def test_unknown_channel(self):
        self.assertFalse(senders.send("pigeon", {"name": "x"}, "hi", CFG)[0])


if __name__ == "__main__":
    unittest.main()
