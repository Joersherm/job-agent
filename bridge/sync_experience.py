"""Keep the employer entry on the resume in step with the commits made in the repos in experience_repos_dir.

Commits by the configured author are listed, and when that list has changed since the last run
Claude rewrites the entry's bullets from the commit subjects alone. The commit count is
computed here, never by the model. Only the entry's own block in resume.txt is replaced.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = ROOT / "settings.json"
STATE = ROOT / "state" / "experience.json"
RESUME = Path.home() / ".applypilot" / "resume.txt"


def commit_subjects(repos_dir: Path, author: str, since: str) -> dict[str, list[str]]:
    """{repo name: unique non-merge commit subjects by this author on any branch}."""
    out: dict[str, list[str]] = {}
    for repo in sorted(repos_dir.iterdir()):
        if not (repo / ".git").exists():
            continue
        res = subprocess.run(
            ["git", "-C", str(repo), "log", "--all", "--no-merges", f"--author={author}",
             f"--since={since}", "--format=%s"],
            capture_output=True, text=True, timeout=60)
        subjects = sorted({s.strip() for s in res.stdout.splitlines() if s.strip()})
        if subjects:
            out[repo.name] = subjects
    return out


def fingerprint(commits: dict[str, list[str]]) -> str:
    return hashlib.sha256(json.dumps(commits, sort_keys=True).encode()).hexdigest()


def replace_entry(resume: str, header: str, bullets: list[str]) -> str:
    """Swap the block that starts at the company line (up to the next blank line)."""
    company = header.split(" — ")[0] + " — "
    lines = resume.splitlines()
    start = next((i for i, l in enumerate(lines) if l.startswith(company)), None)
    if start is None:
        raise ValueError("entry not found in resume")
    end = start + 1
    while end < len(lines) and lines[end].strip():
        end += 1
    return "\n".join(lines[:start] + [header] + bullets + lines[end:]) + "\n"


def ask_claude(commits: dict[str, list[str]], settings: dict) -> list[str]:
    listing = "\n".join(f"[{repo}] {s}" for repo, subs in commits.items() for s in subs)
    prompt = (
        "Write 4 resume bullets for a part-time AI engineering intern, from these git commit subjects "
        "and nothing else.\n"
        f"Stack the intern has confirmed: {settings['experience_stack']}.\n"
        "Rules: group related commits into one bullet; past tense; plain wording; each bullet under 300 "
        "characters; no numbers, percentages or metrics; do not claim users, launches, revenue or "
        "production use; do not name technologies other than the stack above or ones in the subjects; "
        "do not mention commit counts. Output only the 4 bullets, each starting with '- '.\n\n"
        + listing)
    env = dict(os.environ, PATH=f"{Path.home()}/.local/bin:/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", ""))
    res = subprocess.run([shutil.which("claude", path=env["PATH"]) or "claude", "--model",
                          settings.get("experience_model", "sonnet"), "-p", prompt],
                         capture_output=True, text=True, timeout=240, env=env)
    bullets = [l.strip() for l in res.stdout.splitlines() if l.strip().startswith("- ")]
    if res.returncode != 0 or not 3 <= len(bullets) <= 6 or any(len(b) > 330 for b in bullets):
        raise RuntimeError(f"unusable model output (exit {res.returncode}): {res.stdout[:200]!r}")
    return bullets


def sync(settings: dict | None = None, log=print) -> bool:
    """True if the resume entry was rewritten."""
    settings = settings or json.loads(SETTINGS.read_text())
    repos_dir = Path(settings["experience_repos_dir"]).expanduser()
    commits = commit_subjects(repos_dir, settings["experience_author"], settings["experience_since"])
    if not commits:
        return False
    fp = fingerprint(commits)
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    if state.get("fingerprint") == fp:
        return False
    bullets = ask_claude(commits, settings)
    total = sum(len(v) for v in commits.values())
    bullets.append(f"- Made {total} commits through merge requests across {len(commits)} "
                   f"repositor{'y' if len(commits) == 1 else 'ies'}")
    resume = RESUME.read_text()
    new = replace_entry(resume, settings["experience_header"], bullets)
    backups = STATE.parent / "resume-backups"
    backups.mkdir(parents=True, exist_ok=True)
    (backups / f"resume-{fp[:8]}.txt").write_text(resume)
    RESUME.write_text(new)
    STATE.write_text(json.dumps({"fingerprint": fp, "commits": total}))
    log(f"Resume: rewrote the employer entry from {total} commit(s) in {len(commits)} repo(s)")
    return True


if __name__ == "__main__":
    print("rewritten" if sync() else "unchanged")
