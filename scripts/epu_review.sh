#!/usr/bin/env bash
set -euo pipefail

APPTAINER_IMAGE=${APPTAINER_IMAGE:-/resources/cryo-em/epu_mapper_review.sif}
DEFAULT_HOST=${HOST_OVERRIDE:-127.0.0.1}
DEFAULT_PORT=${PORT_OVERRIDE:-8000}

usage() {
  cat <<USAGE
Usage: $(basename "$0") --epu-dir /path/to/session_root [--atlas /path/to/Atlas] [options] [-- review_app args]

Required:
  --epu-dir PATH      Path to the session root that contains EpuSession.dm,
                      a Metadata/ folder, and one or more Images-Disc* directories.
                      The wrapper binds this directory so overlays and outputs can
                      be written next to the EPU data.

Common optional:
  --atlas PATH        Path to the atlas directory or atlas screenshot
                      (absolute or relative).
  --session-label TXT Prefix added to generated PDF filenames.
  --no-overlay        Skip automatic creation/display of foil overlays (enabled by default).
  --details-only      Generate the detailed PDF for every GridSquare and exit (no web UI).
  --details-output PATH Custom path for the detailed PDF when using --details-only.

Less frequently changed:
  --host HOST         Host interface for uvicorn (default: ${DEFAULT_HOST}).
  --port PORT         Port for uvicorn (default: ${DEFAULT_PORT}).
  --overlay-transform NAME  Force overlay transform (identity, rot90, rot180, rot270, mirror_x,
                            mirror_y, mirror_diag, mirror_diag_inv, auto). Default: identity.
  --image PATH        Override Apptainer image (default: ${APPTAINER_IMAGE}).
  -h, --help          Show this help.

Arguments after "--" are forwarded directly to review_app.py.
USAGE
}

SESSION_DIR=""
ATLAS_PATH=""
SESSION_LABEL=""
DETAILS_ONLY=0
DETAILS_OUTPUT=""
HOST="$DEFAULT_HOST"
PORT="$DEFAULT_PORT"
EXTRA_ARGS=()
OVERLAY=1
OVERLAY_TRANSFORM="identity"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --epu-dir)
      SESSION_DIR="$2"
      shift 2
      ;;
    --atlas)
      ATLAS_PATH="$2"
      shift 2
      ;;
    --session-label)
      SESSION_LABEL="$2"
      shift 2
      ;;
    --details-only)
      DETAILS_ONLY=1
      shift
      ;;
    --details-output)
      DETAILS_OUTPUT="$2"
      shift 2
      ;;
    --overlay)
      OVERLAY=1
      shift
      ;;
    --no-overlay)
      OVERLAY=0
      shift
      ;;
    --overlay-transform)
      OVERLAY_TRANSFORM="$2"
      shift 2
      ;;
    --host)
      HOST="$2"
      shift 2
      ;;
    --port)
      PORT="$2"
      shift 2
      ;;
    --image)
      APPTAINER_IMAGE="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      EXTRA_ARGS+=("$@")
      break
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ -z "$SESSION_DIR" ]]; then
  echo "--epu-dir is required." >&2
  usage
  exit 1
fi

if [[ ! -d "$SESSION_DIR" ]]; then
  echo "Session directory '$SESSION_DIR' does not exist." >&2
  exit 1
fi
SESSION_DIR="$(cd "$SESSION_DIR" && pwd)"

if [[ ! -f "$APPTAINER_IMAGE" ]]; then
  echo "Cannot find Apptainer image: $APPTAINER_IMAGE" >&2
  exit 1
fi

resolve_images_dir() {
  local base="$1"
  if [[ -n "${IMAGES_SUBDIR:-}" && -d "$base/$IMAGES_SUBDIR" ]]; then
    printf '%s\n' "$(cd "$base/$IMAGES_SUBDIR" && pwd)"
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

if [[ "$(basename "$SESSION_DIR")" == Images-Disc* ]]; then
  GRID_DIR="$SESSION_DIR"
  SESSION_ROOT="$(cd "$(dirname "$SESSION_DIR")" && pwd)"
else
  SESSION_ROOT="$SESSION_DIR"
  if ! GRID_DIR=$(resolve_images_dir "$SESSION_ROOT"); then
    echo "Unable to locate an Images-Disc* directory inside '$SESSION_ROOT'." >&2
    echo "Set IMAGES_SUBDIR or pass the Images-Disc* directory directly." >&2
    exit 1
  fi
fi

SESSION_DM="$SESSION_ROOT/EpuSession.dm"
METADATA_DIR="$SESSION_ROOT/Metadata"
MISSING_OVERLAY=()
[[ -f "$SESSION_DM" ]] || MISSING_OVERLAY+=("EpuSession.dm")
[[ -d "$METADATA_DIR" ]] || MISSING_OVERLAY+=("Metadata folder")
if [[ "$OVERLAY" == "1" && ${#MISSING_OVERLAY[@]} -gt 0 ]]; then
  echo "WARNING: Foil overlays disabled because ${MISSING_OVERLAY[*]} not found under '$SESSION_ROOT'." >&2
  echo "         The app will continue without overlays; supply --no-overlay to silence this warning." >&2
  OVERLAY=0
fi

BIND_ARGS=(--bind "$SESSION_ROOT:$SESSION_ROOT")
if [[ "$GRID_DIR" != "$SESSION_ROOT" ]]; then
  BIND_ARGS+=(--bind "$GRID_DIR:$GRID_DIR")
fi
if [[ -n "$ATLAS_PATH" ]]; then
  if [[ ! -f "$ATLAS_PATH" ]]; then
    echo "Atlas file '$ATLAS_PATH' not found." >&2
    exit 1
  fi
  ATLAS_PATH="$(cd "$(dirname "$ATLAS_PATH")" && pwd)/$(basename "$ATLAS_PATH")"
  atlas_dir=$(cd "$(dirname "$ATLAS_PATH")" && pwd)
  if [[ "$atlas_dir" != "$SESSION_ROOT" && "$atlas_dir" != "$GRID_DIR" ]]; then
    BIND_ARGS+=(--bind "$atlas_dir:$atlas_dir")
  fi
fi

CMD=(apptainer exec "${BIND_ARGS[@]}" "$APPTAINER_IMAGE" start-review-app "$SESSION_ROOT" --host "$HOST" --port "$PORT")
if [[ -n "$ATLAS_PATH" ]]; then
  CMD+=(--atlas "$ATLAS_PATH")
fi
if [[ -n "$SESSION_LABEL" ]]; then
  CMD+=(--session-label "$SESSION_LABEL")
fi
if [[ "$OVERLAY" == "1" ]]; then
  CMD+=(--overlay)
  if [[ -n "$OVERLAY_TRANSFORM" ]]; then
    CMD+=(--overlay-transform "$OVERLAY_TRANSFORM")
  fi
else
  CMD+=(--no-overlay)
fi
if [[ "$DETAILS_ONLY" == "1" ]]; then
  CMD+=(--details-only)
fi
if [[ -n "$DETAILS_OUTPUT" ]]; then
  CMD+=(--details-output "$DETAILS_OUTPUT")
fi
if [[ ${#EXTRA_ARGS[@]} -gt 0 ]]; then
  CMD+=(-- "${EXTRA_ARGS[@]}")
fi

#echo "Running: ${CMD[*]}"
exec "${CMD[@]}"
