#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "${EPU_MAPPER_PYTHON:-/opt/anaconda3/envs/EPU_mapping/bin/python}" "$SCRIPT_DIR/app.py" "$@"
