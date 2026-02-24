#!/usr/bin/env python3
"""Visualize FoilHole stage positions overlaid on a GridSquare image.

Usage:
    PYTHONPATH=src python scripts/plot_foilhole_positions.py \
        Example_data/Images-Disc1/GridSquare_16736150 \
        --output debug_overlay.png

    # Process all GridSquares in a disc directory:
    PYTHONPATH=src python scripts/plot_foilhole_positions.py \
        Example_data/Images-Disc1 \
        --output debug_overlay.png
"""

from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
import textwrap
from typing import Callable, Iterable
import xml.etree.ElementTree as ET

try:
    import matplotlib  # type: ignore
    matplotlib.use("Agg")  # type: ignore[attr-defined]
    import matplotlib.pyplot as plt  # type: ignore
    from matplotlib.patches import Circle  # type: ignore
except Exception:  # matplotlib is optional at import time
    matplotlib = None
    plt = None
    Circle = None
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from build_collage import (
    find_grid_image,
    gather_foil_and_data,
    parse_grid_info,
    parse_foil_position,
    _collect_grids,
    _load_image,
)

_EPU_NS = {
    "p": "http://schemas.datacontract.org/2004/07/Applications.Epu.Persistence",
    "system": "http://schemas.datacontract.org/2004/07/System",
    "so": "http://schemas.datacontract.org/2004/07/Fei.SharedObjects",
    "g": "http://schemas.datacontract.org/2004/07/System.Collections.Generic",
    "s": "http://schemas.datacontract.org/2004/07/Fei.Applications.Common.Services",
    "a": "http://schemas.datacontract.org/2004/07/System.Drawing",
    "tp": "http://schemas.datacontract.org/2004/07/Fei.Applications.Common.Types",
}


# ---------------------------------------------------------------------------
# XML parsing helpers for cryoFLARE Metadata .dm files
# ---------------------------------------------------------------------------

def _find_metadata_root(grid_dir: Path) -> Path | None:
    for p in [grid_dir] + list(grid_dir.parents):
        md = p / "Metadata"
        if md.is_dir():
            return md
    return None


_PIXEL_CENTER_CACHE: dict[Path, dict[str, tuple[float, float]]] = {}
_SESSION_INFO_CACHE: dict[Path, dict[str, float]] = {}

_OVERLAY_DEBUG = bool(os.environ.get("OVERLAY_DEBUG"))
_FORCED_TRANSFORM: str | None = os.environ.get("OVERLAY_FORCE_TRANSFORM")


def _find_session_root(grid_dir: Path) -> Path | None:
    """Locate the directory that contains EpuSession.dm for this grid."""
    for parent in [grid_dir] + list(grid_dir.parents):
        candidate = parent / "EpuSession.dm"
        if candidate.is_file():
            return parent
    return None


def _load_session_detector_info(grid_dir: Path) -> dict[str, float]:
    """Read detector readout size/binning from EpuSession.dm."""
    session_root = _find_session_root(grid_dir)
    if not session_root:
        return {}
    session_path = session_root / "EpuSession.dm"
    if not session_path.is_file():
        return {}
    if session_path in _SESSION_INFO_CACHE:
        return _SESSION_INFO_CACHE[session_path]
    info: dict[str, float] = {}
    try:
        root = ET.parse(session_path).getroot()
    except Exception:
        _SESSION_INFO_CACHE[session_path] = info
        return info
    # Prefer the first MicroscopeSettings block that defines readout area/binning.
    for ms in root.findall(".//p:MicroscopeSettings", _EPU_NS):
        if "detector_width" not in info or "detector_height" not in info:
            readout = ms.find(".//so:ReadoutArea", _EPU_NS)
            if readout is not None:
                w_node = readout.find(".//a:width", _EPU_NS)
                h_node = readout.find(".//a:height", _EPU_NS)
                try:
                    if w_node is not None and w_node.text:
                        info["detector_width"] = float(w_node.text)
                    if h_node is not None and h_node.text:
                        info["detector_height"] = float(h_node.text)
                except Exception:
                    pass
        if "binning_x" not in info or "binning_y" not in info:
            bin_node = ms.find(".//so:Binning", _EPU_NS)
            if bin_node is not None:
                bx_node = bin_node.find(".//a:x", _EPU_NS)
                by_node = bin_node.find(".//a:y", _EPU_NS)
                try:
                    if bx_node is not None and bx_node.text:
                        info["binning_x"] = float(bx_node.text)
                    if by_node is not None and by_node.text:
                        info["binning_y"] = float(by_node.text)
                except Exception:
                    pass
        if all(k in info for k in ("detector_width", "detector_height")):
            break
    _SESSION_INFO_CACHE[session_path] = info
    return info


def _parse_dm_targets(dm_path: Path) -> dict[str, tuple[float, float]]:
    info: dict[str, tuple[float, float]] = {}
    try:
        tree = ET.parse(dm_path)
        root = tree.getroot()
    except Exception:
        return info
    def iter_tags(tagname: str):
        for e in root.iter():
            if e.tag.lower().endswith(tagname.lower()):
                yield e
    for arr in iter_tags("m_serializationArray"):
        for node in list(arr):
            hole_id = None
            x = None
            y = None
            for e in node.iter():
                tag = e.tag.lower()
                if tag.endswith("key") and e.text:
                    hole_id = e.text
                elif tag.endswith("x") and e.text and x is None:
                    try:
                        x = float(e.text)
                    except Exception:
                        pass
                elif tag.endswith("y") and e.text and y is None:
                    try:
                        y = float(e.text)
                    except Exception:
                        pass
            if hole_id and x is not None and y is not None:
                info[hole_id] = (x, y)
        if info:
            return info
    x = None
    y = None
    for e in root.iter():
        tag = e.tag.lower()
        if tag.endswith("x") and e.text and x is None:
            try:
                x = float(e.text)
            except Exception:
                pass
        elif tag.endswith("y") and e.text and y is None:
            try:
                y = float(e.text)
            except Exception:
                pass
    if x is not None and y is not None:
        hole_id = dm_path.stem
        if hole_id.startswith("TargetLocation_"):
            hole_id = hole_id[len("TargetLocation_"):]
        info[hole_id] = (x, y)
    return info


def _load_hole_positions(grid_dir: Path) -> dict[str, tuple[float, float]]:
    md = _find_metadata_root(grid_dir)
    if not md:
        return {}
    positions: dict[str, tuple[float, float]] = {}
    grid_name = grid_dir.name
    # load per-grid square DM first
    primary = md / f"{grid_name}.dm"
    if primary.is_file():
        positions.update(_parse_dm_targets(primary))
    # load per-hole target files next
    target_dir = md / grid_name
    if target_dir.is_dir():
        for dm in sorted(target_dir.glob("TargetLocation_*.dm")):
            positions.update(_parse_dm_targets(dm))
    return positions


def _load_dm_pixel_centers(grid_dir: Path) -> dict[str, tuple[float, float]]:
    """Extract foil PixelCenter coordinates from the grid's metadata DM."""
    md = _find_metadata_root(grid_dir)
    if not md:
        return {}
    dm_path = md / f"{grid_dir.name}.dm"
    if not dm_path.is_file():
        return {}
    cached = _PIXEL_CENTER_CACHE.get(dm_path)
    if cached is not None:
        return cached
    centers: dict[str, tuple[float, float]] = {}
    try:
        root = ET.parse(dm_path).getroot()
    except Exception:
        _PIXEL_CENTER_CACHE[dm_path] = centers
        return centers
    for kv in root.findall(".//g:KeyValuePairOfintTargetLocationXmlBpEWF4JT", _EPU_NS):
        value = kv.find("g:value", _EPU_NS)
        if value is None:
            continue
        hole_id = None
        id_node = value.find("./tp:Id", _EPU_NS)
        if id_node is None:
            id_node = value.find(".//tp:Id", _EPU_NS)
        if id_node is not None and id_node.text:
            hole_id = id_node.text.strip()
        if not hole_id:
            continue
        pixel_center = value.find("p:PixelCenter", _EPU_NS)
        if pixel_center is None:
            continue
        x_node = pixel_center.find("a:x", _EPU_NS)
        y_node = pixel_center.find("a:y", _EPU_NS)
        if x_node is None or y_node is None or not x_node.text or not y_node.text:
            continue
        try:
            centers[hole_id] = (float(x_node.text), float(y_node.text))
        except Exception:
            continue
    _PIXEL_CENTER_CACHE[dm_path] = centers
    return centers


def _load_dm_square_metadata(grid_dir: Path) -> dict[str, float]:
    """Extract stage/pixel data for the square from its DM file."""
    md = _find_metadata_root(grid_dir)
    if not md:
        return {}
    dm_path = md / f"{grid_dir.name}.dm"
    if not dm_path.is_file():
        return {}
    try:
        import xml.etree.ElementTree as ET
        root = ET.parse(dm_path).getroot()
    except Exception:
        return {}
    info: dict[str, float] = {}
    node = root.find("so:microscopeData/so:stage/so:Position", _EPU_NS)
    if node is not None:
        for child in node:
            tag = child.tag.lower()
            if tag.endswith("x") and child.text:
                try:
                    info["stage_x"] = float(child.text)
                except Exception:
                    pass
            if tag.endswith("y") and child.text:
                try:
                    info["stage_y"] = float(child.text)
                except Exception:
                    pass
    pixel_node = root.find("so:SpatialScale/so:pixelSize/so:x/so:numericValue", _EPU_NS)
    if pixel_node is not None and pixel_node.text:
        try:
            info["pixel_size"] = float(pixel_node.text)
        except Exception:
            pass
    return info


def _epu_stage_payload(xml_path: Path) -> dict[str, float]:
    """Replicate the stage/pixel extraction logic from epubrowser's epu.plot_foilhole.py."""
    info: dict[str, float] = {}
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
    except Exception:
        return info

    def _get(path: str) -> float | None:
        node = root.find(path, _EPU_NS)
        if node is not None and node.text:
            try:
                return float(node.text)
            except Exception:
                return None
        return None

    stage_x = _get("so:microscopeData/so:stage/so:Position/so:X")
    stage_y = _get("so:microscopeData/so:stage/so:Position/so:Y")
    pixel_size = _get("so:SpatialScale/so:pixelSize/so:x/so:numericValue")
    if stage_x is not None and stage_y is not None:
        info["stage_x"] = stage_x
        info["stage_y"] = stage_y
    if pixel_size is not None:
        info["pixel_size"] = pixel_size
    return info


def _project_marker_epu(
    square_info: dict[str, float],
    foil_info: dict[str, float],
    base_w: float,
    base_h: float,
    scale_x: float,
    scale_y: float,
) -> tuple[float, float] | None:
    """Compute foil coordinates using the epubrowser stage math with proper scaling."""
    try:
        sq_px = square_info["pixel_size"]
        dx = square_info["stage_x"] - foil_info["stage_x"]
        dy = square_info["stage_y"] - foil_info["stage_y"]
    except KeyError:
        return None
    try:
        px_raw = (base_w / 2.0) + (dx / sq_px)
        py_raw = (base_h / 2.0) - (dy / sq_px)
    except Exception:
        return None
    px = px_raw * scale_x
    py = py_raw * scale_y
    return px, py


def _fit_to_frame(px: float, py: float, width: int, height: int) -> tuple[float, float, bool]:
    """Clamp coordinates into the visible frame but remember if they exceeded bounds."""
    in_bounds = 0 <= px < width and 0 <= py < height
    px = min(max(px, 0.0), float(width - 1))
    py = min(max(py, 0.0), float(height - 1))
    return px, py, in_bounds


_TRANSFORM_FUNCS: dict[str, Callable[[float, float], tuple[float, float]]] = {
    "identity": lambda u, v: (u, v),
    "rot90": lambda u, v: (v, 1.0 - u),
    "rot180": lambda u, v: (1.0 - u, 1.0 - v),
    "rot270": lambda u, v: (1.0 - v, u),
    "mirror_x": lambda u, v: (1.0 - u, v),
    "mirror_y": lambda u, v: (u, 1.0 - v),
    "mirror_diag": lambda u, v: (v, u),
    "mirror_diag_inv": lambda u, v: (1.0 - v, 1.0 - u),
}


def set_forced_transform(name: str | None) -> None:
    """Force overlays to use a specific transform name or reset to auto."""
    global _FORCED_TRANSFORM
    if not name or name == "auto":
        _FORCED_TRANSFORM = None
        return
    if name not in _TRANSFORM_FUNCS:
        raise ValueError(f"unknown transform '{name}'")
    _FORCED_TRANSFORM = name


def _select_best_pixel_center_transform(
    norm_centers: dict[str, tuple[float, float]],
    fallback_coords: dict[str, tuple[float, float, bool]],
    grid_w: int,
    grid_h: int,
) -> tuple[str | None, dict[str, tuple[float, float, bool]], list[dict]]:
    """Try all rotations/flips and pick the one best matching fallback coords/in-bounds."""
    if not norm_centers:
        return None, {}, []

    best_choice: dict | None = None
    candidates_info: list[dict] = []
    for name, func in _TRANSFORM_FUNCS.items():
        transformed: dict[str, tuple[float, float, bool]] = {}
        matches = 0
        error = 0.0
        in_bounds = 0
        for foil_id, (u, v) in norm_centers.items():
            tu, tv = func(u, v)
            tu = min(max(tu, 0.0), 1.0)
            tv = min(max(tv, 0.0), 1.0)
            px = tu * grid_w
            py = tv * grid_h
            px, py, ib = _fit_to_frame(px, py, grid_w, grid_h)
            transformed[foil_id] = (px, py, ib)
            if ib:
                in_bounds += 1
            fb = fallback_coords.get(foil_id)
            if fb:
                fx, fy, _ = fb
                error += (px - fx) ** 2 + (py - fy) ** 2
                matches += 1
        score = error / matches if matches else float("inf")
        candidate = {
            "name": name,
            "transformed": transformed,
            "matches": matches,
            "score": score,
            "in_bounds": in_bounds,
        }
        candidates_info.append(candidate)
        if best_choice is None:
            best_choice = candidate
            continue
        if candidate["matches"] and not best_choice["matches"]:
            best_choice = candidate
            continue
        if candidate["matches"] and best_choice["matches"]:
            if candidate["score"] + 1e-6 < best_choice["score"]:
                best_choice = candidate
                continue
            if abs(candidate["score"] - best_choice["score"]) <= 1e-6 and candidate["in_bounds"] > best_choice["in_bounds"]:
                best_choice = candidate
                continue
        if candidate["matches"] == 0 and best_choice["matches"] == 0:
            if candidate["in_bounds"] > best_choice["in_bounds"]:
                best_choice = candidate
            elif candidate["in_bounds"] == best_choice["in_bounds"] and len(candidate["transformed"]) > len(best_choice["transformed"]):
                best_choice = candidate
    if best_choice is None:
        return None, {}, candidates_info
    if _FORCED_TRANSFORM:
        forced = next((c for c in candidates_info if c["name"] == _FORCED_TRANSFORM), None)
        if forced:
            best_choice = forced
    if _OVERLAY_DEBUG:
        print(f"[overlay] candidate scores ({len(norm_centers)} centers, {len(fallback_coords)} fallbacks):")
        for cand in candidates_info:
            print(
                f"  - {cand['name']}: matches={cand['matches']} "
                f"score={cand['score']:.3f} in_bounds={cand['in_bounds']} "
                f"count={len(cand['transformed'])}"
            )
        print(f"[overlay] using transform: {best_choice['name']}")
    return best_choice["name"], best_choice["transformed"], candidates_info


def _compute_stage_marker(
    foil_id: str,
    position_path: Path,
    latest_path: Path,
    hole_positions: dict[str, tuple[float, float]],
    square_stage_x: float | None,
    square_stage_y: float | None,
    square_pixel_size: float | None,
    base_w: float,
    base_h: float,
    scale_x: float,
    scale_y: float,
    grid_size: tuple[int, int],
    epubrowser_square: dict[str, float] | None,
    grid_meta: dict,
    inv_matrix: tuple[float, float, float, float] | None,
) -> tuple[float, float, bool, bool] | None:
    """Project foil coordinates using stage metadata."""
    width, height = grid_size

    if (
        foil_id in hole_positions
        and square_stage_x is not None
        and square_stage_y is not None
        and square_pixel_size
    ):
        hx, hy = hole_positions[foil_id]
        dx = hx - square_stage_x
        dy = hy - square_stage_y
        px_raw = (base_w / 2.0) + dx / square_pixel_size
        py_raw = (base_h / 2.0) - dy / square_pixel_size
        px = px_raw * scale_x
        py = py_raw * scale_y
        px, py, in_bounds = _fit_to_frame(px, py, width, height)
        return px, py, in_bounds, True

    xml_path = position_path.with_suffix(".xml")
    if not xml_path.is_file():
        return None

    if epubrowser_square:
        epubrowser_foil = _epu_stage_payload(xml_path)
        coords = None
        if epubrowser_foil.get("stage_x") is not None and epubrowser_foil.get("stage_y") is not None:
            coords = _project_marker_epu(epubrowser_square, epubrowser_foil, base_w, base_h, scale_x, scale_y)
        if coords:
            px, py = coords
            px, py, in_bounds = _fit_to_frame(px, py, width, height)
            return px, py, in_bounds, False

    fp = parse_foil_position(xml_path)
    fmeta = parse_grid_info(xml_path)
    if "stage_x" not in fp or "stage_y" not in fp:
        return None

    center_stage_x = fp["stage_x"]
    center_stage_y = fp["stage_y"]

    if "center_x" in fp and "center_y" in fp:
        try:
            with Image.open(position_path) as foil_img:
                fw, fh = foil_img.size
            dx_px = float(fp["center_x"]) - (fw / 2.0)
            dy_px = float(fp["center_y"]) - (fh / 2.0)
            if fmeta.get("ref_matrix"):
                fm11, fm12, fm21, fm22 = fmeta["ref_matrix"]
                dx_stage = (fm11 * dx_px) + (fm12 * dy_px)
                dy_stage = (fm21 * dx_px) + (fm22 * dy_px)
                center_stage_x = fp["stage_x"] + dx_stage
                center_stage_y = fp["stage_y"] + dy_stage
            elif fmeta.get("pixel_size"):
                dy_img_up = -dy_px
                rx_px, ry_px = dx_px, dy_img_up
                if "rotation" in fp:
                    theta = float(fp["rotation"])
                    c, s = math.cos(theta), math.sin(theta)
                    rx_px = c * dx_px - s * dy_img_up
                    ry_px = s * dx_px + c * dy_img_up
                center_stage_x = fp["stage_x"] + rx_px * fmeta["pixel_size"]
                center_stage_y = fp["stage_y"] + ry_px * fmeta["pixel_size"]
        except Exception:
            pass

    try:
        ref_stage_x = square_stage_x if square_stage_x is not None else grid_meta["stage_x"]
        ref_stage_y = square_stage_y if square_stage_y is not None else grid_meta["stage_y"]
        ref_pixel = square_pixel_size if square_pixel_size else grid_meta["pixel_size"]
        dx_stage = center_stage_x - ref_stage_x
        dy_stage = center_stage_y - ref_stage_y
        half_w = base_w / 2.0
        half_h = base_h / 2.0
        if inv_matrix:
            inv_m11, inv_m12, inv_m21, inv_m22 = inv_matrix
            dx_px = (inv_m11 * dx_stage) + (inv_m12 * dy_stage)
            dy_px = (inv_m21 * dx_stage) + (inv_m22 * dy_stage)
            px_raw = half_w + dx_px
            py_raw = half_h + dy_px
        else:
            px_raw = half_w + (dx_stage / ref_pixel)
            py_raw = half_h - (dy_stage / ref_pixel)
        px = px_raw * scale_x
        py = py_raw * scale_y
    except Exception:
        return None

    px, py, in_bounds = _fit_to_frame(px, py, width, height)
    return px, py, in_bounds, False


def _markers_from_coords(
    coords: dict[str, tuple[float, float, bool]],
    path_map: dict[str, Path],
) -> list[tuple[float, float, bool, int, Path]]:
    markers: list[tuple[float, float, bool, int, Path]] = []
    label_idx = 1
    for foil_id in sorted(path_map.keys()):
        if foil_id not in coords:
            continue
        px, py, in_bounds = coords[foil_id]
        markers.append((px, py, in_bounds, label_idx, path_map[foil_id]))
        label_idx += 1
    return markers


# ---------------------------------------------------------------------------
# Marker computation (unchanged logic, cleaned up)
# ---------------------------------------------------------------------------

def compute_markers(
    grid_dir: Path,
    debug_dump: Path | None = None,
) -> tuple[Image.Image, list[tuple[float, float, bool, int, Path]]]:
    grid_image_path = find_grid_image(grid_dir)
    grid_image = _load_image(grid_image_path)
    if grid_image is None:
        raise RuntimeError(f"failed to load grid image: {grid_image_path}")

    grid_xml = grid_dir / grid_image_path.with_suffix(".xml").name
    if not grid_xml.is_file():
        raise RuntimeError(f"GridSquare XML not found: {grid_xml}")
    grid_meta = parse_grid_info(grid_xml)
    epubrowser_square = _epu_stage_payload(grid_xml)

    dm_square_meta = _load_dm_square_metadata(grid_dir)
    session_info = _load_session_detector_info(grid_dir)
    session_w = session_info.get("detector_width")
    session_h = session_info.get("detector_height")
    bin_x = session_info.get("binning_x", 1.0) or 1.0
    bin_y = session_info.get("binning_y", 1.0) or 1.0
    if session_w:
        session_w *= bin_x
    if session_h:
        session_h *= bin_y
    readout_w = float(grid_meta.get("readout_width")) if grid_meta.get("readout_width") else None
    readout_h = float(grid_meta.get("readout_height")) if grid_meta.get("readout_height") else None
    base_w = float(session_w or readout_w or grid_image.width or 4096.0)
    base_h = float(session_h or readout_h or grid_image.height or 4096.0)
    scale_x = grid_image.width / base_w
    scale_y = grid_image.height / base_h
    square_stage_x = dm_square_meta.get("stage_x", grid_meta.get("stage_x"))
    square_stage_y = dm_square_meta.get("stage_y", grid_meta.get("stage_y"))
    square_pixel_size = dm_square_meta.get("pixel_size", grid_meta.get("pixel_size"))

    ref = grid_meta.get("ref_matrix")
    inv_matrix: tuple[float, float, float, float] | None = None
    if ref:
        m11, m12, m21, m22 = ref
        det = (m11 * m22) - (m12 * m21)
        if det != 0:
            inv_m11 = m22 / det
            inv_m12 = -m12 / det
            inv_m21 = -m21 / det
            inv_m22 = m11 / det
            inv_matrix = (inv_m11, inv_m12, inv_m21, inv_m22)
        else:
            inv_m11 = inv_m12 = inv_m21 = inv_m22 = None
            inv_matrix = None
    else:
        inv_m11 = inv_m12 = inv_m21 = inv_m22 = None
        inv_matrix = None

    markers: list[tuple[float, float, bool, int, Path]] = []
    foils, _ = gather_foil_and_data(grid_dir)
    marker_idx = 1
    hole_positions = _load_hole_positions(grid_dir)
    pixel_centers = _load_dm_pixel_centers(grid_dir)
    used_metadata_positions = False

    selected_foils: list[tuple[str, Path, Path]] = []
    latest_paths: dict[str, Path] = {}
    for foil_id in sorted(foils.keys()):
        if not foils[foil_id]:
            continue
        latest_path = foils[foil_id][-1]
        position_path = latest_path if latest_path.with_suffix(".xml").is_file() else None
        if position_path is None:
            for candidate in reversed(foils[foil_id]):
                if candidate.with_suffix(".xml").is_file():
                    position_path = candidate
                    break
        if position_path is None:
            continue
        selected_foils.append((foil_id, position_path, latest_path))
        latest_paths[foil_id] = latest_path

    fallback_coords: dict[str, tuple[float, float, bool, bool]] = {}
    for foil_id, position_path, latest_path in selected_foils:
        fallback = _compute_stage_marker(
            foil_id,
            position_path,
            latest_path,
            hole_positions,
            square_stage_x,
            square_stage_y,
            square_pixel_size,
            base_w,
            base_h,
            scale_x,
            scale_y,
            (grid_image.width, grid_image.height),
            epubrowser_square,
            grid_meta,
            inv_matrix,
        )
        if fallback:
            fallback_coords[foil_id] = fallback

    norm_centers: dict[str, tuple[float, float]] = {}
    if base_w > 0 and base_h > 0:
        for fid, (px_det, py_det) in pixel_centers.items():
            u = px_det / base_w
            v = py_det / base_h
            if math.isfinite(u) and math.isfinite(v):
                norm_centers[fid] = (u, v)

    fallback_for_eval = {fid: (px, py, inb) for fid, (px, py, inb, _meta) in fallback_coords.items()}
    _transform_name, transformed_centers, transform_candidates = _select_best_pixel_center_transform(
        norm_centers, fallback_for_eval, grid_image.width, grid_image.height
    )
    if transformed_centers:
        used_metadata_positions = True

    if debug_dump:
        debug_dump_path = Path(debug_dump)
        debug_dump_path.mkdir(parents=True, exist_ok=True)
        for candidate in transform_candidates:
            if not candidate["transformed"]:
                continue
            markers_debug = _markers_from_coords(candidate["transformed"], latest_paths)
            if not markers_debug:
                continue
            suffix = candidate["name"]
            out_file = debug_dump_path / f"{grid_dir.name}_{suffix}.png"
            title = f"{grid_dir.name} ({suffix})"
            plot_overlay(grid_image.copy(), markers_debug, title=title, output=out_file)

    for foil_id, position_path, latest_path in selected_foils:
        if foil_id in transformed_centers:
            px, py, in_bounds = transformed_centers[foil_id]
            markers.append((px, py, in_bounds, marker_idx, latest_path))
            marker_idx += 1
            continue
        fallback = fallback_coords.get(foil_id)
        if fallback:
            px, py, in_bounds, meta_based = fallback
            if meta_based:
                used_metadata_positions = True
            markers.append((px, py, in_bounds, marker_idx, latest_path))
            marker_idx += 1
            continue

    if markers and not any(m[2] for m in markers) and not used_metadata_positions:
        xs = [m[0] for m in markers]
        ys = [m[1] for m in markers]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        if max_x != min_x and max_y != min_y:
            margin = max(8, int(min(grid_image.size) * 0.05))
            scale_x = (grid_image.width - 2 * margin) / (max_x - min_x)
            scale_y = (grid_image.height - 2 * margin) / (max_y - min_y)
            scale = min(scale_x, scale_y)
            mapped = []
            for px, py, _in, label, path in markers:
                nx = margin + (px - min_x) * scale
                ny = margin + (py - min_y) * scale
                mapped.append((nx, ny, True, label, path))
            markers = mapped

    if not markers:
        raise RuntimeError("No foil metadata with stage positions found.")

    return grid_image.convert("RGB"), markers


def _foil_label_from_path(path: Path, label: int) -> str:
    stem = path.stem
    parts = stem.split("_")
    foil_id = parts[1] if len(parts) > 1 else stem
    timestamp = "_".join(parts[2:]) if len(parts) > 2 else ""
    if timestamp:
        return f"{label} {foil_id} {timestamp}"
    return f"{label} {foil_id}"


def _build_thumbnail_panel(markers: list[tuple[float, float, bool, int, Path]], thumb_size: int = 180) -> Image.Image:
    if not markers:
        return Image.new("RGB", (thumb_size, thumb_size), color=(30, 30, 30))
    n = len(markers)
    cols = 2 if n <= 20 else 3
    rows = (n + cols - 1) // cols
    tile_w = thumb_size
    font = ImageFont.load_default()
    label_texts: list[str] = []
    max_text_h = 0
    for _, _, _, label, path in markers:
        label_text = _foil_label_from_path(path, label)
        label_text = textwrap.fill(label_text, width=18)
        text_h = 12 + (label_text.count("\n") * 12)
        if text_h > max_text_h:
            max_text_h = text_h
        label_texts.append(label_text)
    label_pad = 8
    tile_h = thumb_size + max_text_h + label_pad
    panel = Image.new("RGB", (cols * tile_w, rows * tile_h), color=(20, 20, 20))
    for idx, (_, _, _, label, path) in enumerate(markers):
        col = idx % cols
        row = idx // cols
        x0 = col * tile_w
        y0 = row * tile_h
        try:
            with Image.open(path) as im:
                img = im.convert("RGB")
        except Exception:
            img = Image.new("RGB", (tile_w, thumb_size), color=(50, 50, 50))
        scale = min(tile_w / img.width, thumb_size / img.height)
        new_w = max(1, int(img.width * scale))
        new_h = max(1, int(img.height * scale))
        resized = img.resize((new_w, new_h))
        tile = Image.new("RGB", (tile_w, tile_h), color=(30, 30, 30))
        tile.paste(resized, ((tile_w - new_w) // 2, (thumb_size - new_h) // 2))
        draw = ImageDraw.Draw(tile)
        label_text = label_texts[idx]
        draw.rectangle([(0, tile_h - max_text_h - label_pad), (tile_w, tile_h)], fill=(0, 0, 0))
        draw.text((6, tile_h - max_text_h - label_pad + 2), label_text, fill=(255, 255, 255), font=font)
        panel.paste(tile, (x0, y0))
    return panel


# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------

def plot_overlay(
    grid_img: Image.Image,
    markers: list[tuple[float, float, bool, int, Path]],
    title: str = "",
    output: Path | None = None,
    dpi: int = 150,
    include_panel: bool = False,
) -> None:
    w, h = grid_img.size
    radius = max(8, int(min(w, h) * 0.012))
    font_size = max(7, radius * 0.9)

    n_in = sum(1 for _, _, ib, _, _ in markers if ib)
    n_out = len(markers) - n_in

    panel_arr = None
    panel_w = 0
    if include_panel and markers:
        panel_img = _build_thumbnail_panel(markers)
        panel_h, panel_w = panel_img.size[1], panel_img.size[0]
        if panel_h != h:
            new_panel_w = max(1, int(panel_w * (h / panel_h)))
            panel_img = panel_img.resize((new_panel_w, h))
            panel_w = new_panel_w
        panel_arr = np.array(panel_img)

    total_w = w + panel_w
    total_h = h
    fig = plt.figure(figsize=(total_w / dpi, total_h / dpi), dpi=dpi)
    if panel_arr is not None:
        left_w = w / total_w
        right_w = panel_w / total_w
        ax = fig.add_axes([0, 0, left_w, 1])
        ax_panel = fig.add_axes([left_w, 0, right_w, 1])
    else:
        ax = fig.add_axes([0, 0, 1, 1])
        ax_panel = None

    # render the underlying GridSquare image in true grayscale so marker colors stay distinct
    display_img = np.array(grid_img.convert("L"))
    ax.imshow(display_img, cmap="gray", origin="upper", aspect="equal", vmin=0, vmax=255)
    ax.set_xlim(0, w)
    ax.set_ylim(h, 0)
    ax.axis("off")

    if ax_panel is not None and panel_arr is not None:
        ax_panel.imshow(panel_arr, origin="upper", aspect="equal")
        ax_panel.axis("off")

    COLOR_IN = "#2ecc71"
    COLOR_OUT = "#e74c3c"

    for px, py, in_bounds, label, _path in markers:
        color = COLOR_IN if in_bounds else COLOR_OUT
        circle = Circle(
            (px, py),
            radius=radius,
            linewidth=0.9,
            edgecolor=color,
            facecolor="none",
        )
        ax.add_patch(circle)
        ax.plot([px - radius * 0.55, px + radius * 0.55], [py, py], color=color, lw=0.8)
        ax.plot([px, px], [py - radius * 0.55, py + radius * 0.55], color=color, lw=0.8)
        ax.text(
            px + radius + 2,
            py - radius,
            str(label),
            color=color,
            fontsize=font_size,
            fontweight="bold",
            va="top",
            ha="left",
        )

    heading = title or "FoilHole positions"
    ax.set_title(heading, fontsize=13, fontweight="bold", pad=6)
    if output:
        fig.savefig(output, dpi=dpi)
        print(f"Saved: {output}")
    else:
        plt.show()
    plt.close(fig)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Overlay FoilHole positions on GridSquare image(s)")
    parser.add_argument("grid_dir", type=Path, help="GridSquare_* directory or parent disc directory")
    parser.add_argument("--output", type=Path, default=None, help="Output PNG path (default: <grid_dir>/foil_overlay.png)")
    parser.add_argument("--dpi", type=int, default=150, help="Output DPI (default: 150)")
    parser.add_argument("--include-panel", action="store_true", help="Include FoilHole thumbnails beside the overlay (default: off)")
    parser.add_argument(
        "--transform",
        choices=["auto"] + sorted(_TRANSFORM_FUNCS.keys()),
        default="identity",
        help="Rotation/mirror transform to use (default: identity; pick 'auto' to let the tool choose).",
    )
    parser.add_argument(
        "--dump-transforms",
        type=Path,
        default=None,
        help="Directory to save overlay PNGs for every candidate rotation/mirror transform",
    )
    args = parser.parse_args()

    grid_dir = args.grid_dir.resolve()
    if args.transform:
        set_forced_transform(args.transform)

    if not grid_dir.is_dir():
        raise SystemExit(f"Directory not found: {grid_dir}")

    if grid_dir.name.startswith("GridSquare_"):
        grids = [grid_dir]
    else:
        collected = _collect_grids(grid_dir)
        grids = [g for _, g in collected]
        if not grids:
            raise SystemExit(f"No GridSquare directories found in {grid_dir}")

    dump_base = args.dump_transforms.resolve() if args.dump_transforms else None

    for gdir in grids:
        debug_dir = None
        if dump_base:
            debug_dir = dump_base if len(grids) == 1 else dump_base / gdir.name
        try:
            grid_img, markers = compute_markers(gdir, debug_dump=debug_dir)
        except Exception as exc:
            print(f"Skipping {gdir.name}: {exc}")
            continue

        if args.output and len(grids) == 1:
            out = args.output
        elif args.output:
            out = args.output.parent / f"{gdir.name}_{args.output.name}"
        else:
            out = gdir / "foil_overlay.png"

        plot_overlay(grid_img, markers, title=gdir.name, output=out, dpi=args.dpi, include_panel=args.include_panel)


if __name__ == "__main__":
    main()
