"""One Playwright for the whole process.

Workday and the browser-based site scrapers both drive Chrome. Playwright's sync API can only be
started once per process, so they share this single instance (it ends with the process).
"""

_pw = None


def get():
    global _pw
    if _pw is None:
        from playwright.sync_api import sync_playwright
        _pw = sync_playwright().start()
    return _pw
