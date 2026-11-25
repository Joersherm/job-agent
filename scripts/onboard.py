#!/usr/bin/env python3
"""Interactive onboarding. Run after scripts/setup.sh:

    .venv/bin/python scripts/onboard.py

1. Runs `applypilot init` for your resume, profile and API key (saved in ~/.applypilot/).
2. Links those files into ./.applypilot/, where the pipeline looks for them.
3. Asks your field, levels, roles, locations and timing, and writes settings.json plus RoleDar's
   titles.yaml and locations.yaml.
4. Optionally finds employers for your field (scripts/find_companies.py).

Safe to run again. Files it overwrites are first copied to <name>.<time>.bak.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOME_DIR = Path.home() / ".applypilot"
LINK_DIR = ROOT / ".applypilot"
SETTINGS = ROOT / "settings.json"
LOCATIONS = ROOT / "vendor" / "roledar" / "locations.yaml"
TITLES = ROOT / "vendor" / "roledar" / "titles.yaml"
LEVELS = {  # choice -> (name, words a title must contain)
    "1": ("internship", r"\bintern(?:ship)?s?\b|\bco-?op\b"),
    "2": ("entry-level", r"\bentry[- ]level\b|\bjunior\b|\bjr\b|\bnew[- ]grad|\bgraduate\b|\btrainee\b|"
                         r"\bapprentice|\bassociate\b"),
    "3": ("senior or lead", r"\bsenior\b|\bsr\b|\blead\b|\bprincipal\b|\bmanager\b|\bdirector\b"),
    "4": ("any level", ""),
}
SENIOR_WORDS = r"\bsenior\b|\bsr\b|\bprincipal\b|\bdirector\b|\bph\.?d\b"
US_NAMES = ("united states", "us", "usa", "u.s.", "united states of america")
US_STATES = dict(pair.split(":") for pair in (
    "Alabama:AL,Alaska:AK,Arizona:AZ,Arkansas:AR,California:CA,Colorado:CO,Connecticut:CT,Delaware:DE,Florida:FL,"
    "Georgia:GA,Hawaii:HI,Idaho:ID,Illinois:IL,Indiana:IN,Iowa:IA,Kansas:KS,Kentucky:KY,Louisiana:LA,Maine:ME,"
    "Maryland:MD,Massachusetts:MA,Michigan:MI,Minnesota:MN,Mississippi:MS,Missouri:MO,Montana:MT,Nebraska:NE,"
    "Nevada:NV,New Hampshire:NH,New Jersey:NJ,New Mexico:NM,New York:NY,North Carolina:NC,North Dakota:ND,Ohio:OH,"
    "Oklahoma:OK,Oregon:OR,Pennsylvania:PA,Rhode Island:RI,South Carolina:SC,South Dakota:SD,Tennessee:TN,Texas:TX,"
    "Utah:UT,Vermont:VT,Virginia:VA,Washington:WA,West Virginia:WV,Wisconsin:WI,Wyoming:WY").split(","))
CITY_NAMES = ("New York", "Washington")  # usually meant as the city: matched literally, type NY or WA for the state
FOREIGN = ("Canada", "Europe", "EMEA", "APAC", "India", "LATAM", "UK")
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
    """Names such as 'Washington, D.C.' or 'Registered Nurse' as one case-insensitive regex."""
    return "|".join(rf"(?<!\w){re.escape(n)}(?!\w)" for n in names)


def exact(name: str) -> str:
    return rf"(?<!\w){re.escape(name)}(?!\w)"


def place_regex(place: str) -> str:
    """One chosen location. US states and the US also match the codes job boards use ("Austin, TX")."""
    code = rf"(?-i:, (?:{'|'.join(US_STATES.values())})(?!\w))"
    if place.lower() in US_NAMES:
        names = "|".join(exact(n) for n in ["United States", "USA", "U.S.", *US_STATES])
        return rf"{names}|(?-i:(?<!\w)US(?!\w))|{code}"
    for name, abbr in US_STATES.items():
        if place == abbr or (place.lower() == name.lower() and name not in CITY_NAMES):
            return rf"(?<![\w]){re.escape(name)}(?!\w)|(?-i:, {abbr}(?!\w))"
    if place.isupper() and len(place) <= 3:  # LA, UK: capitals only
        return rf"(?-i:{exact(place)})"
    return exact(place)


def places_regex(places: list[str]) -> str:
    return "|".join(place_regex(p) for p in places)


def role_regex(phrases: list[str]) -> str:
    """Role names, plurals included. Short capital aliases (OR, MA, HR) only match as written, not after a comma."""
    return "|".join(rf"(?-i:(?<!, )(?<!\w){re.escape(p)}s?(?!\w))" if p.isupper() and len(p) <= 3
                    else rf"(?<!\w){re.escape(p)}(?:e?s)?(?!\w)" for p in phrases)


def title_regex(levels: list[str], phrases: list[str]) -> str:
    """A title must name one of the roles and, unless any level is fine, one of the levels."""
    parts = []
    if "4" not in levels:
        if set(levels) <= {"1", "2"}:
            parts.append(f"(?!.*(?:{SENIOR_WORDS}))")
        parts.append("(?=.*(?:" + "|".join(LEVELS[n][1] for n in levels) + "))")
    return "^" + "".join(parts) + f"(?=.*(?:{role_regex(phrases)}))"


def job_settings(field: str, levels: list[str], roles: list[dict]) -> dict:
    """roles: [{"title": ..., "also": [...]}], as chosen by the user."""
    phrases = list(dict.fromkeys(p for r in roles for p in [r["title"], *r["also"]]))
    titles = [r["title"] for r in roles]
    level_names = ", ".join(LEVELS[n][0] for n in levels)
    interns_only = levels == ["1"]
    # Searchable job boards: one search for internships, otherwise one per role (capped to keep scans short).
    queries = [] if interns_only else (["intern"] if "1" in levels else []) + titles[:5]
    page = "" if interns_only else "|".join(
        ([LEVELS["1"][1]] if "1" in levels else []) + [role_regex(phrases)])
    return {"job_field": field, "title_regex": title_regex(levels, phrases), "search_queries": queries,
            "page_regex": page, "software_title_regex": "",
            "job_target": f"{level_names} roles in {field}: {', '.join(titles)}"}


def titles_yaml(regex: str) -> str:
    return ("# Written by scripts/onboard.py.\ntitles:\n  - name: Wanted\n"
            f"    patterns: {json.dumps([regex])}\n    excludes: []\n")


def build_settings(base: dict, cities: list[str], remote: bool) -> dict:
    parts = ([places_regex(cities)] if cities else []) + ([r"\bremote\b"] if remote else [])
    return {**base,
            "location_regex": "|".join(parts),
            # Applied to first. Remote-only searches put remote roles first.
            "priority_regex": places_regex(cities) if cities else r"\bremote\b",
            "include_remote": remote}


def locations_yaml(cities: list[str], remote: bool, country: str = "United States") -> str:
    """RoleDar's location filter. country limits remote roles; blank means remote anywhere."""
    patterns, exclude = [], []
    if remote and not country:
        patterns = ["Remote"]
    elif remote:
        us = country.lower() in US_NAMES
        for n in (["United States", "US", "USA"] if us else [country]):
            patterns += [rf"Remote.*{exact(n)}", rf"{exact(n)}.*Remote"]  # "Remote - US" and "US - Remote"
        patterns.append(r"^Remote$")
        if us:  # "Remote - India (US shift)" is not open to the US. Places the user chose are never excluded.
            wanted = {c.lower() for c in cities}
            foreign = "|".join(n for n in FOREIGN if n.lower() not in wanted)
            exclude = [rf"Remote.*(?<!\w)(?:{foreign})(?!\w)", rf"(?<!\w)(?:{foreign})(?!\w).*Remote"]
    # cities stays empty: RoleDar matches it as raw substrings ("CA" would match "Canada").
    allow = {"cities": [], "states": [], "regions": [place_regex(c) for c in cities], "remote_patterns": patterns}
    lines = ["# Written by scripts/onboard.py.", "allow:"]
    lines += [f"  {key}: {json.dumps(value)}" for key, value in allow.items()]
    lines.append(f"exclude: {json.dumps(exclude)}")
    return "\n".join(lines) + "\n"


def json_list(text: str) -> list:
    """The longest JSON list of objects in a reply, ignoring any text (and brackets) around it."""
    best: list = []
    decoder = json.JSONDecoder()
    for m in re.finditer(r"\[", text):
        try:
            data = decoder.raw_decode(text, m.start())[0]
        except ValueError:
            continue
        if isinstance(data, list) and len(data) > len(best) and all(isinstance(x, dict) for x in data):
            best = data
    return best


def ask_json(prompt: str, tools: str = "") -> list:
    """Ask Claude for a JSON list. tools is "" (none) or "WebSearch"; it never gets files or a shell.
    Returns [] if Claude is unavailable or the reply is unusable."""
    exe = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")
    cmd = [exe, "--model", "sonnet", "--tools", tools, "--strict-mcp-config", "--setting-sources", "",
           "--no-session-persistence"] + (["--allowedTools", tools] if tools else []) + ["-p"]
    try:
        with tempfile.TemporaryDirectory() as empty:
            res = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=900, cwd=empty)
        if res.returncode != 0:
            raise RuntimeError(f"claude exit {res.returncode}: {(res.stderr or res.stdout)[:150]}")
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"Claude could not answer ({str(exc)[:150]}).")
        return []
    data = json_list(res.stdout)
    if not data:
        print("Claude's reply held no usable list.")
    return data


def clean_roles(data: list) -> list[dict]:
    def ok(text) -> bool:
        return isinstance(text, str) and 1 < len(text.strip()) < 60 and "|" not in text
    return [{"title": r["title"].strip(),
             "also": [a.strip() for a in (r.get("also") if isinstance(r.get("also"), list) else []) if ok(a)][:6]}
            for r in data if isinstance(r, dict) and ok(r.get("title"))][:30]


def choose_roles(answer: str, suggested: list[dict]) -> list[dict]:
    """'all', numbers such as '1,3', or the user's own titles separated by ';'."""
    if answer.lower() == "all":
        return suggested
    if re.fullmatch(r"[\d,\s]+", answer):
        picks = [int(n) for n in re.findall(r"\d+", answer)]
        return [suggested[n - 1] for n in picks if 0 < n <= len(suggested)]
    if re.fullmatch(r"(?:\d|[\s,.&+\-–]|to|and)+", answer):  # ranges are not supported: ask again
        return []
    return [{"title": t.strip(), "also": []} for t in re.split(r"[;,]", answer) if len(t.strip()) > 1]


def ask_search() -> dict:
    field = ""
    while not field:
        field = ask("What field or industry? (e.g. nursing, accounting, marketing, software)")
    print("Levels: " + ", ".join(f"{n} {name}" for n, (name, _) in LEVELS.items()))
    levels: list[str] = []
    while not levels:
        answer = ask("Which levels? e.g. 1 or 1,2", "1")
        if re.fullmatch(r"[1-4,\s]+", answer):
            levels = sorted(set(re.findall(r"[1-4]", answer)))
    if "2" in levels or "3" in levels:
        print("A level matches only titles that say so. Choose 4 to include plain titles too; scoring judges the fit.")
    print("Looking up positions in this field...")
    suggested = clean_roles(ask_json(
        f"List the main job positions in this field: {field!r}. Reply with only a JSON list of up to 20 items: "
        "{\"title\": position name without any level word, \"also\": [other names job postings use for it]}."))
    for i, r in enumerate(suggested, 1):
        print(f"{i:3}. {r['title']}" + (f"  (also: {', '.join(r['also'])})" if r["also"] else ""))
    hint = "all, numbers like 1,3, or your own titles separated by ; or ," if suggested else "titles separated by ; or ,"
    roles: list[dict] = []
    while not roles:
        roles = choose_roles(ask(f"Which positions? ({hint})"), suggested)
    print("Chosen: " + ", ".join(r["title"] for r in roles))
    return job_settings(field, levels, roles)


def ask_locations() -> tuple[list[str], bool, str]:
    while True:
        print("Add locations one at a time (city, state or country). Add other spellings as separate entries.")
        places: list[str] = []
        while place := ask(f"Location {len(places) + 1} (blank when done)"):
            places.append(place)
        remote = yes("Include remote roles?", True)
        country = ask("Remote roles must be open to which country? Type any for anywhere", "United States") if remote else ""
        if places or remote:
            return places, remote, "" if country.lower() == "any" else country
        print("Add at least one location, or include remote.")


def write(path: Path, text: str) -> None:
    if path.exists():
        # A new name every time, so an earlier backup is never overwritten.
        shutil.copy2(path, path.with_name(f"{path.name}.{datetime.now():%Y%m%d-%H%M%S-%f}.bak"))
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
    source = SETTINGS if SETTINGS.exists() else ROOT / "settings.example.json"
    base = json.loads(source.read_text(encoding="utf-8"))
    search = ask_search()
    places, remote, country = ask_locations()
    settings = {**build_settings(base, places, remote), **search,
                "job_locations": places + ([f"remote ({country or 'anywhere'})"] if remote else [])}
    settings["us_fallback"] = (country.lower() in US_NAMES or not remote) and yes(
        "Also apply elsewhere in the US, including in person, when there are too few matches?", False)
    print("\nA full scan of several hundred companies takes about 15 minutes, so shorter gaps mean back-to-back scans.")
    settings["scan_gap_minutes"] = number("Minutes between the start of one scan and the next", 60, 15, 1440)
    hours = number("Apply only within how many hours of a job being posted?", 24, 1, 720)
    settings["posted_within_minutes"] = settings["max_age_minutes"] = hours * 60
    settings["newsletter"] = "internship" in settings["job_target"] and yes(
        "Also read the Direct Consideration internship newsletter (mostly tech and finance)?", False)
    settings["watchlist"] = yes("Also run the built-in watchlist (a fixed set of tech and finance companies)?", False)
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

    reseed_if_changed(base, settings)
    write(SETTINGS, json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
    if LOCATIONS.parent.is_dir():
        write(TITLES, titles_yaml(settings["title_regex"]))
        write(LOCATIONS, locations_yaml(places, remote, country))
    else:
        print("vendor/roledar is missing: run scripts/setup.sh, then run this again.")


def reseed_if_changed(old: dict, new: dict) -> None:
    """After a change of search, the next scan must only record what is already open, not apply to it."""
    keys = ("title_regex", "location_regex", "search_queries", "job_locations")
    if all(old.get(k) == new.get(k) for k in keys):
        return
    for name in ("last_run.json", "last_run_out_of_region.json"):
        (LOCATIONS.parent / "state" / name).unlink(missing_ok=True)
    print("Your search changed: the next scan only records current openings and applies to none.")


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
        if yes("\nFind companies that hire for your roles now? Uses Claude Code and takes several minutes", True):
            subprocess.run([sys.executable, str(ROOT / "scripts" / "find_companies.py")])
    except (KeyboardInterrupt, EOFError):
        print("\nStopped. Run it again any time.")
        return 1
    print("\nDone. Test without submitting (forms are still filled and your resume uploaded):\n.venv/bin/python bridge/pipeline.py --dry-run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
