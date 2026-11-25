# Credits and licenses

This project is glue plus patches on top of other people's open-source work. It would not exist without them.
The upstream code is **not** copied into this repo. `scripts/setup.sh` clones it at a pinned commit and applies
the patches in `patches/`.

| Project | Author | License | What this repo uses it for |
|---|---|---|---|
| [RoleDar](https://github.com/dalecook/roledar) | Dale Cook | MIT | Scrapes company job boards (Greenhouse, Ashby, Lever, ...). Patched: extra filters, Workday and company career-site scrapers, parallel scanning, company list. Pinned at `e432d7a`. |
| [ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot) | Pickle-Pixel and contributors | AGPL-3.0 | Scores jobs, tailors the resume and cover letter, fills out applications. Patched: apply prompt, Chrome handling, scoring and tailoring prompts, LaTeX resume output. Pinned at `4a8d521`. |
| [ai-job-search](https://github.com/MadsLorentzen/ai-job-search) | Mads Lorentzen | MIT | Cloned and read for ideas while building this. No code from it is used. |
| Jake's Resume (LaTeX template) | Jake Gutierrez, building on Sourabh Bajaj's template | MIT | The resume layout that `latex_resume.py` imitates. |
| [Direct Consideration](https://directconsideration.com) | — | — | A newsletter of internships that `bridge/newsletter.py` reads. |

## What is original here

`bridge/` (the pipeline that ties the pieces together, queueing, rate limiting, notifications, the answer
learner, the reports, the tests), `run-*.sh`, `launchd/`, and `scripts/`.

## License notes

- ApplyPilot is AGPL-3.0, so `patches/applypilot/` (a modification of it) is AGPL-3.0 as well.
- `patches/roledar/` follows RoleDar's MIT license.
- The original code in `bridge/` has no license yet. Until one is added, it is all rights reserved.
