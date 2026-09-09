# Personal CryoSPARC density fork

Originally developed on `experimental/cryosparc-density`, this feature is now
integrated into the main **Acquisition** dashboard as a collapsible optional panel.
Use the standard launcher; the command launcher here remains a compatibility
shortcut. Implementation lives in `src/cryosparc_density*`, not a separate fork.

## Launch

Double-click **Launch Density Mapper.command** in this folder to open the GUI
launcher. Choose the EPU session folder (or Images-Disc1) and the optional **Atlas
folder** (or a specific JPEG/PNG atlas preview), then click **Start density mapper**.
Keep `Atlas.dm` and the matching XML beside the atlas preview for mapped positions.
The launcher remembers both paths, opens the browser once ready, and shows server
errors and logs. **Stop**, change paths, and restart to use another atlas.
Reconnect clustertmp first and use its current
mount path, which may end in `-1` after reconnecting. The script uses Matthias's
existing EPU_mapping Python environment; set `EPU_MAPPER_PYTHON` to override it.

Alternatively:

```sh
/opt/anaconda3/envs/EPU_mapping/bin/python experiments/cryosparc_density/app.py \
  /path/to/EPU-session --atlas /path/to/Atlas
```

In the dashboard select a particle `.cs` file and, if needed, the matching
passthrough file. Click **Import particle density** after EPU indexing finishes.
For J53 use **particles_selected.cs** and
**J53_passthrough_particles_selected.cs**. Do not select `templates_selected.cs`.
Both selected and excluded subsets are supported, but must be imported separately.
Import replaces the previous density dataset, not the EPU review annotations.

## Views and interpretation

- After particle import, the left GridSquare list shows **mapped particle counts**
  for the imported subset, including zero matches. Counts sum matched Data-image
  particles without requiring spatial calibration; zero is not proof of an empty
  square. Reimport replaces counts; browser reload restores them for this server run.
- **Mean density per FoilHole** (default): color-coded fills on hole positions in
  the GridSquare image. Value is selected-particle count divided by the summed
  EPU camera area of exposures represented in the supplied particle subset.
- **Binned spatial density**: coarse 4×4 cells per represented exposure, projected
  through its planned EPU footprint onto the GridSquare. No individual particle
  dots are rendered. Use **Zoom to selected hole** to inspect local distributions.
- Acquisition groups use **outline colors only**: solid centering holes, dashed
  beam-shift holes. Density uses **fills only**; selecting a hole thickens its
  outline without changing the density color.
- Opacity, visibility, and continuous **color gradients** are adjustable:
  purple–teal–yellow (default), blue–cyan–yellow–red, or grayscale. The visible
  gradient legend has numeric endpoints. The linear scale is recalculated per
  GridSquare, not a global ranking.
- Blank/unrepresented exposures are **unknown**, not inferred zero-particle
  images. Selected-only files cannot distinguish an unprocessed exposure from
  one whose particles were all excluded. Accordingly, the mean is conditional
  on represented exposures and is not an unbiased whole-hole abundance estimate.
- Overlapping exposures contribute independently to the mean-area denominator.
  Spatial cells can overlap; they are not merged into a coverage-corrected mosaic.
- Positions use EPU **planned**, not independently measured, beam landing sites.
  Particle coordinates assume an uncropped, unrotated full-frame micrograph.
  Fractional positions tolerate ordinary downsampling, but not cropping or an
  import-specific flip/rotation. Register those cases before interpretation.
- Original pick coordinates are used; residual alignment shifts are not applied.
- Density results are in memory for this server run; reload of the browser keeps
  them, restarting the server requires reimport. Standard reports do not include
  this experimental overlay yet.

## Matching and safety

Main and passthrough fields are joined by **particle UID**, including reordered
passthrough tables. Missing or duplicate UIDs fail explicitly. Micrographs match
the full EPU filename identity (hole, acquisition area, setting, timestamp),
allowing CryoSPARC alignment/denoising suffixes. Ambiguous matches are omitted and
reported; the importer never guesses from a hole ID alone.

File-based import needs no CryoSPARC API credentials or particle stacks. NumPy
loads `.cs` tables with `allow_pickle=False`; temporary uploads are removed after
processing. Limit: 256 MiB per uploaded file. Data MRCs are not read. Positional
metadata is loaded only for the selected square via EPU Mapper's local cache.

## J53 validation (2026-09-09)

Source: `/groups/plaschka/shared/data/em/Krios/260904_BUNN_GRAFIX/cryosparc/CS-260904-bunn-grafix/J53`.
Job type: `select_2D`. The main selected table contains alignments; the passthrough
contains location fields. All **11,650 particles** matched the cached clustertmp
EPU index: **2,573 exposures / 500 holes**, with zero invalid/ambiguous/unmatched
particle records. On cached `GridSquare_3855646`, **141 represented exposures /
26 holes** had usable geometry, producing 2,256 coarse spatial cells. Validation
used a disposable copy of the index, preserving user reviews. This verifies
metadata association and rendering, not physical registration of denoised images.

Small local copies of the two J53 selected tables are retained under
`~/Library/Caches/EPUMapper/cryosparc-J53-YlCfCL/` for testing. No shared CryoSPARC
jobs or files were modified; no fork or branch has been pushed to GitHub.

## Coordinate references

CryoSPARC documents fractional coordinates and bottom-up Y in
[Subset Particles by Statistic](https://guide.cryosparc.com/processing-data/all-job-types-in-cryosparc/particle-curation/job-subset-particles-by-statistic).
The importer converts Y to the top-down EPU preview convention.
See also [Manipulating .cs Files](https://guide.cryosparc.com/processing-data/tutorials-and-case-studies/manipulating-.cs-files-created-by-cryosparc)
for coordinate fields and residual alignment shifts.

Tests cover UID joins, duplicates, incomplete passthrough, filename ambiguity,
invalid coordinates, Y direction, area normalization, projection, and multipart
upload/cleanup. All 53 project tests passed; mean/spatial rendering and visibility
were verified in the browser against the cached real-data square. Run:

```sh
/opt/anaconda3/envs/EPU_mapping/bin/python -m unittest discover -s tests -q
```
