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
git -C vendor/roledar    apply ../../patches/roledar/roledar.patch
git -C vendor/ApplyPilot apply ../../patches/applypilot/applypilot.patch
cp patches/roledar/workday.py             vendor/roledar/scrapers/workday.py
cp patches/applypilot/latex_resume.py     vendor/ApplyPilot/src/applypilot/scoring/latex_resume.py
python3 -m venv .venv
.venv/bin/pip install -r vendor/roledar/requirements.txt
.venv/bin/pip install ./vendor/ApplyPilot
# python-jobspy pins an old numpy, so it is installed without its dependencies (see ApplyPilot's README).
.venv/bin/pip install --no-deps python-jobspy
.venv/bin/pip install pydantic tls-client requests markdownify regex
.venv/bin/playwright install chromium
[ -f settings.json ] || cp settings.example.json settings.json
echo "Done. Next: .venv/bin/python scripts/onboard.py"
