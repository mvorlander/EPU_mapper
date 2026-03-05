# EPU Screening Review App

The EPU Mapper web app speeds up the review of Data from Thermo Fisher EPU screening sessions so you can quickly decide which GridSquares (and FoilHoles inside them) are worth on. It renders every square, lets you add per-square rating and comments, and exports PDF summaries.

# UI overview
The screenshot shows the reviewing app. 

![UI overview](images/UI_overview.png)

**Top left pannel:** 
- Shows the Atlas by default (when available); otherwise it starts on the current GridSquare image. Clicking any Atlas/GridSquare/FoilHole/Data image updates this viewer to the last-clicked item. You can adjust contrast via **Show MRC for selected image** on the right. Zoom stays inside the viewer window, and **Pan** lets you drag around the zoomed image.

**Right panel:**
- Allows you to add comments and a rating of the square. If you click the checkbox on the right, screening images from that square will be included in a final PDF report. A minimal report showing only the user rating and comments next to the atlas will always be created.

**Bottom pannel:**
- Shows FoilHoles next to Data images

## What You Need

- Point the app at the **session root** (the directory that contains `EpuSession.dm`,
`Metadata/`, and one or more `Images-Disc*` subfolders).
- Atlas input (optional but strongly recommended):
  - **Preferred (new mode):** an **Atlas root directory** that contains `Atlas_*.jpg` (or `.png`) plus metadata (`.dm`; optional `.mrc` for contrast).
  - **Legacy mode:** a static atlas screenshot (`.jpg`/`.png`) without metadata-based highlighting.

<details>
<summary>Advanced path options</summary>

- The app picks the first disc automatically; override it with
  `--images-subdir Images-Disc2` or `IMAGES_SUBDIR=Images-Disc2`.
- Power users can still point directly at an `Images-Disc*` folder (or even a
  single `GridSquare_<ID>` directory) when debugging individual squares, but
  the session root keeps all metadata together and remains the recommended
  default.

</details>

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

The `.dm` files inside `Metadata/` plus the top-level `EpuSession.dm` are used to plot the FoilHole positions onto the GridSquare images.
For atlas mapping, pass `--atlas` as either an atlas image path or an atlas
directory.

## Windows Installer

1. Download the latest `EPUMapperReviewInstaller_<version>.exe` from the
   [Releases page](https://github.com/mvorlander/EPU_mapper/releases).
2. Double-click the installer and accept the defaults (the installer bundles
   Python, so no extra dependencies are needed).
3. Launch **EPU Mapper Review** from the Start Menu shortcut, choose your
   session root (or `Images-Disc*` folder), then select one Atlas mode:
   - **Use EPU atlas data** (default): provide the Atlas root directory.
   - **Use atlas screenshot with screened GridSquares**: provide a static atlas image.
   Click **Start review**.
4. Overlay transform is now under **Show advanced settings** (hidden by
   default in the launcher).

Advanced packaging details for maintainers are documented separately in
`windows/README.md`.

## Run Locally (conda)

Use the provided `environment.yml` to create a reproducible Conda environment.

**Installation**

```bash
conda env create -f environment.yml          # first time only
conda activate epu-mapper
# pull in dependency updates later with: conda env update -f environment.yml
```

**Usage**

```bash
./scripts/run_review_app.sh /path/to/session_root --atlas /path/to/Atlas --host 127.0.0.1 --port 8000 --open
```
<details>
If you prefer to target a specific disc directly, replace `/path/to/session_root`
with `/path/to/Images-Disc1` (or another disc) and drop `--images-subdir`. When
the session root contains multiple discs, add `--images-subdir Images-Disc2` (or
set `IMAGES_SUBDIR=Images-Disc2`) to pick one explicitly. Remove `--overlay` (or
add `--no-overlay`) if the metadata files are missing or you only want raw JPEGs.

Prefer running through `scripts/run_review_app.sh` whenever possible—it keeps
`PYTHONPATH` pointed at `src/` and mirrors the exact invocation the container
and Windows builds use.
</details>

### Optional helpers

- **Prefix PDF names** – provide a session/grid label once and reuse it for
  both reports. Either set `SESSION_LABEL=MyRun` (or `GRID_LABEL=/REPORT_PREFIX`)
  before launching, or pass `--grid-label MyRun` / `--session-label MyRun` to
  the wrapper/Windows launcher. The resulting files become
  `MyRun_Screening_overview.pdf` and `MyRun_Screening_details.pdf`.
- **Add one session-level summary sentence** – after the final GridSquare, the
  completion page includes a text field for a single summary sentence that is
  included in generated reports.
- **Skip the UI and export everything** – add `--details-only`
  (alias: `--export-all-details`) to the command to render the detailed PDF for
  *every* GridSquare, then exit immediately. Use `--details-output path/to/out.pdf`
  if you want to override the default filename.

### GridSquare Order

- GridSquares are displayed in acquisition order based on timestamps parsed from
  `GridSquare_YYYYMMDD_HHMMSS.jpg` file names (earliest first), which should
  better match EPU acquisition screenshots.
- If timestamps are missing/unparseable, the app falls back to `GridSquare_<ID>`
  numeric ordering.


### Troubleshooting (ports)

- If the app fails to start with “Address already in use,” the port is occupied.
  Either change the port (`./scripts/run_review_app.sh ... --port 8010`) or stop
  the other instance.
- On macOS/Linux run `lsof -i :8000` to find the owning process and terminate it
  (e.g., `kill <PID>`). On Windows run `netstat -ano | find "8000"` or use Task
  Manager to close the conflicting app.
- The Windows launcher also exposes the port field, so you can bump it to an
  unused value without leaving the GUI.

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
- Overlays default to the `identity` transform (matching EPU’s orientation). In case you find the plotted positions don't match, there are options to force rotating the GridSquare image.

<details>
<summary>GridSquare rotation options</summary>

- If you know a specific rotation/flip is needed, supply
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

</details>

## Atlas Marker Overlay

- When you provide an atlas image via `--atlas`, the app now tries to read
  `Atlas.dm` from the same folder and marks the currently reviewed GridSquare
  directly on the atlas panel.
- `--atlas` accepts either an atlas image file or an atlas directory. If you
  pass a directory, the app auto-picks the latest matching `Atlas_*.jpg/.png`.
- If the atlas JPG is downsampled, marker coordinates are scaled automatically
  (using `Atlas_*.mrc` dimensions when present).
- If no atlas metadata is found, the app falls back to the plain atlas image.
  Use `--no-atlas-overlay` to disable this overlay behavior.
- In the Windows launcher, this behavior maps to:
  - **Use EPU atlas data** → metadata-based atlas overlays in UI/PDF
  - **Use atlas screenshot with screened GridSquares** → static atlas mode
- The atlas panel is clickable: selecting it enables `Show MRC` (if
  `Atlas_*.mrc` is present), the same contrast sliders, and zoom controls
  (`Zoom -`, `Zoom +`, `Reset zoom`) used for the main image viewer.

## Outputs

- `Screening_overview.pdf` – one-page overview of ratings, selections, and
  atlas snapshot.
- `Screening_details.pdf` – montage pages for squares you marked for data
  collection, including foil/data thumbnails plus metadata.
- `review_responses.json` – the persisted ratings, comments, and inclusion
  flags, written next to the disc so you can resume later.
- `review_summary.txt` – optional one-line session summary entered on the final
  page before downloading reports.

Use the web UI to download either report once you finish reviewing. The app’s
sole goal is to surface the best GridSquares/FoilHoles for downstream data
collection decisions.
