#!/usr/bin/env bash
# Run every test that does not need the MSI. These all pass on the Mac: no GPU,
# no camera, no serial port.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
exec python3 -m unittest discover -s tests -p "test_*.py" -t . "$@"
