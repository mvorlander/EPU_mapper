# Obtaining EPU acquisition data

## Launching acquisition review

Open `EPU Mapper.app`, select the session and matching Atlas folder, and start **Unified review**. Optionally check **Ignore Data images** to avoid scanning Data folders. This loading preference is remembered; there is no review-mode selector.

The dashboard opens while indexing continues in the background. Click a GridSquare to load its first FoilHole/Data pair. **Click** hole overlays or use **Previous/Next hole**; use **Previous/Next exposure** for individual images from one hole. Ignoring Data preserves all four panels, showing a clear Data-loading-off message, and retains automatic FoilHole overlays. Positions come from `Metadata/GridSquare_*.dm` pixel centers, with an automatic fallback to GridSquare and FoilHole XML stage coordinates. GridSquare XML reference dimensions are required. Missing metadata for one hole does not hide the others: **Refresh index** as copying progresses to add newly available holes. Coordinates are not guessed or clamped into the image.

JPEG/PNG previews load on demand into a local cache. Only Atlas and GridSquare viewers offer **Load MRC**. All viewers support scroll zoom, drag pan, enlargement, contrast routines, gamma and preview-scale Gaussian low-pass filtering. Adjustments do not change original images.

**Refresh index** explicitly checks for changed source data. **Prepare local previews** copies indexed JPEG/PNG previews, not MRCs, to the local cache; repeating resumes already cached files. It does not create a portable session bundle. Cached images and annotations can be reopened with the share disconnected; uncached images still need the share. Annotations are stored locally and can be downloaded as JSON. Legacy GridSquare ratings, comments and suitability are copied into the local unified index without altering source files or overwriting newer local annotations.

**Atlas annotations** switches between live suitability, rating, EPU categories and raw imagery. **Add target / area** places a point or a two-corner rectangle for manual collection planning, without changing microscope metadata. Overlay opacity is independent of image contrast.

**Export HTML screening report** embeds current annotations and JPEG images into an offline browser report. Choose one suitable square, all suitable squares, or explicitly all screened images. The optional high-resolution Atlas reads its MRC when available and embeds a JPEG up to 4096 pixels; MRC files are never embedded. Exported Atlas layers have their own opacity control. Export runs independently of the launcher's full-session bundle export. Detailed PDF export and the legacy report editor retain their separate legacy review data; unified edits are not mirrored back into that editor.

CLI equivalents:

```bash
python src/review_app.py /path/to/session --atlas /path/to/Atlas --auto-port --open
python src/review_app.py /path/to/session --atlas /path/to/Atlas --ignore-data --auto-port --open
```

## What to request

Ask the facility for the **EPU acquisition session**, not just the movie folder or a CryoSPARC project. Movie transfers often omit the overview images and positional metadata needed to map exposures back to physical holes.

Suggested message:

> Could you provide the original EPU acquisition session folder for this collection, preserving its folder structure? I need `EpuSession.dm`, the complete `Metadata` directory, and all `Images-Disc*` directories with their `GridSquare_*` folders. Please include the GridSquare JPEG/PNG images and XML files, the `FoilHoles` JPEG/PNG images and XML files, and the `Data` JPEG/PNG previews and matching XML files for every exposure. Please also include the corresponding Atlas directory with `Atlas.dm`, the Atlas JPEG/PNG and XML, and, if available, the Atlas and GridSquare MRCs. I do not need Data MRCs, EER files, or movie fractions for this visual review. If previews were stored separately, please include those and explain their correspondence to the session.

Expected structure (exact names and storage locations vary):

```text
session/
  EpuSession.dm
  Metadata/
    GridSquare_*.dm
    ...additional EPU metadata...
  Images-Disc1/
    GridSquare_<id>/
      GridSquare_<timestamp>.jpg
      GridSquare_<timestamp>.xml
      GridSquare_<timestamp>.mrc       optional
      FoilHoles/
        FoilHole_<id>_<timestamp>.jpg
        FoilHole_<id>_<timestamp>.xml
      Data/
        FoilHole_<id>_Data_...jpg      multiple exposures per hole
        FoilHole_<id>_Data_...xml
  Images-Disc2/                        if present; include every disc

Atlas/                                may be stored separately
  Atlas.dm
  Atlas_*.jpg
  Atlas_*.xml
  Atlas_*.mrc                          optional
```

For **FoilHole-only review**, omit the entire `Data` directory. Keep the session metadata, GridSquare images/XML, FoilHole images/XML, and Atlas. This greatly reduces file count and avoids transferring Data previews at all.

On a slow network share, preserve original filenames and directories rather than flattening the files. A local preview copy can be much more responsive than repeatedly reading many small files over SMB. Missing previews cannot be reconstructed from metadata alone; request JPEG/PNG previews explicitly if the facility normally transfers only movies.

## Synthetic test collection

The fixture is clearly labelled **SIMULATED**, and is not experimental data. It contains 10,000 Data JPEG/XML pairs, 1,668 physical FoilHoles and 17 GridSquares. Complete holes have 4–8 exposures; the final hole has one exposure to exercise an incomplete acquisition. GridSquare and Atlas MRCs are included; Data MRCs are absent.

Generate into a **new** directory:

```bash
python scripts/simulate_acquisition.py /path/to/new/SIMULATED --images 10000
python scripts/validate_simulated_acquisition.py /path/to/new/SIMULATED
```

Generation refuses to overwrite an existing directory. The validator checks all exposure/metadata filenames and physical-hole associations, and decodes representative images from every GridSquare; it does not read every image over the network.

The fixture exercises scale, image grouping, known overlay coordinates, and slow-share access. It does **not** validate every microscope's metadata conventions or represent realistic specimen quality, CTF, or motion statistics. A real acquisition remains necessary for that validation.
