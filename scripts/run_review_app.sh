#!/usr/bin/env bash
set -euo pipefail

# Helper wrapper so conda users can launch the review app without manually
# exporting PYTHONPATH. Usage:
#   scripts/run_review_app.sh /path/to/session_root --atlas /path/to/atlas.jpg

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 /path/to/session_root [extra review_app.py args]" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SRC_DIR="${ROOT_DIR}/src"

if [[ ! -d "$SRC_DIR" ]]; then
  echo "Cannot locate src directory at $SRC_DIR" >&2
  exit 1
fi

if ! command -v python >/dev/null 2>&1; then
  echo "python is not on PATH. Activate your conda environment first (conda activate epu-mapper)." >&2
  exit 1
fi

export PYTHONPATH="${SRC_DIR}${PYTHONPATH:+:${PYTHONPATH}}"
exec python -m review_app "$@"
