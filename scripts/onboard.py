#!/usr/bin/env python3
"""Interactive onboarding. Run after scripts/setup.sh:

    .venv/bin/python scripts/onboard.py

1. Runs `applypilot init` for your resume, profile and API key (saved in ~/.applypilot/).
2. Links those files into ./.applypilot/, where the pipeline looks for them.
3. Asks what jobs you want and writes settings.json and RoleDar's locations.yaml.

Safe to run again. Files it overwrites are first copied to <name>.bak.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOME_DIR = Path.home() / ".applypilot"
LINK_DIR = ROOT / ".applypilot"
SETTINGS = ROOT / "settings.json"
LOCATIONS = ROOT / "vendor" / "roledar" / "locations.yaml"
SHARED = ("profile.json", "resume.txt", ".env", "searches.yaml")


def ask(question: str, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    return input(f"{question}{hint}: ").strip() or default


def yes(question: str, default: bool) -> bool:
    while True:
        answer = ask(f"{question} (y/n)", "y" if default else "n").lower()
        if answer in ("y", "yes", "n", "no"):
            return answer.startswith("y")


def number(question: str, default: int, low: int, high: int) -> int:
    while True:
        answer = ask(f"{question} ({low}-{high})", str(default))
        if answer.isdigit() and low <= int(answer) <= high:
            return int(answer)


def word_regex(names: list[str]) -> str:
    """'New York, Washington, D.C.' style names as one case-insensitive regex."""
    return "|".join(rf"(?<!\w){re.escape(n)}(?!\w)" for n in names)


def build_settings(base: dict, cities: list[str], remote: bool) -> dict:
    parts = ([word_regex(cities)] if cities else []) + ([r"\bremote\b"] if remote else [])
    return {**base,
            "location_regex": "|".join(parts),
            # Applied to first. Remote-only searches put remote roles first.
            "priority_regex": word_regex(cities) if cities else r"\bremote\b",
            "include_remote": remote}


def locations_yaml(cities: list[str], remote: bool) -> str:
    """RoleDar's location filter. JSON strings and lists are valid YAML."""
    remote_patterns = [r"Remote.*United States", r"Remote.*\bUS\b", r"Remote.*\bUSA\b", r"^Remote$"] if remote else []
    allow = {"cities": cities, "states": [], "regions": [word_regex([c]) for c in cities],
             "remote_patterns": remote_patterns}
    exclude = [r"Remote.*Canada", r"Remote.*(Europe|EMEA|APAC|India|LATAM|UK)"]
    lines = ["# Written by scripts/onboard.py. Excludes win over allow rules.", "allow:"]
    lines += [f"  {key}: {json.dumps(value)}" for key, value in allow.items()]
    lines.append(f"exclude: {json.dumps(exclude)}")
    return "\n".join(lines) + "\n"


def write(path: Path, text: str) -> None:
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    path.write_text(text, encoding="utf-8")
    print(f"Wrote {path.relative_to(ROOT)}")


def personal_files() -> None:
    print("\n== 1. Your resume, profile and API key ==")
    print("These are saved on this computer in ~/.applypilot/, outside the repo.")
    print("Use Gemini as the AI provider: the pipeline requires a Gemini API key.")
    print("When asked for a model, enter the llm_model value in settings.example.json.")
    print("The job-site password is stored as plain text. Use one you use nowhere else, or leave it blank.\n")
    exists = (HOME_DIR / "profile.json").exists()
    if yes("Run the ApplyPilot wizard now?" if not exists else "A profile exists. Redo the wizard (overwrites it)?",
           not exists):
        env = {k: v for k, v in os.environ.items() if k != "APPLYPILOT_DIR"}
        if subprocess.run([str(ROOT / ".venv" / "bin" / "applypilot"), "init"], env=env).returncode != 0:
            raise KeyboardInterrupt  # cancelled or failed: stop without a traceback


def link_files() -> None:
    LINK_DIR.mkdir(exist_ok=True)
    for name in SHARED:
        source, link = HOME_DIR / name, LINK_DIR / name
        if source.exists() and name != "resume.txt":
            source.chmod(0o600)
        if link.is_symlink() or not link.exists():
            link.unlink(missing_ok=True)
            link.symlink_to(source)
        else:
            print(f"Kept your existing .applypilot/{name} (not a link)")
    from applypilot.database import init_db
    init_db(LINK_DIR / "applypilot.db")
    missing = [n for n in SHARED if not (LINK_DIR / n).exists()]
    if missing:
        print(f"Still missing in ~/.applypilot/: {', '.join(missing)} (see README.md, 'Your files')")


def preferences() -> None:
    print("\n== 2. What should the agent apply to? ==")
    print("It looks for internships and co-ops. Location rules are built for US searches.")
    source = SETTINGS if SETTINGS.exists() else ROOT / "settings.example.json"
    base = json.loads(source.read_text(encoding="utf-8"))
    while True:
        raw = ask("Preferred cities, separated by ; (add aliases, e.g. New York; NYC; Boston). Blank for none")
        cities = [c.strip() for c in raw.split(";") if c.strip()]
        remote = yes("Include remote roles?", True)
        if cities or remote:
            break
        print("Choose at least one city, or include remote.")
    settings = build_settings(base, cities, remote)
    settings["us_fallback"] = yes("Also apply elsewhere in the US, including in person, when there are too few matches?",
                                  False)
    if not yes("Apply only to software, data and IT roles?", True):
        settings["software_title_regex"] = ""
    elif not settings.get("software_title_regex"):
        example = json.loads((ROOT / "settings.example.json").read_text(encoding="utf-8"))
        settings["software_title_regex"] = example["software_title_regex"]
    settings["max_per_hour"] = number("Most applications per hour", 3, 1, 20)
    settings["min_score"] = number("Minimum fit score to apply (higher is pickier)", 7, 1, 10)
    print("Auto-submit sends applications without your review. Start with n and check the filled forms.")
    settings["auto_submit"] = yes("Submit automatically?", False)

    if yes("Optional: update one resume entry from your git commits? Uses Claude Code", False):
        settings["experience_repos_dir"] = ask("Folder that contains your repos", "~/work-repos")
        settings["experience_author"] = ask("Your git author name")
        settings["experience_since"] = ask("Count commits since (YYYY-MM-DD)")
        settings["experience_header"] = ask("Exact first line of that resume entry (Company — Role — Dates)")
        settings["experience_stack"] = ask("Technologies you used there")
        if not (settings["experience_author"] and settings["experience_since"] and settings["experience_header"]):
            print("Missing details, so this feature stays off.")
            settings["experience_repos_dir"] = ""
    else:
        settings["experience_repos_dir"] = ""

    write(SETTINGS, json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
    if LOCATIONS.parent.is_dir():
        write(LOCATIONS, locations_yaml(cities, remote))
    else:
        print("vendor/roledar is missing: run scripts/setup.sh, then run this again.")


def main() -> int:
    if not (ROOT / ".venv" / "bin" / "applypilot").exists():
        print("Run ./scripts/setup.sh first.")
        return 1
    if Path(sys.prefix).resolve() != (ROOT / ".venv").resolve():
        print("Run this with: .venv/bin/python scripts/onboard.py")
        return 1
    try:
        personal_files()
        preferences()
        link_files()
    except (KeyboardInterrupt, EOFError):
        print("\nStopped. Run it again any time.")
        return 1
    print("\nDone. Test without submitting:\n  .venv/bin/python bridge/pipeline.py --dry-run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
