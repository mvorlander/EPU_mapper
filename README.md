# EPU Screening Review App

The EPU Mapper web app speeds up the review of Data from Thermo Fisher EPU screening sessions so you can quickly decide which GridSquares (and FoilHoles inside them) are worth on. It renders every square, lets you add per-square rating and comments, and exports PDF summaries.

# UI overview
The screenshot shows the reviewing app. 

![UI overview](images/UI_overview.png)

**Top left pannel:** 
- shows the current Gridsquare image by default. If you click on any other image, the last-clicked image is shown there instead. You can adjust the contrast by clicking on the "Show MRC to adjust contrast..." button on the right. 

**Right panel:**
- Allows you to add comments and a rating of the square. If you click the checkbox on the right, screening images from that square will be included in a final PDF report. A minimal report showing only the user rating and comments next to the atlas will always be created.

**Bottom pannel:**
- shows FoilHoles next to Data images

## What You Need

Point the app at the base directory that EPU calls `Images-Disc*`. At minimum
you should see folders named `GridSquare_<ID>`, each containing the grid JPEG
plus optional `FoilHoles/` and `Data/` subfolders with JPEG/XML pairs.
You can also pass the **session root** (the folder that contains `EpuSession.dm`,
`Metadata/`, and one or more `Images-Disc*` subdirectories); the app will pick
the first matching disc automatically or you can force it via `--images-subdir`.

To draw foil overlays, keep the session metadata next to the disc:

```
Images-Disc1/
├── GridSquare_19828383/
│   ├── GridSquare_20260220_132420.jpg
│   ├── FoilHoles/FoilHole_19919351_20260220_132420.jpg (+ .xml)
│   └── Data/FoilHole_19919351_Data_20260220_132420.jpg (+ .xml)
├── Metadata/
│   ├── GridSquare_19828383.dm
│   └── (optional) TargetLocation_*.dm files when exported by EPU
├── EpuSession.dm
└── review_responses.json / PDFs   # written by the app
```

The `.dm` files inside `Metadata/` and the top-level `EpuSession.dm` are enough
for the overlay logic; per-target DM files are nice-to-have but not required.
Place an atlas JPEG anywhere and pass it with `--atlas` if you want the atlas
panel filled in—this screenshot is highly recommended because it gives reviewers
global context and helps align the foil overlay with the overall grid.

## Run Locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
PYTHONPATH=src .venv/bin/python src/review_app.py /path/to/Images-Disc1 \
    --atlas /path/to/atlas.jpg --host 127.0.0.1 --port 8000 --open
```

Open the printed URL in your browser. Remove `--overlay` (or add
`--no-overlay`) if the session lacks DM metadata.

Tip: you can also point the script at the session root (the folder that holds
`EpuSession.dm`, `Metadata/`, and multiple `Images-Disc*` directories) and add
`--images-subdir Images-Disc1` or set `IMAGES_SUBDIR=Images-Disc1` to pick the
disc you want to review.

## Run via Apptainer

1. Build/refresh the container and copy it to the cluster:
   ```bash
   ./scripts/build_and_copy_epu_mapper.sh
   ```
2. On the cluster, launch the review UI:
   ```bash
   /groups/plaschka/shared/software/containers/wrappers/epu_review.sh \
       --epu-dir /groups/.../SessionRoot --atlas /groups/.../atlas.jpg
   ```

Pass the *session root* (the folder containing `EpuSession.dm`, `Metadata/`,
and one or more `Images-Disc*` subdirectories) via `--epu-dir`. The wrapper
binds that directory so metadata stays visible. Overlays are on by default; add
`--no-overlay`
if you only want the raw JPEGs. A big banner with the URL is printed so users
in plain terminals know what to paste into a browser.

## Foil Overlay Utilities

- The main app writes `foil_overlay.png` beside each grid automatically (use
  `--no-overlay` if you prefer to disable this). If the required `Metadata/`
  or `EpuSession.dm` files are missing, overlays are skipped gracefully and a
  banner explains why.
- Overlays default to the `identity` transform (matching EPU’s orientation).
  If you know a specific rotation/flip is needed, supply
  `--overlay-transform rot90` (or `rot180`, `rot270`, `mirror_x`,
  `mirror_y`, `mirror_diag`, `mirror_diag_inv`) or use `--overlay-transform auto`
  to let the tool pick the best match on the fly.
- To debug mapping logic on a single square:
  ```bash
  PYTHONPATH=src MPLCONFIGDIR=/tmp/mplcache FONTCONFIG_PATH=/tmp/mplcache \
    python scripts/plot_foilhole_positions.py \
      Example_data/prefloated/Images-Disc1/GridSquare_19828383 \
      --output /tmp/GridSquare_19828383_overlay.png \
      --dump-transforms /tmp/outdir
  ```
  That command also saves diagnostic PNGs for each tested rotation/mirror in
  `/tmp/outdir`.

## Outputs

- `Screening_overview.pdf` – one-page overview of ratings, selections, and
  atlas snapshot.
- `Screening_details.pdf` – montage pages for squares you marked for data
  collection, including foil/data thumbnails plus metadata.
- `review_responses.json` – the persisted ratings, comments, and inclusion
  flags, written next to the disc so you can resume later.

Use the web UI to download either report once you finish reviewing. The app’s
sole goal is to surface the best GridSquares/FoilHoles for downstream data
collection decisions.
