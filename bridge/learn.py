#!/usr/bin/env python3
"""Watch a failed application's Chrome window and save the questions you answer by hand.

  learn.py --port 9222 --company Zebra --url JOB_URL --out learned_answers.json

Started by pipeline.py when a failed application is left open. It reads the form
fields over Chrome's debug port. A field that was empty when the window was left
open and has a value by the time you finish counts as a question the agent did
not know, and its answer is saved. The agent's prompt reuses saved answers.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

POLL_SECONDS = 2
MAX_QUESTION = 200
MAX_ANSWER = 500

# Never saved, whatever the field.
SENSITIVE_RE = re.compile(
    r"password|passcode|\bssn\b|social security|security answer|one.time|verification code|"
    r"card number|cvv|routing|account number|date of birth|\bdob\b", re.I)
PLACEHOLDER_RE = re.compile(r"^(select( one)?|choose|please select|--+|search)\b", re.I)

SNAPSHOT_JS = r"""
() => {
  const out = [];
  const txt = el => ((el && (el.innerText || el.textContent)) || '').replace(/\s+/g, ' ').trim();
  const visible = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const labelOf = el => {
    const al = el.getAttribute('aria-label'); if (al) return al;
    const lb = el.getAttribute('aria-labelledby');
    if (lb) { const t = lb.split(/\s+/).map(i => txt(document.getElementById(i))).join(' ').trim(); if (t) return t; }
    if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) return txt(l); }
    const wrap = el.closest('label'); if (wrap) return txt(wrap);
    const fs = el.closest('fieldset'); if (fs) { const lg = fs.querySelector('legend'); if (lg) return txt(lg); }
    return el.getAttribute('placeholder') || el.name || '';
  };
  const groupLabel = el => {
    const fs = el.closest('fieldset'); if (fs) { const lg = fs.querySelector('legend'); if (lg) return txt(lg); }
    const g = el.closest('[role=radiogroup],[role=group]');
    if (g) { const al = g.getAttribute('aria-label'); if (al) return al;
             const lb = g.getAttribute('aria-labelledby');
             if (lb) return lb.split(/\s+/).map(i => txt(document.getElementById(i))).join(' ').trim(); }
    return '';
  };
  const radios = {};
  for (const el of document.querySelectorAll('input,textarea,select,button[aria-haspopup]')) {
    if (!visible(el)) continue;
    const type = (el.type || '').toLowerCase();
    if (['password', 'file', 'hidden', 'submit', 'button', 'image', 'reset', 'search', 'checkbox'].includes(type)) continue;
    if (el.tagName === 'BUTTON') { out.push({q: labelOf(el), v: txt(el)}); continue; }
    if (type === 'radio') {
      const q = groupLabel(el); if (!q) continue;
      radios[q] = radios[q] || '';
      if (el.checked) radios[q] = labelOf(el);
      continue;
    }
    if (el.tagName === 'SELECT') { const o = el.selectedOptions[0]; out.push({q: labelOf(el), v: o && el.value ? txt(o) : ''}); continue; }
    out.push({q: labelOf(el), v: el.value || ''});
  }
  for (const q of Object.keys(radios)) out.push({q, v: radios[q]});
  return out;
}
"""


def normalize(question: str) -> str:
    q = re.sub(r"\s+", " ", question).strip()
    q = re.sub(r"\b(select one|required)\b", "", q, flags=re.I)
    q = re.sub(r"[*:\s]+$", "", q).strip()
    return q[:MAX_QUESTION]


def usable(question: str, answer: str) -> bool:
    if not question or not answer.strip():
        return False
    if SENSITIVE_RE.search(question) or PLACEHOLDER_RE.match(answer.strip()):
        return False
    return True


def clean_snapshot(raw: list[dict]) -> dict[str, str]:
    """{normalized question: answer}, dropping sensitive fields and placeholders."""
    out = {}
    for item in raw:
        q = normalize(item.get("q", ""))
        v = re.sub(r"\s+", " ", item.get("v", "")).strip()[:MAX_ANSWER]
        if PLACEHOLDER_RE.match(v):
            v = ""
        if q and not SENSITIVE_RE.search(q):
            out[q] = v
    return out


def new_answers(baseline: dict[str, str], seen: dict[str, str]) -> dict[str, str]:
    """Questions that were empty at the start and later got a usable answer."""
    return {q: a for q, a in seen.items() if usable(q, a) and not baseline.get(q)}


def merge(path: Path, learned: dict[str, str], company: str, url: str) -> int:
    saved = json.loads(path.read_text()) if path.exists() else {}
    when = datetime.now(timezone.utc).isoformat()
    for q, a in learned.items():
        saved[q.lower()] = {"question": q, "answer": a, "company": company, "url": url, "at": when}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(saved, indent=1))
    os.replace(tmp, path)
    return len(learned)


def snapshot_all(context) -> dict[str, str]:
    merged: dict[str, str] = {}
    for page in context.pages:
        for frame in page.frames:
            try:
                merged.update(clean_snapshot(frame.evaluate(SNAPSHOT_JS)))
            except Exception:
                continue  # frame navigating or closed
    return merged


def watch(port: int, max_minutes: float) -> tuple[dict[str, str], dict[str, str]]:
    """Poll the window until it closes. Returns (baseline, last non-empty answer per question)."""
    from playwright.sync_api import sync_playwright
    baseline: dict[str, str] | None = None
    seen: dict[str, str] = {}
    deadline = time.time() + max_minutes * 60
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://localhost:{port}")
        context = browser.contexts[0]
        while time.time() < deadline:
            # Playwright swallows evaluate errors per frame, so check for a closed window explicitly.
            if not browser.is_connected() or not context.pages:
                break
            try:
                snap = snapshot_all(context)
            except Exception:
                break  # window closed
            if baseline is None:
                baseline = dict(snap)
            for q, a in snap.items():
                if a:
                    seen[q] = a
            time.sleep(POLL_SECONDS)
    return baseline or {}, seen


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--company", default="")
    parser.add_argument("--url", default="")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--max-minutes", type=float, default=25)
    args = parser.parse_args()
    baseline, seen = watch(args.port, args.max_minutes)
    saved = merge(args.out, new_answers(baseline, seen), args.company, args.url)
    print(f"saved {saved} new answer(s) to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
