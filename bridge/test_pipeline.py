import sqlite3
import subprocess
import tempfile
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pipeline

SETTINGS = {
    "max_age_minutes": 60,
    "max_per_hour": 5,
    "title_regex": r"\bintern(ship)?s?\b|\bco-?op\b",
    "location_regex": r"new york|\bnyc\b|\bny\b|brooklyn|manhattan|remote",
    "auto_submit": True,
}
T = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)


def row(title, url, location="New York, NY", status="new"):
    return {"status": status, "company": "Acme", "title": title, "url": url, "location": location}


def empty_ledger():
    return {"seen": {}, "queued": {}, "done": [], "attempts": []}


class SelectFresh(unittest.TestCase):
    def test_filters_title_location_and_status(self):
        rows = [
            row("Software Engineering Intern", "u1"),
            row("Summer 2027 Internship, Data", "u2", "Remote - United States"),
            row("Senior Engineer", "u3"),
            row("Software Intern", "u4", "Austin, TX"),
            row("Software Intern", "u5", status="closed"),
            row("Internal Tools Engineer", "u6"),
        ]
        ledger = empty_ledger()
        got = [r["url"] for r in pipeline.select_fresh(rows, ledger, SETTINGS, T)]
        self.assertEqual(got, ["u1", "u2"])

    def test_posting_is_only_considered_once(self):
        ledger = empty_ledger()
        rows = [row("Software Intern", "u1")]
        self.assertEqual(len(pipeline.select_fresh(rows, ledger, SETTINGS, T)), 1)
        self.assertEqual(len(pipeline.select_fresh(rows, ledger, SETTINGS, T)), 0)

    def test_us_fallback_tier_takes_other_us_cities_only(self):
        rows = [
            row("Software Intern", "u1", "Austin, TX"),
            row("Data Intern", "u2", "San Francisco, CA, United States"),
            row("Software Intern", "u3", "London, UK"),
            row("Software Intern", "u4", "New York, NY"),
            row("Senior Engineer", "u5", "Austin, TX"),
            row("ML Intern", "u6", "Toronto, Canada"),
        ]
        got = pipeline.select_fresh(rows, empty_ledger(), SETTINGS, T, tier="us")
        self.assertEqual([r["url"] for r in got], ["u1", "u2"])
        self.assertTrue(all(r["tier"] == "us" for r in got))

    def test_is_us(self):
        self.assertTrue(pipeline.is_us("Seattle, WA"))
        self.assertTrue(pipeline.is_us("Remote - USA"))
        self.assertFalse(pipeline.is_us("Dublin, Ireland"))
        self.assertFalse(pipeline.is_us("Paris"))


class ApplyRoute(unittest.TestCase):
    def test_workday_needs_prior_success_and_unknown_sites_are_manual(self):
        hist = [{"url": "https://acme.wd5.myworkdayjobs.com/en-US/x/job/a", "outcome": "applied"},
                {"url": "https://globex.wd501.myworkdayjobs.com/en-US/z/job/b", "outcome": "needs you"}]
        self.assertEqual(pipeline.apply_route("https://acme.wd5.myworkdayjobs.com/en-US/x/job/c", hist)[0], "auto")
        self.assertEqual(pipeline.apply_route("https://globex.wd501.myworkdayjobs.com/en-US/z/job/d", hist)[0], "manual")
        self.assertEqual(pipeline.apply_route("https://careers.example.com/job/1", hist)[0], "manual")
        self.assertEqual(pipeline.apply_route("https://jobs.ashbyhq.com/acme/123", hist)[0], "auto")
        self.assertEqual(pipeline.apply_route("https://jobs.lever.co/acme/123", hist)[0], "manual")

    def test_greenhouse_essay_question_blocks(self):
        import io
        def fake(required_textarea):
            body = {"questions": [{"required": True, "label": "Why us?",
                                   "fields": [{"name": "question_1", "type": "textarea" if required_textarea else "input_text"}]}]}
            return lambda api: io.StringIO(json.dumps(body))
        url = "https://job-boards.greenhouse.io/acme/jobs/42"
        self.assertEqual(pipeline.apply_route(url, [], fake(False))[0], "auto")
        route, why = pipeline.apply_route(url, [], fake(True))
        self.assertEqual(route, "manual")
        self.assertIn("Why us?", why)


class PickToApply(unittest.TestCase):
    def test_caps_at_five_per_rolling_hour_newest_first(self):
        ledger = empty_ledger()
        for i in range(8):
            ledger["queued"][f"u{i}"] = (T - timedelta(minutes=30 - i)).isoformat()
        got = pipeline.pick_to_apply(ledger, SETTINGS, T)
        self.assertEqual(got, ["u7", "u6", "u5", "u4", "u3"])

    def test_attempts_in_last_hour_use_up_budget(self):
        ledger = empty_ledger()
        ledger["queued"] = {f"u{i}": (T - timedelta(minutes=5)).isoformat() for i in range(4)}
        ledger["attempts"] = [(T - timedelta(minutes=10)).isoformat()] * 3
        ledger["attempts"].append((T - timedelta(minutes=90)).isoformat())
        self.assertEqual(len(pipeline.pick_to_apply(ledger, SETTINGS, T)), 2)

    def test_skips_stale_and_done(self):
        ledger = empty_ledger()
        ledger["queued"] = {
            "old": (T - timedelta(minutes=61)).isoformat(),
            "fresh": (T - timedelta(minutes=59)).isoformat(),
            "done": (T - timedelta(minutes=1)).isoformat(),
        }
        ledger["done"] = ["done"]
        self.assertEqual(pipeline.pick_to_apply(ledger, SETTINGS, T), ["fresh"])


class Enqueue(unittest.TestCase):
    def test_inserts_once_and_records_queue_time(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE jobs (url TEXT PRIMARY KEY, title TEXT, location TEXT, "
            "site TEXT, strategy TEXT, discovered_at TEXT)"
        )
        ledger = empty_ledger()
        rows = [row("Software Intern", "u1")]
        self.assertEqual(pipeline.enqueue(conn, rows, ledger, T), 1)
        self.assertEqual(pipeline.enqueue(conn, rows, ledger, T), 0)
        self.assertIn("u1", ledger["queued"])


class Alerts(unittest.TestCase):
    def test_same_problem_alerts_once_per_cooldown(self):
        ledger = pipeline.load_ledger(pipeline.ROOT / "does-not-exist.json")
        self.assertTrue(pipeline.should_alert(ledger, "k", T))
        self.assertFalse(pipeline.should_alert(ledger, "k", T + timedelta(hours=5)))
        self.assertTrue(pipeline.should_alert(ledger, "k", T + timedelta(hours=7)))

    def test_different_problems_do_not_block_each_other(self):
        ledger = pipeline.load_ledger(pipeline.ROOT / "does-not-exist.json")
        self.assertTrue(pipeline.should_alert(ledger, "a", T))
        self.assertTrue(pipeline.should_alert(ledger, "b", T))


class ClickActions(unittest.TestCase):
    def test_commands_per_kind(self):
        import notify
        self.assertEqual(notify.action_command("file", "/x/.env"), ["open", "-e", "/x/.env"])
        self.assertEqual(notify.action_command("url", "https://a.b"), ["open", "https://a.b"])
        term = notify.action_command("terminal", 'cd "x" && run')
        self.assertIn('do script "cd \\"x\\" && run"', term[-1])
        with self.assertRaises(ValueError):
            notify.action_command("nope", "")


class ApplyOutcome(unittest.TestCase):
    def test_reads_status_and_error(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "a.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE jobs (url TEXT PRIMARY KEY, title TEXT, apply_status TEXT, "
                         "apply_error TEXT, tailored_resume_path TEXT)")
            conn.execute("INSERT INTO jobs VALUES ('u1', 'Intern', 'failed', 'captcha', '/r.pdf')")
            conn.execute("INSERT INTO jobs VALUES ('u2', 'Intern 2', NULL, NULL, NULL)")
            conn.commit()
            conn.close()
            self.assertEqual(pipeline.apply_outcome("u1", db), ("failed", "captcha", "Intern", True))
            self.assertEqual(pipeline.apply_outcome("u2", db), (None, None, "Intern 2", False))
            self.assertEqual(pipeline.apply_outcome("zzz", db), (None, None, None, False))


class HourlyRules(unittest.TestCase):
    def test_posted_time_filter(self):
        def at(minutes_ago):
            return (T - timedelta(minutes=minutes_ago)).isoformat()
        self.assertTrue(pipeline.posted_recently({"posted_at": at(30)}, SETTINGS, T))
        self.assertTrue(pipeline.posted_recently({"posted_at": at(69)}, SETTINGS, T))
        self.assertFalse(pipeline.posted_recently({"posted_at": at(71)}, SETTINGS, T))
        self.assertFalse(pipeline.posted_recently({"posted_at": "2026-09-03T13:30:34-04:00"}, SETTINGS, T))
        self.assertTrue(pipeline.posted_recently({"posted_at": "2026-11-03T14:30:00Z"}, SETTINGS, T))
        self.assertTrue(pipeline.posted_recently({"posted_at": ""}, SETTINGS, T))
        self.assertTrue(pipeline.posted_recently({}, SETTINGS, T))
        self.assertTrue(pipeline.posted_recently({"posted_at": "not a date"}, SETTINGS, T))

    def test_old_postings_are_recorded_but_not_selected(self):
        old = dict(row("Software Intern", "u1"), posted_at="2026-09-03T13:30:34-04:00")
        new = dict(row("Data Intern", "u2"), posted_at=(T - timedelta(minutes=20)).isoformat())
        ledger = empty_ledger()
        got = pipeline.select_fresh([old, new], ledger, SETTINGS, T)
        self.assertEqual([r["url"] for r in got], ["u2"])
        self.assertIn("u1", ledger["seen"])

    def test_scan_runs_once_per_clock_hour(self):
        ledger = empty_ledger()
        self.assertTrue(pipeline.scan_due(ledger, T))
        ledger["last_slot"] = pipeline.hour_slot(T)
        self.assertFalse(pipeline.scan_due(ledger, T + timedelta(minutes=5)))
        self.assertTrue(pipeline.scan_due(ledger, T + timedelta(hours=1)))


class Learn(unittest.TestCase):
    def test_only_questions_empty_at_start_and_answered_later_are_saved(self):
        import learn
        baseline = learn.clean_snapshot([
            {"q": "First Name *", "v": "Alex"},
            {"q": "Are you a veteran? Select One Required", "v": "Select One"},
            {"q": "How many years of Java?", "v": ""},
            {"q": "Create Password", "v": ""},
        ])
        seen = learn.clean_snapshot([
            {"q": "First Name *", "v": "Alex"},
            {"q": "Are you a veteran? Select One Required", "v": "No"},
            {"q": "How many years of Java?", "v": "2"},
            {"q": "Create Password", "v": "hunter2"},
        ])
        self.assertEqual(learn.new_answers(baseline, seen),
                         {"Are you a veteran?": "No", "How many years of Java?": "2"})

    def test_merge_overwrites_same_question_and_keeps_others(self):
        import json
        import tempfile
        from pathlib import Path
        import learn
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "learned.json"
            self.assertEqual(learn.merge(path, {"Do you need sponsorship?": "No"}, "Globex", "u1"), 1)
            learn.merge(path, {"Do you need sponsorship?": "Yes", "Notice period": "2 weeks"}, "Initech", "u2")
            saved = json.loads(path.read_text())
            self.assertEqual(saved["do you need sponsorship?"]["answer"], "Yes")
            self.assertEqual(len(saved), 2)


class OpenWindow(unittest.TestCase):
    def write(self, d, pid, age_s):
        import json
        import time
        from pathlib import Path
        path = Path(d) / "kept_chrome.json"
        path.write_text(json.dumps({"pid": pid, "url": "u1", "reason": "x", "at": time.time() - age_s}))
        return path

    def test_dead_window_is_ignored_and_cleared(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = self.write(d, 2**22 + 12345, 0)
            self.assertIsNone(pipeline.kept_window(path))
            self.assertFalse(path.exists())

    def test_young_window_holds_applying_old_one_is_closed(self):
        import os
        import subprocess
        import tempfile
        proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
        try:
            with tempfile.TemporaryDirectory() as d:
                young = self.write(d, proc.pid, 60)
                self.assertTrue(pipeline.hold_for_open_window({"keep_window_minutes": 20}, young))
                old = self.write(d, proc.pid, 21 * 60)
                self.assertFalse(pipeline.hold_for_open_window({"keep_window_minutes": 20}, old))
                self.assertIsNotNone(proc.wait(timeout=5))
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_window_click_action(self):
        import notify
        cmd = notify.action_command("window", "1234")
        self.assertIn("unix id is 1234", cmd[-1])


class Report(unittest.TestCase):
    def test_records_then_renders_escaped_html(self):
        import tempfile
        from pathlib import Path
        import report
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "apps.jsonl"
            meta = {"company": "A&B", "title": "<b>Intern</b>", "location": "New York, NY"}
            pipeline.record_application("https://x.test/1", meta, "applied", "", path)
            pipeline.record_application("https://x.test/2", meta, "needs you", "captcha", path)
            entries = report.load(path)
            self.assertEqual(report.counts(entries)["applied"], 1)
            page = report.render(entries, "T", "S")
            self.assertIn("A&amp;B", page)
            self.assertNotIn("<b>Intern</b>", page)
            self.assertIn("captcha", page)

    def test_empty_report_renders(self):
        import report
        self.assertIn("Nothing yet", report.render([], "T", "S"))


class ApplyReadiness(unittest.TestCase):
    def make_db(self, d):
        from pathlib import Path
        db = Path(d) / "a.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE jobs (url TEXT PRIMARY KEY, fit_score INTEGER, scored_at TEXT, "
                     "score_reasoning TEXT, tailored_resume_path TEXT, apply_status TEXT, apply_error TEXT)")
        conn.executemany("INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, NULL)", [
            ("err", 0, "t", "LLM error: 429", None, None),
            ("low", 0, "t", "not relevant", None, None),
            ("ready", 8, "t", "good", "/r.pdf", None),
        ])
        conn.commit()
        conn.close()
        return db

    def test_only_llm_errors_are_requeued_and_capped(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            db = self.make_db(d)
            ledger = empty_ledger()
            self.assertEqual(pipeline.reset_failed_scores(["err", "low", "ready"], ledger, db), 1)
            self.assertEqual(pipeline.job_state("err", db), (False, None))
            self.assertEqual(pipeline.job_state("low", db), (False, 0))
            self.assertEqual(ledger["score_retries"]["err"], 1)
            conn = sqlite3.connect(db)
            conn.execute("UPDATE jobs SET fit_score = 0 WHERE url = 'err'")
            conn.commit()
            conn.close()
            ledger["score_retries"]["err"] = 3
            self.assertEqual(pipeline.reset_failed_scores(["err"], ledger, db), 0)

    def test_fresh_job_gets_a_status_apply_url_can_match(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            db = self.make_db(d)
            pipeline.mark_ready_for_apply("ready", db)
            pipeline.mark_ready_for_apply("low", db)
            conn = sqlite3.connect(db)
            rows = dict(conn.execute("SELECT url, apply_status FROM jobs"))
            conn.close()
            self.assertEqual(rows["ready"], "pending")
            self.assertIsNone(rows["low"])

    def test_failed_applications_can_be_requeued(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            db = self.make_db(d)
            conn = sqlite3.connect(db)
            conn.execute("UPDATE jobs SET apply_status = 'failed' WHERE url = 'ready'")
            conn.execute("UPDATE jobs SET apply_status = 'applied' WHERE url = 'low'")
            conn.commit()
            conn.close()
            ledger = empty_ledger()
            ledger["done"] = ["ready", "low"]
            self.assertEqual(pipeline.requeue_failed(ledger, T, db), ["ready"])
            self.assertEqual(ledger["done"], ["low"])
            self.assertEqual(ledger["queued"]["ready"], T.isoformat())

    def test_budget_counts_only_last_hour(self):
        ledger = empty_ledger()
        ledger["attempts"] = [(T - timedelta(minutes=10)).isoformat()] * 2 + [(T - timedelta(hours=2)).isoformat()]
        self.assertEqual(pipeline.hourly_budget(ledger, SETTINGS, T), 3)


SAMPLE_ISSUE = """
<html><body><p>Nov 3, 2026</p>
<h1>Daily Drops</h1>
<h3>Acme SWE Internship</h3>
<p>Cohort: Internship</p><p>Acme is hiring in New York, NY and Austin, TX.</p>
<p><a href="https://acme.test/ny?utm_source=x&gh_jid=1">Apply to Acme (New York) &rarr;</a></p>
<p><a href="https://acme.test/atx?utm_source=x&gh_jid=2">Apply to Acme (Austin) &rarr;</a></p>
<h2>Beta Remote Role</h2>
<p>Cohort: Internship &amp; New Grad<br>A paid role, remote from the USA.</p>
<p><a href="https://beta.test/job?utm_source=x">Apply to Beta &rarr;</a></p>
<h3>Gamma PhD Research Intern</h3>
<p>Cohort: PhD</p><p>Remote.</p><p><a href="https://gamma.test/1">Apply to Gamma</a></p>
<h3>Delta Internship</h3>
<p>Cohort: Internship</p><p>Includes remote work in Virginia, Ohio.</p>
<p><a href="https://delta.test/1">Apply to Delta</a></p>
<h3>Omega Analyst Internships</h3>
<p>Cohort: Internship</p><p>Omega posted analyst internships, both based in New York City.</p>
<p><a href="https://omega.test/st">Apply to Omega (Sales &amp; Trading) &rarr;</a></p>
<p><a href="https://omega.test/er">Apply to Omega (Research) &rarr;</a></p>
<h3>Keep Reading</h3><p>no links here</p>
</body></html>
"""


class Newsletter(unittest.TestCase):
    def test_parse_and_select(self):
        import newsletter
        date, items = newsletter.parse_issue(SAMPLE_ISSUE)
        self.assertEqual(date, "Nov 3, 2026")
        self.assertEqual([i["title"] for i in items],
                         ["Acme SWE Internship", "Beta Remote Role", "Gamma PhD Research Intern", "Delta Internship",
                          "Omega Analyst Internships"])
        self.assertEqual(items[1]["cohort"], "Internship & New Grad")
        ledger = {"seen": {}}
        rows = newsletter.select(items, ledger, T)
        # Beta is "Internship & New Grad", so it is not an internship-only role.
        self.assertEqual([(r["company"], r["url"]) for r in rows],
                         [("Acme", "https://acme.test/ny?gh_jid=1"),
                          ("Omega", "https://omega.test/st"), ("Omega", "https://omega.test/er")])
        self.assertEqual(newsletter.select(items, ledger, T), [])

    def test_location_rules(self):
        import newsletter
        self.assertTrue(newsletter.location_ok("job/New-York-New-York/Intern"))
        self.assertTrue(newsletter.location_ok("job/Remote/AI-Intern"))
        self.assertFalse(newsletter.location_ok("Philadelphia-Pennsylvania"))
        self.assertFalse(newsletter.location_ok("remote work in Virginia, Ohio"))


class LatexResume(unittest.TestCase):
    def test_escape_and_layout(self):
        from applypilot.scoring import latex_resume as lr
        self.assertEqual(lr.esc("R&D 50% #1 a_b"), r"R\&D 50\% \#1 a\_b")
        self.assertEqual(lr.esc("Mar 2026 - Present"), "Mar 2026 -- Present")
        text = ("Jo Ex\nSWE Intern\na@b.c | 123 | https://github.com/jo\n\nEXPERIENCE\n"
                "Dev at Acme & Co\nPython | Jan 2026 - Present\n- Built 100% of it\n\nEDUCATION\nState University | B.A.\n")
        self.assertTrue(lr.is_resume(text))
        tex = lr.build_latex(text)
        self.assertIn(r"\resumeSubheading{Acme \& Co}{Jan 2026 -- Present}{Dev}", tex)
        self.assertIn(r"\resumeItem{Built 100\% of it}", tex)
        self.assertIn("github.com/jo", tex)
        self.assertNotIn("https://", tex)
        self.assertFalse(lr.is_resume("Dear hiring manager,\n\nHello"))


class SyncExperience(unittest.TestCase):
    def test_replace_entry_swaps_only_its_block(self):
        import sync_experience as se
        resume = "EXPERIENCE\nAcme — Intern — 2026\n- old a\n- old b\n\nOther — Dev\n- keep\n"
        out = se.replace_entry(resume, "Acme — Intern — 2026", ["- new a"])
        self.assertEqual(out, "EXPERIENCE\nAcme — Intern — 2026\n- new a\n\nOther — Dev\n- keep\n")

    def test_commits_filtered_by_author_and_fingerprint_changes(self):
        import sync_experience as se
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "r1"
            repo.mkdir()
            def git(*a, **kw):
                subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, **kw)
            git("init")
            for who, msg in (("alice", "mine one"), ("bob", "theirs")):
                git("-c", f"user.name={who}", "-c", "user.email=x@y.z", "commit", "--allow-empty", "-m", msg)
            (Path(d) / "notgit").mkdir()
            got = se.commit_subjects(Path(d), "alice", "2020-01-01")
            self.assertEqual(got, {"r1": ["mine one"]})
            before = se.fingerprint(got)
            git("-c", "user.name=alice", "-c", "user.email=x@y.z", "commit", "--allow-empty", "-m", "mine two")
            self.assertNotEqual(se.fingerprint(se.commit_subjects(Path(d), "alice", "2020-01-01")), before)


if __name__ == "__main__":
    unittest.main()


class Priority(unittest.TestCase):
    def test_new_york_first(self):
        self.assertEqual(pipeline.priority({"location": "New York, NY"}), 0)
        self.assertEqual(pipeline.priority({"location": "Washington, DC"}), 1)
        self.assertEqual(pipeline.priority({"location": "Austin, TX", "tier": "us"}), 2)
        self.assertLess(pipeline.priority({"location": "Brooklyn, NY"}), pipeline.priority({"location": "Remote"}))
