#!/usr/bin/env python3
"""Watch a stuck application's Chrome window while you fix it, record how, then continue it.

  resolve.py --port 9222 --url JOB_URL --company X --title T --reason "what went wrong"

Started by pipeline.py when an application fails and its window is left open. It
  1. records what you do in the window (clicks, fields you fill, pages you go to) and
     what the agent could not answer, saving it to learned_solutions.json / learned_answers.json
     so later agent runs can repeat the fix on the same site;
  2. when you stop for IDLE_SECONDS after fixing something, asks "Continue the application?";
  3. on Continue, runs `pipeline.py --continue URL` so the agent carries on from the page you left.

Passwords, codes and personal identifiers you type are never stored (only that you entered them).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))
import learn  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
APPLYPILOT_DIR = Path(os.environ.get("APPLYPILOT_DIR", ROOT / ".applypilot"))
SOLUTIONS = APPLYPILOT_DIR / "learned_solutions.json"
ANSWERS = APPLYPILOT_DIR / "learned_answers.json"
PY = sys.executable
POLL = 2
IDLE_SECONDS = 90
MAX_STEPS = 40

RECORDER_JS = r"""
(() => {
  if (window.__fixrec) return;
  window.__fixrec = true; window.__fixlog = window.__fixlog || [];
  const txt = el => ((el && (el.innerText || el.textContent)) || '').replace(/\s+/g, ' ').trim().slice(0, 80);
  const label = el => {
    const al = el.getAttribute && el.getAttribute('aria-label'); if (al) return al.slice(0, 80);
    if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) return txt(l); }
    const w = el.closest && el.closest('label'); if (w) return txt(w);
    return (el.getAttribute && (el.getAttribute('placeholder') || el.name || el.getAttribute('data-automation-id'))) || txt(el);
  };
  document.addEventListener('click', e => {
    const el = e.target.closest && e.target.closest('button,a,[role=button],[role=option],[role=checkbox],[role=radio],input[type=checkbox],input[type=radio],li');
    if (!el) return;
    window.__fixlog.push({k: 'click', t: label(el) || txt(el), at: Date.now()});
  }, true);
  document.addEventListener('change', e => {
    const el = e.target; if (!el || !el.tagName) return;
    const type = (el.type || '').toLowerCase();
    let v = el.value || '';
    if (el.tagName === 'SELECT' && el.selectedOptions[0]) v = txt(el.selectedOptions[0]);
    window.__fixlog.push({k: 'input', t: label(el), v: v.slice(0, 100), type, at: Date.now()});
  }, true);
})();
"""

SENSITIVE_VALUE_RE = re.compile(r"@|\d{7,}")


def describe(ev: dict) -> str | None:
    kind, target = ev.get("k"), (ev.get("t") or "").strip()
    if kind == "click":
        return f'Clicked "{target}"' if target else None
    if kind == "nav":
        return f"Went to {ev['t']}"
    if kind == "input":
        value = ev.get("v", "")
        hide = (ev.get("type") == "password" or learn.SENSITIVE_RE.search(target)
                or SENSITIVE_VALUE_RE.search(value) or len(value) > 60 or not value)
        if hide:
            return f'Entered "{target or "a field"}" (value not stored)'
        return f'Set "{target or "a field"}" to "{value}"'
    return None


def compress(events: list[dict]) -> list[str]:
    """Plain steps, dropping repeats (typing fires several events) and keeping order."""
    steps: list[str] = []
    for ev in events:
        s = describe(ev)
        if s and (not steps or steps[-1] != s):
            if ev.get("k") == "input" and steps and steps[-1].startswith(s.split(" to ")[0].split(" (")[0]):
                steps[-1] = s  # same field edited again: keep the final value
            else:
                steps.append(s)
    return steps[:MAX_STEPS]


def save_solution(problem: str, url: str, company: str, steps: list[str]) -> None:
    if not steps:
        return
    saved = json.loads(SOLUTIONS.read_text()) if SOLUTIONS.exists() else []
    saved.append({"at": datetime.now(timezone.utc).isoformat(), "company": company, "url": url,
                  "host": urlparse(url).netloc.lower(), "problem": problem, "steps": steps})
    tmp = SOLUTIONS.with_suffix(".tmp")
    tmp.write_text(json.dumps(saved[-200:], indent=1))
    os.replace(tmp, SOLUTIONS)


def ask_continue(title: str) -> bool:
    """True = continue the application now."""
    msg = (f"{title}: it looks like you fixed the problem. Continue the application from this page? "
           "(It continues by itself in 40 seconds.)")
    script = ('display alert "Continue the application?" message ' + json.dumps(msg) +
              ' buttons {"Still working", "Continue"} default button "Continue" giving up after 40')
    out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True).stdout
    return "Still working" not in out


def watch(port: int, title: str, max_minutes: float) -> tuple[str, list[dict], dict, dict]:
    """Returns (outcome, events, baseline, seen). outcome: 'continue' or 'closed'."""
    from playwright.sync_api import sync_playwright
    events: list[dict] = []
    baseline: dict | None = None
    seen: dict = {}
    urls: dict[int, str] = {}
    acted, last = False, time.time()
    deadline = time.time() + max_minutes * 60
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://localhost:{port}")
        context = browser.contexts[0]
        context.add_init_script(RECORDER_JS)
        while time.time() < deadline:
            if not browser.is_connected() or not context.pages:
                return "closed", events, baseline or {}, seen
            try:
                for page in context.pages:
                    try:
                        page.evaluate(RECORDER_JS)
                        got = page.evaluate("window.__fixlog.splice(0)") or []
                    except Exception:
                        continue
                    for ev in got:
                        events.append(ev)
                        acted, last = True, time.time()
                    u = page.url.split("?")[0]
                    if urls.get(id(page)) not in (None, u) and u.startswith("http"):
                        p_ = urlparse(u)
                        events.append({"k": "nav", "t": f"{p_.netloc}{p_.path}"[:120]})
                        acted, last = True, time.time()
                    urls[id(page)] = u
                snap = learn.snapshot_all(context)
            except Exception:
                return "closed", events, baseline or {}, seen
            if baseline is None:
                baseline = dict(snap)
            for q, a in snap.items():
                if a:
                    seen[q] = a
            if acted and time.time() - last >= IDLE_SECONDS:
                if ask_continue(title):
                    return "continue", events, baseline or {}, seen
                acted, last = False, time.time()
            time.sleep(POLL)
    return "closed", events, baseline or {}, seen


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--url", required=True)
    ap.add_argument("--company", default="")
    ap.add_argument("--title", default="")
    ap.add_argument("--reason", default="")
    ap.add_argument("--max-minutes", type=float, default=120)
    a = ap.parse_args()
    outcome, events, baseline, seen = watch(a.port, a.title or a.url, a.max_minutes)
    steps = compress(events)
    save_solution(a.reason, a.url, a.company, steps)
    learn.merge(ANSWERS, learn.new_answers(baseline, seen), a.company, a.url)
    print(f"{outcome}: recorded {len(steps)} step(s)")
    if outcome == "continue":
        subprocess.run([PY, str(Path(__file__).with_name("pipeline.py")), "--continue", a.url,
                        "--port", str(a.port)])
    return 0


if __name__ == "__main__":
    sys.exit(main())
