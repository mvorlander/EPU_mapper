#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<USAGE
Usage: start-review-app [GRID_DIR] [--atlas PATH] [--host HOST] [--port PORT] [extra review_app args]

Environment variables:
  GRID_DIR   Default GridSquare root directory (default: /data)
  ATLAS_FILE Optional atlas filename relative to GRID_DIR
  HOST       Listener address (default: 0.0.0.0)
  PORT       Listener port (default: 8000)
USAGE
}

GRID_DIR_DEFAULT="${GRID_DIR:-/data}"
ATLAS_DEFAULT="${ATLAS_FILE:-}"
HOST_DEFAULT="${HOST:-0.0.0.0}"
PORT_DEFAULT="${PORT:-8000}"
OPEN_BROWSER="${AUTO_OPEN_BROWSER:-1}"

POSITIONAL=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --open-browser|--open)
      OPEN_BROWSER=1
      shift
      ;;
    --no-open-browser|--no-open)
      OPEN_BROWSER=0
      shift
      ;;
    --atlas)
      ATLAS_DEFAULT="$2"
      shift 2
      ;;
    --host)
      HOST_DEFAULT="$2"
      shift 2
      ;;
    --port)
      PORT_DEFAULT="$2"
      shift 2
      ;;
    --)
      shift
      POSITIONAL+=("$@")
      break
      ;;
    *)
      POSITIONAL+=("$1")
      shift
      ;;
  esac
done

if [[ ${#POSITIONAL[@]} -gt 0 ]]; then
  GRID_DIR_DEFAULT="${POSITIONAL[0]}"
  POSITIONAL=("${POSITIONAL[@]:1}")
fi

if [[ ! -d "$GRID_DIR_DEFAULT" ]]; then
  echo "Grid directory '$GRID_DIR_DEFAULT' not found. Bind or specify the path." >&2
  exit 1
fi

CMD=(python /app/src/review_app.py "$GRID_DIR_DEFAULT" --host "$HOST_DEFAULT" --port "$PORT_DEFAULT")
if [[ -n "$ATLAS_DEFAULT" ]]; then
  CMD+=(--atlas "$ATLAS_DEFAULT")
fi
if [[ "$OPEN_BROWSER" == "1" ]]; then
  CMD+=(--open)
fi

SERVER_URL="http://${HOST_DEFAULT}:${PORT_DEFAULT}"
echo ""
echo "Press Ctrl+C when you are done to stop the server."echo ""
echo ""
echo "============================================================"
echo "  GRID REVIEW APP RUNNING"
echo "  Paste this URL into your browser:"
echo "      ${SERVER_URL}"
echo "============================================================"
echo ""

exec "${CMD[@]}" "${POSITIONAL[@]}"
