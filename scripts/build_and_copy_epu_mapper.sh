#!/usr/bin/env bash
set -euo pipefail

IMAGE_NAME="epu_mapper_review"
TAR_NAME="${IMAGE_NAME}.tar"
TARGET_HOST="matthias.vorlaender@cbe.vbc.ac.at"
REMOTE_CONTAINER_DIR="/groups/plaschka/shared/software/containers"
REMOTE_WRAPPER_DIR="${REMOTE_CONTAINER_DIR}/wrappers"
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DOCKERFILE_PATH="$ROOT_DIR/container/Dockerfile"
WRAPPER_SCRIPT_LOCAL="$ROOT_DIR/scripts/epu_review.sh"

cd "$ROOT_DIR"

if ! docker info >/dev/null 2>&1; then
  echo "Docker daemon is not available. Please start Docker and retry." >&2
  exit 1
fi

echo "Building Docker image ${IMAGE_NAME}..."
docker build --platform linux/amd64 -f "$DOCKERFILE_PATH" -t "$IMAGE_NAME" .

echo "Saving Docker image to ${TAR_NAME}..."
docker save -o "$TAR_NAME" "$IMAGE_NAME":latest

echo "Ensuring remote directories exist..."
ssh "$TARGET_HOST" "mkdir -p ${REMOTE_CONTAINER_DIR} ${REMOTE_WRAPPER_DIR}"

echo "Copying image archive to ${TARGET_HOST}:${REMOTE_CONTAINER_DIR}..."
scp "$TAR_NAME" "$TARGET_HOST:${REMOTE_CONTAINER_DIR}/"

echo "Building Apptainer image on remote host..."
ssh "$TARGET_HOST" "cd ${REMOTE_CONTAINER_DIR} && apptainer build --force ${IMAGE_NAME}.sif docker-archive://$TAR_NAME && rm -f $TAR_NAME"

echo "Copying wrapper script to remote host..."
scp "$WRAPPER_SCRIPT_LOCAL" "$TARGET_HOST:${REMOTE_WRAPPER_DIR}/"

echo "Done. Apptainer image stored at ${REMOTE_CONTAINER_DIR}/${IMAGE_NAME}.sif"
