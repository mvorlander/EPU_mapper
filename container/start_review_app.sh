#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<USAGE
Usage: start-review-app [SESSION_DIR] [--atlas PATH] [--host HOST] [--port PORT] [options] [-- extra args]

Environment variables:
  SESSION_DIR    Default EPU session root (default: /data)
  IMAGES_SUBDIR  Override the Images-Disc* directory name (optional)
  ATLAS_FILE     Optional atlas filename relative to SESSION_DIR
  HOST           Listener address (default: 0.0.0.0)
  PORT           Listener port (default: 8000)
  ENABLE_OVERLAY Set to 0 to skip foil overlays (default: 1)
USAGE
}

SESSION_DIR_DEFAULT="${SESSION_DIR:-/data}"
IMAGES_SUBDIR_DEFAULT="${IMAGES_SUBDIR:-}"
ATLAS_DEFAULT="${ATLAS_FILE:-}"
HOST_DEFAULT="${HOST:-0.0.0.0}"
PORT_DEFAULT="${PORT:-8000}"
OPEN_BROWSER="${AUTO_OPEN_BROWSER:-1}"
OVERLAY_DEFAULT="${ENABLE_OVERLAY:-1}"
OVERLAY_TRANSFORM="${OVERLAY_TRANSFORM:-identity}"

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
    --overlay)
      OVERLAY_DEFAULT=1
      shift
      ;;
    --no-overlay)
      OVERLAY_DEFAULT=0
      shift
      ;;
    --overlay-transform)
      OVERLAY_TRANSFORM="$2"
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
  SESSION_DIR_DEFAULT="${POSITIONAL[0]}"
  POSITIONAL=("${POSITIONAL[@]:1}")
fi

if [[ ! -d "$SESSION_DIR_DEFAULT" ]]; then
  echo "Session directory '$SESSION_DIR_DEFAULT' not found. Bind or specify the path." >&2
  exit 1
fi

SESSION_DIR_DEFAULT="$(cd "$SESSION_DIR_DEFAULT" && pwd)"

resolve_images_dir() {
  local base="$1"
  if [[ -n "$IMAGES_SUBDIR_DEFAULT" && -d "$base/$IMAGES_SUBDIR_DEFAULT" ]]; then
    printf '%s\n' "$(cd "$base/$IMAGES_SUBDIR_DEFAULT" && pwd)"
    return 0
  fi
  if [[ -d "$base/Images-Disc1" ]]; then
    printf '%s\n' "$(cd "$base/Images-Disc1" && pwd)"
    return 0
  fi
  shopt -s nullglob
  local matches=("$base"/Images-Disc*)
  shopt -u nullglob
  if (( ${#matches[@]} == 1 )); then
    printf '%s\n' "$(cd "${matches[0]}" && pwd)"
    return 0
  fi
  return 1
}

GRID_DIR_RESOLVED=""
SESSION_ROOT_RESOLVED="$SESSION_DIR_DEFAULT"
if [[ "$(basename "$SESSION_DIR_DEFAULT")" == Images-Disc* ]]; then
  GRID_DIR_RESOLVED="$SESSION_DIR_DEFAULT"
  SESSION_ROOT_RESOLVED="$(cd "$(dirname "$SESSION_DIR_DEFAULT")" && pwd)"
elif tmp=$(resolve_images_dir "$SESSION_DIR_DEFAULT"); then
  GRID_DIR_RESOLVED="$tmp"
else
  GRID_DIR_RESOLVED="$SESSION_DIR_DEFAULT"
fi

SESSION_DM="$SESSION_ROOT_RESOLVED/EpuSession.dm"
METADATA_DIR="$SESSION_ROOT_RESOLVED/Metadata"
MISSING_COMPONENTS=()
[[ -f "$SESSION_DM" ]] || MISSING_COMPONENTS+=("EpuSession.dm")
[[ -d "$METADATA_DIR" ]] || MISSING_COMPONENTS+=("Metadata folder")
OVERLAY_NOTICE=""
if [[ "$OVERLAY_DEFAULT" == "1" && ${#MISSING_COMPONENTS[@]} -gt 0 ]]; then
  OVERLAY_DEFAULT=0
  OVERLAY_NOTICE="Foil overlays disabled: missing ${MISSING_COMPONENTS[*]} near ${SESSION_ROOT_RESOLVED}."
fi

if [[ -n "$OVERLAY_NOTICE" ]]; then
  echo "WARNING: ${OVERLAY_NOTICE}" >&2
fi

if [[ ! -d "$GRID_DIR_RESOLVED" ]]; then
  echo "Resolved Grid directory '$GRID_DIR_RESOLVED' not found." >&2
  exit 1
fi

CMD=(python /app/src/review_app.py "$GRID_DIR_RESOLVED" --host "$HOST_DEFAULT" --port "$PORT_DEFAULT")
if [[ -n "$ATLAS_DEFAULT" ]]; then
  CMD+=(--atlas "$ATLAS_DEFAULT")
fi
if [[ "$OPEN_BROWSER" == "1" ]]; then
  CMD+=(--open)
fi
if [[ "$OVERLAY_DEFAULT" == "1" ]]; then
  CMD+=(--overlay)
  if [[ -n "$OVERLAY_TRANSFORM" ]]; then
    CMD+=(--overlay-transform "$OVERLAY_TRANSFORM")
  fi
fi

SERVER_URL="http://${HOST_DEFAULT}:${PORT_DEFAULT}"
echo ""
echo "Press Ctrl+C when you are done to stop the server."
echo ""
cat <<MSG

============================================================
  GRID REVIEW APP RUNNING
  Paste this URL into your browser:
      ${SERVER_URL}
============================================================

MSG
if [[ -n "$OVERLAY_NOTICE" ]]; then
  echo "NOTE: ${OVERLAY_NOTICE}"
  echo ""
fi

exec "${CMD[@]}" "${POSITIONAL[@]}"
