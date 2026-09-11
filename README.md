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
- Share annotated screening results as self-contained HTML reports that open
  without EPU Mapper or the original data.

EPU Mapper supports both screening and multi-exposure collections. It helps
document collection decisions. 
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
   Atlas and GridSquare MRCs can be loaded for closer inspection.
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
standard browser; regenerate them after changing annotations.

The launcher also offers a **portable session bundle** for copying the session
and Atlas to another computer. This is separate from HTML export and takes
longer because it copies original files.

**Detailed PDF export** is available in the launcher, but does not incorporate
the current dashboard's annotations. Use HTML to share an annotated assessment.

## Optional: CryoSPARC particle mapping

Import a particle `.cs` file and its matching passthrough file, if needed, through
**CryoSPARC particle density** in the dashboard. Color-coded densities and
per-square counts show where the selected particles came from.

These describe the imported subset, not total particle abundance or ice thickness.
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

Scroll to zoom and drag to pan. **Enlarge** opens a larger view; **Zoom to selected
hole** focuses the GridSquare on the active hole. Under **Adjust image**, try
auto-contrast, black/white levels, gamma or low-pass filtering. These change the
display, not the original data. Use **Load MRC** on the Atlas or GridSquare when
you need a higher-resolution view.

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
