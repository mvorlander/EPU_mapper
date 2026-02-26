# EPU Mapper Container Tooling

This folder contains the Docker assets that are used to produce the
Apptainer image deployed on the Plaschka cluster.

- `Dockerfile`: builds the python:3.11-slim based image with all project files.
- `start_review_app.sh`: entrypoint executed both inside Docker and from the
  Apptainer wrapper.

## Build and copy workflow (VBC maintainers)

1. From the repo root, run `./scripts/build_and_copy_epu_mapper.sh`.
   - Set `DOCKER_PLATFORM=linux/arm64/v8` if you are on Apple Silicon but still
     need to produce an `amd64` image for the cluster (default is
     `linux/amd64`).
   - The script builds the Docker image, saves it to a tarball, copies it to
     `${REMOTE_CONTAINER_DIR}` on the cluster, converts it into
     `${IMAGE_NAME}.sif`, and copies the `epu_review.sh` wrapper into the
     `${REMOTE_WRAPPER_DIR}` alongside the container.
   - Customize `TARGET_HOST`, `REMOTE_CONTAINER_DIR`, or `REMOTE_WRAPPER_DIR`
     in the script if your environment differs from the shared defaults.
2. Whenever you change `requirements.txt` or source files that ship inside the
   container, rerun the script so the `.sif` stays current.

## Launching Apptainer on the cluster

Use the copied wrapper to start the UI for a specific session:

```bash
/groups/plaschka/shared/software/containers/wrappers/epu_review.sh \
    --epu-dir /groups/.../SessionRoot --atlas /groups/.../atlas.jpg
```

Pass the *session root* (the folder containing `EpuSession.dm`, `Metadata/`,
and one or more `Images-Disc*` subdirectories) via `--epu-dir`. The wrapper
binds that directory so metadata stays visible and the resulting PDFs/JSON and
overlay PNGs are written next to your data (unless you add `--no-overlay`).
