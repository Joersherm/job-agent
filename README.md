# Job agent

An always-on agent that finds fresh internship postings and applies to them.

```
RoleDar  ->  bridge/pipeline.py  ->  ApplyPilot
(scrape      (filter, dedupe,        (score, tailor resume +
 job boards)  rate-limit, queue)      cover letter, fill the form)
```

- **RoleDar** watches company ATS boards.
- **`bridge/pipeline.py`** keeps only roles that match `title_regex` and `location_regex`, drops ones it has seen,
  and enforces `max_per_hour` and the freshness window.
- **ApplyPilot** tailors the resume and applies. `"auto_submit": false` fills forms without submitting. Start there.

Built on RoleDar and ApplyPilot. See [CREDITS.md](CREDITS.md) for who wrote what and under which licenses.

## Layout

| Path | What |
|---|---|
| `bridge/pipeline.py` | Main loop: scan, filter, queue, apply, alert |
| `bridge/watchlist.py`, `newsletter.py` | Extra job sources |
| `bridge/learn.py`, `resolve.py` | Saves answers you type into a failed form so the agent reuses them (never passwords or IDs) |
| `bridge/sync_experience.py` | Optionally rewrites one resume entry from your git commit subjects |
| `bridge/report.py` | HTML reports of every application |
| `bridge/notify.py`, `guard.py` | Clickable popups; a watchdog that kills stuck cycles |
| `patches/` | My changes to RoleDar and ApplyPilot |
| `launchd/`, `run-*.sh` | macOS scheduling (hourly cycle, always-on daemon, daily report) |
| `settings.example.json` | All tunables |

## Setup (macOS)

```bash
./scripts/setup.sh                      # clones upstream at pinned commits, applies patches, builds .venv
.venv/bin/applypilot init               # your resume, profile, work authorization, Gemini API key
.venv/bin/python bridge/pipeline.py --dry-run   # first run only seeds the ledger
./scripts/install-launchd.sh            # optional: run it on a schedule
```

Edit `settings.json` (created from `settings.example.json`), and the company list in `vendor/roledar/companies.yaml`.
Read the code in `vendor/` before running it. It drives a real browser and can submit applications for you.

Tests: `cd bridge && ../.venv/bin/python -m unittest -v test_pipeline test_resolve`

## Not in this repo

Your resume, profile, API keys, application history, logs and reports live in `.applypilot/`, `state/`, `logs/`,
`reports/` and `resume.pdf`. All are git-ignored. Don't commit them.
