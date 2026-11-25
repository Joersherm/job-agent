"""Template for companies that run their own careers site instead of a standard ATS.

One base class does the shared work (paging, de-duplication, turning rows into JobPostings,
errors). A company either:

  * uses the generic scraper, configured only in companies.yaml:
        - name: Meta
          ats: site
          ats_config:
            url: https://www.metacareers.com/jobs?q=intern&page={page}
            render: true                       # load it in a real (hidden) Chrome first
            link_selector: a[href*="/profile/job_details/"]
            title_selector: div                # optional: where inside the link the title is
            location_selector: ...             # optional
            card_selector: tr                  # optional: the row around the link; then title_line /
            title_line: 0                      #   location_line pick the n-th text line of that row, or
            location_line: 3                   #   location_after: <label> takes the line after a label
            default_location: United States    # optional: used when the site shows no location
            max_pages: 3
  * or has its own subclass (inheritance) when the generic one is not enough:
        class MetaScraper(SiteScraper): ...    # override load()/parse()/pages as needed
        register it in SITE_IMPLS and point at it with `impl: meta` in ats_config.

Hooks a subclass can override, all optional except parse():
    page_url(n)   address of results page n          (default: config url with {page} filled in)
    load(n)       raw text of page n, None to stop   (default: HTTP GET, or Chrome when render: true)
    parse(raw, n) list of row dicts: title, url, location, department, posted_at
    row_ok(row)   keep or drop a row                 (default: needs a title and a url)
"""

from __future__ import annotations

import atexit
import functools
import ipaddress
import json
import os
import re
import signal
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

import requests

from . import _pw
from .base import BaseScraper, EmptyResult, JobPosting, ScrapeError

UA = {"User-Agent": "Mozilla/5.0"}
SITE_PORT = int(os.environ.get("SITE_CHROME_PORT", "9227"))  # own port: never shares another scan's Chrome
SITE_PROFILE = Path(__file__).resolve().parents[1] / "state" / f"site-chrome-{SITE_PORT}"
_browser = {"pw": None, "browser": None, "tab": None}


def iso_day(raw: str) -> str:
    """Date-only boards: older days are kept so stale roles stay stale; today's date is left blank
    (time unknown) so a role posted today still counts as freshly discovered."""
    raw = (raw or "").strip()
    for fmt, cut in (("%B %d, %Y", None), ("%Y-%m-%d", 10)):
        try:
            day = datetime.strptime(raw[:cut] if cut else raw, fmt).date()
        except ValueError:
            continue
        return "" if day >= datetime.now().date() else day.isoformat()
    return ""


SEARCH_TERM = re.compile(r"(?<=[?&]q=)intern(?=&|#|$)")  # an exact "q=intern"


def uses_query(config: dict) -> bool:
    """False when a generic reader would load the same address whatever the search term."""
    url = config.get("url", "")
    custom = config.get("impl") not in (None, "meta", "uber")  # these send the term outside the address
    return custom or "{query}" in url or bool(SEARCH_TERM.search(url))


# ---------------------------------------------------------------- addresses a model suggested

@functools.lru_cache(maxsize=256)
def _public_host(host: str) -> bool:
    try:
        return all(ipaddress.ip_address(i[4][0]).is_global for i in socket.getaddrinfo(host, 443))
    except (OSError, ValueError):
        return False


def public_url(url: str) -> bool:
    """https on a public host only."""
    if "\\" in url or any(c <= " " for c in url):
        return False
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        return parts.scheme == "https" and "." in host and not parts.username and _public_host(host)
    except ValueError:
        return False


# ---------------------------------------------------------------- hidden Chrome (shared, one tab)

def _chrome_pid() -> int | None:
    out = subprocess.run(["lsof", "-ti", f"tcp:{SITE_PORT}", "-sTCP:LISTEN"], capture_output=True, text=True).stdout.split()
    return int(out[0]) if out else None


def close_browser() -> None:
    try:
        if _browser["browser"]:
            _browser["browser"].close()
    except Exception:
        pass
    _browser.update(pw=None, browser=None, tab=None)
    pid = _chrome_pid()
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def _tab():
    """The one reused tab. Chrome starts hidden and in the background so no window takes focus."""
    if _browser["browser"] is None:
        if _chrome_pid():
            close_browser()  # leftover from a crashed scan
        SITE_PROFILE.mkdir(parents=True, exist_ok=True)
        subprocess.run(["open", "-g", "-j", "-n", "-a", "Google Chrome", "--args",
                        f"--remote-debugging-port={SITE_PORT}", f"--user-data-dir={SITE_PROFILE}",
                        "--no-first-run", "--no-default-browser-check", "about:blank"], check=True)
        for _ in range(40):
            if _chrome_pid():
                break
            time.sleep(0.5)
        _browser["pw"] = _pw.get()
        _browser["browser"] = _browser["pw"].chromium.connect_over_cdp(f"http://localhost:{SITE_PORT}")
        atexit.register(close_browser)
    if _browser["tab"] is not None and _browser["tab"].is_closed():
        _browser["tab"] = None
    if _browser["tab"] is None:
        ctx = _browser["browser"].contexts[0]
        _browser["tab"] = ctx.pages[0] if ctx.pages else ctx.new_page()
    return _browser["tab"]


# ---------------------------------------------------------------- the template

class SiteScraper(BaseScraper):
    ATS_NAME = "site"
    MAX_PAGES = 8
    PAUSE = 1.0  # seconds between page loads

    # ---- hooks
    def page_url(self, n: int) -> str:
        url = self.config.get("url")
        if not url:
            raise ScrapeError(f"{self.company_name}: ats_config needs a url")
        term = quote(self.config.get("query") or "intern", safe="")
        if self.config.get("query"):
            url = SEARCH_TERM.sub(lambda m: term, url)
        url = url.replace("{query}", term)
        return url.replace("{page}", str(n + 1)).replace("{offset}", str(n * int(self.config.get("page_size", 0))))

    def load(self, n: int) -> str | None:
        url = self.page_url(n)
        if n and "{page}" not in self.config.get("url", "") and "{offset}" not in self.config.get("url", ""):
            return None  # a fixed address has just one page
        if self.config.get("render"):
            return self.render(url)
        try:
            r = requests.get(url, headers=UA, timeout=self.TIMEOUT_SECONDS)
            r.raise_for_status()
        except requests.RequestException as exc:
            raise ScrapeError(f"{self.company_name} fetch failed on page {n + 1}: {exc}")
        return r.text

    def parse(self, raw: str, n: int) -> list[dict]:
        raise NotImplementedError(f"{type(self).__name__} must implement parse()")

    def row_ok(self, row: dict) -> bool:
        url = row.get("url", "")
        if self.config.get("public_only") and not public_url(url):
            return False
        hosts = self.config.get("hosts")  # the job-link hosts the user approved
        if hosts and urlsplit(url).hostname not in hosts:
            return False
        return bool(row.get("title") and url)

    # ---- helpers for subclasses
    def render(self, url: str) -> str:
        """HTML of the page after its scripts have run, from the shared hidden Chrome."""
        try:
            checked = self.config.get("public_only")
            if checked and not public_url(url):
                raise ValueError("not a public https address")
            pg = _tab()
            pg.goto(url, wait_until="domcontentloaded", timeout=45000)
            wait = self.config.get("wait_selector") or self.config.get("link_selector")
            if wait:
                try:
                    pg.wait_for_selector(wait, timeout=int(self.config.get("wait_ms", 15000)))
                except Exception:
                    pass  # an empty page is reported as "no jobs" by fetch()
            else:
                try:
                    pg.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass  # pages with constant background requests are still readable
            html = pg.content()
            if checked and not public_url(pg.url):
                raise ValueError("redirected to a private or non-https address")
            return html
        except Exception as exc:
            close_browser()  # Chrome died or hung: the next company gets a fresh one
            raise ScrapeError(f"{self.company_name} browser load failed: {exc}")

    def browser_json(self, origin: str, path: str):
        """Same-origin fetch from inside the hidden Chrome, for hosts that refuse plain scripts."""
        try:
            pg = _tab()
            if not pg.url.startswith(origin):
                pg.goto(origin + "/robots.txt", wait_until="domcontentloaded", timeout=45000)
            status, text = pg.evaluate(
                "async (u) => { const r = await fetch(u, {signal: AbortSignal.timeout(30000), headers: "
                "{'Accept': 'application/json'}}); return [r.status, await r.text()]; }", path)
        except Exception as exc:
            close_browser()
            raise ScrapeError(f"{self.company_name} browser fetch failed: {exc}")
        if status >= 400:
            raise ScrapeError(f"{self.company_name} fetch failed: {status} for {origin}{path}")
        try:
            return json.loads(text)
        except ValueError as exc:
            raise ScrapeError(f"{self.company_name} returned non-JSON: {exc}")

    def get_json(self, url: str, **kw):
        try:
            r = requests.request(kw.pop("method", "GET"), url, headers=UA, timeout=self.TIMEOUT_SECONDS, **kw)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as exc:
            raise ScrapeError(f"{self.company_name} fetch failed: {exc}")

    # ---- the shared loop
    def fetch(self) -> list[JobPosting]:
        base = self.config.get("base_url") or self.config.get("url", "")
        out: list[JobPosting] = []
        seen: set[str] = set()
        for n in range(int(self.config.get("max_pages", self.MAX_PAGES))):
            if n:
                time.sleep(self.PAUSE)
            raw = self.load(n)
            if raw is None:
                break
            parsed = self.parse(raw, n)
            if not parsed and n == 0:  # a slow page can render empty once: look again before giving up
                time.sleep(3)
                raw = self.load(0)
                parsed = self.parse(raw, 0) if raw else []
            fresh = 0
            for row in parsed:
                row = {**row, "url": urljoin(base, row.get("url", ""))}
                if not self.row_ok(row) or row["url"] in seen:
                    continue
                seen.add(row["url"])
                fresh += 1
                out.append(JobPosting(company=self.company_name, title=row["title"].strip(), url=row["url"],
                                      location=(row.get("location") or "").strip(),
                                      department=(row.get("department") or "").strip(),
                                      posted_at=row.get("posted_at") or ""))
            if fresh == 0:
                break
        if not out:
            raise EmptyResult(f"{self.company_name}: the page had no jobs (its layout may have changed)")
        return out


class GenericSiteScraper(SiteScraper):
    """Config-only scraper: finds job links with a CSS selector in the (rendered) page."""

    def parse(self, raw: str, n: int) -> list[dict]:
        from bs4 import BeautifulSoup
        sel = self.config.get("link_selector")
        if not sel:
            raise ScrapeError(f"{self.company_name}: ats_config needs link_selector")
        rows = []
        cfg = self.config
        soup = BeautifulSoup(raw, "html.parser")
        cards = soup.select(cfg["card_selector"]) if cfg.get("card_selector") else []  # any CSS selector
        owner = {id(a): c for c in cards for a in c.select(sel)}  # link -> the card that contains it
        links = soup.select(sel)
        owner.update({id(c): c for c in cards if any(c is l for l in links)})  # the card can be the link itself
        for a in soup.select(sel):
            card = owner.get(id(a))
            lines = list(card.stripped_strings) if card else []
            title = (lines[cfg["title_line"]] if card and "title_line" in cfg and len(lines) > cfg["title_line"]
                     else self._text(card or a, cfg.get("title_selector")))
            if card and "location_line" in cfg:
                loc = lines[cfg["location_line"]] if len(lines) > cfg["location_line"] else ""
            elif card and cfg.get("location_after") in lines:
                i = lines.index(cfg["location_after"]) + 1
                loc = lines[i] if i < len(lines) else ""
            else:
                loc = self._text(card or a, cfg["location_selector"]) if cfg.get("location_selector") else ""
            loc = loc.split("\n")[0].strip()  # some sites append the street address on a new line
            if cfg.get("vague_location") and re.search(cfg["vague_location"], loc, re.I):
                loc = cfg.get("default_location", "")  # e.g. "Multiple Locations": say only what is known
            rows.append({"title": title, "url": a.get("href", ""), "location": loc or cfg.get("default_location", "")})
        return rows

    @staticmethod
    def _text(node, selector) -> str:
        el = node.select_one(selector) if selector else node
        return el.get_text(" ", strip=True) if el else ""


# Company-specific subclasses register here and are chosen with `impl: <name>` in ats_config.
SITE_IMPLS: dict[str, type[SiteScraper]] = {}


def make_site_scraper(company_name: str, config: dict) -> SiteScraper:
    cls = SITE_IMPLS.get((config or {}).get("impl", ""), GenericSiteScraper)
    return cls(company_name, config)
