#!/usr/bin/env bash
set -euo pipefail
IMAGE_NAME="predictomes_vis"
TAR_NAME="predictomes_vis.tar"
TARGET_DIR="/groups/plaschka/shared/software/predictomes/containers"
TARGET_HOST="matthias.vorlaender@cbe.vbc.ac.at"
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

if ! docker info >/dev/null 2>&1; then
  echo "Error: Docker daemon is not running. Please start Docker Desktop or your Docker service and retry." >&2
  exit 1
fi

BUILD_MARKER=".build.predictomes_vis.done"
SAVE_MARKER=".save.predictomes_vis.done"
UPLOAD_MARKER=".upload.predictomes_vis.done"
WRAPPER_MARKER=".wrapper.predictomes_vis.done"
APPTAINER_MARKER=".apptainer.predictomes_vis.done"

# Allow overwrite by always rebuilding/saving/uploading; markers are informational only.
docker build --platform linux/amd64 -f predictomes_vis/container/Dockerfile -t "$IMAGE_NAME" .
touch "$BUILD_MARKER"

docker save -o "$TAR_NAME" "$IMAGE_NAME":latest
touch "$SAVE_MARKER"

scp "$TAR_NAME" "$TARGET_HOST:$TARGET_DIR/"
touch "$UPLOAD_MARKER"

scp scripts/predictomes_vis.sh "$TARGET_HOST:/groups/plaschka/shared/software/predictomes/wrappers/"
touch "$WRAPPER_MARKER"

ssh "$TARGET_HOST" "cd $TARGET_DIR && apptainer build --force ${IMAGE_NAME}.sif docker-archive://$TAR_NAME"
touch "$APPTAINER_MARKER"
