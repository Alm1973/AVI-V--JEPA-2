#!/usr/bin/env bash
# Run every test that does not need the MSI. These all pass on the Mac: no GPU,
# no camera, no serial port.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
# Prefer the project venv when it exists: some tests need numpy (the latent
# probe's arithmetic), and Homebrew python refuses system installs under
# PEP 668. Without it those tests skip rather than fail.
PY=python3
[ -x ./.venv/bin/python ] && PY=./.venv/bin/python
exec "$PY" -m unittest discover -s tests -p "test_*.py" -t . "$@"
