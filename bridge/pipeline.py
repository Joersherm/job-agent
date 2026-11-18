#!/usr/bin/env python3
"""One cycle: RoleDar scan -> fresh NY/remote internships -> ApplyPilot.

Run it every 10-15 minutes (see run-cycle.sh). Each cycle:
  1. Runs RoleDar, which diffs company ATS boards against its last run.
  2. Keeps new rows that look like NY/remote internships.
  3. Queues them in ApplyPilot's database.
  4. Has ApplyPilot enrich, score, tailor and write cover letters.
  5. Applies to the freshest queued jobs, at most max_per_hour per rolling hour,
     skipping anything first seen more than max_age_minutes ago.

The very first RoleDar run sees every open posting as "new", so that run only
seeds the ledger and applies to nothing.

When something needs you, a clickable popup says what and opens the thing to
fix (see notify.py). The same problem re-alerts at most every 6 hours.
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import csv
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import newsletter
import sync_experience

ROOT =Path(__file__).resolve().parent.parent
ROLEDAR = ROOT / "vendor" / "roledar"
SETTINGS = ROOT / "settings.json"
LEDGER = ROOT / "state" / "ledger.json"
LOG = ROOT / "logs" / "cycle.log"
APPS = ROOT / "state" / "applications.jsonl"
PY = ROOT / ".venv" / "bin" / "python"
APPLYPILOT = ROOT / ".venv" / "bin" / "applypilot"
# The pipeline keeps its own ApplyPilot folder so it only ever scores and tailors the
# internships it queued (profile, resume and .env are symlinks to ~/.applypilot).
APPLYPILOT_DIR = Path(os.environ.get("APPLYPILOT_DIR", ROOT / ".applypilot"))
os.environ["APPLYPILOT_DIR"] = str(APPLYPILOT_DIR)
# Patched ApplyPilot leaves a failed application's Chrome window open (see kept_window()).
os.environ["APPLYPILOT_KEEP_FAILED"] = "1"
BASE_PORT = 9222  # apply slot N uses Chrome on port 9222 + N


def kept_path(port: int) -> Path:
    """Marker for the failed application's window left open on this port (one per browser slot)."""
    return APPLYPILOT_DIR / f"kept_chrome_{port}.json"


KEPT = kept_path(BASE_PORT)
APPLYPILOT_DB = APPLYPILOT_DIR / "applypilot.db"
ENV_FILE = APPLYPILOT_DIR / ".env"
PROFILE = APPLYPILOT_DIR / "profile.json"
NOTIFY = Path(__file__).with_name("notify.py")
INIT_CMD = f"cd {ROOT} && .venv/bin/applypilot init"


def now() -> datetime:
    return datetime.now(timezone.utc)


def log(msg: str) -> None:
    print(f"{now().isoformat(timespec='seconds')} {msg}", flush=True)


_SHARED: dict | None = None      # daemon mode: one ledger shared by the scanner and applier threads
LEDGER_LOCK = threading.RLock()
_POOL: futures.ThreadPoolExecutor | None = None   # daemon mode: application threads outlive one apply pass
_RUNNING: dict = {}
_LOGGED: dict = {}
_LAST_PREP = 0.0


def log_changed(key: str, msg: str) -> None:
    """Log a repeating status line only when it changes (the daemon polls every 30 seconds)."""
    if _LOGGED.get(key) != msg:
        _LOGGED[key] = msg
        log(msg)


def load_ledger(path: Path = LEDGER) -> dict:
    if _SHARED is not None and path == LEDGER:
        return _SHARED
    ledger = {"seen": {}, "queued": {}, "done": [], "attempts": [], "alerts": {}}
    if path.exists():
        ledger.update(json.loads(path.read_text()))
    return ledger


def save_ledger(ledger: dict, path: Path = LEDGER) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER_LOCK:
        for attempt in range(5):  # another thread may add a key while this one is being written
            try:
                text = json.dumps(ledger, indent=1)
                break
            except RuntimeError:
                if attempt == 4:
                    raise
                time.sleep(0.05)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text)
        os.replace(tmp, path)


def record_application(url: str, meta: dict, outcome: str, note: str, path: Path = APPS) -> None:
    """Append one line per application attempt; report.py turns these into the daily page."""
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"at": now().isoformat(), "url": url, "company": meta.get("company", ""),
             "title": meta.get("title", ""), "location": meta.get("location", ""),
             "outcome": outcome, "note": note}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def should_alert(ledger: dict, key: str, t: datetime, cooldown_hours: float = 6) -> bool:
    """True if this problem has not been alerted within the cooldown. Records the alert."""
    last = ledger["alerts"].get(key)
    if last and datetime.fromisoformat(last) > t - timedelta(hours=cooldown_hours):
        return False
    ledger["alerts"][key] = t.isoformat()
    return True


def alert(ledger: dict, key: str, title: str, message: str, kind: str, arg: str) -> None:
    """Fire a clickable popup (detached, so the cycle never waits on it)."""
    log(f"ALERT {title}: {message}")
    if not should_alert(ledger, key, now()):
        return
    save_ledger(ledger)
    subprocess.Popen(
        [sys.executable, str(NOTIFY), title, message[:420], kind, arg],
        start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def has_gemini_key() -> bool:
    if os.environ.get("GEMINI_API_KEY"):
        return True
    if not ENV_FILE.exists():
        return False
    for line in ENV_FILE.read_text().splitlines():
        name, _, value = line.partition("=")
        if name.strip() == "GEMINI_API_KEY" and value.strip().strip("'\""):
            return True
    return False


US_STATES = ("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND "
             "OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC").split()
US_RE = re.compile(r"United States|\bUSA?\b|\bU\.S\.|\b(" + "|".join(US_STATES) + r")\b(?!\w)")
NON_US_RE = re.compile(r"canada|\buk\b|united kingdom|london|europe|emea|apac|india|latam|mexico|brazil|"
                       r"germany|france|ireland|singapore|australia|israel|china|japan|toronto|vancouver", re.I)


NYC_RE = re.compile(r"new york|\bnyc\b|brooklyn|manhattan|queens|bronx|staten island", re.I)


def is_nyc(location: str) -> bool:
    return bool(NYC_RE.search(location or ""))


def priority(meta: dict) -> int:
    """0 = New York City, 1 = other local (DC / remote), 2 = rest of the US."""
    if meta.get("tier", "local") != "local":
        return 2
    return 0 if is_nyc(meta.get("location", "")) else 1


def is_us(location: str) -> bool:
    """True when the location text carries a US signal (country, or a state code) and no foreign one."""
    loc = location or ""
    return bool(US_RE.search(loc)) and not NON_US_RE.search(loc)


def select_fresh(rows: list[dict], ledger: dict, settings: dict, t: datetime, tier: str = "local",
                 mark_unmatched: bool = True) -> list[dict]:
    """Return RoleDar 'new' rows that match the filters and were never seen before.

    tier "local": NY / DC / remote (the regions RoleDar is configured for).
    tier "us": any other US internship, used to fill the hourly quota.
    Every new row is recorded in ledger['seen'], match or not, so a posting is
    only ever considered once.
    """
    title_re = re.compile(settings["title_regex"], re.I)
    loc_re = re.compile(settings["location_regex"], re.I)
    out = []
    for row in rows:
        if row.get("status") != "new":
            continue
        url = row["url"]
        if url in ledger["seen"]:
            continue
        loc = row.get("location", "")
        place_ok = loc_re.search(loc) if tier == "local" else (is_us(loc) and not loc_re.search(loc))
        ok = bool(title_re.search(row["title"]) and place_ok and posted_recently(row, settings, t))
        if ok or mark_unmatched:
            ledger["seen"][url] = t.isoformat()
        if ok:
            out.append({**row, "tier": tier})
    return out


def posted_recently(row: dict, settings: dict, t: datetime) -> bool:
    """True if the job board says the role was posted in the last posted_within_minutes.

    Boards that give no posting time (or an unreadable one) count as recent, because
    the role was only just discovered.
    """
    raw = row.get("posted_at") or ""
    try:
        posted = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return True
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)
    return posted >= t - timedelta(minutes=settings.get("posted_within_minutes", 70))


def scan_us_fallback(ledger: dict, settings: dict, t: datetime) -> list[dict]:
    """New US internships outside NY/DC, used only to fill the hourly quota.

    Uses RoleDar's --out-of-region snapshot. The first run only records what already exists
    (otherwise every open posting would look new), and the queue is capped so tailoring stays cheap.
    """
    seeding = not (ROLEDAR / "state" / "last_run_out_of_region.json").exists()
    started = time.time()
    if run([str(PY), "scrape.py", "--out-of-region"], cwd=ROLEDAR, timeout=1500) != 0:
        log("US fallback scan failed; continuing with NY/DC only")
        return []
    report = latest_report(started)
    if report is None:
        return []
    rows = select_fresh(read_rows(report), ledger, settings, t, tier="us")
    if seeding:
        log(f"US fallback seeded with {len(ledger['seen'])} known postings; none applied")
        return []
    rows.sort(key=lambda r: r.get("posted_at") or "", reverse=True)
    cap = settings.get("us_fallback_max_queue", 10)
    log(f"{len(rows)} new US internship(s) outside NY/DC (keeping up to {cap} as backups)")
    return rows[:cap]


def hour_slot(t: datetime) -> str:
    """The local clock hour, e.g. '2026-11-04 08'."""
    return t.astimezone().strftime("%Y-%m-%d %H")


def scan_due(ledger: dict, t: datetime) -> bool:
    return ledger.get("last_slot") != hour_slot(t)


def enqueue(conn: sqlite3.Connection, rows: list[dict], ledger: dict, t: datetime) -> int:
    """Insert rows into ApplyPilot's jobs table, same columns as its store_jobs()."""
    added = 0
    for row in rows:
        cur = conn.execute(
            "INSERT OR IGNORE INTO jobs (url, title, location, site, strategy, discovered_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (row["url"], row["title"], row.get("location"), row["company"], "roledar", t.isoformat()),
        )
        added += cur.rowcount
        ledger["queued"][row["url"]] = t.isoformat()
        ledger.setdefault("meta", {})[row["url"]] = {
            "company": row["company"], "title": row["title"], "location": row.get("location", ""),
            "tier": row.get("tier", "local")}
    conn.commit()
    return added


def hourly_budget(ledger: dict, settings: dict, t: datetime) -> int:
    hour_ago = t - timedelta(hours=1)
    used = sum(1 for a in ledger["attempts"] if datetime.fromisoformat(a) > hour_ago)
    return max(settings["max_per_hour"] - used, 0)


def reset_failed_scores(urls: list[str], ledger: dict, db: Path = APPLYPILOT_DB, max_retries: int = 3) -> int:
    """Re-queue jobs whose scoring hit an LLM error (ApplyPilot stores those as score 0).

    Without this, a Gemini rate limit silently turns a good internship into a
    score-0 skip. Each job is retried at most max_retries times.
    """
    reset = 0
    retries = ledger.setdefault("score_retries", {})
    conn = sqlite3.connect(db)
    try:
        for url in urls:
            if retries.get(url, 0) >= max_retries:
                continue
            cur = conn.execute(
                "UPDATE jobs SET fit_score = NULL, scored_at = NULL "
                "WHERE url = ? AND fit_score = 0 AND score_reasoning LIKE '%LLM error%'", (url,))
            if cur.rowcount:
                retries[url] = retries.get(url, 0) + 1
                reset += 1
        conn.commit()
    finally:
        conn.close()
    return reset


def kept_window(path: Path = KEPT) -> dict | None:
    """The Chrome window left open by the last failed application, if it is still running."""
    if not path.exists():
        return None
    try:
        info = json.loads(path.read_text())
        os.kill(info["pid"], 0)
    except (OSError, ValueError, KeyError):
        path.unlink(missing_ok=True)
        return None
    return info


def start_learning(window: dict, meta: dict, url: str) -> None:
    """Detached watcher that saves the questions you answer by hand in the open window."""
    subprocess.Popen(
        [str(PY), str(Path(__file__).with_name("learn.py")), "--port", str(window["port"]),
         "--company", meta.get("company", ""), "--url", url,
         "--out", str(APPLYPILOT_DIR / "learned_answers.json")],
        start_new_session=True, stdout=open(LOG, "a"), stderr=subprocess.STDOUT)


def start_resolve(window: dict, meta: dict, url: str, title: str, reason: str) -> None:
    """Detached watcher: records how you fix the problem, then continues the application."""
    subprocess.Popen(
        [str(PY), str(Path(__file__).with_name("resolve.py")), "--port", str(window["port"]),
         "--url", url, "--company", meta.get("company", ""), "--title", title or "", "--reason", reason or ""],
        start_new_session=True, stdout=open(LOG, "a"), stderr=subprocess.STDOUT)


def close_kept_window(path: Path = KEPT) -> None:
    info = kept_window(path)
    if info:
        try:
            os.killpg(info["pid"], signal.SIGTERM)
        except OSError:
            pass
    path.unlink(missing_ok=True)


def continue_application(url: str, settings: dict, port: int | None = None) -> int:
    """Carry on a stuck application in the window you just fixed, from the page you left it on."""
    ledger = load_ledger()
    meta = ledger.get("meta", {}).get(url, {})
    port = port or BASE_PORT
    window = kept_window(kept_path(port))
    if (not window or window.get("url") != url) and _port_up(port):
        # The marker is gone but the window you fixed is still there: adopt it and carry on.
        pids = subprocess.run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"], capture_output=True, text=True).stdout.split()
        if pids:
            window = {"pid": int(pids[0]), "port": port, "url": url, "reason": "", "at": time.time()}
            kept_path(port).write_text(json.dumps(window))
    if not window or window.get("url") != url:
        log("No open window for that application any more; nothing to continue")
        return 1
    mark_ready_for_apply(url)
    cmd = [str(APPLYPILOT), "apply", "--url", url, "--model", settings.get("apply_model", "haiku")]
    run(cmd, env={**os.environ, "APPLYPILOT_REUSE_CHROME": "1", "APPLYPILOT_CONTINUE": "1",
                  "APPLYPILOT_WORKER_ID": str(port - BASE_PORT),
                  "APPLYPILOT_ANY_US": "1" if priority(meta) > 0 else "0"})
    status, error, title, _ = apply_outcome(url)
    name = title or meta.get("title") or url
    if status == "applied":
        record_application(url, meta, "applied", "continued after manual fix")
        close_kept_window(kept_path(port))
        alert(ledger, f"continued:{url}", "Application submitted",
              f"{name}: submitted after your fix. It will remember how you solved it.", "url", url)
        return 0
    reason = error or (status if status not in (None, "pending") else None) or "did not submit"
    if reason in ("no_result_line", "did not submit", "failed"):
        reason = agent_note(slot=port - BASE_PORT) or reason
    if reason == "stuck_on_page":
        reason = "the agent got stuck on a page (probably a dropdown or question it could not answer) - please finish that part"
    record_application(url, meta, "needs you", reason)
    window = kept_window(kept_path(port))
    if window:
        start_resolve(window, meta, url, title or "", reason)
        alert(ledger, f"apply2:{url}:{now().isoformat()}", "Still stuck - application needs you",
              f"{name}: {reason}. Click to jump to the open application; I will keep watching "
              "and continue when you fix it.", "tab", f"{window['port']}|{url}")
    return 1


MANUAL_PORT = 9224
MANUAL_PROFILE = APPLYPILOT_DIR / "chrome-manual"
MANUAL_PID = ROOT / "state" / "manual_learn.pid"


def _port_up(port: int) -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2)
        return True
    except Exception:
        return False


def _pid_alive(pid_file: Path) -> bool:
    try:
        os.kill(int(pid_file.read_text()), 0)
        return True
    except (OSError, ValueError):
        return False


def open_manual_window(url: str, meta: dict) -> None:
    """Open the posting in a separate Chrome window (not the agent's) and watch it for answers you type.

    One window and one watcher serve every manual role; each role is just another tab. The watcher
    saves the questions you answer to learned_answers.json, which later agent runs reuse.
    """
    if _port_up(MANUAL_PORT):
        req = urllib.request.Request(f"http://127.0.0.1:{MANUAL_PORT}/json/new?{url}", method="PUT")
        try:
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass
    else:
        MANUAL_PROFILE.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["open", "-n", "-g", "-a", "Google Chrome", "--args",
                          f"--remote-debugging-port={MANUAL_PORT}", f"--user-data-dir={MANUAL_PROFILE}",
                          "--no-first-run", "--no-default-browser-check", url])
        for _ in range(20):
            if _port_up(MANUAL_PORT):
                break
            time.sleep(0.5)
    if not _pid_alive(MANUAL_PID) and _port_up(MANUAL_PORT):
        proc = subprocess.Popen(
            [str(PY), str(Path(__file__).with_name("learn.py")), "--port", str(MANUAL_PORT),
             "--company", "applied by hand", "--url", url, "--max-minutes", "240",
             "--out", str(APPLYPILOT_DIR / "learned_answers.json")],
            start_new_session=True, stdout=open(LOG, "a"), stderr=subprocess.STDOUT)
        MANUAL_PID.write_text(str(proc.pid))


def hold_for_open_window(settings: dict, path: Path = KEPT) -> bool:
    """True while a failed application's window is open and young enough to wait for.

    The next application reuses the same browser slot and would close that window,
    so applying pauses while you finish it. After keep_window_minutes it is closed.
    """
    info = kept_window(path)
    if info is None:
        return False
    if time.time() - info["at"] < settings.get("keep_window_minutes", 20) * 60:
        return True
    try:
        os.killpg(info["pid"], signal.SIGTERM)  # started in its own session, so pgid == pid
    except OSError:
        pass
    path.unlink(missing_ok=True)
    return False


def requeue_failed(ledger: dict, t: datetime, db: Path = APPLYPILOT_DB) -> list[str]:
    """Put jobs whose application failed back in the queue with a fresh window."""
    redo = []
    conn = sqlite3.connect(db)
    try:
        for url in list(ledger["done"]):
            cur = conn.execute("UPDATE jobs SET apply_status = NULL, apply_error = NULL "
                               "WHERE url = ? AND apply_status = 'failed'", (url,))
            if cur.rowcount:
                ledger["done"].remove(url)
                ledger["queued"][url] = t.isoformat()
                redo.append(url)
        conn.commit()
    finally:
        conn.close()
    return redo


RETRY_FILE = ROOT / "state" / "retry.txt"


def restart_requested(ledger: dict, t: datetime, path: Path = RETRY_FILE, db: Path = APPLYPILOT_DB) -> list[str]:
    """Restart applications listed (one URL, or part of one, per line) in state/retry.txt, then clear the file."""
    if not path.exists():
        return []
    wanted = [l.strip() for l in path.read_text().splitlines() if l.strip()]
    path.unlink(missing_ok=True)
    redo = []
    conn = sqlite3.connect(db)
    try:
        for url in list(ledger["queued"]):
            if any(w in url for w in wanted):
                conn.execute("UPDATE jobs SET apply_status = NULL, apply_error = NULL WHERE url = ?", (url,))
                if url in ledger["done"]:
                    ledger["done"].remove(url)
                ledger["queued"][url] = t.isoformat()  # fresh window
                redo.append(url)
        conn.commit()
    finally:
        conn.close()
    if redo:
        save_ledger(ledger)
        log("Restarting: " + ", ".join(redo))
    return redo


def job_state(url: str, db: Path = APPLYPILOT_DB) -> tuple[bool, int | None]:
    """(has a tailored resume, fit_score) for a queued job."""
    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT tailored_resume_path IS NOT NULL, fit_score FROM jobs WHERE url = ?",
                           (url,)).fetchone()
    finally:
        conn.close()
    return (bool(row[0]), row[1]) if row else (False, None)


def mark_ready_for_apply(url: str, db: Path = APPLYPILOT_DB) -> None:
    """`applypilot apply --url` selects with `apply_status != 'in_progress'`, which is NULL
    (no match) for a job that has never been tried. Give it a non-null status first."""
    conn = sqlite3.connect(db)
    try:
        conn.execute("UPDATE jobs SET apply_status = 'pending' WHERE url = ? AND apply_status IS NULL "
                     "AND tailored_resume_path IS NOT NULL", (url,))
        conn.commit()
    finally:
        conn.close()


SCRIPTED_ATS = ("jobs.ashbyhq.com", "smartrecruiters.com")
FREE_FIELDS = {"resume_text", "cover_letter_text"}


def greenhouse_blocker(url: str, fetch=None) -> str:
    """'' when a Greenhouse form only asks things the agent can answer, else why it cannot."""
    m = re.search(r"greenhouse\.io/(?:embed/job_app\?for=)?([^/?&]+)/jobs/(\d+)", url)
    if not m:
        return "Greenhouse form hosted on a custom page"
    api = f"https://boards-api.greenhouse.io/v1/boards/{m.group(1)}/jobs/{m.group(2)}?questions=true"
    try:
        data = json.load(fetch(api) if fetch else urllib.request.urlopen(api, timeout=15))
    except Exception:
        return "could not read the application form"
    for q in data.get("questions", []):
        if not q.get("required"):
            continue
        for f in q.get("fields", []):
            if f.get("type") == "textarea" and f.get("name") not in FREE_FIELDS:
                return f"requires a written answer: {q.get('label', '')[:80]}"
    return ""


def workday_tenant(url: str) -> str:
    m = re.match(r"https?://([^./]+)\.wd\d+\.myworkdayjobs\.com", url)
    return m.group(1) if m else ""


def apply_route(url: str, history: list[dict] | None = None, fetch=None) -> tuple[str, str]:
    """("auto", "") when the agent is expected to finish this form, else ("manual", reason)."""
    if history is None:
        history = [json.loads(l) for l in APPS.read_text().splitlines()] if APPS.exists() else []
    if "greenhouse.io" in url:
        why = greenhouse_blocker(url, fetch)
        return ("manual", why) if why else ("auto", "")
    if any(h in url for h in SCRIPTED_ATS):
        return "auto", ""
    if "jobs.lever.co" in url:  # Lever forms often sit behind a CAPTCHA; trust it only after a success
        if any("jobs.lever.co" in h["url"] and h["outcome"] == "applied" for h in history):
            return "auto", ""
        return "manual", "Lever forms usually need a CAPTCHA, which the agent cannot pass"
    tenant = workday_tenant(url)
    if tenant:
        worked = any(workday_tenant(h["url"]) == tenant and h["outcome"] == "applied" for h in history)
        if worked:
            return "auto", ""
        return "manual", "Workday needs a new account for this employer, which the agent has not set up successfully"
    return "manual", "this company's own application site is not one the agent has completed before"


def resume_pdf(url: str, db: Path = APPLYPILOT_DB) -> str:
    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT tailored_resume_path FROM jobs WHERE url = ?", (url,)).fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        return ""
    pdf = Path(row[0]).with_suffix(".pdf")
    return str(pdf if pdf.exists() else row[0])


def cover_pdf(url: str, db: Path = APPLYPILOT_DB) -> str:
    """The tailored cover letter PDF for a job, or '' when none was made."""
    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT cover_letter_path FROM jobs WHERE url = ?", (url,)).fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        return ""
    pdf = Path(row[0]).with_suffix(".pdf")
    return str(pdf) if pdf.exists() else ""


def pick_to_apply(ledger: dict, settings: dict, t: datetime) -> list[str]:
    """Freshest queued URLs that are still young enough, within the hourly budget."""
    cutoff = t - timedelta(minutes=settings["max_age_minutes"])
    budget = hourly_budget(ledger, settings, t)
    done = set(ledger["done"])
    fresh = [
        (url, ts) for url, ts in list(ledger["queued"].items())
        if url not in done and datetime.fromisoformat(ts) >= cutoff
    ]
    # New York roles first, then DC/remote, then the rest of the US; newest first within each group.
    fresh.sort(key=lambda x: x[1], reverse=True)
    fresh.sort(key=lambda x: priority(ledger.get("meta", {}).get(x[0], {})))
    return [url for url, _ in fresh[:budget]]


PLAIN = {
    "stuck_on_page": "The agent made no progress for a while on one page and gave up",
    "no_result_line": "The agent stopped without saying whether it submitted",
    "in_progress": "The application was cut off before it finished",
    "did not submit": "The agent finished without submitting",
    "failed": "The application failed",
}
CRASHES = ("in_progress", "no_result_line", "did not submit", "failed")  # nothing on the page to fix


def page_problems(port: int, url: str) -> str:
    """What the open application page is complaining about (invalid / required fields), in plain words."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://localhost:{port}", timeout=8000)
            pages = [pg for pg in browser.contexts[0].pages if pg.url.startswith("http")]
            base = url.split("?")[0].rstrip("/")
            page = next((pg for pg in pages if pg.url.split("?")[0].rstrip("/") == base), pages[0] if pages else None)
            if page is None:
                return ""
            found = page.evaluate("""() => {
              const label = el => {
                const l = el.id && document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
                return ((l && l.innerText) || el.getAttribute('aria-label') || el.name || '').replace(/\\s+/g, ' ').trim().slice(0, 50);
              };
              const out = [];
              document.querySelectorAll('[aria-invalid=true]').forEach(e => { const t = label(e); if (t) out.push(t); });
              document.querySelectorAll('[role=alert]').forEach(e => { const t = (e.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 70); if (t) out.push(t); });
              return [...new Set(out)].slice(0, 3);
            }""")
            return "; ".join(found)
    except Exception:
        return ""


def explain(reason: str, slot: int, url: str, window: dict | None) -> str:
    """Plain-English problem text: what the agent was doing, and what the page is complaining about."""
    reason = str(reason or "")
    plain = PLAIN.get(reason, reason or "It did not submit")
    note = agent_note(110, slot=slot)
    bits = [plain + "."]
    if note and note not in plain and note != reason:
        bits.append(f'Last thing it was doing: "{note}"')
    if window:
        problem = page_problems(window["port"], url)
        if problem:
            bits.append(f"The page is flagging: {problem}.")
    return " ".join(bits)


def agent_note(limit: int = 140, slot: int | None = None) -> str:
    """The last thing the apply agent said in its newest log (for that browser slot), for the popup text."""
    pattern = f"claude_*_w{slot}_*.txt" if slot is not None else "claude_*.txt"
    logs = sorted((APPLYPILOT_DIR / "logs").glob(pattern), key=lambda f: f.stat().st_mtime)
    if not logs:
        return ""
    lines = [l.strip(" *#") for l in logs[-1].read_text(errors="ignore").splitlines()
             if l.strip() and "RESULT:" not in l]
    return lines[-1][:limit] if lines else ""


def apply_outcome(url: str, db: Path = APPLYPILOT_DB) -> tuple[str | None, str | None, str | None, bool]:
    """(apply_status, apply_error, title, tailored) for a job, or Nones if unknown."""
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT apply_status, apply_error, title, tailored_resume_path IS NOT NULL "
            "FROM jobs WHERE url = ?", (url,)
        ).fetchone()
    finally:
        conn.close()
    return (row[0], row[1], row[2], bool(row[3])) if row else (None, None, None, False)


def latest_report(started: float) -> Path | None:
    reports = sorted((ROLEDAR / "reports" / "jobs").glob("*.csv"), key=lambda p: p.stat().st_mtime)
    if reports and reports[-1].stat().st_mtime >= started - 1:
        return reports[-1]
    return None


def read_rows(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def run(cmd: list[str], cwd: Path | None = None, timeout: int | None = None, env: dict | None = None) -> int:
    """Run a command; a scan that hangs is killed after `timeout` seconds instead of blocking every later cycle."""
    log("$ " + " ".join(str(c) for c in cmd))
    try:
        return subprocess.run(cmd, cwd=cwd, timeout=timeout, env=env).returncode
    except subprocess.TimeoutExpired:
        log(f"Timed out after {timeout}s and was stopped: {' '.join(str(c) for c in cmd)}")
        return 124


def queue_rows(rows: list[dict], ledger: dict, t: datetime) -> None:
    """Put found internships in the apply queue (the applier loop picks them up within 30 seconds)."""
    if not rows or not APPLYPILOT_DB.exists():
        return
    with LEDGER_LOCK:
        conn = sqlite3.connect(APPLYPILOT_DB)
        try:
            enqueue(conn, rows, ledger, t)
        finally:
            conn.close()
        save_ledger(ledger)
    log(f"Queued {len(rows)} internship(s) to apply to")


def scan_pass(settings: dict, ledger: dict) -> None:
    """One sweep of the job boards; whatever it finds is queued the moment it is found."""
    t = now()
    try:
        sync_experience.sync(settings, log)
    except Exception as exc:  # the resume is a side task; never block applications on it
        log(f"Resume sync skipped: {exc}")
    seeding = not (ROLEDAR / "state" / "last_run.json").exists()
    started = time.time()
    report = None
    if run([str(PY), "scrape.py"], cwd=ROLEDAR, timeout=1500) == 0:
        report = latest_report(started)
    if report is None:
        alert(ledger, "roledar", "Job scan failed",
              "RoleDar could not scan company boards. Click to open the log.", "file", str(LOG))
        return
    local_rows = read_rows(report)
    fresh = select_fresh(local_rows, ledger, settings, t, mark_unmatched=False)
    if seeding:
        log(f"Seed run: recorded {len(ledger['seen'])} postings, applying to none")
        save_ledger(ledger)
        return
    log(f"{len(fresh)} new NYC/DC matching internship(s) in this sweep")
    queue_rows(fresh, ledger, t)
    # Reach beyond NYC/DC only when there are not enough local roles to fill the hour's quota.
    if settings.get("us_fallback", True) and sum(is_nyc(r.get("location", "")) for r in fresh) < hourly_budget(ledger, settings, t):
        more = select_fresh(local_rows, ledger, settings, t, tier="us") + scan_us_fallback(ledger, settings, t)
        queue_rows(more, ledger, t)
    try:
        queue_rows(newsletter.select(newsletter.fetch_items(datetime.now()), ledger, t), ledger, t)
    except Exception as e:
        alert(ledger, "newsletter", "Newsletter check failed",
              f"Could not read Direct Consideration ({type(e).__name__}). Click to open the log.",
              "file", str(LOG))
    save_ledger(ledger)


def daemon() -> int:
    """Run forever: one thread rescans every scan_gap_minutes, the main loop applies to what is queued."""
    global _SHARED
    _SHARED = load_ledger()
    log("Daemon started")

    def scan_loop() -> None:
        while True:
            began = time.time()
            settings = json.loads(SETTINGS.read_text())
            if settings.get("llm_model"):
                os.environ["LLM_MODEL"] = settings["llm_model"]
            try:
                scan_pass(settings, _SHARED)
            except Exception as exc:
                log(f"Scan pass failed: {exc}")
            wait = settings.get("scan_gap_minutes", 15) * 60 - (time.time() - began)
            time.sleep(max(wait, 60))

    threading.Thread(target=scan_loop, name="scanner", daemon=True).start()
    while True:
        try:
            main(["--apply-only"])
        except Exception as exc:
            log(f"Apply pass failed: {exc}")
        time.sleep(30)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="Fill application forms but do not submit.")
    parser.add_argument("--test-notify", action="store_true", help="Show a sample popup and exit.")
    parser.add_argument("--retry-failed", action="store_true",
                        help="Re-queue applications that failed; the next cycle retries them.")
    parser.add_argument("--continue", dest="continue_url", metavar="URL",
                        help="Continue a stuck application in its open window (used by resolve.py).")
    parser.add_argument("--port", type=int, help="(with --continue) the window's debug port.")
    parser.add_argument("--daemon", action="store_true",
                        help="Run forever: rescan every scan_gap_minutes while applying to what is found.")
    parser.add_argument("--apply-only", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.daemon:
        return daemon()

    if args.continue_url:
        cfg = json.loads(SETTINGS.read_text())
        if cfg.get("llm_model"):
            os.environ["LLM_MODEL"] = cfg["llm_model"]
        os.environ["APPLYPILOT_APPLY_IDLE"] = str(int(cfg.get("apply_idle_minutes", 10)) * 60)
        os.environ["APPLYPILOT_APPLY_TIMEOUT"] = str(int(cfg.get("apply_timeout_minutes", 60)) * 60)
        return continue_application(args.continue_url, cfg, args.port)

    if args.retry_failed:
        ledger = load_ledger()
        redo = requeue_failed(ledger, now())
        save_ledger(ledger)
        print(f"Re-queued {len(redo)} failed application(s)")
        return 0

    if args.test_notify:
        LOG.parent.mkdir(exist_ok=True)
        LOG.touch()
        subprocess.run([sys.executable, str(NOTIFY), "Job agent test",
                        "Click to open the log file.", "file", str(LOG)])
        return 0

    settings = json.loads(SETTINGS.read_text())
    if settings.get("llm_model"):
        os.environ["LLM_MODEL"] = settings["llm_model"]  # wins over the model in .env
    os.environ["APPLYPILOT_APPLY_IDLE"] = str(int(settings.get("apply_idle_minutes", 10)) * 60)
    os.environ["APPLYPILOT_APPLY_TIMEOUT"] = str(int(settings.get("apply_timeout_minutes", 60)) * 60)
    ledger = load_ledger()
    t = now()
    restart_requested(ledger, t)

    if not args.apply_only:
        try:
            sync_experience.sync(settings, log)
        except Exception as exc:  # the resume is a side task; never block applications on it
            log(f"Resume sync skipped: {exc}")

    # Scan for new postings once per clock hour. If the computer was asleep or the scan failed,
    # every later poll in the same hour tries again; once it succeeds, polls only work the queue.
    fresh: list[dict] = []
    bg = None
    more: dict = {}
    if not args.apply_only and scan_due(ledger, t):
        seeding = not (ROLEDAR / "state" / "last_run.json").exists()
        started = time.time()
        report = None
        if run([str(PY), "scrape.py"], cwd=ROLEDAR, timeout=1500) == 0:
            report = latest_report(started)
        if report is None:
            alert(ledger, "roledar", "Job scan failed",
                  "RoleDar could not scan company boards. Click to open the log.", "file", str(LOG))
            return 1

        local_rows = read_rows(report)
        fresh = select_fresh(local_rows, ledger, settings, t, mark_unmatched=False)
        if seeding:
            log(f"Seed run: recorded {len(ledger['seen'])} postings, applying to none")
            ledger["last_slot"] = hour_slot(t)
            save_ledger(ledger)
            return 0
        # Beyond NYC/DC (rest-of-US scan, newsletter) runs in the background while the NYC/DC roles found
        # above are already being applied to. It works on a scratch copy of 'seen'; the main thread merges it.
        need_us = (settings.get("us_fallback", True)
                   and sum(is_nyc(r.get("location", "")) for r in fresh) < hourly_budget(ledger, settings, t))
        if settings.get("us_fallback", True) and not need_us:
            log(f"{len(fresh)} NYC/DC internships found (enough in NYC); skipping the rest-of-US scan")
        scratch = {"seen": dict(ledger["seen"])}

        def scan_rest() -> None:
            try:
                if need_us:
                    # in-region rows outside NYC/DC (e.g. upstate NY) count as US backups too
                    more["rows"] += select_fresh(local_rows, scratch, settings, t, tier="us")
                    more["rows"] += scan_us_fallback(scratch, settings, t)
                try:
                    items = newsletter.select(newsletter.fetch_items(datetime.now()), scratch, t)
                    log(f"{len(items)} new matching internship(s) from the newsletter")
                    more["rows"] += items
                except Exception as e:
                    more["newsletter_error"] = type(e).__name__
            except Exception as e:
                log(f"Background scan failed: {e}")

        more = {"rows": [], "newsletter_error": None}
        bg = threading.Thread(target=scan_rest, name="scan-rest")
        bg.start()
        ledger["last_slot"] = hour_slot(t)
        log(f"{len(fresh)} new NYC/DC matching internship(s) posted in the last "
            f"{settings.get('posted_within_minutes', 70)} minutes; the rest-of-US scan continues in the background")
    elif not args.apply_only:
        log("Already scanned this hour; only working through the queue")

    def work(rows: list[dict]) -> int:
        """Queue these rows, prepare them and apply (several at once), within the hourly cap."""
        if not APPLYPILOT_DB.exists() or not PROFILE.exists():
            save_ledger(ledger)
            alert(ledger, "no-setup", "Finish ApplyPilot setup",
                  "No profile yet. Click to run applypilot init in Terminal.", "terminal", INIT_CMD)
            return 1
        if not has_gemini_key():
            save_ledger(ledger)
            alert(ledger, "no-key", "Gemini API key missing",
                  "Add GEMINI_API_KEY to the file that opens when you click.", "file", str(ENV_FILE))
            return 1

        conn = sqlite3.connect(APPLYPILOT_DB)
        try:
            enqueue(conn, rows, ledger, t)
        finally:
            conn.close()
        save_ledger(ledger)

        # Everything still fresh and untried; the hourly cap is enforced as applications happen.
        to_apply = pick_to_apply(ledger, {**settings, "max_per_hour": 10**6}, t)
        if not to_apply:
            log_changed("idle", "Nothing fresh to apply to") if args.apply_only else log("Nothing fresh to apply to")
            return 0
        budget = hourly_budget(ledger, settings, t)
        if budget == 0:
            log_changed("idle", "Hourly cap reached; waiting") if args.apply_only else log("Hourly cap reached; waiting")
            return 0

        global _LAST_PREP
        if args.apply_only:
            if all(job_state(u)[0] for u in to_apply) or time.time() - _LAST_PREP < 300:
                to_apply = [u for u in to_apply if job_state(u)[0]]
                if not to_apply:
                    return 0
                skip_prep = True
            else:
                skip_prep = False
            _LAST_PREP = time.time() if not skip_prep else _LAST_PREP
        else:
            skip_prep = False
        reset = 0 if skip_prep else reset_failed_scores(to_apply, ledger)
        if reset:
            log(f"Re-queued {reset} job(s) whose scoring hit an LLM error")
            save_ledger(ledger)
        if not skip_prep and run([str(APPLYPILOT), "run", "enrich", "score", "tailor", "cover",
                                  "--min-score", str(settings["min_score"])]) != 0:
            alert(ledger, "ap-run", "ApplyPilot could not prepare applications",
                  "Tailoring failed. Click to open the log.", "file", str(LOG))
        def apply_order(url: str) -> tuple:
            meta = ledger.get("meta", {}).get(url, {})
            return (priority(meta), -(job_state(url)[1] or 0))
        to_apply.sort(key=apply_order)  # New York first, then DC/remote, then the best-fitting US backups
        lock = threading.Lock()
        slots = settings.get("apply_slots", [0, 6, 7, 8])  # browser slots: Chrome on port 9222 + slot
        global _POOL
        if args.apply_only:  # daemon: applications keep running between passes
            if _POOL is None:
                _POOL = futures.ThreadPoolExecutor(max_workers=len(slots))
            pool, running = _POOL, _RUNNING
        else:
            pool, running = futures.ThreadPoolExecutor(max_workers=len(slots)), {}

        def free_slot() -> int | None:
            for f in [f for f in running if f.done()]:  # reap finished applications
                running.pop(f)
                if f.exception():
                    log(f"An application thread crashed: {f.exception()}")
            busy = set(running.values())
            for sl in slots:
                if sl not in busy and not hold_for_open_window(settings, kept_path(BASE_PORT + sl)):
                    return sl
            return None

        def apply_one(url: str, slot: int) -> None:
            """One application in its own browser slot; a stuck one only ties up that slot."""
            port = BASE_PORT + slot
            meta = ledger.get("meta", {}).get(url, {})
            dry = args.dry_run or not settings["auto_submit"]
            mark_ready_for_apply(url)
            cmd = [str(APPLYPILOT), "apply", "--url", url, "--model", settings.get("apply_model", "haiku")]
            if dry:
                cmd.append("--dry-run")
            env = {**os.environ, "APPLYPILOT_WORKER_ID": str(slot),
                   "APPLYPILOT_ANY_US": "1" if priority(meta) > 0 else "0"}  # outside NYC: any US city is fine
            rc = run(cmd, env=env)
            if dry:
                record_application(url, meta, "dry run", "")
                return
            status, error, title, _ = apply_outcome(url)
            if rc == 0 and status == "applied":
                record_application(url, meta, "applied", "")
                return
            reason = error or (status if status not in (None, "pending") else None) or "did not submit"
            window = kept_window(kept_path(port))
            if reason in CRASHES and not window:
                # It crashed or was cut off with nothing on the page to fix: retry once by itself, no popup.
                tries = ledger.setdefault("auto_retries", {})
                if tries.get(url, 0) < 1:
                    tries[url] = tries.get(url, 0) + 1
                    log(f"Retrying once by itself ({reason}): {meta.get('title') or url}")
                    RETRY_FILE.parent.mkdir(exist_ok=True)
                    with open(RETRY_FILE, "a") as fh:
                        fh.write(url + "\n")
                    return
            if str(reason).startswith("not_eligible"):
                # The agent read the posting and you do not qualify (e.g. location): nothing for you to fix.
                record_application(url, meta, "skipped", f"not eligible: {reason}")
                close_kept_window(kept_path(port))
                return
            what = explain(reason, slot, url, window)
            record_application(url, meta, "needs you", what)
            with lock:
                if window and window.get("url") == url:
                    start_resolve(window, meta, url, title or "", what)
                    alert(ledger, f"apply:{url}", "Application needs you",
                          f"{title or url}: {what} Click to jump to the application, exactly where it stopped. "
                          "Fix it there; I'll record how and continue once you're done.",
                          "tab", f"{window['port']}|{url}")
                else:
                    alert(ledger, f"apply:{url}", "Application needs you",
                          f"{title or url}: {what} No window was left open, so click opens the posting.", "url", url)

        for url in to_apply:
            if budget == 0:
                break
            meta = ledger.get("meta", {}).get(url, {})
            has_resume, fit = job_state(url)
            if not has_resume:
                exhausted = ledger.get("score_retries", {}).get(url, 0) >= 3
                if (fit is not None and fit > 0) or exhausted:
                    log(f"Skipped (scored below min_score or scoring kept failing): {meta.get('title') or url}")
                    record_application(url, meta, "skipped", "scored below min_score or scoring failed")
                    ledger["done"].append(url)
                    save_ledger(ledger)
                else:
                    log_changed("ready:" + url, f"Not ready yet (scoring/tailoring pending), will retry: {meta.get('title') or url}")
                continue
            route, why = apply_route(url) if settings.get("preflight", True) else ("auto", "")
            if route == "manual":
                pdf = resume_pdf(url)
                cover = cover_pdf(url)
                name = f"{meta.get('company', '')} {meta.get('title') or ''}".strip() or url
                log(f"Not auto-applying ({why}): {name}")
                record_application(url, meta, "resume ready (apply by hand)", why)
                ledger["done"].append(url)
                save_ledger(ledger)
                alert(ledger, f"manual:{url}", "Resume ready - apply by hand",
                      f"{name}: {why}. " + ("View the tailored resume" + (" and cover letter" if cover else "")
                                           + ", then Apply to jump to the application "
                                           "(open in a window that saves your answers)." if pdf else
                                           "No resume could be made; click to open the posting."),
                      *(("resume_apply", f"{pdf}|{url}|{cover}") if pdf else ("url", url)))
                try:
                    open_manual_window(url, meta)
                except Exception as exc:
                    log(f"Could not open the watched window: {exc}")
                continue
            while True:  # wait for a browser slot that no stuck application is sitting in
                slot = free_slot()
                if slot is not None or not running:
                    break
                finished, _ = futures.wait(running, return_when=futures.FIRST_COMPLETED)
                for f in finished:
                    running.pop(f)
                    if f.exception():
                        log(f"An application thread crashed: {f.exception()}")
            if slot is None:
                log_changed("slots", "Every browser slot has a stuck application open; waiting for you to finish one")
                break
            budget -= 1
            with lock:  # counted now, so the hourly cap and "already tried" hold while it is still running
                ledger["done"].append(url)
                ledger["attempts"].append(now().isoformat())
                save_ledger(ledger)
            running[pool.submit(apply_one, url, slot)] = slot
        if not args.apply_only:
            for f in futures.as_completed(list(running)):
                if f.exception():
                    log(f"An application thread crashed: {f.exception()}")
            pool.shutdown()
        return 0

    rc = work(fresh)
    if bg is not None:
        bg.join()
        for url, seen_at in scratch["seen"].items():
            ledger["seen"].setdefault(url, seen_at)
        save_ledger(ledger)
        if more.get("newsletter_error"):
            alert(ledger, "newsletter", "Newsletter check failed",
                  f"Could not read Direct Consideration ({more['newsletter_error']}). Click to open the log.",
                  "file", str(LOG))
        if more["rows"]:
            log(f"{len(more['rows'])} more internship(s) from the rest-of-US scan / newsletter; applying to those next")
            rc = work(more["rows"]) or rc
    return rc


if __name__ == "__main__":
    sys.exit(main())
