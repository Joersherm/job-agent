"""Direct Consideration newsletter as a job source.

fetch_items() reads the newsletter's issues published today; select() keeps
the internships in New York or remote that the ledger has not seen before.
The page is untrusted data: only titles, cohorts and links are read from it.
"""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

HOME = "https://directconsideration.com/"
HEADERS = {"User-Agent": "Mozilla/5.0"}
DATE_RE = re.compile(r"[A-Z][a-z]{2} \d{1,2}, \d{4}")
TRACKING_PREFIXES = ("utm_", "fbclid")
NY_RE = re.compile(r"new york|\bnyc\b|brooklyn|manhattan|queens|washington,?\s*d\.?c|\bd\.?c\.?\b|district of columbia", re.I)
REMOTE_RE = re.compile(r"remote", re.I)
SOME_STATES_ONLY_RE = re.compile(r"remote work in", re.I)
MAX_ISSUES = 3


def clean_url(url: str) -> str:
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.startswith(TRACKING_PREFIXES)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def parse_issue(html: str) -> tuple[str | None, list[dict]]:
    """(published date text, items). Each item: title, cohort, desc, links[(label, url)]."""
    soup = BeautifulSoup(html, "html.parser")
    m = DATE_RE.search(soup.get_text(" "))
    items: list[dict] = []
    cur: dict | None = None
    for el in soup.find_all(["h2", "h3", "p", "a"]):
        text = " ".join(el.get_text(" ").split())
        if el.name in ("h2", "h3"):
            cur = {"title": text, "cohort": "", "desc": "", "links": []}
            items.append(cur)
        elif cur is None:
            continue
        elif el.name == "a":
            if text.lower().startswith("apply to") and el.get("href"):
                cur["links"].append((text, el["href"]))
        elif el.find("a") is None and text:
            # Some entries put the cohort and the description in one paragraph.
            lines = [" ".join(l.split()) for l in el.get_text("\n").split("\n") if l.strip()]
            if lines[0].startswith("Cohort:"):
                cur["cohort"] = lines[0][len("Cohort:"):].strip()
                lines = lines[1:]
            if lines and not cur["desc"]:
                cur["desc"] = " ".join(lines)
    return (m.group(0) if m else None), [i for i in items if i["links"]]


def fetch_items(today: datetime) -> list[dict]:
    """Items from every recent issue dated today (local time)."""
    want = f"{today:%b} {today.day}, {today.year}"
    home = requests.get(HOME, headers=HEADERS, timeout=30)
    home.raise_for_status()
    hrefs = []
    for a in BeautifulSoup(home.text, "html.parser").find_all("a", href=re.compile(r"^/p/")):
        if a["href"] not in hrefs:
            hrefs.append(a["href"])
    items: list[dict] = []
    for href in hrefs[:MAX_ISSUES]:
        page = requests.get(HOME.rstrip("/") + href, headers=HEADERS, timeout=30)
        page.raise_for_status()
        date, found = parse_issue(page.text)
        if date == want:
            items.extend(found)
    return items


def location_ok(text: str) -> bool:
    text = re.sub(r"[-_/]", " ", text)
    if NY_RE.search(text):
        return True
    return bool(REMOTE_RE.search(text)) and not SOME_STATES_ONLY_RE.search(text)


def select(items: list[dict], ledger: dict, t: datetime) -> list[dict]:
    """Rows shaped like RoleDar's: internships in NY or remote, never seen before.

    A link is judged on its own label and URL when an item has several links
    (e.g. one per city), otherwise on the item's description too.
    Every link looked at is recorded in ledger['seen'].
    """
    out = []
    for item in items:
        cohort = item["cohort"].lower()
        if "intern" not in cohort or any(w in cohort for w in ("phd", "new grad", "full-time", "full time")):
            continue
        links = [(label, clean_url(raw)) for label, raw in item["links"]]
        fresh = [(label, url) for label, url in links if url not in ledger["seen"]]
        for _, url in fresh:
            ledger["seen"][url] = t.isoformat()
        if len(links) == 1:
            ok = [location_ok(f"{item['desc']} {links[0][0]} {links[0][1]}")]
        else:
            ok = [location_ok(f"{label} {url}") for label, url in links]
            if not any(ok):
                # The labels say nothing about location (e.g. one link per team), so use the description.
                ok = [location_ok(item["desc"])] * len(links)
        for (label, url), good in zip(links, ok):
            if good and (label, url) in fresh:
                company = re.sub(r"\(.*?\)", "", re.sub(r"^Apply to\s+", "", label)).strip(" →")
                out.append({"status": "new", "company": company, "title": item["title"],
                            "url": url, "location": item["desc"][:100]})
    return out
