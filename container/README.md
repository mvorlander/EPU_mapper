# EPU Mapper Container Tooling

This folder contains the Docker assets that are used to produce the
Apptainer image deployed on the Plaschka cluster.

- `Dockerfile`: builds the python:3.11-slim based image with all project files.
- `start_review_app.sh`: entrypoint executed both inside Docker and from the
  Apptainer wrapper.

Typical workflow:

1. Run `scripts/build_and_copy_epu_mapper.sh` locally. The script builds the
   Docker image, converts it into an Apptainer `.sif` on the remote host, and
   copies the `run_epu_mapper_apptainer.sh` wrapper alongside it.
2. On the cluster, launch the UI with
   `run_epu_mapper_apptainer.sh --grid-dir /path/to/GridSquare_root --atlas /path/to/atlas.jpg`.
   The script binds the supplied directories so PDFs and response files are
   written next to the input data.

Update `requirements.txt` and rerun the build script any time dependencies or
source files change.
