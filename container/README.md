# EPU Mapper Container Tooling

This folder contains the Docker assets that are used to produce the
Apptainer image deployed on the VBC cluster. End users do **not** need to
touch these files—the container image and wrapper are already installed.

- `Dockerfile`: builds the python:3.11-slim based image with all project files.
- `start_review_app.sh`: entrypoint executed inside the container.

## Running the cluster wrapper

Launch the review UI directly from the managed wrapper located at
`/resources/cryo-em/epu_review.sh`:

```bash
/resources/cryo-em/epu_review.sh \
    --epu-dir /groups/.../SessionRoot \
    --atlas /groups/.../atlas.jpg
```

- `--epu-dir` must point to the session root that contains `EpuSession.dm`,
  `Metadata/`, and one or more `Images-Disc*` folders.
- Add `--atlas` when you have an atlas JPEG for the session (recommended).
- Use `--no-overlay` if the metadata files are missing or you want to skip
  overlay generation. All other command‑line options from `scripts/epu_review.sh`
  are also available (host, port, `--overlay-transform`, extra args, etc.).

The wrapper binds the provided session directory into the container so that
overlays, logs, PDFs, and `review_responses.json` are written next to the EPU
data.

## Maintainer note

Cluster admins can rebuild and copy the Apptainer image via
`./scripts/build_and_copy_epu_mapper.sh`, which now targets
`/resources/cryo-em/` on the remote host. End users do not need to run this.
