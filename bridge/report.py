#!/usr/bin/env python3
"""Build the applications report from state/applications.jsonl.

  report.py           write reports/applications.html (everything, newest first)
  report.py --daily   also write reports/daily-YYYY-MM-DD.html for the last 24 hours
                      and pop up a link to it (scheduled for 7 pm)
"""
from __future__ import annotations

import argparse
import html
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APPS = ROOT / "state" / "applications.jsonl"
OUT = ROOT / "reports"
NOTIFY = Path(__file__).with_name("notify.py")

SECTIONS = [
    ("applied", "Applied"),
    ("needs you", "Needs you"),
    ("skipped", "Skipped"),
    ("dry run", "Dry run (not submitted)"),
]

STYLE = """
:root{--bg:#fff;--fg:#1a1a1a;--muted:#666;--line:#e3e3e3;--accent:#0a7d3b}
@media(prefers-color-scheme:dark){:root{--bg:#161616;--fg:#eee;--muted:#999;--line:#333;--accent:#4cc28a}}
body{font:15px/1.5 -apple-system,system-ui,sans-serif;background:var(--bg);color:var(--fg);margin:0 auto;max-width:960px;padding:24px 16px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 8px}p{color:var(--muted);margin:0}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:13px}a{color:var(--accent)}
.note{color:var(--muted);font-size:13px}
@media(max-width:600px){th:nth-child(3),td:nth-child(3){display:none}}
"""


def load(path: Path = APPS) -> list[dict]:
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            e = json.loads(line)
            e["at"] = datetime.fromisoformat(e["at"])
            entries.append(e)
    entries.sort(key=lambda e: e["at"], reverse=True)
    return entries


def counts(entries: list[dict]) -> dict[str, int]:
    return {key: sum(1 for e in entries if e["outcome"] == key) for key, _ in SECTIONS}


def render(entries: list[dict], title: str, subtitle: str) -> str:
    c = counts(entries)
    parts = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'>",
             "<meta name='viewport' content='width=device-width,initial-scale=1'>",
             f"<title>{html.escape(title)}</title><style>{STYLE}</style></head><body>",
             f"<h1>{html.escape(title)}</h1><p>{html.escape(subtitle)}</p>"]
    if not entries:
        parts.append("<h2>Nothing yet</h2><p>No applications in this period.</p>")
    for key, label in SECTIONS:
        rows = [e for e in entries if e["outcome"] == key]
        if not rows:
            continue
        parts.append(f"<h2>{label} ({c[key]})</h2><table><tr><th>Time</th><th>Company</th>"
                     "<th>Location</th><th>Role</th></tr>")
        for e in rows:
            when = e["at"].astimezone().strftime("%b %d, %I:%M %p")
            role = f"<a href='{html.escape(e['url'], quote=True)}'>{html.escape(e['title'] or e['url'])}</a>"
            if e.get("note"):
                role += f"<div class='note'>{html.escape(e['note'])}</div>"
            parts.append(f"<tr><td>{when}</td><td>{html.escape(e['company'])}</td>"
                         f"<td>{html.escape(e['location'])}</td><td>{role}</td></tr>")
        parts.append("</table>")
    parts.append("</body></html>")
    return "".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--daily", action="store_true", help="Write the 24-hour report and show a popup.")
    args = parser.parse_args()

    OUT.mkdir(exist_ok=True)
    entries = load()
    t = datetime.now(timezone.utc)
    everything = OUT / "applications.html"
    everything.write_text(render(entries, "All applications", f"{counts(entries)['applied']} applied so far"),
                          encoding="utf-8")
    print(everything)
    if not args.daily:
        return 0

    day = [e for e in entries if e["at"] >= t - timedelta(hours=24)]
    c = counts(day)
    page = OUT / f"daily-{t.astimezone():%Y-%m-%d}.html"
    page.write_text(render(day, f"Applications for {t.astimezone():%A, %B %d}",
                           f"{c['applied']} applied, {c['needs you']} need you, last 24 hours"),
                    encoding="utf-8")
    companies = sorted({e["company"] for e in day if e["outcome"] == "applied" and e["company"]})
    if c["applied"]:
        msg = f"Applied to {c['applied']}: {', '.join(companies)}. Click to see the roles."
    else:
        msg = "No applications in the last 24 hours."
    subprocess.Popen([sys.executable, str(NOTIFY), "Daily job applications", msg, "url", page.as_uri()],
                     start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(page)
    return 0


if __name__ == "__main__":
    sys.exit(main())
