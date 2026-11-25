import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import sync_projects as sp

RESUME = """PROJECTS
Demo App — Python — Jul 2026 – Aug 2026
- Built a demo app
- Wrote 2 tests

Other — C — Jun 2026
- Left alone

TECHNICAL SKILLS
"""


class SyncProjectsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.folder = self.tmp / "demo"
        self.folder.mkdir()
        (self.folder / "app.py").write_text("print('hi')\n")
        (self.folder / ".env").write_text("KEY=1")
        self.resume = self.tmp / "resume.txt"
        self.resume.write_text(RESUME)
        patches = {"STATE": self.tmp / "s.json", "BACKUPS": self.tmp / "b", "CHANGES": self.tmp / "c.log",
                   "RESUME": self.resume}
        for k, v in patches.items():
            p = mock.patch.object(sp, k, v)
            p.start()
            self.addCleanup(p.stop)
        self.settings = {"experience_projects": [{"entry": "Demo App", "dir": str(self.folder), "include": ["*"]}],
                         "experience_min_hours": 0}

    def change(self, body, age=3600):
        f = self.folder / "app.py"
        f.write_text(body)
        os.utime(f, (time.time() - age,) * 2)

    def run_sync(self, reply):
        return sp.sync(self.settings, log=lambda m: None, ask=lambda p, m: reply)

    def test_first_look_only_records(self):
        self.assertEqual(self.run_sync("- X\n- Y"), [])
        self.assertEqual(self.resume.read_text(), RESUME)

    def test_change_rewrites_only_that_entry(self):
        self.run_sync("")
        self.change("def test_a(): pass\n")
        self.assertEqual(self.run_sync("- Built a demo app with a command line\n- Wrote 2 tests"), ["Demo App"])
        text = self.resume.read_text()
        self.assertIn("command line", text)
        self.assertIn("Other — C — Jun 2026\n- Left alone\n\nTECHNICAL SKILLS", text)
        self.assertNotIn("– Aug 2026", text)

    def test_invented_number_is_rejected(self):
        self.run_sync("")
        self.change("x = 1\n")
        self.assertEqual(self.run_sync("- Served 5000 users\n- Wrote 2 tests"), [])
        self.assertEqual(self.resume.read_text(), RESUME)

    def test_recent_edit_waits(self):
        self.run_sync("")
        self.change("x = 2\n", age=0)
        self.assertEqual(self.run_sync("- A\n- B"), [])

    def test_unchanged_reply_and_secrets(self):
        self.run_sync("")
        self.change("x = 3\n")
        self.assertEqual(self.run_sync("UNCHANGED"), [])
        self.assertNotIn(".env", [p.name for p in sp.source_files(self.settings["experience_projects"][0])])


if __name__ == "__main__":
    unittest.main()
