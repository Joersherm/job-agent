"""Scrape companies in parallel lanes, with a time budget.

Each lane is its own process, so each has its own Playwright and its own Chrome (Workday and the
site scrapers use different ports). Inside a lane companies are still read one at a time, so no
job board gets more requests per second than before. When the budget runs out, the report is built
from what finished; the rest are left for the next scan instead of the whole scan being lost.
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import os
import queue
import random
import time

log = logging.getLogger("scrape")

BUDGET_SECONDS = int(os.environ.get("SCAN_BUDGET_SECONDS", "1380"))  # below the caller's 1500s kill
BROWSER_ATS = {"site", "meta", "uber"}


def lane_of(company) -> str:
    if company.ats == "workday":
        return "workday"
    if company.ats in BROWSER_ATS or company.ats_config.get("browser") or company.ats_config.get("render"):
        return "browser"  # everything else that drives the site Chrome shares one lane
    return "http"


def _work(jobs, out, parent: int) -> None:
    import scrape  # in the child process

    try:
        for index, company in jobs:
            if os.getppid() != parent:  # the scan was killed: do not carry on alone
                break
            log.info("Scraping %s (%s)", company.name, company.ats)
            r = scrape.scrape_one(company)
            if r.succeeded:
                log.info("  %s: %d total postings", company.name, len(r.postings))
            elif r.skipped_reason:
                log.info("  %s: skipped (ats=%s)", company.name, r.skipped_reason)
            else:
                log.warning("  %s: FAILED — %s", company.name, r.error)
            out.put((index, r.postings, r.error, r.skipped_reason))
    finally:
        _close_browsers()


def _close_browsers() -> None:
    """Child processes do not run atexit hooks, so each lane closes its own Chrome."""
    from scrapers import site, workday

    for state, close in ((site._browser, site.close_browser), (workday._state, workday._close_browser)):
        if state["browser"] is not None:
            close()


def scrape_all(companies, budget: float = BUDGET_SECONDS, target=_work) -> dict[int, tuple]:
    """{company index: (postings, error, skipped_reason)} for every company finished in time."""
    lanes: dict[str, list] = {}
    for index, company in enumerate(companies):
        lanes.setdefault(lane_of(company), []).append((index, company))

    for jobs in lanes.values():
        random.shuffle(jobs)  # shortcut: random fairness; persist a resume point if lanes keep overrunning
    ctx = mp.get_context("spawn")
    out = ctx.Queue()
    workers = {name: ctx.Process(target=target, args=(jobs, out, os.getpid()), name=name, daemon=True)
               for name, jobs in lanes.items()}
    for w in workers.values():
        w.start()

    done: dict[int, tuple] = {}
    end = time.monotonic() + budget
    while len(done) < len(companies) and time.monotonic() < end:
        try:
            index, *result = out.get(timeout=0.5)
        except queue.Empty:
            if not any(w.is_alive() for w in workers.values()) and out.empty():
                break
            continue
        done[index] = tuple(result)

    stopped = False
    for name, w in workers.items():
        w.join(timeout=max(0.0, min(30.0, end - time.monotonic())) if len(done) == len(companies) else 0)
        if w.is_alive():
            stopped = True
            log.warning("Lane %s did not finish in time; stopping it", name)
            w.terminate()
            w.join(timeout=10)
    if stopped:
        _kill_scan_chromes()

    for name, jobs in lanes.items():
        missed = [c.name for i, c in jobs if i not in done]
        if missed:
            log.warning("Lane %s: %d compan(ies) not reached this scan, starting with %s", name, len(missed), missed[0])
    return done


def _kill_scan_chromes() -> None:
    """A stopped lane cannot close its Chrome, so close the scan's own windows by port."""
    from scrapers import site, workday

    site.close_browser()
    workday._close_browser()
