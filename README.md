# EPU Mapper Review App

EPU Mapper is a FastAPI-based web application for reviewing cryo-EM automated
screening sessions. It converts the folder structure produced by Thermo Fisher
EPU (e.g. `Images-Disc1`) into an interactive browser workflow that lets you
score each GridSquare, inspect FoilHole/Data images, and export comprehensive
PDF reports.

## Highlights

- **GridSquare Review UI** – browse grid, foil, and data thumbnails, toggle
  JPEG/MRC views, record ratings and notes, and mark squares for inclusion.
- **Metadata-aware PDFs** – generates a one-page roll-up report and a detailed
  "selected" report containing montage pages for the GridSquares you included.
- **Atlas integration** – optionally overlay atlas screenshots for consistent
  navigation.
- **Portable deployment** – run locally with Python or via the provided
  container/Apptainer workflow.

## Expected Data Layout

Point the app at an EPU session directory that contains subfolders named
`GridSquare_<ID>` plus optional `FoilHoles`/`Data` subdirectories. A minimal
example looks like this:

```
Images-Disc1/
├── GridSquare_16736167/
│   ├── GridSquare_20260218_014209.jpg
│   ├── GridSquare_20260218_014209.mrc
│   ├── FoilHoles/
│   │   ├── FoilHole_1_....jpg
│   │   └── ...
│   └── Data/
│       ├── FoilHole_1_Data_....jpg
│       ├── FoilHole_1_Data_....xml
│       └── ...
├── GridSquare_16736168/
│   └── ...
├── review_responses.json   # created by the app; optional on startup
└── review_report.pdf       # generated output (optional)
```

Only JPEGs are required; MRC files (for contrast tweaking) and XML metadata are
used automatically when present. Atlas screenshots can live anywhere as long as
you pass their path with `--atlas` (or place them alongside each GridSquare).

## Running Locally (Python)

1. Create a virtual environment and install dependencies:
   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```
2. Launch the app:
   ```bash
   PYTHONPATH=src .venv/bin/python src/review_app.py /path/to/Images-Disc1 \
       --atlas /path/to/atlas_w_square_numbering.JPG --host 127.0.0.1 --port 8000
   ```
3. Open the printed URL (default `http://127.0.0.1:8000`) in your browser.
4. When you finish rating all GridSquares, click **Download report** or
   **Download selected report**.

## Running via Apptainer

The repository includes scripts to build and deploy an Apptainer image to the
Plaschka cluster.

### Build & Deploy (local machine with Docker)

```bash
./scripts/build_and_copy_epu_mapper.sh
```

This script:
1. Builds `container/Dockerfile` (python:3.11-slim + project sources).
2. Saves the image, uploads it to `matthias.vorlaender@cbe.vbc.ac.at`, and
   converts it into `/groups/plaschka/shared/software/containers/epu_mapper_review.sif`.
3. Copies the wrapper to `/groups/plaschka/shared/software/containers/wrappers/`.

### Run on the cluster

```bash
/groups/plaschka/shared/software/containers/wrappers/run_epu_mapper_apptainer.sh \
    --epu-dir /groups/.../Images-Disc1 \
    --atlas /groups/.../atlas_w_square_numbering.JPG \
    --host 0.0.0.0 --port 8010
```

The wrapper automatically binds the EPU directory (and the atlas directory if
it lies elsewhere) into the container so that responses and PDFs are saved next
to your data. The script prints a prominent banner with the URL to paste into a
browser. Use `--no-open` if you do *not* want the auto-open hint added inside
containers.

## Reports & Outputs

- `review_report.pdf` – single-page overview showing atlas preview, counts, and
  a table listing each GridSquare, rating, inclusion flag, and reviewer notes.
- `review_report_selected.pdf` – generated from `/selected_report`; includes a
  montage page per included GridSquare with atlas/grid/foil/data panels and
  metadata when available.
- `review_responses.json` – simple JSON map storing ratings/notes/inclusion
  flags; placed in the root EPU directory and reloaded automatically.

## Development Notes

- Source code lives in `src/`. Key modules:
  - `review_app.py` – FastAPI routes + HTML front-end served to reviewers.
  - `build_collage.py` – image handling, PDF generation, metadata parsing.
- Tests are not yet formalized; use the provided Example data to smoke test the
  UI and report generation.
- Container assets reside in `container/`; deployment scripts in `scripts/`.

Pull requests are welcome! Please open an issue if you encounter data layouts
that the parser doesn’t recognize or if you need support for additional file
formats.
