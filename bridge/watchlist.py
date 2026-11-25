#!/usr/bin/env python3
"""Watch-only list: tell me when these companies post an internship anywhere in the US.

Never applies. It only records what it finds in reports/watchlist.md and raises a popup.
Each posting is reported once. The first run reports everything currently open.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("WORKDAY_CHROME_PORT", "9226")  # the always-on scanner uses 9225; never share its Chrome
sys.path.insert(0, str(ROOT / "vendor" / "roledar"))
STATE = ROOT / "state" / "watchlist.json"
REPORT = ROOT / "reports" / "watchlist.md"
INTERN_RE = re.compile(r"\bintern(ship)?s?\b|\bco-?op\b", re.I)
UA = {"User-Agent": "Mozilla/5.0"}
US_STATES = ("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND "
             "OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC").split()
US_RE = re.compile(r"United States|\bUSA?\b|\bU\.S\.|\b(" + "|".join(US_STATES) + r")\b(?!\w)")
NON_US_RE = re.compile(r"canada|\buk\b|united kingdom|london|europe|emea|apac|india|latam|mexico|brazil|germany|"
                       r"france|ireland|singapore|australia|israel|china|japan|toronto|vancouver|poland|hong kong", re.I)

# Boards the roledar scrapers already understand.
ATS = {
    "Squarespace": ("greenhouse", {"board": "squarespace"}),
    "Palantir": ("lever", {"slug": "palantir"}),
    "Baseten": ("ashby", {"org": "baseten"}),
    "Salesforce": ("workday", {"host": "salesforce.wd12.myworkdayjobs.com", "tenant": "salesforce",
                               "site": "External_Career_Site"}),
    "CVS": ("workday", {"host": "cvshealth.wd1.myworkdayjobs.com", "tenant": "cvshealth",
                        "site": "CVS_Health_Careers"}),
    "Barclays": ("workday", {"host": "barclays.wd3.myworkdayjobs.com", "tenant": "barclays",
                             "site": "External_Career_Site_Barclays"}),
}
# No public feed found yet (reported every run so it is never silently skipped).
PAREN = {"Amazon", "Palantir"}  # shown in parentheses
UNAVAILABLE = ["Walmart", "Bloomberg", "Millennium"]


def is_us(location: str) -> bool:
    return bool(US_RE.search(location or "")) and not NON_US_RE.search(location or "")


def amazon() -> list[dict]:
    out, offset = [], 0
    while offset < 300:
        r = requests.get("https://www.amazon.jobs/en/search.json", headers=UA, timeout=30, params={
            "base_query": "intern", "result_limit": 100, "offset": offset, "sort": "recent",
            "country[]": "USA"})
        r.raise_for_status()
        jobs = r.json().get("jobs") or []
        for j in jobs:
            out.append({"company": "Amazon", "title": j["title"], "location": j.get("normalized_location") or j.get("location", ""),
                        "url": "https://www.amazon.jobs" + j["job_path"], "posted": j.get("posted_date", "")})
        if len(jobs) < 100:
            break
        offset += 100
    return out


def ibm() -> list[dict]:
    r = requests.post("https://www-api.ibm.com/search/api/v2", timeout=30, json={
        "appId": "careers", "scopes": ["careers2"], "size": 100, "from": 0, "lang": "zz", "all": False,
        "query": {"bool": {"must": [{"simple_query_string": {"query": "intern", "fields": ["title^3"]}}]}},
        "sort": [{"dcdate": "desc"}], "_source": ["title", "url", "dcdate", "field_keyword_05", "field_keyword_19"],
        "p": "https://www.ibm.com/careers/search"})
    r.raise_for_status()
    out = []
    for h in r.json()["hits"]["hits"]:
        s = h["_source"]
        loc = f'{s.get("field_keyword_19", "")}, {s.get("field_keyword_05", "")}'.strip(", ")
        out.append({"company": "IBM", "title": s["title"], "location": loc, "url": s["url"], "posted": s.get("dcdate", "")})
    return out


def goldman() -> list[dict]:
    out = []
    for exp in ("EARLY_CAREER", "PROFESSIONAL"):
        q = ('{ roleSearch(searchQueryInput:{page:{pageNumber:0,pageSize:100},sort:{sortStrategy:POSTED_DATE,'
             'sortOrder:DESC},filters:[],experiences:[%s],searchTerm:"intern"}){items{roleId jobTitle '
             'locations{city state country} externalSource{sourceId}}}}' % exp)
        r = requests.post("https://api-higher.gs.com/gateway/api/v1/graphql", json={"query": q}, timeout=30)
        r.raise_for_status()
        for it in r.json()["data"]["roleSearch"]["items"]:
            loc = "; ".join(", ".join(x for x in (l["city"], l["state"], l["country"]) if x) for l in it["locations"])
            out.append({"company": "Goldman Sachs", "title": it["jobTitle"], "location": loc,
                        "url": f'https://higher.gs.com/roles/{it["externalSource"]["sourceId"]}', "posted": ""})
    return out


def via_roledar(name: str, ats: str, cfg: dict) -> list[dict]:
    from scrapers.ashby import AshbyScraper
    from scrapers.greenhouse import GreenhouseScraper
    from scrapers.lever import LeverScraper
    from scrapers.workday import WorkdayScraper
    cls = {"lever": LeverScraper, "ashby": AshbyScraper, "greenhouse": GreenhouseScraper, "workday": WorkdayScraper}[ats]
    return [{"company": name, "title": p.title, "location": p.location, "url": p.url, "posted": p.posted_at[:10]}
            for p in cls(name, cfg).fetch()]


def posted_day(r: dict):
    """Posting date, or None when the board gives none."""
    raw = (r.get("posted") or "").strip()
    for fmt, n in (("%Y-%m-%d", 10), ("%B %d, %Y", None)):
        try:
            return datetime.strptime(raw[:n] if n else raw, fmt).date()
        except ValueError:
            pass
    return None


def opened_today(r: dict, first: bool) -> bool:
    day = posted_day(r)
    if day:
        return day == datetime.now().date()
    # Workday leaves the date blank for "posted today"; other boards blank means unknown.
    return r["url"].find("myworkdayjobs") >= 0 or not first


def gather(log=print, failed: list | None = None) -> list[dict]:
    sources = [("Amazon", amazon), ("IBM", ibm), ("Goldman Sachs", goldman)]
    sources += [(n, (lambda n=n, a=a, c=c: via_roledar(n, a, c))) for n, (a, c) in ATS.items()]
    rows = []
    for name, fn in sources:
        for attempt in range(2):
            try:
                rows += fn()
                break
            except Exception as exc:
                if attempt == 1:
                    if failed is not None:
                        failed.append(name)
                    log(f"Watchlist: {name} failed ({type(exc).__name__}: {str(exc)[:100]})")
                else:
                    time.sleep(5)
    return [r for r in rows if INTERN_RE.search(r["title"]) and is_us(r["location"])]


def run(log=print, notify=None) -> list[dict]:
    """Find postings not reported before. notify(title, message, kind, arg) raises the popup."""
    state = json.loads(STATE.read_text()) if STATE.exists() else {"seen": {}}
    first = not state["seen"]
    failed: list[str] = []
    found = gather(log, failed)
    new = [r for r in found if r["url"] not in state["seen"] and opened_today(r, first)]
    for r in found:  # older postings are recorded so they never show up later
        state["seen"].setdefault(r["url"], "old")
    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    for r in new:
        state["seen"][r["url"]] = stamp
    if new:
        REPORT.parent.mkdir(exist_ok=True)
        old = REPORT.read_text() if REPORT.exists() else "# Watchlist internships (US), opened today\n"
        head, _, rest = old.partition("\n")
        lines = [f"\n## {stamp}" + (" " if first else "")]
        for r in sorted(new, key=lambda r: (r["company"], r["title"])):
            line = f"**{r['company']}** [{r['title']}]({r['url']}) - {r['location']}"
            lines.append(f"- ({line})" if r["company"] in PAREN else f"- {line}")
        REPORT.write_text(head + "\n" + "\n".join(lines) + "\n" + rest)
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(state, indent=1))
    log(f"Watchlist: {len(new)} new internship(s) at watched companies (not applying)")
    if not new and notify:  # a daily digest even when empty, so silence never looks like "it did not run"
        miss = f" Could not check: {', '.join(failed)}." if failed else ""
        notify("Watchlist, 4 pm", "No new internships opened today at your watched companies." + miss,
               "file", str(REPORT if REPORT.exists() else STATE))
    if new and notify:
        by = {}
        for r in new:
            by[r["company"]] = by.get(r["company"], 0) + 1
        notify("Watchlist internships", ", ".join(f"({c} {n})" if c in PAREN else f"{c} {n}" for c, n in sorted(by.items())) +
               ". Click for the list.", "file", str(REPORT))
    return new


if __name__ == "__main__":
    import subprocess
    notify_py = Path(__file__).with_name("notify.py")
    for r in run(notify=lambda title, msg, kind, arg: subprocess.Popen(
            [sys.executable, str(notify_py), title, msg[:200], kind, arg], start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)):
        print(r["company"], "|", r["title"], "|", r["location"])
