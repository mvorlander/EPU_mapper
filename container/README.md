# EPU Mapper Container Tooling

This folder contains the Docker assets that are used to produce the
Apptainer image deployed on the Plaschka cluster.

- `Dockerfile`: builds the python:3.11-slim based image with all project files.
- `start_review_app.sh`: entrypoint executed both inside Docker and from the
  Apptainer wrapper.

Typical workflow:

1. Run `scripts/build_and_copy_epu_mapper.sh` locally. The script builds the
   Docker image, converts it into an Apptainer `.sif` on the remote host, and
   copies the `epu_review.sh` wrapper alongside it.
2. On the cluster, launch the UI with
   `epu_review.sh --epu-dir /path/to/session_root --atlas /path/to/atlas.jpg`.
   Point `--epu-dir` at the folder that contains `EpuSession.dm`, `Metadata/`,
   and your `Images-Disc*` subdirectories. The wrapper binds that root so
   metadata stays visible and PDFs/JSON/overlays are written next to your data
   (unless `--no-overlay` is supplied).

Update `requirements.txt` and rerun the build script any time dependencies or
source files change.
