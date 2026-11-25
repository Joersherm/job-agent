"""Company-specific subclasses of SiteScraper, for sites the generic scraper cannot read on its own.

Each one overrides only what differs: load() (how to get page n) and parse() (rows from it).
Paging, de-duplication, JobPosting building and error handling come from SiteScraper.
Use them from companies.yaml either as `ats: amazon` (and so on) or `ats: site` + `impl: amazon`.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone

import requests

from .base import ScrapeError
from .site import SITE_IMPLS, UA, GenericSiteScraper, SiteScraper, iso_day
from .site import _tab as site_tab


class AmazonScraper(SiteScraper):
    ATS_NAME = "amazon"
    MAX_PAGES = 6

    def load(self, n: int) -> str:
        data = self.get_json("https://www.amazon.jobs/en/search.json", params={
            "base_query": self.config.get("query", "intern"), "result_limit": 100, "offset": n * 100,
            "sort": "recent", "country[]": "USA"})
        return json.dumps(data)

    def parse(self, raw: str, n: int) -> list[dict]:
        return [{"title": j["title"], "url": "https://www.amazon.jobs" + j["job_path"],
                 "location": j.get("normalized_location") or j.get("location", ""),
                 "department": j.get("job_category", ""), "posted_at": iso_day(j.get("posted_date", ""))}
                for j in json.loads(raw).get("jobs") or []]


class IbmScraper(SiteScraper):
    ATS_NAME = "ibm"

    def load(self, n: int) -> str | None:
        if n:
            return None  # one request returns the newest 100
        return json.dumps(self.get_json("https://www-api.ibm.com/search/api/v2", method="POST", json={
            "appId": "careers", "scopes": ["careers2"], "size": 100, "from": 0, "lang": "zz", "all": False,
            "query": {"bool": {"must": [{"simple_query_string": {"query": self.config.get("query", "intern"), "fields": ["title^3"]}}]}},
            "sort": [{"dcdate": "desc"}],
            "_source": ["title", "url", "dcdate", "field_keyword_05", "field_keyword_19"],
            "p": "https://www.ibm.com/careers/search"}))

    def parse(self, raw: str, n: int) -> list[dict]:
        rows = []
        for h in json.loads(raw)["hits"]["hits"]:
            s = h["_source"]
            rows.append({"title": s["title"], "url": s["url"], "posted_at": iso_day(s.get("dcdate", "")),
                         "location": f'{s.get("field_keyword_19", "")}, {s.get("field_keyword_05", "")}'.strip(", ")})
        return rows


class GoldmanScraper(SiteScraper):
    ATS_NAME = "goldman"

    def load(self, n: int) -> str | None:
        if n:
            return None  # early-career only: the experienced-hire search is all VP/Associate noise
        q = ('{ roleSearch(searchQueryInput:{page:{pageNumber:0,pageSize:100},sort:{sortStrategy:POSTED_DATE,'
             'sortOrder:DESC},filters:[],experiences:[EARLY_CAREER],searchTerm:%s}){items{roleId jobTitle '
             'locations{city state country} externalSource{sourceId}}}}') % json.dumps(self.config.get("query", "intern"))
        return json.dumps(self.get_json("https://api-higher.gs.com/gateway/api/v1/graphql",
                                        method="POST", json={"query": q}))

    def parse(self, raw: str, n: int) -> list[dict]:
        try:
            items = json.loads(raw)["data"]["roleSearch"]["items"]
        except (KeyError, TypeError) as exc:
            raise ScrapeError(f"Goldman Sachs returned an unexpected shape: {exc}")
        return [{"title": it["jobTitle"], "url": f'https://higher.gs.com/roles/{it["externalSource"]["sourceId"]}',
                 "location": "; ".join(", ".join(x for x in (l["city"], l["state"], l["country"]) if x)
                                       for l in it["locations"])} for it in items]


class GoogleScraper(SiteScraper):
    """Google's careers results page embeds the job list in its HTML (no JSON API)."""

    ATS_NAME = "google"
    MAX_PAGES = 7
    BASE = "https://www.google.com/about/careers/applications/jobs/results/"
    ENTRY = re.compile(r'\["(\d{12,})","([^"]+)","https://www\.google\.com/about/careers/applications/signin')
    LOC = re.compile(r'"([A-Za-z .\'\-]+, [A-Z]{2}, USA)"')

    def load(self, n: int) -> str:
        try:
            r = requests.get(self.BASE, headers=UA, timeout=self.TIMEOUT_SECONDS, params={
                "q": self.config.get("query", "intern"), "location": "United States",
                "page": n + 1, **({"target_level": "INTERN_AND_APPRENTICE"}
                                  if "intern" in self.config.get("query", "intern") else {})})
            r.raise_for_status()
        except requests.RequestException as exc:
            raise ScrapeError(f"Google fetch failed on page {n + 1}: {exc}")
        return r.text

    def parse(self, raw: str, n: int) -> list[dict]:
        slugs = {m.split("-", 1)[0]: m for m in set(re.findall(r"jobs/results/(\d+-[a-z0-9-]+)", raw))}
        starts = list(self.ENTRY.finditer(raw))
        rows = []
        for i, m in enumerate(starts):
            seg = raw[m.end(): starts[i + 1].start() if i + 1 < len(starts) else m.end() + 20000]
            title = m.group(2)
            if "\\" in title:
                title = title.encode().decode("unicode_escape", "ignore")
            rows.append({"title": title, "url": self.BASE + slugs.get(m.group(1), m.group(1)),
                         "location": "; ".join(dict.fromkeys(self.LOC.findall(seg[:20000])))})
        return rows


class EightfoldScraper(SiteScraper):
    """Eightfold careers sites (Micron, Microsoft, Netflix ...). config: host, domain, optional api: v2."""

    ATS_NAME = "eightfold"
    MAX_PAGES = 15
    PAUSE = 2.0

    def load(self, n: int) -> str:
        host, domain = self.config["host"], self.config["domain"]
        if self.config.get("api") == "v2":
            url = f"https://{host}/api/apply/v2/jobs"
            params = {"domain": domain, "query": self.config.get("query", "intern"), "start": n * 10, "num": 10}
        else:
            url = f"https://{host}/api/pcsx/search"
            params = {"domain": domain, "query": self.config.get("query", "intern"), "start": n * 10,
                      "sort_by": "timestamp"}
        if self.config.get("browser"):  # host refuses plain scripts: ask from inside a real Chrome
            from urllib.parse import urlencode
            return json.dumps(self.browser_json(f"https://{host}", f"{url[len('https://' + host):]}?{urlencode(params)}"))
        for wait in (0, 15, 45):  # Eightfold answers 429 when asked quickly: back off and retry
            time.sleep(wait)
            try:
                return json.dumps(self.get_json(url, params=params))
            except ScrapeError as exc:
                if "429" not in str(exc) or wait == 45:
                    raise

    def parse(self, raw: str, n: int) -> list[dict]:
        d = json.loads(raw)
        positions = (d.get("data") or {}).get("positions") or d.get("positions") or []
        host = self.config["host"]
        rows = []
        for p in positions:
            ts = p.get("postedTs") or p.get("t_create")
            posted = datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else ""
            loc = p.get("location") or "; ".join(p.get("locations") or [])
            rows.append({"title": p.get("name", ""), "location": loc, "posted_at": posted,
                         "department": p.get("department", ""),
                         "url": p.get("canonicalPositionUrl") or f"https://{host}{p.get('positionUrl', '')}"})
        return rows


class OracleHcmScraper(SiteScraper):
    """Oracle Recruiting Cloud careers sites (JPMorgan, Oracle). config: host, site (e.g. CX_1001)."""

    ATS_NAME = "oraclehcm"
    MAX_PAGES = 10
    SIZE = 50

    def load(self, n: int) -> str:
        host, site = self.config["host"], self.config["site"]
        finder = (f"findReqs;siteNumber={site},keyword={self.config.get('query', 'intern')},"
                  f"limit={self.SIZE},offset={n * self.SIZE},sortBy=POSTING_DATES_DESC")
        return json.dumps(self.get_json(f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions",
                                        params={"onlyData": "true", "expand": "requisitionList", "finder": finder}))

    def parse(self, raw: str, n: int) -> list[dict]:
        items = json.loads(raw).get("items") or []
        host, site = self.config["host"], self.config["site"]
        out = []
        for it in items:
            for r in it.get("requisitionList") or []:
                out.append({"title": r.get("Title", ""), "location": r.get("PrimaryLocation", ""),
                            "department": r.get("JobFamily", "") or "", "posted_at": iso_day(r.get("PostedDate", "")),
                            "url": f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{r['Id']}"})
        return out


class AtlassianScraper(SiteScraper):
    """Atlassian publishes every listing in one JSON file; keep the internships."""

    ATS_NAME = "atlassian"

    def load(self, n: int) -> str | None:
        if n:
            return None
        return json.dumps(self.get_json("https://www.atlassian.com/endpoint/careers/listings"))

    def parse(self, raw: str, n: int) -> list[dict]:
        return [{"title": j.get("title", ""), "url": (j.get("portalJobPost") or {}).get("portalUrl", ""),
                 "location": "; ".join(l.split(" - ")[0] for l in j.get("locations") or []),
                 "department": j.get("category", "")}
                for j in json.loads(raw) if re.search(os.environ.get("JOB_PAGE_REGEX") or r"\bintern(ship)?s?\b", j.get("title", ""), re.I)]


class UberScraper(GenericSiteScraper):
    """Uber's list says "Paris, France + 1 location"; the job page has the full list and the post date."""

    ATS_NAME = "uber"
    MORE = re.compile(r"\+\s*\d+\s+locations?", re.I)

    def parse(self, raw: str, n: int) -> list[dict]:
        rows = super().parse(raw, n)
        for row in rows:
            if not self.MORE.search(row.get("location", "")):
                continue
            try:
                row["location"] = self.detail_location(row["url"]) or row["location"]
            except ScrapeError:
                pass  # keep the short form; the job is still reported
        return rows

    def detail_location(self, url: str) -> str:
        from urllib.parse import urljoin
        pg = site_tab()
        pg.goto(urljoin(self.config.get("base_url", ""), url), wait_until="domcontentloaded", timeout=45000)
        pg.wait_for_selector("text=Location", timeout=10000)
        lines = [t.strip() for t in pg.evaluate("document.body.innerText").split("\n") if t.strip()]
        return lines[lines.index("Location") + 1] if "Location" in lines else ""


class MetaScraper(GenericSiteScraper):
    """Meta: reuses the generic link finder, but each card shows title, then locations, then tags."""

    ATS_NAME = "meta"
    LOCATION = re.compile(r"^(.+, [A-Z]{2}|Remote.*|⋅)$")

    def parse(self, raw: str, n: int) -> list[dict]:
        from bs4 import BeautifulSoup
        rows = []
        for a in BeautifulSoup(raw, "html.parser").select('a[href*="/profile/job_details/"]'):
            parts = list(a.stripped_strings)
            if not parts:
                continue
            locs = []
            for t in parts[1:]:
                if not self.LOCATION.match(t):
                    break
                if t != "⋅":
                    locs.append(t)
            rows.append({"title": parts[0], "url": a["href"], "location": "; ".join(locs)})
        return rows


SITE_IMPLS.update({"amazon": AmazonScraper, "ibm": IbmScraper, "goldman": GoldmanScraper, "google": GoogleScraper, "meta": MetaScraper, "uber": UberScraper,
                   "eightfold": EightfoldScraper, "oraclehcm": OracleHcmScraper, "atlassian": AtlassianScraper})
