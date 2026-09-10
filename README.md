# EPU Screening Review App

The EPU Mapper web app speeds up review of Thermo Fisher EPU screening sessions so you can quickly decide which GridSquares (and FoilHoles inside them) are worth following up. It renders every square, lets you add per-square ratings/comments, and exports PDF or self-contained HTML reports.

## Recent changes

### [v0.8.0](https://github.com/mvorlander/EPU_mapper/releases/tag/v0.8.0) — Unified review workspace

- **HTML Atlas legends and hover details:** legends follow the active annotation layer; marker tooltips show suitability, rating and comments, including in EPU-category view.
- **Clear session annotations:** reset reviews, manual targets and observed shifts from **Session annotation settings**, with typed confirmation and an automatic downloadable JSON backup. Images, EPU metadata and existing exports stay unchanged.
- **Live Atlas annotations:** suitability, ratings, EPU categories and manual collection targets/areas, with independent overlay opacity.
- **Portable HTML reports:** export current annotations with one suitable square, all suitable squares, or explicitly all screened images. Optional high-resolution Atlas and offline overlay-opacity control; JPEG images embedded, no MRC files.
- **Linked Atlas selection:** the displayed GridSquare has a white, outline-only Atlas highlight that follows square navigation while preserving annotation colors and overlay opacity.
- **Stitched Atlas scaling:** normalize square positions to the assembled MRC dimensions, not the camera-tile readout. Header-only reads keep JPEG browsing lightweight.
- **Atlas overlays:** retain screening annotations when loading Atlas MRCs; adjust overlay opacity independently of image contrast in both dashboards.
- **One dashboard:** screening and collection now open the same acquisition-backed interface. **Ignore Data images** changes loading scope, not layout; selections always use clicks. Existing square ratings, comments and suitability are imported without overwriting local edits.
- **Laptop-friendly layout:** horizontal review bar, aligned image viewers, side-mounted tools on wider screens and bottom toolbars on smaller laptops. Adjustment and overlay menus share a compact row; **Zoom to selected hole** is also available in FoilHole-only mode.

### v0.7.1 — Consistent image adjustment presets

- **Consistent image tools:** restore the screening viewer’s robust/strong auto-contrast, full-range, histogram-equalization and reset presets, plus gamma/low-pass sliders, in every Acquisition and FoilHole-only image viewer.

### v0.7.0 — Acquisition browsing, particle density and targeting checks

- **Acquisition mode:** browse many JPEG/PNG exposures per FoilHole, with separate square, hole, and exposure navigation.
- **FoilHole-only mode:** map and inspect holes without scanning Data directories.
- **Automatic overlays:** use FoilHole XML positions when the session's DM metadata is missing or still copying; refresh to add newly arrived holes.
- **Beam-shift groups:** solid centering-hole outlines and matching-color dashed neighbors, with adjustable circle radius/fill and optional clickable planned Data footprints from EPU metadata.
- **Stable acquisition selection:** click instead of hover, zoom to the selected hole, clearer Data footprints, and explicit explanations for beam-shifted targets without their own FoilHole preview.
- **Network-friendly loading:** background indexing, local caching and annotations, explicit refresh, and optional Atlas/GridSquare MRC loading.
- **Optional CryoSPARC density:** gradient fills independent of acquisition-group outlines, per-square particle counts, and a collapsible import panel with Clear.
- **Clearer atlas handling:** availability/suitability legends and refreshed atlas metadata when reopening a cached session. Missing count fields are never displayed as zero.
- **Visible processing:** dashboard and viewer activity indicators distinguish indexing, image loading, queued mapping, and particle import, with elapsed time for slow reads.
- **Linked selection:** the displayed hole/exposure synchronizes to a white, outline-only GridSquare highlight; density colors remain independent and stale previews clear during navigation.
- **Targeting checks:** mark observed FoilHole positions in a separate arrow layer, compare per-square shifts, and export the original/observed coordinates as JSON without modifying EPU metadata.

### [v0.6.0](https://github.com/mvorlander/EPU_mapper/releases/tag/v0.6.0) — Collection plans and image tools

- **Portable results:** Interactive JPEG-only HTML plans (no MRCs), PDF checklists,
  and independent full-session exports that preserve originals and annotations.
- **Image tools:** Enlarge, auto-contrast, black/white levels, gamma, optional
  low-pass filtering, and a persistent highlight for the displayed FoilHole.
- **Faster review:** Prioritized shortlists, preferred exposures, hover previews,
  filters, undo, and keyboard navigation.
- **Safer data:** Recoverable review drafts, visible save failures, and improved
  acquisition-aware FoilHole/Data and MRC matching.
- **Reliable launch:** Automatic free-port selection and browser readiness checks
  tied to the newly started session.

### [v0.5.1](https://github.com/mvorlander/EPU_mapper/releases/tag/v0.5.1) — Flexible reports

- **Report scope:** Choose one highest-rated suitable GridSquare or all screened
  GridSquares with their available images.
- **Export formats:** Generate either report as a **PDF** or portable,
  self-contained **HTML** file.

### [v0.3–v0.5](https://github.com/mvorlander/EPU_mapper/releases/tag/v0.5.0) — Dashboard redesign

- **Unified dashboard:** Linked the **Atlas, GridSquare, FoilHole, and Data**
  viewers with hover previews and Previous/Next navigation.
- **Faster image review:** Added **PNG-first previews** with on-demand **MRC**
  loading, contrast adjustment, zoom, and pan.
- **Review and targeting:** Added persistent ratings, comments, suitability
  decisions, live Atlas annotations, and manual unscreened targets.
- **Portable and reliable:** Added portable sessions, session-safe image
  matching, and more dependable **macOS and Windows launchers**.

## CryoSPARC particle density

### Observed targeting shifts

In the unified dashboard, select a hole with its own FoilHole preview.
Open **Observed targeting shifts** under the GridSquare, click **Mark observed
position**, then click the actual hole centre on the GridSquare. Scroll to zoom;
Esc cancels. The separate toggleable layer draws **EPU → observed** arrows without
moving the original markers, acquisition footprints, or particle densities.
Mark again to replace the correction for that preview, or remove it with
**Remove selected correction**. Observations save locally and are included in
annotation JSON; **Export observed shifts** exports just these measurements.
The per-square summary uses normalized image coordinates (right/down positive),
not stage coordinates. It describes only manually marked cases, not an unbiased
test of whether errors are random/systematic. Arrows are not yet in HTML/PDF reports.

### Importing particle subsets

In the launcher, choose the session and atlas and leave **Ignore Data images**
unchecked, then open **CryoSPARC particle density · optional** in the
dashboard. Import your particle `.cs` file and its matching passthrough `.cs` if
requested. **Clear density** restores ordinary acquisition overlays.

The particle table needs `uid`, `location/micrograph_path`,
`location/center_x_frac`, and `location/center_y_frac`, either itself or split
between the main and UID-matched passthrough table. For J53, use both
`particles_selected.cs` and `J53_passthrough_particles_selected.cs`. No CryoSPARC
server, particle stacks, class averages, `job.json`, or Data MRCs are needed.
EPU previews and XML/DM metadata are still required to map particles onto
GridSquares and calculate physical density from planned acquisition footprints.

Counts describe the imported subset, not all particles in the acquisition. The
color scale is linear and relative within each square. Density is held in memory
until the server stops and is not yet included in portable reports/session bundles.
After new files finish copying, use **Refresh index**, wait for completion, then
reimport the particle files. When upgrading, restart the server first;
reloading the browser alone does not update its backend.

**Atlas colors:** the **Atlas annotations** menu selects suitability (green suitable,
red unsuitable, grey unmarked), ratings (1 red through 5 green), EPU categories,
or the raw image. The legend explains the selected layer; opacity is independent
of contrast. A white outline marks the active GridSquare. These colors are
separate from the particle-density gradient on the GridSquare image.

## Why use it

- Inspect GridSquare, FoilHole, and Data images in one page.
- Use the atlas-first dashboard to jump directly to any screened GridSquare and
  browse all of its associated images without leaving the overview.
- Click an Atlas square to open its foil-overlay view beside the Atlas. Click a FoilHole to
  update the linked FoilHole and Data viewers below.
- Map the acquired FoilHoles onto the GridSquare and the current GridSquare
  position on the atlas to pick the best areas.
- Load fast JPEG/PNG previews by default, request an MRC only for the
  Atlas or GridSquare that needs closer inspection, and
  enlarge, adjust contrast, zoom, or pan continuously (scroll to zoom, then
  drag—no separate pan tool). These controls work for PNG and MRC previews.
- Inspect hole and exposure counts in the GridSquare list without confusing
  Data-preview availability with collection suitability.
- Rate each GridSquare, add reviewer comments, mark it suitable or unsuitable
  for collection, and choose whether it stays in the final report.


### Screening review

The unified workspace links Atlas, GridSquare, FoilHole and Data images, with
live ratings/suitability overlays, adjustable opacity and image contrast tools.

![Screening review with linked Atlas, GridSquare, FoilHole and Data images](images/EPU_mapper_screening_v080.png)

### CryoSPARC particle mapping

The same workspace supports multi-exposure collections and optional CryoSPARC
particle-density overlays. Hole outlines identify acquisition groups; gradient
fills show relative particle density independently. Counts refer to the imported
particle subset, not total abundance; transparent holes mean unknown, not zero.

![CryoSPARC particle mapping with per-square particle counts and density-colored FoilHoles](images/EPU_mapper_cryosparc_v080.png)

### Export reports to guide data collection

EPU Mapper generates PDF or self-contained HTML reports with Atlas overviews,
ratings, collection-suitability annotations, comments, and the requested
GridSquare/FoilHole/Data imagery. These reports provide a portable record for
choosing targets and setting up high-resolution data collection.

Use **Export HTML screening report** in the unified dashboard. Choose one suitable
square (default), all suitable squares, or explicitly all screened squares and
their indexed exposures. Annotations for all squares remain available even when
the image scope is restricted. Export runs in the background and provides a
download link when ready. Detailed PDF export remains available in the launcher.

Double-click the HTML file to open the read-only plan in a modern browser:
no server, EPU Mapper installation, or Internet connection is needed. Full
session bundles are exported separately with **Export full session** and include
`EPUMapperSession.epumap` for resuming edits in the launcher. Neither export waits
for or automatically generates the other. The original MRCs
remain in the full bundle; the lightweight HTML embeds JPEG previews only
(quality 90, up to 1800 px for screening images and 4096 px for the Atlas in unified
reports). The high-resolution option reads the Atlas MRC if available, embeds a
JPEG, and keeps annotations separate for raw/category views and opacity control.

### Review to collection

Use the horizontal review bar to rate a square, mark it suitable or unsuitable,
flag it for follow-up and add comments. **Cmd/Ctrl + Enter** saves a comment and
advances to the next square; image navigation also saves pending edits.
**Add target / area** places an unscreened collection target or rectangular area
on the Atlas. These are planning annotations, not microscope commands.

**Session annotation settings** can clear all local reviews, manual targets and
observed shifts after typed confirmation. A downloadable JSON backup is saved
first; original images, EPU metadata, particle densities and old exports remain
unchanged. Refreshing will not re-import the cleared legacy reviews.

Developer checks: `python -m unittest discover -s tests -v` in the application
environment. No GitHub publication is required to build/install local changes.

## Installation

### Lightweight macOS launcher

The launcher opens **Unified review** for both screening and multi-exposure collections. Check **Ignore Data images** to skip expensive Data scans on network drives; the same Atlas, GridSquare, FoilHole and Data panels remain in place. Contrast presets, low-pass, atlas opacity, adjustable hole outlines and zoom-to-hole are available together. See [input requirements](docs/acquisition-input-data.md) for the folders to request from your facility.

After creating the Conda environment below, build a small native launcher and
install it into your user Applications folder:

```bash
./scripts/build_macos_launcher.sh --install
```

Open **EPU Mapper** from Finder or Spotlight. The launcher remembers recent
sessions and their atlas locations, reopens browse dialogs at the last-used
input location, starts the local dashboard, and provides a Stop
button plus access to details-only PDF export. A browser preparation page opens
immediately and redirects to the dashboard when session scanning is complete.
If the server fails during launch, the waiting page and launcher both show a
prominent red error. The launcher dialog includes the final server messages and
the path to the persistent log.
Keep the launcher open while using the dashboard; its **Stop server** button or
quitting the launcher stops the local site. Server output is also retained at
`~/Library/Logs/EPUMapper/server.log` for troubleshooting.
The app reuses the local Conda environment rather than bundling a second Python runtime. See
[`macos/README.md`](macos/README.md) for selecting a specific Python environment.

### Windows portable app

1. Download the latest `EPUMapperReview_portable_<version>.zip` from the
   [Releases page](https://github.com/mvorlander/EPU_mapper/releases).
2. Extract the complete ZIP to a local folder; keep its bundled files together.
3. Run `EPUMapperReview.exe`. Python and dependencies are bundled; no separate
   installation is needed.


### Install in conda env (for macOS or Linux)

Use the provided `environment.yml` to create a reproducible Conda environment.

**Installation**

```bash
conda env create -f environment.yml          # first time only
conda activate epu-mapper
# pull in dependency updates later with: conda env update -f environment.yml
```

**Usage**

```bash
./scripts/run_review_app.sh //offloaddata/path/to/session/output --atlas /path/to/Atlas --host 127.0.0.1 --port 8000 --open
```

This uses the same recommended inputs as the GUI: the EPU session output folder
for the main path, and the Atlas directory for `--atlas`.


## Step-by-step walkthrough

### 1. Find the EPU output folder

![EPU session setup](images/EPU_screen_setup.png)

Use the same folder shown as `Output folder` in the EPU session setup. This is the path you should paste into the app as the `EPU session output folder`, and it should contain one or more `Images-Disc*` folders with a structure like this:

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


### 2. Start the launcher and fill the launcher fields

![EPU Mapper launcher](images/EPU_mapper_GUI_new.png)

- `EPU session output folder:` use the EPU `Output folder` path shown above.
- `Use EPU atlas data (Recommended):` point this to the `Atlas/` folder that EPU created when generating the atlases.
- `Session/Grid label (optional):` adds a prefix to the exported PDF filenames.
- `Start review:` launches the web app.
- `Atlas/GridSquare only (skip FoilHole processing):` loads just the atlas and
  GridSquare mapping, which is much faster for sessions with very large numbers
  of FoilHoles.
- `Export detailed PDF without review:` skips the interactive UI and generates a
  detailed PDF for all GridSquares immediately.
- `Export portable session…:` copies the complete EPU session, Atlas, review
  annotations, and manual targets into a self-contained folder. Its
  `EPUMapperSession.epumap` manifest contains only relative paths.
- `Open portable session…:` loads an `.epumap` manifest and resolves the copied
  session and Atlas relative to its new folder, so the bundle can be moved to
  another disk or computer.

### 3. Use the screening dashboard

The dashboard indexes the session in the background and loads JPEG/PNG previews
on demand. Click an Atlas marker or a square in the left list to open the linked
workspace. Atlas/GridSquare and FoilHole/Data form a central 2×2 image area,
with ratings, suitability and comments in a horizontal review bar above it.
The first matched FoilHole/Data pair appears automatically;
click another numbered hole, or use **Previous hole** / **Next hole** below the
Data viewer, to update both lower viewers. Press
Command+Enter on macOS (or Ctrl+Enter elsewhere) to save and advance to the
next GridSquare.

Use **Previous GridSquare** / **Next GridSquare** directly under the GridSquare
viewer to step through the acquisition order. Each viewer has **Enlarge** and
**Adjust image** controls: auto-contrast, black/white percentiles,
gamma, and optional Gaussian low-pass filtering. Changes affect display only,
not originals or exports. Close the enlarged view with its button or Escape.
Full-session copying is a separate operation in the desktop launcher.

Every viewer supports scroll-to-zoom and drag-to-pan. Atlas and GridSquare offer
MRC loading when available; Data and FoilHole browsing uses JPEG/PNG previews.
White outlines link the active FoilHole to the GridSquare and the active
GridSquare to the Atlas. Atlas annotations update live. Legends remain outside
the image, and overlay opacity is adjustable independently.

Choose **Add target / area** under the Atlas to mark unscreened collection targets.
For rectangular areas, enable **Rectangular area** and click opposite corners.
The annotations are saved locally and appear as dashed cyan outlines; they are
included in annotation JSON and unified HTML reports.

### 4. Review GridSquares in the web app

Use the dashboard to inspect images, adjust preview contrast, rate each
GridSquare, add comments, and select collection targets.

### 5. Export the detailed pages

Export a PDF or self-contained HTML report after review. Reports show the
selected GridSquare in context with its Atlas location, GridSquare image, and
matched FoilHole/Data imagery. The unified HTML export uses current local
annotations; the launcher's detailed PDF export retains its separate legacy
review data. Unified edits are not mirrored into the legacy PDF report editor.

## Additional info

- **Prefix PDF names** – provide a session/grid label once and reuse it for
  generated reports. Either set `SESSION_LABEL=MyRun` (or `GRID_LABEL` / `REPORT_PREFIX`)
  before launching, or pass `--grid-label MyRun` / `--session-label MyRun` to
  the wrapper/Windows launcher. The default file becomes
  `MyRun_Screening_report.pdf` (and `MyRun_Screening_details.pdf` if you use details-only export).
- **HTML download names** – the session label (or source-folder name) becomes
  `<session-name>-screening-report.html`.
- **Skip the UI and export everything** – add `--details-only`
  (alias: `--export-all-details`) to the command to render the detailed PDF for
  *every* GridSquare, then exit immediately. The Windows launcher exposes the
  same behavior via **Export detailed PDF without review**. Use
  `--details-output path/to/out.pdf` if you want to override the default filename.
- **Ignore Data images** – skip Data scanning while retaining FoilHole mapping
  and the same unified layout; also available as `--ignore-data` on the CLI.

### GridSquare Order

- GridSquares are displayed in acquisition order based on timestamps parsed from
  `GridSquare_YYYYMMDD_HHMMSS.jpg` file names (earliest first), which should
  better match EPU acquisition screenshots.
- If timestamps are missing/unparseable, the app falls back to `GridSquare_<ID>`
  numeric ordering.


### Troubleshooting (ports)

- The launcher automatically selects a free port if the preferred port is busy,
  leaving the existing application running. Use the dashboard it opens, not an
  old browser tab: the new address may differ from `127.0.0.1:8000`.
- For command-line launches, pass **`--auto-port`** for the same fallback, or
  choose a specific port with `--port 8010`. A fixed-port collision fails before
  session images are loaded.

## Container Workflow (VBC only)

The Apptainer workflow used on the VBC cluster is documented in
`container/README.md`. It covers building/copying the `.sif` via
`scripts/build_and_copy_epu_mapper.sh` and running the `epu_review.sh` wrapper.
Most users outside VBC can ignore this section.


## Outputs

- `<session-name>-screening-report.html` – self-contained, JPEG-only report with
  selectable Atlas layers, matching legends, metadata hover text, opacity control,
  clickable positions and a searchable shortlist. Opens without a server or
  original data. Scope: one suitable square, all suitable squares, or explicitly
  all screened squares/exposures. Old exported snapshots do not change with edits.
- `acquisition-annotations.json` – downloadable local review annotations, including
  manual targets and observed shifts. The working copy is stored in the local
  session cache, not written to the source share.
- `annotations-before-clear.json` – backup download after clearing session annotations.
- `Screening_report.pdf` / `Screening_details.pdf` – legacy PDF outputs from the
  launcher/legacy editor. Legacy reviews use `review_responses.json`,
  `manual_collection_targets.json` and `review_summary.txt`; these are separate
  from current unified annotations.
- `EPUMapperSession.epumap` – manifest in the separately exported full-session
  folder, alongside copied originals and annotations. No HTML is generated by
  this export.

Use the web UI to download the combined report once you finish reviewing.

## License

EPU Mapper is released under the [MIT License](LICENSE). You may use, copy,
modify, distribute, sublicense, and sell it for academic, commercial, or other
purposes, subject to the license terms.

## Acknowledgements

- Max Wilkinson (`wilkinm@mskcc.org`) shared code that helped with mapping
  FoilHole positions onto GridSquare images.
