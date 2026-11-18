#!/usr/bin/env python3
"""Show a clickable macOS popup.

Usage: notify.py TITLE MESSAGE KIND ARG

KIND is what clicking does:
  file      open ARG in TextEdit
  url       open ARG in the default browser
  window    bring the app with process id ARG to the front
  tab       ARG is "PORT|URL": select that application's tab in the Chrome window on that debug port and
            bring the window to the front (falls back to opening the URL)
  terminal  run ARG in a new Terminal window
  apply_followup  ARG is the application URL; a "Go to application" popup shown over the resume
  resume_apply  ARG is "RESUME.pdf|APPLY_URL|COVER_LETTER.pdf" (cover letter optional): a dialog with View resume (opens the PDF) and
            Apply (jumps to the application's tab in the watched window, else opens the link)

Uses terminal-notifier (a real notification banner) when it is installed,
otherwise an alert dialog with an Open button.
"""
from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path


def applescript_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def action_command(kind: str, arg: str) -> list[str]:
    if kind == "file":
        return ["open", "-e", arg]
    if kind == "url":
        return ["open", arg]
    if kind == "window":
        # Bring the app that owns process id ARG to the front.
        return ["osascript", "-e", 'tell application "System Events" to set frontmost of '
                f"(first process whose unix id is {int(arg)}) to true"]
    if kind == "tab":
        port, _, url = arg.partition("|")
        return [sys.executable, __file__, "--go", port, url]
    if kind == "terminal":
        return [
            "osascript",
            "-e", 'tell application "Terminal" to activate',
            "-e", f'tell application "Terminal" to do script {applescript_quote(arg)}',
        ]
    raise ValueError(f"unknown kind {kind!r}")


def terminal_notifier() -> str | None:
    found = shutil.which("terminal-notifier")
    if found:
        return found
    for p in ("/opt/homebrew/bin/terminal-notifier", "/usr/local/bin/terminal-notifier"):
        if Path(p).exists():
            return p
    return None


MANUAL_PORT = 9224


def go_to_application(url: str, port: int = MANUAL_PORT) -> None:
    """Bring forward the application's tab in the Chrome window on `port`; open the link if it is gone."""
    import json
    import urllib.request
    MANUAL_PORT = port  # noqa: N806  (shadows the module default for this call only)
    try:
        tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{MANUAL_PORT}/json", timeout=3))
        pages = [t for t in tabs if t.get("type") == "page"]
        base = url.split("?")[0].rstrip("/")
        # The tab for this application, or (the page may have moved on after Apply) the window's first tab.
        tab = next((t for t in pages if t.get("url", "").split("?")[0].rstrip("/") == base or url in t.get("url", "")),
                   pages[0])
        urllib.request.urlopen(f"http://127.0.0.1:{MANUAL_PORT}/json/activate/{tab['id']}", timeout=3)
        pid = subprocess.run(["lsof", "-ti", f"tcp:{MANUAL_PORT}", "-sTCP:LISTEN"],
                             capture_output=True, text=True).stdout.split()[0]
        subprocess.run(["osascript", "-e", 'tell application "System Events" to set frontmost of '
                        f"(first process whose unix id is {int(pid)}) to true"])
    except Exception:
        subprocess.run(["open", url])


def save_to_downloads(pdf: str) -> str:
    """Copy the resume into ~/Downloads (never overwriting) and return the copy's path."""
    import shutil as sh
    try:
        src = Path(pdf)
        dest = Path.home() / "Downloads" / src.name
        n = 1
        while dest.exists():
            n += 1
            dest = dest.with_name(f"{src.stem} ({n}){src.suffix}")
        sh.copy2(src, dest)
        return str(dest)
    except OSError:
        return pdf


def show_resume_apply(title: str, message: str, arg: str) -> None:
    pdf, url, cover = (arg.split("|") + ["", ""])[:3]
    script = (f"display alert {applescript_quote(title)} message {applescript_quote(message)} "
              'as critical buttons {"Dismiss", "Apply", "View resume"} default button "View resume"')
    out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True).stdout
    if "View resume" in out:
        files = [save_to_downloads(pdf)] + ([save_to_downloads(cover)] if cover else [])
        subprocess.run(["open", *files])
        # A second popup, detached, so it sits over the resume while it is being read.
        subprocess.Popen([sys.executable, __file__, "Ready to apply?",
                          ("Your resume and cover letter are open (saved to Downloads). " if cover else "Your resume is open (saved to Downloads). ") + "Go to the application whenever you are ready.",
                          "apply_followup", url], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif "Apply" in out:
        go_to_application(url)


def show_followup(title: str, message: str, url: str) -> None:
    time.sleep(1.5)  # let the PDF viewer come up first
    script = (f"display alert {applescript_quote(title)} message {applescript_quote(message)} "
              'buttons {"Not yet", "Go to application"} default button "Go to application"')
    out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True).stdout
    if "Go to application" in out:
        go_to_application(url)


def show(title: str, message: str, kind: str, arg: str) -> None:
    if kind == "apply_followup":
        show_followup(title, message, arg)
        return
    if kind == "resume_apply":
        show_resume_apply(title, message, arg)
        return
    cmd = action_command(kind, arg)
    tn = terminal_notifier()
    if tn:
        subprocess.run([tn, "-title", title, "-message", message, "-sound", "default",
                        "-execute", shlex.join(cmd)])
        return
    script = (f"display alert {applescript_quote(title)} message {applescript_quote(message)} "
              'as critical buttons {"Dismiss", "Open"} default button "Open"')
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if "Open" in result.stdout:
        subprocess.run(cmd)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--go":
        go_to_application(sys.argv[3], int(sys.argv[2]))
        sys.exit(0)
    if len(sys.argv) != 5:
        sys.exit(__doc__)
    show(*sys.argv[1:])
