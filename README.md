# Job agent

An always-on agent that finds fresh internship postings and applies to them.

```
RoleDar  ->  bridge/pipeline.py  ->  ApplyPilot
(scrape      (filter, dedupe,        (score, tailor resume +
 job boards)  rate-limit, queue)      cover letter, fill the form)
```

- **RoleDar** watches company job boards (see [Job sources](#job-sources)).
- **`bridge/pipeline.py`** keeps only roles that match `title_regex` and `location_regex`, drops ones it has seen,
  and enforces `max_per_hour` and the freshness window.
- **ApplyPilot** tailors the resume and applies. `"auto_submit": false` fills forms without submitting. Start there.

Built on RoleDar and ApplyPilot. See [CREDITS.md](CREDITS.md) for who wrote what and under which licenses.

## Layout

| Path | What |
|---|---|
| `bridge/pipeline.py` | Main loop: scan, filter, queue, apply, alert |
| `bridge/watchlist.py`, `newsletter.py` | Extra job sources |
| `bridge/sync_projects.py` | Optionally updates resume project entries when a project folder changes |
| `bridge/learn.py`, `resolve.py` | Saves answers you type into a failed form so the agent reuses them (never passwords or IDs) |
| `bridge/sync_experience.py` | Optionally rewrites one resume entry from your git commit subjects |
| `bridge/report.py` | HTML reports of every application |
| `bridge/notify.py`, `guard.py` | Clickable popups; a watchdog that kills stuck cycles |
| `patches/` | My changes to RoleDar and ApplyPilot |
| `launchd/`, `run-*.sh` | macOS scheduling (hourly cycle, always-on daemon, daily report) |
| `settings.example.json` | All tunables |

## Job sources

| Source | Read by |
|---|---|
| Greenhouse, Ashby, Lever, SmartRecruiters, Workable | RoleDar |
| Workday | `patches/roledar/workday.py` |
| Amazon, IBM, Goldman Sachs, Google, Atlassian, and Eightfold or Oracle HCM sites | `patches/roledar/custom.py` |
| Other company career pages, including readers for Uber and Meta | `patches/roledar/site.py` and `custom.py`, set up per company in `companies.yaml` |

`patches/roledar/lanes.py` scans these in parallel within a time limit. Career sites change often, so expect some
readers to need fixes. Check the scan log for failures.

## Setup (macOS)

You need Python 3.11+, Git, Google Chrome, Node.js 18+, the [Claude Code CLI](https://claude.ai/code) (signed in), and
a Gemini API key from [aistudio.google.com](https://aistudio.google.com). A LaTeX install (`pdflatex`) is optional.

```bash
./scripts/setup.sh                                # download upstream projects, apply patches, install
.venv/bin/python scripts/onboard.py               # answer questions: your info and what jobs you want
.venv/bin/python bridge/pipeline.py --dry-run     # first run only records existing postings
./scripts/install-launchd.sh                      # optional: run it on a schedule
```

Read the code in `vendor/` before running it. It drives a real browser and can submit applications for you.

## Onboarding

`scripts/onboard.py` runs in two steps, and you can run it again to change your answers.

1. **About you.** It starts ApplyPilot's wizard (`applypilot init`): resume, contact details, work authorization,
   salary, skills, facts the AI must not change, and your Gemini key.
2. **What you want.** Preferred cities, remote roles, whether to include the rest of the US, applications per hour,
   software roles only or all internships,
   minimum fit score, auto-submit (off unless you turn it on) and the optional resume-from-commits feature.

It writes `settings.json` and `vendor/roledar/locations.yaml`, keeping the previous versions as `.bak` files.

Limits: it searches for internships and co-ops, and the location rules assume a US search. To watch different
companies, edit `vendor/roledar/companies.yaml`. `bridge/watchlist.py` has its own company list in the code.

## Your files

None of your data is in this repo. You add it through onboarding, or by hand:

| File | What it holds | How to add it |
|---|---|---|
| `~/.applypilot/resume.txt` | Plain-text resume the AI tailors from | Give the wizard a `.txt` resume. If you give it a PDF, also give it the text version |
| `~/.applypilot/profile.json` | Contact details, work authorization, skills, resume facts | Created by the wizard. See `vendor/ApplyPilot/profile.example.json` for the fields |
| `~/.applypilot/.env` | `GEMINI_API_KEY=your-key` | Created by the wizard, or add that line yourself |
| `settings.json` | Job preferences and limits | Created by onboarding, or copy `settings.example.json` |

The pipeline reads the first three through links in `./.applypilot/`. Onboarding creates them. If you add the files by
hand, run onboarding once and answer `n` when it offers the wizard.

`state/`, `logs/` and `reports/` (application history, logs and reports) are created as the agent runs. You don't add
them.

**Keep these private.** `profile.json` and `.env` hold personal data, keys and any job-site password in plain text.
`settings.json`, `.applypilot/`, `state/`, `logs/`, `reports/` and `resume.pdf` are git-ignored. Never commit them,
and check `git status` before you push.

To have project entries on your resume follow your project folders, add them to `experience_projects` in
`settings.json`. Each needs `dir` (the folder) and `entry` (the start of that entry's first line in `resume.txt`).
Optional: `author` (for group projects), `include` (file patterns) and `must_keep` (phrases to preserve).
This sends excerpts of those files to Claude, so list only folders you're comfortable sharing with it.

Edit `settings.json` any time to fine-tune. Tests: `cd bridge && ../.venv/bin/python -m unittest -v test_pipeline test_resolve test_sync_projects`
