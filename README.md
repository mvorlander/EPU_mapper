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
│   └── GridSquare_19828383.dm
├── EpuSession.dm
└── review_responses.json / PDFs   # written by the app
```

The `.dm` files inside `Metadata/` and the top-level `EpuSession.dm` are enough
for the overlay logic—per-target `TargetLocation_*.dm` files are not required.
Place an atlas JPEG anywhere and pass it with `--atlas` if you want the atlas
panel filled in—this screenshot is highly recommended because it gives reviewers
global context and helps align the foil overlay with the overall grid.

## Windows Installer

1. Download the latest `EPUMapperReviewInstaller_<version>.exe` from the
   [Releases page](https://github.com/mvorlander/EPU_mapper/releases).
2. Double-click the installer and accept the defaults (the installer bundles
   Python, so no extra dependencies are needed).
3. Launch **EPU Mapper Review** from the Start Menu desktop shortcut, choose
   your session root (or `Images-Disc*` folder) plus an atlas JPEG, and click
   **Start review**.

Advanced packaging details for maintainers are documented separately in
`windows/README.md`.

## Run Locally (conda)

Use the provided `environment.yml` to create a reproducible Conda environment:

```bash
conda env create -f environment.yml          # first time only
conda activate epu-mapper
# pull in dependency updates later with: conda env update -f environment.yml
PYTHONPATH=src python src/review_app.py /path/to/Images-Disc1 \
    --atlas /path/to/atlas.jpg --host 127.0.0.1 --port 8000 --open
```

Point the script either at the `Images-Disc*` folder or at the session root
(the folder that holds `EpuSession.dm`, `Metadata/`, and your discs). Add
`--images-subdir Images-Disc1` or set `IMAGES_SUBDIR=Images-Disc1` if you pass
the session root and want to choose a specific disc. Remove `--overlay` (or add
`--no-overlay`) if the metadata files are missing or you only want raw JPEGs.

## Container Workflow (VBC only)

The Apptainer workflow used on the VBC cluster is documented in
`container/README.md`. It covers building/copying the `.sif` via
`scripts/build_and_copy_epu_mapper.sh` and running the `epu_review.sh` wrapper.
Most users outside VBC can ignore this section.

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
