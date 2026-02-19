#!/usr/bin/env bash
set -euo pipefail

APPTAINER_IMAGE=${APPTAINER_IMAGE:-/groups/plaschka/shared/software/containers/epu_mapper_review.sif}
DEFAULT_HOST=${HOST_OVERRIDE:-127.0.0.1}
DEFAULT_PORT=${PORT_OVERRIDE:-8000}

usage() {
  cat <<USAGE
Usage: $(basename "$0") --epu-dir /path/to/Images-Disc1 [--atlas /path/to/atlas.jpg] [options] [-- review_app args]

Required:
  --epu-dir PATH      Path to the EPU-generated folder (typically "Images-Disc1") that
                      contains the automated screening session output.

Common optional:
  --atlas PATH        Path to the atlas screenshot (absolute or relative).

Less frequently changed:
  --host HOST         Host interface for uvicorn (default: ${DEFAULT_HOST}).
  --port PORT         Port for uvicorn (default: ${DEFAULT_PORT}).
  --image PATH        Override Apptainer image (default: ${APPTAINER_IMAGE}).
  -h, --help          Show this help.

Arguments after "--" are forwarded directly to review_app.py.
USAGE
}

EPU_DIR=""
ATLAS_PATH=""
HOST="$DEFAULT_HOST"
PORT="$DEFAULT_PORT"
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --epu-dir)
      EPU_DIR="$2"
      shift 2
      ;;
    --atlas)
      ATLAS_PATH="$2"
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

if [[ -z "$EPU_DIR" ]]; then
  echo "--epu-dir is required." >&2
  usage
  exit 1
fi

if [[ ! -d "$EPU_DIR" ]]; then
  echo "Grid directory '$EPU_DIR' does not exist." >&2
  exit 1
fi
EPU_DIR="$(cd "$EPU_DIR" && pwd)"

if [[ ! -f "$APPTAINER_IMAGE" ]]; then
  echo "Cannot find Apptainer image: $APPTAINER_IMAGE" >&2
  exit 1
fi

BIND_ARGS=(--bind "$EPU_DIR:$EPU_DIR")
if [[ -n "$ATLAS_PATH" ]]; then
  if [[ ! -f "$ATLAS_PATH" ]]; then
    echo "Atlas file '$ATLAS_PATH' not found." >&2
    exit 1
  fi
  ATLAS_PATH="$(cd "$(dirname "$ATLAS_PATH")" && pwd)/$(basename "$ATLAS_PATH")"
  atlas_dir=$(cd "$(dirname "$ATLAS_PATH")" && pwd)
  if [[ "$atlas_dir" != "$EPU_DIR" ]]; then
    BIND_ARGS+=(--bind "$atlas_dir:$atlas_dir")
  fi
fi

CMD=(apptainer exec "${BIND_ARGS[@]}" "$APPTAINER_IMAGE" start-review-app "$EPU_DIR" --host "$HOST" --port "$PORT")
if [[ -n "$ATLAS_PATH" ]]; then
  CMD+=(--atlas "$ATLAS_PATH")
fi
if [[ ${#EXTRA_ARGS[@]} -gt 0 ]]; then
  CMD+=(-- "${EXTRA_ARGS[@]}")
fi

#echo "Running: ${CMD[*]}"
exec "${CMD[@]}"
