"""Workday public job board API (the JSON feed behind every *.myworkdayjobs.com careers site).

Endpoint: POST https://{host}/wday/cxs/{tenant}/{site}/jobs
ats_config: host (e.g. tmobile.wd1.myworkdayjobs.com), tenant (e.g. tmobile), site (e.g. External).

Workday gives only a coarse "Posted Today / Yesterday / N Days Ago" label, so posted_at is left empty
for "today" (the pipeline then relies on the posting being newly discovered) and set to a date for older ones.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone

import requests

from .base import BaseScraper, JobPosting, ScrapeError

PAGE = 20
MAX_PAGES = 25  # up to 500 matches per query
INTERN_RE = re.compile(r"\bintern(ship)?s?\b|\bco-?op\b", re.I)


PAUSE = 1.0          # seconds between Workday requests
BLOCK_LIMIT = 6      # this many requests in a row refused => stop hitting Workday for this run
_state = {"last": 0.0, "refused": 0, "pw": None, "browser": None, "pages": {}}


class _Reply:
    def __init__(self, status: int, text: str):
        self.status_code, self.text = status, text

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error for Workday")


WD_PORT = 9225
WD_PROFILE = Path(__file__).resolve().parents[1] / "state" / "workday-chrome"


def _chrome_pid() -> int | None:
    out = subprocess.run(["lsof", "-ti", f"tcp:{WD_PORT}", "-sTCP:LISTEN"], capture_output=True, text=True).stdout.split()
    return int(out[0]) if out else None


def _close_browser() -> None:
    """Close the scan's Chrome completely (its window never belongs to anything else)."""
    try:
        if _state["browser"]:
            _state["browser"].close()
        if _state["pw"]:
            _state["pw"].stop()
    except Exception:
        pass
    _state["browser"] = _state["pw"] = None
    _state["pages"] = {}
    pid = _chrome_pid()
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def _page_for(host: str, tenant: str, site: str):
    """Workday refuses plain scripts but answers a real Chrome, so reads go through one.

    Chrome is started hidden and in the background (`open -g -j`) so no window ever takes focus.
    """
    from playwright.sync_api import sync_playwright
    if _state["browser"] is None:
        if _chrome_pid():
            _close_browser()  # leftover from a crashed scan
        WD_PROFILE.mkdir(parents=True, exist_ok=True)
        subprocess.run(["open", "-g", "-j", "-n", "-a", "Google Chrome", "--args",
                        f"--remote-debugging-port={WD_PORT}", f"--user-data-dir={WD_PROFILE}",
                        "--no-first-run", "--no-default-browser-check", "about:blank"], check=True)
        for _ in range(40):
            if _chrome_pid():
                break
            time.sleep(0.5)
        _state["pw"] = sync_playwright().start()
        _state["browser"] = _state["pw"].chromium.connect_over_cdp(f"http://localhost:{WD_PORT}")
        atexit.register(_close_browser)
    # One tab for the whole scan: it is navigated from company to company, so no new tab or window
    # is ever created (new tabs are what pull Chrome to the front).
    ctx = _state["browser"].contexts[0]
    pg = _state["pages"].get("tab") or (ctx.pages[0] if ctx.pages else ctx.new_page())
    _state["pages"]["tab"] = pg
    if _state["pages"].get("where") != (host, site):
        pg.goto(f"https://{host}/en-US/{site}", wait_until="domcontentloaded", timeout=45000)
        _state["pages"]["where"] = (host, site)
    return pg


def _fetch(host: str, tenant: str, site: str, path: str, body: dict | None = None):
    """Same-origin fetch inside Chrome. POST when body is given, else GET."""
    if _state["refused"] >= BLOCK_LIMIT:
        raise ScrapeError("Workday is refusing this computer; skipped to let it cool down")
    time.sleep(max(0.0, _state["last"] + PAUSE - time.time()))
    script = """async ([url, body]) => { const r = await fetch(url, {signal: AbortSignal.timeout(30000), method: body ? 'POST' : 'GET',
                 headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
                 body: body ? JSON.stringify(body) : undefined}); return [r.status, await r.text()]; }"""
    for attempt in (1, 2):
        try:
            pg = _page_for(host, tenant, site)
            timer = threading.Timer(60, _close_browser)  # unfreezes a hung evaluate by closing Chrome
            timer.start()
            try:
                status, text = pg.evaluate(script, [f"/wday/cxs/{tenant}/{site}{path}", body])
            finally:
                timer.cancel()
            break
        except Exception as exc:
            _close_browser()  # Chrome died or hung: start a fresh one and try once more
            if attempt == 2:
                _state["refused"] += 1
                raise ScrapeError(f"Workday browser fetch failed for {tenant!r}: {exc}")
    _state["last"] = time.time()
    _state["refused"] = _state["refused"] + 1 if status == 429 else 0
    return _Reply(status, text)


def _posted_at(label: str) -> str:
    low = (label or "").lower()
    if "today" in low or not low:
        return ""
    now = datetime.now(timezone.utc)
    if "yesterday" in low:
        return (now - timedelta(days=1)).isoformat()
    m = re.search(r"(\d+)\+?\s*day", low)
    if m:
        return (now - timedelta(days=int(m.group(1)))).isoformat()
    return ""


class WorkdayScraper(BaseScraper):
    ATS_NAME = "workday"

    def fetch(self) -> list[JobPosting]:
        host = self._require_config("host")
        tenant = self._require_config("tenant")
        site = self._require_config("site")
        query = self.config.get("search", "intern")
        api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
        postings: list[JobPosting] = []
        for page in range(MAX_PAGES):
            try:
                resp = _fetch(host, tenant, site, "/jobs",
                              {"appliedFacets": {}, "limit": PAGE, "offset": page * PAGE, "searchText": query})
                resp.raise_for_status()
                data = resp.json()
            except (requests.RequestException, ValueError) as exc:
                raise ScrapeError(f"Workday fetch failed for {tenant!r}: {exc} ({api})")
            jobs = data.get("jobPostings") or []
            for job in jobs:
                title = (job.get("title") or "").strip()
                path = (job.get("externalPath") or "").strip()
                if not title or not path:
                    continue
                location = (job.get("locationsText") or "").strip()
                if re.fullmatch(r"\d+ Locations?", location) and INTERN_RE.search(title):
                    location = self._all_locations(host, tenant, site, path) or location
                postings.append(JobPosting(
                    company=self.company_name, title=title,
                    url=f"https://{host}/en-US/{site}{path}",
                    location=location,
                    department="", posted_at=_posted_at(job.get("postedOn", "")),
                ))
            # Results are relevance-ranked: once a whole page has no intern/co-op title, stop paging.
            if len(jobs) < PAGE or not any(INTERN_RE.search(j.get("title") or "") for j in jobs):
                break
        return postings

    def _all_locations(self, host: str, tenant: str, site: str, path: str) -> str:
        """Workday collapses multi-site roles to "3 Locations"; the detail page lists them."""
        try:
            info = _fetch(host, tenant, site, path).json().get("jobPostingInfo", {})
        except (requests.RequestException, ValueError):
            return ""
        places = [info.get("location", "")] + list(info.get("additionalLocations") or [])
        return "; ".join(p for p in places if p)
