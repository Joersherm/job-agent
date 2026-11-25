import json
import re
import unittest
from unittest import mock

import yaml

import find_companies as fc  # also puts vendor/roledar on the path
import onboard as ob

NURSE = [{"title": "Registered Nurse", "also": ["RN"]}]


class Roles(unittest.TestCase):
    def match(self, levels, title):
        return bool(re.search(ob.title_regex(levels, ["Registered Nurse", "RN"]), title, re.I))

    def test_level_and_role_must_both_match(self):
        self.assertTrue(self.match(["1"], "Registered Nurse Intern"))
        self.assertFalse(self.match(["1"], "Registered Nurse"))
        self.assertFalse(self.match(["1"], "Marketing Intern"))
        self.assertTrue(self.match(["2"], "Junior RN"))
        self.assertTrue(self.match(["2"], "Entry Level Staff Registered Nurse"))
        self.assertFalse(self.match(["2"], "Associate Director, Registered Nurses"))
        self.assertTrue(self.match(["4"], "Registered Nurses - Night Shift"))
        self.assertFalse(self.match(["4"], "Intern, Burns Research"))

    def test_short_aliases_need_capitals(self):
        rx = ob.title_regex(["4"], ["OR", "MA", "Registered Nurse"])
        self.assertTrue(re.search(rx, "OR Nurse", re.I))
        self.assertFalse(re.search(rx, "Dentist or Hygienist", re.I))
        self.assertFalse(re.search(rx, "Warehouse Associate - Boston, MA", re.I))

    def test_typed_answers(self):
        self.assertEqual(ob.choose_roles("1-3", NURSE), [])
        self.assertEqual([r["title"] for r in ob.choose_roles("Registered Nurse, Midwife", [])],
                         ["Registered Nurse", "Midwife"])
        self.assertEqual(ob.choose_roles("3D Artist", [])[0]["title"], "3D Artist")

    def test_search_terms(self):
        self.assertEqual(ob.job_settings("nursing", ["1"], NURSE)["search_queries"], [])
        got = ob.job_settings("nursing", ["1", "2"], NURSE)
        self.assertEqual(got["search_queries"], ["intern", "Registered Nurse"])
        self.assertTrue(re.search(got["page_regex"], "RN", re.I))

    def test_choosing(self):
        two = NURSE + [{"title": "Nurse Practitioner", "also": []}]
        self.assertEqual(ob.choose_roles("all", two), two)
        self.assertEqual(ob.choose_roles("2, 9", two), two[1:])
        self.assertEqual(ob.choose_roles("Midwife; Doula", two)[1]["title"], "Doula")

    def test_list_is_found_inside_chatty_replies(self):
        reply = 'Found [two]:\n```json\n[{"a": 1}, {"a": 2}]\n```\nSources: [site](https://x.test) [1]'
        self.assertEqual(ob.json_list(reply), [{"a": 1}, {"a": 2}])
        self.assertEqual(ob.json_list("nothing here ["), [])

    def test_model_output_is_cleaned(self):
        self.assertEqual(ob.clean_roles([{"title": "A|B"}, "x", {"title": "Chef", "also": "no"}]),
                         [{"title": "Chef", "also": []}])


class Locations(unittest.TestCase):
    def match(self, place, location):
        return bool(re.search(ob.place_regex(place), location, re.I))

    def test_states_countries_and_codes(self):
        self.assertTrue(self.match("United States", "Austin, TX"))
        self.assertTrue(self.match("California", "San Jose, CA"))
        self.assertTrue(self.match("CA", "San Jose, California"))
        self.assertFalse(self.match("CA", "Remote - Canada"))
        self.assertFalse(self.match("IN", "Hybrid in London"))
        self.assertFalse(self.match("New York", "Buffalo, NY"))

    def test_foreign_remote_is_excluded_for_the_us(self):
        got = yaml.safe_load(ob.locations_yaml([], True))
        self.assertTrue(any(re.search(p, "Remote - India (US shift)") for p in got["exclude"]))
        self.assertFalse(any(re.search(p, "Remote - Indiana, US") for p in got["exclude"]))
        self.assertTrue(any(re.search(p, "US - Remote") for p in got["allow"]["remote_patterns"]))

    def test_yaml(self):
        got = yaml.safe_load(ob.locations_yaml(["London", "Washington, D.C."], True, "United Kingdom"))
        self.assertEqual(got["allow"]["cities"], [])
        self.assertTrue(re.search(got["allow"]["regions"][1], "Washington, D.C."))
        self.assertTrue(any(re.search(p, "Remote - United Kingdom") for p in got["allow"]["remote_patterns"]))
        self.assertFalse(any(re.search(p, "Remote - Canada") for p in got["allow"]["remote_patterns"]))
        self.assertEqual(yaml.safe_load(ob.locations_yaml([], True, ""))["allow"]["remote_patterns"], ["Remote"])


class Companies(unittest.TestCase):
    def test_bad_ids_and_addresses_are_ignored(self):
        got = fc.candidates({"name": "Acme", "slug": "../../x?y", "workday_url": "http://evil.test/acme"})
        self.assertTrue(all(list(cfg.values()) == ["acme"] for _, cfg in got))

    def test_workday_address(self):
        got = fc.candidates({"name": "Acme", "workday_url": "https://acme.wd5.myworkdayjobs.com/en-US/Careers"})
        self.assertEqual(got[0], ("workday", {"host": "acme.wd5.myworkdayjobs.com", "tenant": "acme",
                                              "site": "Careers"}))

    def test_only_boards_with_jobs_verify(self):
        class Empty:
            def __init__(self, *a):
                pass

            def fetch(self):
                return []
        self.assertIsNone(fc.verify({"name": "Acme"}, {b: Empty for b in fc.SLUG_BOARDS}))

    def test_private_addresses_are_refused(self):
        for url in ("http://example.com/jobs", "https://localhost/jobs", "https://127.0.0.1/jobs",
                    "https://192.168.1.1/jobs", "file:///etc/passwd"):
            self.assertFalse(fc.public_url(url), url)

    def test_career_page_kept_only_when_it_reads_jobs_with_locations(self):
        from scrapers.base import JobPosting

        def reader(location):
            class Fake:
                def __init__(self, name, config):
                    self.config = config

                def render(self, url):
                    return '<a class="job" href="/job/1">Nurse</a>'

                def fetch(self):
                    return [JobPosting("Acme", t, f"https://acme.test/{t}", location) for t in ("Nurse", "Cook")]
            return Fake
        company = {"name": "Acme", "careers_url": "https://acme.test/jobs"}
        ask = lambda prompt: [{"link_selector": "a.job", "evil": "x"}]
        with mock.patch.object(fc, "public_url", return_value=True):
            got = fc.read_career_page(company, reader("Boston, MA"), "Nurse", ask)
            self.assertEqual((got["matching"], got["ats_config"]["link_selector"]), (1, "a.job"))
            self.assertNotIn("evil", got["ats_config"])
            self.assertEqual((got["ats_config"]["hosts"], got["board_name"]), (["acme.test"], ""))
            self.assertIsNone(fc.read_career_page(company, reader(""), "Nurse", ask))

    def test_merge(self):
        new = [{"name": n, "ats": "lever", "ats_config": {"slug": n}} for n in ("Old", "New")]
        old = [{"name": "old", "ats": "skip"}]
        self.assertEqual([c["name"] for c in fc.merge(old, new, True)], ["old", "New"])
        self.assertEqual([c["name"] for c in fc.merge(old, new, False)], ["Old", "New"])


if __name__ == "__main__":
    unittest.main()
