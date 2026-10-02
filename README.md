# EPU Mapper

EPU Mapper helps you to review cryo-EM screening results
and select areas for data collection. It links EPU images across Atlas,
GridSquare, FoilHole and Data magnifications, keeping images, their locations
and user annotations together.

The program is free and open source, runs locally, and opens in a web browser.

![EPU screening review with linked Atlas, GridSquare, FoilHole and Data images](images/EPU_mapper_screening_v080.png)

## What you can do

- Navigate between grid positions and their associated images.
- Record ratings and comments, mark squares as suitable or unsuitable, and add
  unscreened collection targets.
- View annotations on the Atlas, with adjustable overlay opacity.
- Zoom, pan, enlarge images and adjust contrast or low-pass filtering.
- Inspect calibrated scale bars (default **200 Å**) and change each viewer’s
  length under **Scale bar**, or choose **Auto size**.
- Exclude FoilHoles without Data previews from review and HTML reports.
- Inspect EPU's recorded FoilHole intensities and final selections, then refine
  an acquired-hole subset with a reversible selection brush.
- Share annotated screening results as self-contained HTML reports that open
  without EPU Mapper or the original data.

EPU Mapper supports both screening and multi-exposure collections. It helps
document collection decisions.

## Changelog: v0.2.9 → v0.10.0

This consolidated summary covers the major changes since v0.2.9.

- **Unified screening and acquisition review.** One browser workspace links
  Atlas, GridSquare, FoilHole and individual Data exposures, including
  multi-exposure collections and a FoilHole-only mode.
- **Annotations and collection planning.** Save ratings, suitability and
  comments locally; view them directly on the Atlas with legends and adjustable
  opacity; mark unscreened targets or rectangular areas for later collection.
- **Better image inspection.** Linked selection, zoom, pan, enlarged views,
  contrast presets, gamma and low-pass filtering, plus on-demand Atlas,
  GridSquare and individual Data MRC loading. Planned exposure footprints and
  observed targeting-shift annotations help inspect acquisition geometry.
  Calibrated scale bars default to 200 Å, with custom lengths and automatic sizing
  in the browser and HTML reports.
- **Portable results.** Export self-contained HTML screening reports with
  embedded images and interactive Atlas layers, or copy a full portable session
  to another computer. HTML reports include independent Atlas and GridSquare
  overlay opacity, unlabelled FoilHole circles and a moving active-hole highlight.
  Annotation JSON and detailed PDF exports remain available.
- **Focused review.** Optionally exclude FoilHoles without Data previews from
  navigation, clickable overlays and HTML reports; the reversible setting is
  saved per session.
- **CryoSPARC particle mapping and filtering.** Map particle subsets back to
  EPU exposures, inspect per-square counts and density overlays, and select
  acquired holes using an intensity histogram and a brush with undo/redo.
  Export filtered particle tables with UID-aligned passthrough rows, a selection
  CSV and a provenance manifest; reuse portable `.epuholes.json` selections
  through the launcher's standalone filtering tab.
- **Large-dataset and network-drive workflows.** Persistent indexing, local
  preview caching and explicit refresh reduce repeated network reads. Native
  file pickers, memory-mapped particle tables, chunked background processing
  and direct-folder exports support large CryoSPARC datasets.
- **Installation and documentation.** Portable Windows builds, a macOS
  launcher, automatic free-port selection and clearer startup diagnostics;
  an illustrated walkthrough and expanded data requirements. A separate
  [comparison tool](docs/cryosparc-ice-comparison.md) relates recorded EPU
  intensities to CryoSPARC's relative ice estimates.

## Install

### Windows

Download the portable ZIP from [Releases](https://github.com/mvorlander/EPU_mapper/releases),
extract it completely, and run **EPUMapperReview.exe**. Python is included.

### macOS

With Conda installed, download the repository using **Code → Download ZIP**
or clone it. Open a terminal in the repository folder and run:

```bash
conda env create -f environment.yml
conda activate epu-mapper
./scripts/build_macos_launcher.sh --install
```

Open **EPU Mapper** from Finder or Spotlight. The launcher uses the Conda
environment, which must remain installed. See [macOS setup details](macos/README.md).

### Linux / command line

Create and activate the same Conda environment, then run:

```bash
python src/review_app.py /path/to/session --atlas /path/to/Atlas --auto-port --open
```

## Review a session

For illustrated instructions, see the [detailed walkthrough](#detailed-walkthrough) below.

1. **Choose your data.** Select the EPU session output folder and its matching
   Atlas in the launcher, then click **Start review**. Keep the launcher open
   while using the dashboard.
2. **Inspect images.** Click a square on the Atlas or in the list, then select
   holes or use the Previous/Next buttons. JPEG/PNG previews load on demand;
   Atlas, GridSquare and individual Data MRCs can be loaded for closer inspection.
3. **Annotate.** Use the review bar for ratings, suitability and comments.
   **Cmd/Ctrl + Enter** in the comment box saves and advances to the next square.
   Use **Add target / area** below the Atlas to mark additional collection targets.
4. **Export.** Open **Export HTML screening report** to share your assessment.

You need the session's image previews and XML/DM metadata, not just movies.
Preserve the original folder structure and include the matching Atlas.
See [which folders to request from your facility](docs/acquisition-input-data.md#what-to-request).

Annotations are saved locally, without changing the source images.
**Export annotations** downloads a JSON copy. **Session annotation settings**
can clear annotations after confirmation, with a backup saved first.

## Share results

HTML reports include Atlas views, annotations and embedded JPEG images.
Choose one suitable square, all suitable squares, or all screened images.
The optional high-resolution Atlas retains separate annotation layers with
legends, metadata hover text and opacity control. Reports open offline in a
standard browser; regenerate them after changing annotations. GridSquare FoilHole
markers have their own opacity control, omit number labels, and highlight the
active hole with a white ring as you click markers or step through exposures.

The launcher also offers a **portable session bundle** for copying the session
and Atlas to another computer. This is separate from HTML export and takes
longer because it copies original files.

**Detailed PDF export** is available in the launcher, but does not incorporate
the current dashboard's annotations. Use HTML to share an annotated assessment.

## Optional: CryoSPARC particle mapping

Import a particle `.cs` file and its matching passthrough file, if needed, through
**CryoSPARC particle density** in the dashboard. Color-coded densities and
per-square counts show where the selected particles came from.

The **FoilHole selection & CryoSPARC export** panel starts with a
histogram-guided recorded-intensity range, or optionally all acquired holes.
The histogram retains EPU's recorded selection as a comparison. Use the
variable-radius selection brush on the GridSquare,
with EPU-style Control/Shift shortcuts, undo and redo,
then apply the selection to any compatible `.cs` table; particle-density
mapping is optional. Matching passthrough rows remain
UID-aligned. The download also includes a hole-selection CSV and a JSON manifest
recording the filter and unmatched-particle policy; particle stacks are not read
or copied.

For multi-million-particle datasets, use **Choose particle dataset…**. The native
file picker indexes the existing table in place and suggests a matching
passthrough when one is needed. This runs in a background job using memory-mapped
chunks, avoiding a browser upload and duplicate input copy. Brush
edits never rewrite particle files. Final filtering is a separate background
export; only the kept tables are written by default, while excluded tables are
optional because they can nearly double output size and time. Set a direct
output folder with its native folder picker for very large tables to avoid
creating a second ZIP-sized copy. A browser-upload fallback remains available
for environments where the native picker cannot be used.

**Download portable selection file** writes a small `.epuholes.json` containing
the resolved exposure selection but no images or particles. The launcher's
**Filter CryoSPARC particles** tab can apply it to later CryoSPARC tables without
opening the dashboard. It needs `location/micrograph_path` in either the particle
or matching passthrough table; particle coordinates are not required.

These describe the imported subset, not total particle abundance or ice thickness.
EPU's `PixelIntensityMean` is a relative image-intensity measurement, not a
calibrated ice-thickness value; the recorded final `Selected` state is retained
as a histogram comparison.
Colors are scaled within each square; blank regions mean unknown, not zero.
Mapping assumes matching image orientation and cropping. Density overlays are
not included in exported reports or bundles.

![CryoSPARC particle mapping with density-colored FoilHoles and per-square counts](images/EPU_mapper_cryosparc_v080.png)

## Tips and help

- **Slow network drive:** check **Ignore Data images** for FoilHole-only review,
  or use **Prepare local previews** to cache JPEG/PNG images.
- **Files still copying:** use **Refresh index** to discover newly available data.
- **Missing overlays:** check that the matching Atlas and positional metadata
  are present; stitched atlases may also require the Atlas MRC header.
- **Connection problems:** use the browser address opened by the launcher;
  it selects a free port automatically. Launch errors show a log location.
- **Observed targeting errors:** use **Observed targeting shifts** to annotate
  a hole's observed position with an arrow. These annotations export as JSON,
  not in HTML/PDF reports.

More details: [data requirements](docs/acquisition-input-data.md) ·
[macOS launcher](macos/README.md).

## License and acknowledgements

Released under the [MIT License](LICENSE).
Max Wilkinson (`wilkinm@mskcc.org`) shared code that helped map FoilHole
positions onto GridSquare images.

## Detailed walkthrough

### 1. Locate the EPU session output

In EPU, find **Session Setup → Output folder**. This identifies where the session
data are saved. Ask your facility for a copy of this session and its matching
Atlas, preserving the folder structure.

![EPU Session Setup with the Output folder highlighted](images/EPU_screen_setup.png)

The path shown on the microscope may differ from the path on your computer.
In EPU Mapper, select the copied or mounted session folder as it appears locally.
Do not select only the movie directory.

A typical session contains:

```text
session/
├── EpuSession.dm
├── Metadata/
│   └── GridSquare_*.dm
└── Images-Disc1/
    └── GridSquare_<id>/
        ├── GridSquare_<timestamp>.jpg
        ├── GridSquare_<timestamp>.xml
        ├── FoilHoles/
        │   └── FoilHole_*.jpg (+ matching .xml)
        └── Data/
            └── FoilHole_*_Data_*.jpg (+ matching .xml)

Atlas/                          may be stored separately
├── Atlas.dm
├── Atlas_*.jpg
├── Atlas_*.xml
└── Atlas_*.mrc
```

Include all `Images-Disc*` folders. JPEG/PNG previews and positional metadata are
needed for review; Data MRCs and movie fractions are not. Atlas and GridSquare
MRCs allow higher-resolution inspection. The matching Atlas MRC header can also
be needed to place overlays correctly on a stitched Atlas.

### 2. Fill in the launcher

![Annotated EPU Mapper launcher showing session and Atlas input fields](images/EPU_mapper_GUI_new.png)

The screenshot illustrates the input fields; some control labels may differ.
Follow the steps below for the current launcher:

1. Set **EPU session output folder** to the session identified above.
2. Select **Use EPU atlas data** and choose the corresponding **Atlas root directory**.
3. Optionally enter a **Session/Grid label** to identify the session in reports.
4. Leave **Ignore Data images** unchecked for screening review. Check it only
   if you want FoilHole images without scanning Data folders.
5. Click **Start review**. The browser opens while the data are indexed.

Keep the launcher running. Use the address it opens: the port may change if
another application is already using it. If startup fails, the launcher shows
the error and the location of the log.

### 3. Navigate the images

Click a square on the Atlas or in the left-hand GridSquare list. Its image appears
beside the Atlas, with the first available FoilHole/Data pair below.

Click a FoilHole marker to change the lower images. **Previous/Next hole** steps
between holes; **Previous/Next exposure** steps between Data images associated
with the selected hole. **Previous/Next square** sits below the GridSquare viewer.
White outlines indicate the active square on the Atlas and active hole on the
GridSquare.

Enable **Show planned Data acquisition areas on GridSquare and FoilHole** to see
all planned exposure footprints on the GridSquare. The FoilHole viewer shows the
footprint of the active Data exposure and updates it as you step between exposures.
These are positions calculated from EPU metadata, not measured beam positions.

Scroll to zoom and drag to pan. **Enlarge** opens a larger view; **Zoom to selected
hole** focuses the GridSquare on the active hole. Under **Adjust image**, try
auto-contrast, black/white levels, gamma or low-pass filtering. These change the
display, not the original data. Use **Load MRC** on the Atlas, GridSquare or Data image when
you need a higher-resolution view.

Each viewer has a **Scale bar** menu. The default is **200 Å**; enter a different
length in Å or enable **Auto size** for the current magnification. Bars update
with zoom, resizing and MRC loading. Calibration uses the EPU XML image dimensions
or the assembled Atlas MRC header, accounting for reduced JPEG previews. If a bar
would be smaller than one screen pixel or larger than the viewer, the menu explains
how to adjust it. Missing calibration is reported rather than estimated. Exported
HTML reports include the same scale controls.

Enable **Exclude FoilHoles without Data images** above the FoilHole list to review
only holes with indexed Data JPEG/PNG previews. The setting is saved per session
and applies to the list, Previous/Next hole navigation, clickable overlays and
HTML reports. Turning it off restores all holes; source files and annotations
are unchanged. This requires Data loading, so it is disabled in **Ignore Data
images** mode. After copying additional previews, use **Refresh index**.

On a network drive, previews are cached as you browse. **Prepare local previews**
caches them in advance; **Refresh index** discovers files added since indexing.

### 4. Review and mark collection targets

In the review bar, select **GridSquare** as the annotation target. Assign a rating,
mark the square suitable or unsuitable, and add a comment. Click **Save annotation**;
navigation also saves pending edits. **Cmd/Ctrl + Enter** in the comment field
saves and advances to the next square.

Choose **Atlas annotations** to view suitability, ratings or EPU categories.
The accompanying legend explains the colors. Reduce **Overlay opacity** to see
the underlying image more clearly, or select **Raw Atlas** to hide annotations.

To nominate an unscreened region, click **Add target / area**, add notes, and click
its position on the Atlas. For an area, enable **Rectangular area** and click two
opposite corners. These targets appear as dashed cyan outlines and can be removed
from the manual-target list. They record collection intentions, not microscope commands.

### 5. Export and share

Open **Export HTML screening report** and choose the image scope: one suitable
square, all suitable squares, or all screened squares and exposures. All square
annotations remain available even when fewer images are included.

Enable the high-resolution Atlas option if required, click **Build portable HTML**,
and wait for the download link. Save `<session-name>-screening-report.html` and
open it in a browser to check it before sharing. The recipient does not need
EPU Mapper or access to your data drive. Atlas layers, legends, hover details
and opacity controls remain available offline. Re-export after changing reviews.

For a copy of the source session rather than a lightweight report, use
**Export portable session…** in the launcher. Open the resulting
`EPUMapperSession.epumap` with **Open portable session…** on the destination computer.
Copying a full session can take considerably longer than generating HTML.

The launcher's **Export detailed PDF without review** produces a separate image
report; it does not include the current dashboard's annotations. Use HTML for
sharing your annotated collection assessment.
