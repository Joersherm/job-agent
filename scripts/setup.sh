#!/bin/bash
# Fetch the upstream projects at the exact commits the patches were made against, apply the patches,
# and install everything into .venv. Run from the repo root: ./scripts/setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."
ROLEDAR_REV=e432d7ad01af8e6bf793f87c43a36a7b9c51847a
APPLYPILOT_REV=4a8d521f67f5139811c0a910ef37410f8e6d836a
mkdir -p vendor
[ -d vendor/roledar ]    || git clone https://github.com/dalecook/roledar.git vendor/roledar
[ -d vendor/ApplyPilot ] || git clone https://github.com/Pickle-Pixel/ApplyPilot.git vendor/ApplyPilot
git -C vendor/roledar    checkout -q "$ROLEDAR_REV"
git -C vendor/ApplyPilot checkout -q "$APPLYPILOT_REV"
# Safe to re-run after an update: an already patched folder is reset and patched again. Your company list and
# search files are kept, and any other edits are saved to vendor/<name>.<time>.diff first.
patch_vendor() { # folder, patch
  local keep; keep=$(mktemp -d)
  if ! git -C "$1" diff --quiet; then
    git -C "$1" diff > "$1.$(date +%Y%m%d-%H%M%S).diff"
    for f in companies.yaml titles.yaml locations.yaml; do [ -f "$1/$f" ] && cp "$1/$f" "$keep/"; done
    git -C "$1" checkout -q -- .
  fi
  git -C "$1" apply "../../$2"
  cp "$keep"/*.yaml "$1/" 2>/dev/null || true
  rm -rf "$keep"
}
patch_vendor vendor/roledar    patches/roledar/roledar.patch
patch_vendor vendor/ApplyPilot patches/applypilot/applypilot.patch
for f in _pw custom site workday; do cp "patches/roledar/$f.py" "vendor/roledar/scrapers/$f.py"; done
cp patches/roledar/lanes.py              vendor/roledar/lanes.py
cp patches/applypilot/latex_resume.py     vendor/ApplyPilot/src/applypilot/scoring/latex_resume.py
python3 -m venv .venv
.venv/bin/pip install -r vendor/roledar/requirements.txt
.venv/bin/pip install ./vendor/ApplyPilot
.venv/bin/pip install --no-deps --force-reinstall ./vendor/ApplyPilot  # same version number, new code
# python-jobspy pins an old numpy, so it is installed without its dependencies (see ApplyPilot's README).
.venv/bin/pip install --no-deps python-jobspy
.venv/bin/pip install pydantic tls-client requests markdownify regex "curl_cffi>=0.16.2"
.venv/bin/playwright install chromium
[ -f settings.json ] || cp settings.example.json settings.json
echo "Done. Next: .venv/bin/python scripts/onboard.py"
