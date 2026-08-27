import argparse
import csv
import io
import errno
import hashlib
import json
import os
import re
import secrets
import shutil
import sys
import urllib.parse
import time
import tempfile
import webbrowser
import threading
import xml.etree.ElementTree as ET
from collections import deque
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse, Response, JSONResponse
from PIL import Image, ImageDraw, ImageFont
import uvicorn

from build_collage import (
    _collect_grids,
    find_grid_image,
    find_grid_mrc,
    gather_foil_and_data,
    _find_overlay_image,
    _latest_only,
    _mrc_to_image,
    write_combined_report,
    write_embedded_html_report,
    write_selected_report,
    _resolve_atlas_path,
    parse_metadata,
    parse_grid_info,
)
from portable_session import export_portable_session, load_portable_session, portable_session_source


def _find_mrc_for_jpg(path: Path) -> Path | None:
    cand = path.with_suffix(".mrc")
    if cand.is_file():
        return cand
    cand = path.with_suffix(".mrcs")
    if cand.is_file():
        return cand
    return None


def _find_atlas_mrc(atlas_path: Path) -> Path | None:
    """Locate the full-resolution atlas MRC across common EPU naming schemes."""
    direct = _find_mrc_for_jpg(atlas_path)
    if direct:
        return direct
    for name in ("Atlas.mrc", "atlas.mrc", "Atlas.mrcs", "atlas.mrcs"):
        candidate = atlas_path.parent / name
        if candidate.is_file():
            return candidate
    candidates = [
        path
        for pattern in ("Atlas_*.mrc", "atlas_*.mrc", "*.mrc", "*.mrcs")
        for path in atlas_path.parent.glob(pattern)
        if path.is_file()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: (path.stat().st_mtime, path.name))


def _format_meta(meta: dict) -> list[str]:
    lines = []
    for key in ("pixel_size", "exposure", "dose", "defocus"):
        if key in meta:
            txt = meta[key]
            if key == "dose":
                txt += " e-/Å²"
            lines.append(f"{key.replace('_', ' ')}: {txt}")
    return lines


def _format_category_score_text(value) -> str:
    if value is None:
        return "N/A"
    try:
        return str(int(value))
    except Exception:
        return str(value)


_OVERLAY_TOOLS: tuple | None = None
_OVERLAY_TRANSFORM: str | None = None
_OVERLAY_EVENTS: deque = deque(maxlen=200)
_ATLAS_MAPPING_CACHE: dict[Path, tuple[dict[str, tuple[float, float]], dict[str, int | None], float | None, float | None, str | None]] = {}
_EPU_CATEGORY_COLORS: dict[int, tuple[int, int, int]] = {
    -1: (148, 163, 184),
    0: (64, 224, 208),
    1: (249, 115, 22),
    2: (59, 130, 246),
    3: (250, 204, 21),
    4: (236, 72, 153),
    5: (192, 132, 252),
    6: (217, 70, 239),
}


def _local_tag(tag: str | None) -> str:
    if not tag:
        return ""
    if "}" in tag:
        return tag.rsplit("}", 1)[-1].lower()
    return tag.lower()


def _as_float(text: str | None) -> float | None:
    if text is None:
        return None
    try:
        return float(text)
    except Exception:
        return None


def _atlas_dm_candidates(atlas_path: Path) -> list[Path]:
    candidates = [
        atlas_path.with_suffix(".dm"),
        atlas_path.parent / "Atlas.dm",
        atlas_path.parent / f"{atlas_path.stem}.dm",
    ]
    seen: set[Path] = set()
    ordered: list[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception:
            continue
        if resolved not in seen:
            seen.add(resolved)
            ordered.append(resolved)
    return ordered


def _parse_atlas_dm_centers_and_categories(dm_path: Path) -> tuple[dict[str, tuple[float, float]], dict[str, int | None]]:
    centers: dict[str, tuple[float, float]] = {}
    categories: dict[str, int | None] = {}
    try:
        root = ET.parse(dm_path).getroot()
    except Exception:
        return centers, categories

    for parent in root.iter():
        if not _local_tag(parent.tag).startswith("keyvaluepairofintnodexml"):
            continue
        key_node = None
        value_node = None
        for child in list(parent):
            name = _local_tag(child.tag)
            if name == "key":
                key_node = child
            elif name == "value":
                value_node = child
        if key_node is None or value_node is None or not key_node.text:
            continue
        key = key_node.text.strip()
        if not key:
            continue
        category_value = None
        for node in list(value_node):
            if _local_tag(node.tag) == "category":
                category_float = _as_float(node.text)
                if category_float is not None:
                    category_value = int(round(category_float))
                break
        categories[key] = category_value

        pos_node = None
        for node in value_node.iter():
            if _local_tag(node.tag) == "positionontheatlas":
                pos_node = node
                break
        if pos_node is None:
            continue

        center_node = None
        for node in list(pos_node):
            if _local_tag(node.tag) == "center":
                center_node = node
                break
        if center_node is None:
            continue

        center_x = None
        center_y = None
        for node in list(center_node):
            name = _local_tag(node.tag)
            if name == "x":
                center_x = _as_float(node.text)
            elif name == "y":
                center_y = _as_float(node.text)
        if center_x is None or center_y is None:
            continue
        centers[key] = (center_x, center_y)
    return centers, categories


def _atlas_reference_dimensions(atlas_path: Path, centers: dict[str, tuple[float, float]]) -> tuple[float | None, float | None]:
    atlas_mrc = _find_atlas_mrc(atlas_path)
    if atlas_mrc and atlas_mrc.is_file():
        try:
            import mrcfile  # local import to avoid hard dependency at module import

            with mrcfile.open(atlas_mrc, permissive=True) as mrc:
                w = float(mrc.header.nx)
                h = float(mrc.header.ny)
                if w > 0 and h > 0:
                    return w, h
        except Exception:
            pass
    if centers:
        max_x = max(v[0] for v in centers.values())
        max_y = max(v[1] for v in centers.values())
        return max_x + 1.0, max_y + 1.0
    return None, None


def _load_atlas_mapping(atlas_path: Path) -> tuple[dict[str, tuple[float, float]], dict[str, int | None], float | None, float | None, str | None]:
    atlas_key = atlas_path.resolve()
    cached = _ATLAS_MAPPING_CACHE.get(atlas_key)
    if cached is not None:
        return cached

    dm_path = next((p for p in _atlas_dm_candidates(atlas_key) if p.is_file()), None)
    if dm_path is None:
        result = ({}, {}, None, None, "Atlas marker unavailable: Atlas.dm metadata not found.")
        _ATLAS_MAPPING_CACHE[atlas_key] = result
        return result

    centers, categories = _parse_atlas_dm_centers_and_categories(dm_path)
    if not centers:
        result = ({}, categories, None, None, f"Atlas marker unavailable: could not parse GridSquare centers from {dm_path.name}.")
        _ATLAS_MAPPING_CACHE[atlas_key] = result
        return result

    ref_w, ref_h = _atlas_reference_dimensions(atlas_key, centers)
    result = (centers, categories, ref_w, ref_h, None)
    _ATLAS_MAPPING_CACHE[atlas_key] = result
    return result


def _atlas_lookup_keys(grid_dir: Path, grid_id: int | float) -> list[str]:
    keys: list[str] = []
    keys.append(str(grid_id))
    keys.append(grid_dir.name)
    digits = "".join(ch for ch in grid_dir.name if ch.isdigit())
    if digits:
        keys.append(digits)
    seen: set[str] = set()
    ordered: list[str] = []
    for key in keys:
        if key and key not in seen:
            seen.add(key)
            ordered.append(key)
    return ordered


def _render_atlas_overlay(
    atlas_path: Path,
    centers: dict[str, tuple[float, float]],
    active_key: str,
    ref_w: float | None,
    ref_h: float | None,
    label: str,
) -> bytes | None:
    if active_key not in centers:
        return None
    # Keep the marked-location view on the same JPEG/PNG source as the default
    # overview. The MRC is an explicit opt-in so the two views cannot silently
    # show different atlas acquisitions.
    atlas_rgb = _open_atlas_rgb(atlas_path, prefer_mrc=False)
    if atlas_rgb is None:
        return None

    width, height = atlas_rgb.size
    scale_x = width / ref_w if ref_w and ref_w > 0 else 1.0
    scale_y = height / ref_h if ref_h and ref_h > 0 else 1.0
    center_x_raw, center_y_raw = centers[active_key]
    center_x = center_x_raw * scale_x
    center_y = center_y_raw * scale_y
    if not (0 <= center_x < width and 0 <= center_y < height):
        return None

    draw = ImageDraw.Draw(atlas_rgb, "RGBA")
    radius = max(12, int(min(width, height) * 0.035))
    ring_width = max(3, radius // 5)
    draw.ellipse(
        (center_x - radius, center_y - radius, center_x + radius, center_y + radius),
        fill=(220, 40, 40, 128),
        outline=(220, 40, 40, 128),
        width=ring_width,
    )
    cross = int(radius * 1.6)
    cross_width = max(2, radius // 6)
    draw.line((center_x - cross, center_y, center_x + cross, center_y), fill=(220, 40, 40, 128), width=cross_width)
    draw.line((center_x, center_y - cross, center_x, center_y + cross), fill=(220, 40, 40, 128), width=cross_width)

    try:
        font = ImageFont.load_default()
    except Exception:
        font = None
    if font is not None:
        text = label
        if hasattr(draw, "textbbox"):
            box = draw.textbbox((0, 0), text, font=font)
            text_w = box[2] - box[0]
            text_h = box[3] - box[1]
        else:
            text_w, text_h = font.getsize(text)
        text_x = min(max(8, center_x + radius + 10), max(8, width - text_w - 8))
        text_y = min(max(8, center_y - radius - text_h - 8), max(8, height - text_h - 8))
        draw.rectangle((text_x - 4, text_y - 3, text_x + text_w + 4, text_y + text_h + 3), fill=(255, 255, 255, 128))
        draw.text((text_x, text_y), text, fill=(150, 25, 25, 128), font=font)

    buf = io.BytesIO()
    atlas_rgb.save(buf, format="PNG")
    return buf.getvalue()


def _category_marker_color(category: int | None) -> tuple[int, int, int]:
    if category is None:
        return (148, 163, 184)
    return _EPU_CATEGORY_COLORS.get(category, (99, 102, 241))


def _open_atlas_rgb(atlas_path: Path, prefer_mrc: bool = True) -> Image.Image | None:
    """Open the best atlas source available, preferring the full-resolution MRC."""
    if prefer_mrc:
        mrc_path = _find_atlas_mrc(atlas_path)
        if mrc_path and mrc_path.is_file():
            image = _mrc_to_image(mrc_path, 1.0, 99.0)
            if image is not None:
                return image.convert("RGB")
    try:
        with Image.open(atlas_path) as atlas_image:
            return atlas_image.convert("RGB")
    except Exception:
        return None


def _render_atlas_raw(atlas_path: Path) -> bytes | None:
    atlas_rgb = _open_atlas_rgb(atlas_path, prefer_mrc=False)
    if atlas_rgb is None:
        return None
    buf = io.BytesIO()
    atlas_rgb.save(buf, format="PNG", compress_level=3)
    return buf.getvalue()


def _render_atlas_screened_overview(
    atlas_path: Path,
    centers: dict[str, tuple[float, float]],
    screened_items: list[tuple[str, str]],
    ref_w: float | None,
    ref_h: float | None,
) -> bytes | None:
    if not centers or not screened_items:
        return None
    atlas_rgb = _open_atlas_rgb(atlas_path, prefer_mrc=False)
    if atlas_rgb is None:
        return None

    width, height = atlas_rgb.size
    scale_x = width / ref_w if ref_w and ref_w > 0 else 1.0
    scale_y = height / ref_h if ref_h and ref_h > 0 else 1.0
    draw = ImageDraw.Draw(atlas_rgb, "RGBA")
    radius = max(10, int(min(width, height) * 0.018))
    ring_width = max(2, radius // 5)
    font = ImageFont.load_default()
    for label, key in screened_items:
        center = centers.get(key)
        if center is None:
            continue
        cx = center[0] * scale_x
        cy = center[1] * scale_y
        if not (0 <= cx < width and 0 <= cy < height):
            continue
        draw.ellipse(
            (cx - radius, cy - radius, cx + radius, cy + radius),
            fill=(37, 99, 235, 128),
            outline=(20, 28, 44, 128),
            width=ring_width,
        )
        if hasattr(draw, "textbbox"):
            box = draw.textbbox((0, 0), label, font=font)
            text_w = box[2] - box[0]
            text_h = box[3] - box[1]
        else:
            text_w, text_h = font.getsize(label)
        draw.text((cx - text_w / 2, cy - text_h / 2), label, fill=(255, 255, 255, 128), font=font)

    buf = io.BytesIO()
    atlas_rgb.save(buf, format="PNG")
    return buf.getvalue()


def _render_atlas_category_overview(
    atlas_path: Path,
    centers: dict[str, tuple[float, float]],
    categories: dict[str, int | None],
    ref_w: float | None,
    ref_h: float | None,
) -> bytes | None:
    if not centers:
        return None
    atlas_rgb = _open_atlas_rgb(atlas_path, prefer_mrc=False)
    if atlas_rgb is None:
        return None

    width, height = atlas_rgb.size
    scale_x = width / ref_w if ref_w and ref_w > 0 else 1.0
    scale_y = height / ref_h if ref_h and ref_h > 0 else 1.0
    draw = ImageDraw.Draw(atlas_rgb, "RGBA")
    radius = max(8, int(min(width, height) * 0.011))
    ring_width = max(1, radius // 4)
    font = ImageFont.load_default()
    for key, center in centers.items():
        cx = center[0] * scale_x
        cy = center[1] * scale_y
        if not (0 <= cx < width and 0 <= cy < height):
            continue
        category_value = categories.get(key)
        try:
            category_value = int(category_value) if category_value is not None else None
        except Exception:
            category_value = None
        r, g, b = _category_marker_color(category_value)
        draw.ellipse(
            (cx - radius, cy - radius, cx + radius, cy + radius),
            fill=(r, g, b, 128),
            outline=(20, 28, 44, 128),
            width=ring_width,
        )
        if category_value is not None:
            label = str(category_value)
            if hasattr(draw, "textbbox"):
                box = draw.textbbox((0, 0), label, font=font)
                text_w = box[2] - box[0]
                text_h = box[3] - box[1]
            else:
                text_w, text_h = font.getsize(label)
            draw.text((cx - text_w / 2, cy - text_h / 2), label, fill=(255, 255, 255, 128), font=font)

    buf = io.BytesIO()
    atlas_rgb.save(buf, format="PNG")
    return buf.getvalue()


def _sanitize_label(label: str | None) -> str:
    if not label:
        return ""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", label.strip())
    return cleaned.strip("_")


def _prefix_from_label(label: str | None) -> str:
    cleaned = _sanitize_label(label)
    return f"{cleaned}_" if cleaned else ""


_SUMMARY_MAX_LEN = 300
_DRAFT_MAX_LEN = 5000
_THUMB_DEFAULT_SIZE = 280


def _summary_file_path(base_dir: Path) -> Path:
    return base_dir / "review_summary.txt"


def _normalize_summary_text(text: str | None) -> str:
    if not text:
        return ""
    # Keep this as a compact single sentence/line for report headers.
    cleaned = " ".join(str(text).strip().split())
    if len(cleaned) > _SUMMARY_MAX_LEN:
        cleaned = cleaned[:_SUMMARY_MAX_LEN].rstrip()
    return cleaned


def _load_review_summary(base_dir: Path) -> str:
    path = _summary_file_path(base_dir)
    if not path.is_file():
        return ""
    try:
        return _normalize_summary_text(path.read_text(encoding="utf-8"))
    except Exception:
        return ""


def _save_review_summary(base_dir: Path, text: str | None) -> str:
    normalized = _normalize_summary_text(text)
    path = _summary_file_path(base_dir)
    try:
        path.write_text(normalized, encoding="utf-8")
    except Exception:
        pass
    return normalized


def _drafts_file_path(base_dir: Path) -> Path:
    return base_dir / "review_drafts.json"


def _load_json_dict(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_json_dict(path: Path, payload: dict) -> None:
    try:
        path.write_text(json.dumps(payload), encoding="utf-8")
    except Exception:
        return


def _preflight_checks(
    base_dir: Path,
    grids: list[tuple[int | float, Path]],
    atlas_name: str | None,
    overlay_requested: bool,
    overlay_enabled: bool,
    atlas_overlay: bool,
    skip_foil_processing: bool = False,
) -> dict[str, list[str]]:
    checks: dict[str, list[str]] = {"errors": [], "warnings": [], "info": []}
    checks["info"].append(f"Detected {len(grids)} GridSquare directories under {base_dir}.")
    session_root, metadata_dir = _find_session_components(base_dir)
    if session_root is not None:
        checks["info"].append(f"Session root detected: {session_root}")
    else:
        checks["warnings"].append("Session root (EpuSession.dm) not detected in parent folders.")
    if metadata_dir is not None:
        checks["info"].append(f"Metadata folder detected: {metadata_dir}")
    else:
        checks["warnings"].append("Metadata folder not detected in parent folders.")

    if skip_foil_processing:
        checks["info"].append("FoilHole processing skipped; atlas/GridSquare-only mode enabled.")
        if overlay_requested:
            checks["info"].append("Foil overlays disabled in atlas/GridSquare-only mode.")
    elif overlay_requested and not overlay_enabled:
        checks["warnings"].append("Foil overlay was requested but disabled due to missing session metadata.")
    elif overlay_requested and overlay_enabled:
        checks["info"].append("Foil overlay generation enabled.")

    readout_scales: dict[str, tuple[float, float]] = {}
    missing_xml = 0
    missing_grid_mrc = 0
    for gid, gdir in grids:
        try:
            grid_img = find_grid_image(gdir)
        except Exception:
            checks["errors"].append(f"{gdir.name}: no GridSquare JPEG found.")
            continue
        grid_xml = gdir / grid_img.with_suffix(".xml").name
        if not grid_xml.is_file():
            missing_xml += 1
            checks["warnings"].append(f"{gdir.name}: grid XML missing ({grid_xml.name}); some mapping checks skipped.")
        else:
            try:
                grid_info = parse_grid_info(grid_xml)
            except Exception as exc:
                checks["warnings"].append(f"{gdir.name}: failed to parse grid XML ({exc}).")
                grid_info = {}
            readout_w = grid_info.get("readout_width")
            readout_h = grid_info.get("readout_height")
            if readout_w and readout_h:
                try:
                    scale_x = float(grid_img.width) / float(readout_w)
                    scale_y = float(grid_img.height) / float(readout_h)
                    readout_scales[gdir.name] = (scale_x, scale_y)
                    if abs(scale_x - scale_y) > 0.02:
                        checks["warnings"].append(
                            f"{gdir.name}: anisotropic readout scaling detected (x={scale_x:.3f}, y={scale_y:.3f})."
                        )
                except Exception:
                    pass
        if find_grid_mrc(gdir) is None:
            missing_grid_mrc += 1

    if missing_xml == 0:
        checks["info"].append("Grid XML files detected for all GridSquares.")
    if missing_grid_mrc:
        checks["warnings"].append(f"{missing_grid_mrc} GridSquares are missing MRC files (JPEG still available).")

    if readout_scales:
        unique_scales = sorted({(round(v[0], 3), round(v[1], 3)) for v in readout_scales.values()})
        if len(unique_scales) == 1:
            sx, sy = unique_scales[0]
            checks["info"].append(f"Readout/image scaling appears consistent (x={sx:.3f}, y={sy:.3f}).")
        else:
            scale_str = ", ".join(f"x={sx:.3f},y={sy:.3f}" for sx, sy in unique_scales[:6])
            checks["warnings"].append(
                "Mixed readout/image scales detected across GridSquares "
                f"({scale_str}). This usually indicates mixed camera binning or export settings."
            )

    if atlas_name:
        atlas_sample = None
        for _gid, gdir in grids:
            atlas_candidate = _resolve_atlas_path(atlas_name, gdir, base_dir)
            if atlas_candidate and atlas_candidate.is_file():
                atlas_sample = atlas_candidate
                break
        if atlas_sample is None:
            checks["warnings"].append(f"Atlas path '{atlas_name}' could not be resolved to an image.")
        else:
            checks["info"].append(f"Atlas image resolved: {atlas_sample}")
            if atlas_overlay:
                centers, _categories, _ref_w, _ref_h, atlas_msg = _load_atlas_mapping(atlas_sample)
                if centers:
                    checks["info"].append(f"Atlas metadata contains {len(centers)} GridSquare center entries.")
                elif atlas_msg:
                    checks["warnings"].append(atlas_msg)
    else:
        checks["info"].append("Atlas not configured; atlas panel will show placeholder content.")

    return checks


def _configure_overlay_transform(value: str | None):
    global _OVERLAY_TRANSFORM
    if value == "auto":
        _OVERLAY_TRANSFORM = None
    elif value in (None, "", "identity"):
        _OVERLAY_TRANSFORM = "identity"
    else:
        _OVERLAY_TRANSFORM = value


def _record_status(message: str) -> None:
    timestamp = time.time()
    print(message, flush=True)
    _OVERLAY_EVENTS.appendleft({"ts": timestamp, "message": message})


def _overlay_tools():
    """Lazy import of overlay helper utilities."""
    global _OVERLAY_TOOLS
    if _OVERLAY_TOOLS is not None:
        return _OVERLAY_TOOLS
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.append(str(root))
    try:
        from scripts.plot_foilhole_positions import compute_markers, plot_overlay, set_forced_transform  # type: ignore
    except Exception as exc:
        print(f"[overlay] unable to import helper module: {exc}")
        return None
    if _OVERLAY_TRANSFORM not in (None, "", "auto"):
        try:
            set_forced_transform(_OVERLAY_TRANSFORM)
            print(f"[overlay] forcing transform: {_OVERLAY_TRANSFORM}")
        except Exception as exc:
            print(f"[overlay] invalid overlay transform '{_OVERLAY_TRANSFORM}': {exc}")
    _OVERLAY_TOOLS = (compute_markers, plot_overlay)
    return _OVERLAY_TOOLS


def _generate_overlay_image(gdir: Path) -> tuple[Path | None, list[dict]]:
    """Generate a session-specific overlay in a writable cache directory."""
    tools = _overlay_tools()
    if not tools:
        return None, []
    compute_markers, plot_overlay = tools
    _record_status(f"[overlay] Generating overlay for {gdir.name}...")
    try:
        grid_img, markers = compute_markers(gdir)
    except Exception as exc:
        _record_status(f"[overlay] skipping {gdir.name}: {exc}")
        return None, []
    if not markers:
        _record_status(f"[overlay] no FoilHole markers for {gdir.name}")
        return None, []
    marker_payload = [
        {
            "x": max(0.0, min(100.0, float(px) / max(1, grid_img.width) * 100.0)),
            "y": max(0.0, min(100.0, float(py) / max(1, grid_img.height) * 100.0)),
            "in_bounds": bool(in_bounds),
            "label": int(label),
            "foil_name": path.name,
        }
        for px, py, in_bounds, label, path in markers
    ]
    try:
        selected_grid = find_grid_image(gdir)
        grid_stat = selected_grid.stat()
        grid_xml = selected_grid.with_suffix(".xml")
        xml_mtime = grid_xml.stat().st_mtime_ns if grid_xml.is_file() else 0
        foil_dir = gdir / "FoilHoles"
        foil_mtime = foil_dir.stat().st_mtime_ns if foil_dir.is_dir() else 0
        cache_source = f"{gdir.resolve()}|{grid_stat.st_size}|{grid_stat.st_mtime_ns}|{xml_mtime}|{foil_mtime}"
    except Exception:
        cache_source = str(gdir.resolve())
    cache_root = Path(tempfile.gettempdir()) / "EPUMapperOverlayCache"
    cache_root.mkdir(parents=True, exist_ok=True)
    out_path = cache_root / f"{hashlib.sha1(cache_source.encode('utf-8')).hexdigest()}.png"
    try:
        plot_overlay(grid_img, markers, title=gdir.name, output=out_path, dpi=180)
    except Exception as exc:
        _record_status(f"[overlay] failed to render overlay for {gdir.name}: {exc}")
        return None, marker_payload
    if out_path.is_file():
        _record_status(f"[overlay] Finished {gdir.name}")
        return out_path, marker_payload
    _record_status(f"[overlay] Overlay file missing for {gdir.name}")
    return None, marker_payload


def _ensure_overlay_image(gdir: Path, base_dir: Path) -> tuple[Path | None, str | None, list[dict]]:
    """Return a fresh overlay PNG path if generation succeeds, else fall back to cached copy."""
    generated, markers = _generate_overlay_image(gdir)
    if generated:
        return generated, None, markers
    cached = _find_overlay_image(gdir, base_dir)
    if cached:
        return cached, "Using cached overlay image (new generation failed).", markers
    return None, "Overlay unavailable for this GridSquare (missing metadata or generation failed).", markers


def _has_grid_dirs(path: Path) -> bool:
    try:
        for entry in path.iterdir():
            if entry.is_dir() and entry.name.startswith("GridSquare_"):
                return True
    except Exception:
        return False
    return False


def _resolve_grid_root(path: Path, preferred_subdir: str | None = None) -> Path:
    """Accept a GridSquare folder, Images-Disc*, or session root and return the actual disc directory."""
    path = path.resolve()
    if not path.exists():
        raise RuntimeError(f"Path not found: {path}")
    if path.name.startswith("GridSquare_") or _has_grid_dirs(path):
        return path
    if path.name.startswith("Images-Disc"):
        return path

    def _select_from_session(session_dir: Path) -> Path:
        if preferred_subdir:
            target = session_dir / preferred_subdir
            if target.is_dir():
                return _resolve_grid_root(target)
        disc1 = session_dir / "Images-Disc1"
        if disc1.is_dir():
            return disc1
        candidates = sorted(p for p in session_dir.iterdir() if p.is_dir() and p.name.startswith("Images-Disc"))
        if len(candidates) == 1:
            return candidates[0]
        if candidates:
            names = ", ".join(p.name for p in candidates)
            raise RuntimeError(
                f"Multiple Images-Disc* directories found in {session_dir}: {names}. "
                "Use --images-subdir or set IMAGES_SUBDIR to pick one."
            )
        raise RuntimeError(
            f"No GridSquare directories found in {session_dir}. "
            "Pass the Images-Disc* folder or set --images-subdir when pointing at the session root."
        )

    return _select_from_session(path)


def _find_session_components(grid_dir: Path) -> tuple[Path | None, Path | None]:
    """Return (session_root, metadata_dir) by scanning parents of `grid_dir`."""
    session_root = None
    metadata_dir = None
    for candidate in [grid_dir] + list(grid_dir.parents):
        if session_root is None and (candidate / "EpuSession.dm").is_file():
            session_root = candidate
        if metadata_dir is None and (candidate / "Metadata").is_dir():
            metadata_dir = candidate / "Metadata"
        if session_root and metadata_dir:
            break
    return session_root, metadata_dir


def create_app(
    base_dir: Path,
    atlas_name: str | None = None,
    report_file: Path | None = None,
    overlay: bool = False,
    overlay_transform: str | None = "identity",
    session_label: str | None = None,
    atlas_overlay: bool = True,
    skip_foil_processing: bool = False,
) -> FastAPI:
    _OVERLAY_EVENTS.clear()
    _configure_overlay_transform(overlay_transform)
    base_dir = base_dir.resolve()
    label_prefix = _prefix_from_label(session_label)
    overlay_enabled = bool(overlay)
    overlay_notice_html = ""
    if skip_foil_processing:
        overlay_enabled = False
        overlay_notice_html = (
            "<div class=\"note\">Atlas/GridSquare-only mode is active: "
            "FoilHole scanning and foil overlay generation are skipped.</div>"
        )
    elif overlay_enabled:
        session_root, metadata_dir = _find_session_components(base_dir)
        missing_bits: list[str] = []
        if session_root is None:
            missing_bits.append("EpuSession.dm")
        if metadata_dir is None:
            missing_bits.append("Metadata folder")
        if missing_bits:
            overlay_enabled = False
            missing_str = ", ".join(missing_bits)
            overlay_notice_html = f"<div class=\"note warn\">Foil overlays disabled: missing {missing_str}. Images will still load.</div>"
            print(f"[overlay] disabled: missing {missing_str} while scanning parents of {base_dir}")
    grids = _collect_grids(base_dir)
    if not grids:
        raise RuntimeError(f"no GridSquare directories found in {base_dir}")
    preflight_state = _preflight_checks(
        base_dir,
        grids,
        atlas_name,
        overlay_requested=bool(overlay),
        overlay_enabled=overlay_enabled,
        atlas_overlay=atlas_overlay,
        skip_foil_processing=skip_foil_processing,
    )
    if preflight_state["errors"]:
        detail = "\n".join(f"- {msg}" for msg in preflight_state["errors"])
        raise RuntimeError(f"Preflight checks failed:\n{detail}")
    for message in preflight_state["warnings"]:
        _record_status(f"[preflight] {message}")
    for message in preflight_state["info"][:3]:
        _record_status(f"[preflight] {message}")
    items = []
    total_grids = len(grids)
    status_state = {"total": total_grids, "loaded": 0}
    for idx_item, (_gid, gdir) in enumerate(grids, start=1):
        _record_status(f"[review_app] Preparing GridSquare {_gid} ({idx_item}/{total_grids})")
        grid_img = find_grid_image(gdir)
        mrc_path = find_grid_mrc(gdir)
        atlas_path = _resolve_atlas_path(atlas_name, gdir, base_dir) if atlas_name else None
        atlas_mrc_path = _find_atlas_mrc(atlas_path) if atlas_path else None
        atlas_centers: dict[str, tuple[float, float]] = {}
        atlas_categories: dict[str, int | None] = {}
        atlas_ref_w: float | None = None
        atlas_ref_h: float | None = None
        atlas_center_key: str | None = None
        atlas_overlay_message: str | None = None
        epu_category_score: int | None = None
        if atlas_path and atlas_path.is_file():
            atlas_centers, atlas_categories, atlas_ref_w, atlas_ref_h, atlas_overlay_message = _load_atlas_mapping(atlas_path)
            for lookup_key in _atlas_lookup_keys(gdir, _gid):
                if lookup_key in atlas_centers:
                    atlas_center_key = lookup_key
                    break
            if atlas_center_key is not None and atlas_center_key in atlas_categories:
                epu_category_score = atlas_categories.get(atlas_center_key)
            else:
                for lookup_key in _atlas_lookup_keys(gdir, _gid):
                    if lookup_key in atlas_categories:
                        epu_category_score = atlas_categories.get(lookup_key)
                        break
            if atlas_overlay and atlas_center_key is None and atlas_centers and atlas_overlay_message is None:
                atlas_overlay_message = "GridSquare not found in Atlas metadata."
        foil_list = []
        data_list = []
        foil_processing_note = None
        if skip_foil_processing:
            foil_processing_note = "FoilHole processing skipped in Atlas/GridSquare-only mode."
        else:
            foils, datas = gather_foil_and_data(gdir)
            foils = _latest_only(foils)
            datas = _latest_only(datas)
            for foil_id in sorted(foils.keys()):
                for foil_path in foils[foil_id]:
                    foil_list.append({"id": foil_id, "path": foil_path, "mrc": _find_mrc_for_jpg(foil_path)})
            for data_id in sorted(datas.keys()):
                for data_path in datas[data_id]:
                    if data_id in foils:
                        meta_lines = []
                        xml_path = data_path.with_suffix(".xml")
                        if xml_path.is_file():
                            meta_lines = _format_meta(parse_metadata(xml_path))
                        data_list.append(
                            {"id": data_id, "path": data_path, "mrc": _find_mrc_for_jpg(data_path), "meta": meta_lines}
                        )
        overlay_path = None
        overlay_message = None
        foil_markers: list[dict] = []
        if overlay_enabled:
            overlay_path, overlay_message, foil_markers = _ensure_overlay_image(gdir, base_dir)
        items.append(
            {
                "id": _gid,
                "dir": gdir,
                "grid_img": grid_img,
                "name": grid_img.name,
                "mrc": mrc_path,
                "atlas": atlas_path,
                "atlas_mrc": atlas_mrc_path,
                "atlas_centers": atlas_centers,
                "atlas_ref_w": atlas_ref_w,
                "atlas_ref_h": atlas_ref_h,
                "atlas_center_key": atlas_center_key,
                "atlas_overlay_message": atlas_overlay_message,
                "epu_category_score": epu_category_score,
                "overlay": overlay_path,
                "overlay_message": overlay_message,
                "foil_markers": foil_markers,
                "foil_processing_note": foil_processing_note,
                "foils": foil_list,
                "data": data_list,
            }
        )
        status_state["loaded"] = idx_item

    atlas_screened_preview: bytes | None = None
    atlas_category_preview: bytes | None = None
    atlas_raw_preview: bytes | None = None
    atlas_mrc_previews: dict[tuple[float, float], bytes] = {}
    atlas_preview_message: str | None = None
    atlas_preview_path: Path | None = None
    atlas_centers_all: dict[str, tuple[float, float]] = {}
    atlas_categories_all: dict[str, int | None] = {}
    atlas_ref_w_all: float | None = None
    atlas_ref_h_all: float | None = None
    atlas_sample_item = next(
        (
            item
            for item in items
            if item.get("atlas") is not None and item["atlas"].is_file()
        ),
        None,
    )
    if atlas_sample_item is not None:
        atlas_preview_path = atlas_sample_item["atlas"]
        centers, categories, ref_w, ref_h, atlas_msg = _load_atlas_mapping(atlas_preview_path)
        atlas_centers_all = centers
        atlas_categories_all = categories
        atlas_ref_w_all = ref_w
        atlas_ref_h_all = ref_h
        atlas_raw_preview = _render_atlas_raw(atlas_preview_path)
        screened_items: list[tuple[str, str]] = []
        for idx_item, item in enumerate(items, start=1):
            center_key = item.get("atlas_center_key")
            if center_key and center_key in centers:
                screened_items.append((str(idx_item), center_key))
        atlas_screened_preview = _render_atlas_screened_overview(
            atlas_preview_path,
            centers,
            screened_items,
            ref_w,
            ref_h,
        )
        atlas_category_preview = _render_atlas_category_overview(
            atlas_preview_path,
            centers,
            categories,
            ref_w,
            ref_h,
        )
        if atlas_screened_preview is None and atlas_category_preview is None:
            atlas_preview_message = atlas_msg or "Atlas overview images unavailable (metadata parsing failed)."
    elif atlas_name:
        atlas_preview_message = "Atlas image not found; atlas overview previews are unavailable."

    responses_file = base_dir / "review_responses.json"
    manual_targets_file = base_dir / "manual_collection_targets.json"
    drafts_file = _drafts_file_path(base_dir)
    summary_state = {"text": _load_review_summary(base_dir)}
    session_storage_key = hashlib.sha1(str(base_dir).encode("utf-8")).hexdigest()[:16]
    session_cache_key = secrets.token_urlsafe(8)
    thumb_cache_dir = Path(tempfile.gettempdir()) / "EPUMapperThumbCache" / session_storage_key
    thumb_cache_dir.mkdir(parents=True, exist_ok=True)
    drafts_lock = threading.Lock()
    report_jobs_lock = threading.Lock()
    portable_jobs_lock = threading.Lock()
    thumb_cache_lock = threading.Lock()

    def _load_responses() -> dict[str, dict]:
        loaded = _load_json_dict(responses_file)
        return {str(k): v for k, v in loaded.items() if isinstance(v, dict)}

    def _save_responses(current: dict[str, dict]) -> None:
        _save_json_dict(responses_file, current)

    def _load_drafts() -> dict[str, dict]:
        loaded = _load_json_dict(drafts_file)
        return {str(k): v for k, v in loaded.items() if isinstance(v, dict)}

    def _save_drafts(current: dict[str, dict]) -> None:
        _save_json_dict(drafts_file, current)

    responses = _load_responses()
    drafts = _load_drafts()
    manual_targets = {
        str(key): value
        for key, value in _load_json_dict(manual_targets_file).items()
        if isinstance(value, dict)
    }
    report_jobs: dict[str, dict] = {}
    portable_jobs: dict[str, dict] = {}

    app = FastAPI()

    def _item_key(idx: int) -> str:
        return items[idx]["dir"].name

    def _normalize_review_entry(payload: dict, default_include: bool = True) -> dict:
        rating_raw = payload.get("rating", 0)
        try:
            rating = int(rating_raw)
        except Exception:
            try:
                rating = int(float(rating_raw))
            except Exception:
                rating = 0
        rating = max(0, min(5, rating))
        comment = str(payload.get("comment", "") or "")
        if len(comment) > _DRAFT_MAX_LEN:
            comment = comment[:_DRAFT_MAX_LEN]
        include = bool(payload.get("include", default_include))
        collection_status = str(payload.get("collection_status", "") or "").strip().lower()
        if collection_status not in ("suitable", "unsuitable"):
            collection_status = "suitable" if bool(payload.get("collect", False)) else ""
        collect = collection_status == "suitable"
        reviewed = bool(payload.get("reviewed", True))
        updated_at_raw = payload.get("updated_at", time.time())
        try:
            updated_at = float(updated_at_raw)
        except Exception:
            updated_at = time.time()
        return {
            "rating": rating,
            "comment": comment,
            "include": include,
            "collect": collect,
            "collection_status": collection_status,
            "reviewed": reviewed,
            "updated_at": updated_at,
        }

    def _resolve_media_path(item: dict, kind: str, name: str | None = None) -> Path | None:
        if kind == "grid":
            return item["grid_img"]
        if kind == "atlas":
            return item.get("atlas")
        if kind == "overlay":
            return item.get("overlay")
        if kind == "foil":
            for entry in item["foils"]:
                if entry["path"].name == (name or ""):
                    return entry["path"]
            return None
        if kind == "data":
            for entry in item["data"]:
                if entry["path"].name == (name or ""):
                    return entry["path"]
            return None
        return None

    def _thumb_cache_path(src: Path, size: int) -> Path | None:
        if not src or not src.is_file():
            return None
        try:
            stat = src.stat()
        except Exception:
            return None
        key_payload = f"{src.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|{size}".encode("utf-8")
        digest = hashlib.sha1(key_payload).hexdigest()
        return thumb_cache_dir / f"{digest}.jpg"

    def _build_thumb(src: Path, size: int) -> Path | None:
        cache_path = _thumb_cache_path(src, size)
        if cache_path is None:
            return None
        if cache_path.is_file():
            return cache_path
        with thumb_cache_lock:
            if cache_path.is_file():
                return cache_path
            try:
                with Image.open(src) as img:
                    thumb = img.convert("RGB")
                    thumb.thumbnail((size, size), Image.LANCZOS)
                tmp_path = cache_path.with_suffix(".tmp.jpg")
                thumb.save(tmp_path, format="JPEG", quality=90, optimize=True)
                tmp_path.replace(cache_path)
                return cache_path
            except Exception:
                return None

    def _build_png_preview(src: Path, size: int) -> Path | None:
        """Build a fast display PNG without touching an MRC source."""
        if not src or not src.is_file():
            return None
        size = max(256, min(2048, int(size)))
        try:
            stat = src.stat()
        except Exception:
            return None
        key_payload = f"png|{src.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|{size}".encode("utf-8")
        cache_path = thumb_cache_dir / f"{hashlib.sha1(key_payload).hexdigest()}_{size}.png"
        if cache_path.is_file():
            return cache_path
        with thumb_cache_lock:
            if cache_path.is_file():
                return cache_path
            try:
                with Image.open(src) as image:
                    preview = image.convert("RGB")
                    preview.thumbnail((size, size), Image.LANCZOS)
                temp_path = cache_path.with_suffix(".tmp.png")
                preview.save(temp_path, format="PNG", compress_level=3)
                temp_path.replace(cache_path)
                return cache_path
            except Exception:
                return None

    def _thumbnail_sources() -> list[Path]:
        sources: list[Path] = []
        for item in items:
            sources.append(item["grid_img"])
            atlas_path = item.get("atlas")
            if atlas_path:
                sources.append(atlas_path)
            overlay_path = item.get("overlay")
            if overlay_path:
                sources.append(overlay_path)
            for entry in item["foils"]:
                sources.append(entry["path"])
            for entry in item["data"]:
                sources.append(entry["path"])
        unique: dict[Path, None] = {}
        for path in sources:
            if path and path.is_file() and path not in unique:
                unique[path] = None
        return list(unique.keys())

    def _prime_thumbnail_cache() -> None:
        sources = _thumbnail_sources()
        if not sources:
            return
        total = len(sources)
        _record_status(f"[thumb] caching {total} thumbnails in background...")
        for idx_src, src in enumerate(sources, start=1):
            _build_thumb(src, _THUMB_DEFAULT_SIZE)
            if idx_src == 1 or idx_src == total or idx_src % 25 == 0:
                _record_status(f"[thumb] cached {idx_src}/{total}")
        _record_status("[thumb] cache ready")

    def _export_rows() -> list[dict]:
        rows: list[dict] = []
        for idx_item, item in enumerate(items):
            name = item["dir"].name
            response = _normalize_review_entry(responses.get(name, {}), default_include=True)
            rows.append(
                {
                    "index": idx_item + 1,
                    "gridsquare_id": item["id"],
                    "gridsquare_dir": name,
                    "gridsquare_image": item["name"],
                    "include": bool(response.get("include", True)),
                    "collect": bool(response.get("collect", False)),
                    "collection_status": response.get("collection_status", ""),
                    "rating": int(response.get("rating", 0)),
                    "comment": str(response.get("comment", "")),
                    "foil_count": len(item["foils"]),
                    "data_count": len(item["data"]),
                    "epu_category_score": item.get("epu_category_score"),
                    "atlas_available": bool(item.get("atlas")),
                    "overlay_available": bool(item.get("overlay")),
                }
            )
        return rows

    def _export_payload() -> dict:
        return {
            "session_root": str(base_dir),
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "summary": summary_state["text"],
            "rows": _export_rows(),
            "manual_unscreened_collection_targets": list(manual_targets.values()),
        }

    def _dashboard_grid_summaries() -> list[dict]:
        summaries: list[dict] = []
        for idx_item, item in enumerate(items):
            response = _normalize_review_entry(responses.get(item["dir"].name, {}), default_include=True)
            center_key = item.get("atlas_center_key")
            center = (item.get("atlas_centers") or {}).get(center_key) if center_key else None
            ref_w = item.get("atlas_ref_w")
            ref_h = item.get("atlas_ref_h")
            position = None
            if center and ref_w and ref_h and ref_w > 0 and ref_h > 0:
                position = {
                    "x": max(0.0, min(100.0, float(center[0]) / float(ref_w) * 100.0)),
                    "y": max(0.0, min(100.0, float(center[1]) / float(ref_h) * 100.0)),
                }
            summaries.append(
                {
                    "idx": idx_item,
                    "id": str(item["id"]),
                    "name": item["name"],
                    "position": position,
                    "category": item.get("epu_category_score"),
                    "foil_count": len(item["foils"]),
                    "data_count": len(item["data"]),
                    "reviewed": item["dir"].name in responses and response["reviewed"],
                    "rating": response["rating"] if item["dir"].name in responses and response["reviewed"] else 0,
                    "include": response["include"],
                    "collect": response["collect"],
                    "collection_status": response["collection_status"],
                }
            )
        return summaries

    def _unscreened_atlas_summaries() -> list[dict]:
        screened_keys = {str(item["id"]) for item in items}
        screened_keys.update(str(item.get("atlas_center_key")) for item in items if item.get("atlas_center_key"))
        summaries: list[dict] = []
        if not atlas_ref_w_all or not atlas_ref_h_all:
            return summaries
        for key, center in atlas_centers_all.items():
            if str(key) in screened_keys:
                continue
            summaries.append(
                {
                    "key": str(key),
                    "id": str(key),
                    "category": atlas_categories_all.get(key),
                    "position": {
                        "x": max(0.0, min(100.0, float(center[0]) / float(atlas_ref_w_all) * 100.0)),
                        "y": max(0.0, min(100.0, float(center[1]) / float(atlas_ref_h_all) * 100.0)),
                    },
                    "selected": str(key) in manual_targets,
                }
            )
        return summaries

    def _hole_preview_records(idx: int, item: dict) -> list[dict]:
        foil_by_name = {entry["path"].name: entry for entry in item["foils"]}
        data_by_id: dict[str, list[dict]] = {}
        for entry in item["data"]:
            data_by_id.setdefault(entry["id"], []).append(entry)
        records: list[dict] = []
        for marker in item.get("foil_markers", []):
            foil = foil_by_name.get(marker.get("foil_name", ""))
            if foil is None:
                continue
            matched = data_by_id.get(foil["id"], [])
            data_entry = matched[-1] if matched else None
            foil_name = foil["path"].name
            data_name = data_entry["path"].name if data_entry else ""
            records.append(
                {
                    "x": marker.get("x", 0),
                    "y": marker.get("y", 0),
                    "marker_label": marker.get("label"),
                    "foil_id": str(foil["id"]),
                    "foil_name": foil_name,
                    "foil_preview": f"/preview.png?idx={idx}&kind=foil&name={urllib.parse.quote(foil_name)}&size=2048&session={session_cache_key}",
                    "foil_has_mrc": bool(foil.get("mrc")),
                    "data_name": data_name,
                    "data_preview": (
                        f"/preview.png?idx={idx}&kind=data&name={urllib.parse.quote(data_name)}&size=2048&session={session_cache_key}"
                        if data_entry
                        else ""
                    ),
                    "data_has_mrc": bool(data_entry and data_entry.get("mrc")),
                    "meta": data_entry.get("meta", []) if data_entry else [],
                }
            )
        return records

    def _job_state(job_id: str) -> dict | None:
        with report_jobs_lock:
            job = report_jobs.get(job_id)
            if job is None:
                return None
            snapshot = dict(job)
        path_value = snapshot.pop("path", None)
        if path_value:
            snapshot["download_url"] = f"/report_jobs/{job_id}/download"
        return snapshot

    def _update_job(job_id: str, **updates) -> None:
        with report_jobs_lock:
            job = report_jobs.get(job_id)
            if job is None:
                return
            job.update(updates)
            job["updated_at"] = time.time()

    def _run_report_job(job_id: str, kind: str, scope: str = "representative") -> None:
        _update_job(job_id, status="running", progress=10, message="Preparing report...")
        all_screened_images = scope == "all_screened"
        report_path, details_path = _report_paths(all_screened_images=all_screened_images)
        job_kind = "full" if kind == "overview" else kind
        target_path = report_path if job_kind == "full" else details_path

        def _write_target(path: Path) -> None:
            if job_kind == "full":
                write_combined_report(
                    base_dir,
                    path,
                    atlas_name,
                    responses,
                    overlay=overlay_enabled,
                    atlas_overlay=atlas_overlay,
                    global_summary=summary_state["text"],
                    skip_foil_processing=skip_foil_processing,
                    all_screened_images=all_screened_images,
                )
            elif job_kind == "details":
                write_selected_report(
                    base_dir,
                    path,
                    atlas_name,
                    responses,
                    overlay=overlay_enabled,
                    atlas_overlay=atlas_overlay,
                    global_summary=summary_state["text"],
                    skip_foil_processing=skip_foil_processing,
                    all_screened_images=all_screened_images,
                )
            else:
                raise ValueError(f"unknown report kind: {job_kind}")

        try:
            _update_job(job_id, progress=35, message="Rendering PDF pages...")
            _write_target(target_path)
        except (PermissionError, OSError):
            target_path = _temp_report_path(target_path.name)
            _update_job(job_id, progress=55, message="Output directory is not writable; using temporary folder...")
            try:
                _write_target(target_path)
            except Exception as exc:
                _update_job(job_id, status="error", progress=100, message=f"Failed to generate report: {exc}", error=str(exc))
                return
        except Exception as exc:
            _update_job(job_id, status="error", progress=100, message=f"Failed to generate report: {exc}", error=str(exc))
            return
        _update_job(job_id, status="done", progress=100, message="Report ready.", path=str(target_path), filename=target_path.name)

    def _portable_job_state(job_id: str) -> dict | None:
        with portable_jobs_lock:
            job = portable_jobs.get(job_id)
            return dict(job) if job is not None else None

    def _update_portable_job(job_id: str, **updates) -> None:
        with portable_jobs_lock:
            job = portable_jobs.get(job_id)
            if job is None:
                return
            job.update(updates)
            job["updated_at"] = time.time()

    def _portable_atlas_source() -> Path | None:
        if atlas_name:
            candidate = Path(atlas_name).expanduser()
            if candidate.exists():
                return candidate.resolve()
        return atlas_preview_path.resolve() if atlas_preview_path and atlas_preview_path.exists() else None

    def _refresh_portable_annotations(manifest_path: Path) -> None:
        """Refresh mutable review files after the bulk session copy completes."""
        loaded = load_portable_session(manifest_path)
        source_root = portable_session_source(base_dir)
        relative_base = base_dir.relative_to(source_root)
        copied_base = Path(loaded["session_path"]) / relative_base
        copied_base.mkdir(parents=True, exist_ok=True)
        for source in (responses_file, manual_targets_file, drafts_file, base_dir / "review_summary.txt"):
            if source.is_file():
                shutil.copy2(source, copied_base / source.name)

    def _run_portable_job(job_id: str, destination: Path) -> None:
        _update_portable_job(job_id, status="running", progress=5, message="Preparing portable session…")

        def log(message: str) -> None:
            clean = str(message).strip()
            if clean:
                _update_portable_job(job_id, progress=45, message=clean)

        try:
            manifest_path = export_portable_session(
                session_path=base_dir,
                atlas_path=_portable_atlas_source(),
                atlas_mode="epu" if atlas_overlay else "static",
                destination_parent=destination,
                label=session_label or portable_session_source(base_dir).name,
                options={
                    "overlay": overlay_enabled,
                    "overlay_transform": overlay_transform or "identity",
                    "atlas_overlay": atlas_overlay,
                    "skip_foil_processing": skip_foil_processing,
                },
                log=log,
            )
            _update_portable_job(job_id, progress=90, message="Refreshing reviews and collection targets…")
            _refresh_portable_annotations(manifest_path)
        except Exception as exc:
            _update_portable_job(
                job_id,
                status="error",
                progress=100,
                message=f"Portable export failed: {exc}",
                error=str(exc),
            )
            return
        _update_portable_job(
            job_id,
            status="done",
            progress=100,
            message="Portable session ready.",
            path=str(manifest_path),
        )

    def review_html(idx: int) -> str:
        item = items[idx]
        has_data = bool(item["data"])
        nodata_html = ""
        if not has_data and not item.get("foil_processing_note"):
            nodata_html = "<div class=\"note warn\">No screening data available for this GridSquare.</div>"
        grid_has_mrc = bool(item["mrc"])
        grid_mrc_json = "true" if grid_has_mrc else "false"
        atlas_has_mrc = bool(item.get("atlas_mrc"))
        atlas_mrc_json = "true" if atlas_has_mrc else "false"
        grid_mrc_note = "" if grid_has_mrc else "<div class=\"note\">No grid MRC available.</div>"
        ts = int(time.time() * 1000)
        default_kind = "atlas" if item["atlas"] else "grid"
        default_label = "Atlas" if default_kind == "atlas" else "GridSquare"
        default_src = f"/preview.png?idx={idx}&kind={default_kind}&size=1600&session={session_cache_key}&t={ts}"
        default_has_mrc_json = atlas_mrc_json if default_kind == "atlas" else grid_mrc_json
        category_text = _format_category_score_text(item.get("epu_category_score"))
        category_subtitle_html = f"<div class=\"subtitle\">EPU category score: {category_text}</div>"
        atlas_note_html = ""
        if item["atlas"]:
            atlas_html = f"<img id=\"atlasimg\" src=\"/thumb?idx={idx}&kind=atlas&size=600\" class=\"atlas-img\" data-kind=\"atlas\" data-has-mrc=\"{1 if atlas_has_mrc else 0}\"/>"
            if item.get("atlas_center_key"):
                atlas_note_html = "<div class=\"note\">Current GridSquare is marked in red.</div>"
            elif item.get("atlas_overlay_message"):
                atlas_note_html = f"<div class=\"note\">{item['atlas_overlay_message']}</div>"
        else:
            atlas_html = (
                "<div class=\"atlas-placeholder\"><div class=\"placeholder-title\">Atlas not provided</div>"
                "<div class=\"placeholder-note\">Add an atlas JPEG/PNG or atlas directory and launch with "
                "<code>--atlas /path/to/Atlas</code> (or the launcher field) so reviewers can align squares quickly.</div></div>"
            )
        grid_frame_html = (
            f"<div class=\"image-frame\"><div id=\"viewer-caption\" class=\"image-caption\">Viewer: {default_label} (last clicked image)</div>"
            f"<div id=\"viewer-viewport\" class=\"viewer-viewport\"><img id=\"gridimg\" class=\"frame-image\" src=\"{default_src}\"/></div></div>"
        )
        data_by_id = {}
        for d in item["data"]:
            data_by_id.setdefault(d["id"], []).append(d)
        foil_id_by_name = {f["path"].name: f["id"] for f in item["foils"]}
        foil_hover_markers = []
        for marker in item.get("foil_markers", []):
            foil_id = foil_id_by_name.get(marker.get("foil_name", ""))
            matched_data = data_by_id.get(foil_id, []) if foil_id is not None else []
            preview_data = matched_data[-1] if matched_data else None
            foil_hover_markers.append(
                {
                    "x": marker.get("x", 0),
                    "y": marker.get("y", 0),
                    "label": marker.get("label"),
                    "foil_id": str(foil_id or marker.get("foil_name", "FoilHole")),
                    "data_name": preview_data["path"].name if preview_data else "",
                    "data_has_mrc": bool(preview_data and preview_data.get("mrc")),
                    "preview": (
                        f"/thumb?idx={idx}&kind=data&name={urllib.parse.quote(preview_data['path'].name)}&size=420"
                        if preview_data
                        else ""
                    ),
                }
            )
        foil_hover_markers_json = json.dumps(foil_hover_markers, separators=(",", ":")).replace("</", "<\\/")
        overlay_html = ""
        overlay_inline_notice = ""
        if item.get("overlay"):
            overlay_html = (
                f"<div class=\"image-frame\"><div class=\"image-caption\">Foil overlay</div>"
                f"<div class=\"foil-overlay-wrap\"><img id=\"overlayimg\" class=\"frame-image\" src=\"/overlay?idx={idx}&t={ts}\"/>"
                f"<div id=\"foil-hover-layer\" class=\"foil-hover-layer\"></div></div>"
                f"<div class=\"note\">Hover a numbered FoilHole to preview its latest Data image.</div></div>"
            )
        elif item.get("overlay_message"):
            overlay_inline_notice = f"<div class=\"note warn\">{item['overlay_message']}</div>"
        grid_section_html = f"<div class=\"grid-panel\">{grid_frame_html}{overlay_html}</div>{overlay_inline_notice}"
        thumb_card_html = ""
        if item["foils"]:
            groups = []
            for f in item["foils"]:
                matched_for_foil = data_by_id.get(f["id"], [])
                hover_data = matched_for_foil[-1] if matched_for_foil else None
                hover_attrs = ""
                if hover_data:
                    hover_attrs = (
                        f" data-hover-preview=\"/thumb?idx={idx}&kind=data&name={urllib.parse.quote(hover_data['path'].name)}&size=420\""
                        f" data-hover-label=\"FoilHole {f['id']} · Data preview\""
                        f" data-hover-data-name=\"{hover_data['path'].name}\""
                        f" data-hover-data-mrc=\"{1 if hover_data['mrc'] else 0}\""
                    )
                foil_thumb = (
                    f"<img class=\"thumb foil-thumb\" loading=\"lazy\" data-kind=\"foil\" data-name=\"{f['path'].name}\" "
                    f"data-has-mrc=\"{1 if f['mrc'] else 0}\" "
                    f"src=\"/thumb?idx={idx}&kind=foil&name={urllib.parse.quote(f['path'].name)}&size={_THUMB_DEFAULT_SIZE}\"{hover_attrs}/>"
                )
                data_imgs = []
                for p in data_by_id.get(f["id"], []):
                    meta_html = ""
                    if p.get("meta"):
                        meta_html = "<div class=\"meta\">" + "<br>".join(p["meta"]) + "</div>"
                    data_imgs.append(
                        f"<div class=\"data-card\"><img class=\"thumb\" loading=\"lazy\" data-kind=\"data\" "
                        f"data-name=\"{p['path'].name}\" data-has-mrc=\"{1 if p['mrc'] else 0}\" "
                        f"src=\"/thumb?idx={idx}&kind=data&name={urllib.parse.quote(p['path'].name)}&size={_THUMB_DEFAULT_SIZE}\"/>{meta_html}</div>"
                    )
                data_block = f"<div class=\"thumb-grid\">{''.join(data_imgs)}</div>" if data_imgs else "<div class=\"note\">No data images for this FoilHole.</div>"
                groups.append(f"<div class=\"foil-group\"><div class=\"foil-row\">{foil_thumb}<div class=\"data-block\">{data_block}</div></div></div>")
            thumb_html = "<div class=\"section-title\">Foil holes and data</div>" + "".join(groups)
            thumb_card_html = f"<div class=\"card\">{thumb_html}</div>"
        elif not item.get("foil_processing_note"):
            thumb_card_html = "<div class=\"card\"><div class=\"section-title\">Foil holes and data</div><div class=\"note\">No foil images found.</div></div>"
        overlay_banner = overlay_notice_html or ""
        warning_items = preflight_state.get("warnings", [])[:4]
        info_items = preflight_state.get("info", [])[:2]
        preflight_rows = warning_items if warning_items else info_items
        preflight_level = "warn" if warning_items else "ok"
        if preflight_rows:
            preflight_li = "".join(f"<li>{msg}</li>" for msg in preflight_rows)
            preflight_title = "Preflight checks" if warning_items else "Preflight checks passed"
            preflight_html = (
                f"<div class=\"preflight-box\"><div class=\"preflight-title note {preflight_level}\">{preflight_title}</div>"
                f"<ul class=\"preflight-list\">{preflight_li}</ul></div>"
            )
        else:
            preflight_html = ""
        next_idx_val = idx + 1 if idx + 1 < len(items) else "null"
        prev_idx_val = idx - 1 if idx - 1 >= 0 else "null"
        total_len = len(items)
        prev_nav_html = f"<a class=\"btn nav-btn\" href=\"/review/{idx - 1}\">← Previous</a>" if idx > 0 else ""
        next_nav_html = f"<a class=\"btn nav-btn\" href=\"/review/{idx + 1}\">Next →</a>" if idx + 1 < total_len else ""
        return f"""<html><head><meta charset=\"utf-8\"><title>Review GridSquare {item['id']}</title>
<style>
:root{{color-scheme:light;--img-size:560px;--thumb-size:190px;--ink:#172033;--muted:#68758b;--line:#dfe5ee;--accent:#2563eb;}}
*{{box-sizing:border-box;}}
body{{margin:0;font-family:Inter,-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:#f4f7fb;color:var(--ink);}}
.page{{max-width:1640px;margin:0 auto;padding:20px 28px 40px;}}
.header{{position:sticky;top:0;z-index:10;display:flex;justify-content:space-between;align-items:center;margin:0 -28px 18px;padding:13px 28px;background:rgba(255,255,255,.94);border-bottom:1px solid var(--line);backdrop-filter:blur(10px);}}
.header-actions{{display:flex;align-items:center;gap:7px;}}
.title{{font-size:19px;font-weight:750;letter-spacing:-.02em;}}
.subtitle{{color:#666;font-size:13px;margin-top:2px;}}
.progress{{color:#66748a;font-size:12px;font-weight:700;padding:0 5px;}}
.layout{{display:grid;grid-template-columns:minmax(0,1fr) 360px;gap:18px;align-items:start;}}
.right{{position:sticky;top:88px;display:flex;flex-direction:column;gap:12px;}}
.right .card{{margin:0;}}
.card{{background:#fff;border:1px solid var(--line);border-radius:14px;padding:15px;box-shadow:0 1px 2px rgba(24,36,60,.03);}}
.grid-panel{{display:flex;flex-wrap:wrap;gap:14px;margin-bottom:16px;}}
.image-frame{{background:#fff;border:1px solid var(--line);border-radius:14px;padding:12px;display:flex;flex-direction:column;gap:8px;flex:1 1 480px;max-width:100%;}}
.image-caption{{font-size:13px;font-weight:600;color:#222;}}
.image-frame img.frame-image{{width:100%;max-width:var(--img-size);max-height:var(--img-size);height:auto;object-fit:contain;display:block;}}
.viewer-viewport{{position:relative;width:100%;max-width:var(--img-size);height:var(--img-size);overflow:hidden;border:1px solid #e1e4e8;border-radius:8px;background:#101827;display:flex;align-items:center;justify-content:center;touch-action:none;}}
.atlas-img{{max-width:100%;height:auto;display:block;}}
.atlas-img.selected{{outline:2px solid #1b6ef3;border-radius:6px;}}
#gridimg{{max-width:100%;max-height:100%;width:auto;height:auto;object-fit:contain;transform-origin:center center;transition:transform 0.05s linear;cursor:zoom-in;user-select:none;-webkit-user-drag:none;}}
.viewer-viewport.zoomed #gridimg{{cursor:grab;}}
.viewer-viewport.zoomed #gridimg.dragging{{cursor:grabbing;}}
.actions{{margin:8px 0;display:flex;gap:7px;flex-wrap:wrap;}}
.btn{{border:1px solid #c9d2df;background:#fff;color:#344258;border-radius:9px;padding:8px 10px;font-size:12px;font-weight:650;cursor:pointer;text-decoration:none;}}
.btn:hover{{background:#f0f2f5;}}
.btn.active{{background:#1b6ef3;color:#fff;border-color:#1b6ef3;}}
.btn.primary{{background:var(--accent);border-color:var(--accent);color:#fff;}}
.btn.nav-btn{{padding:7px 9px;}}
.btn:disabled{{opacity:0.5;cursor:default;}}
.rate-buttons{{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0;}}
.rate{{border:1px solid #c9ced6;background:#fff;border-radius:8px;padding:8px 10px;font-size:14px;cursor:pointer;min-width:38px;}}
.rate.active{{background:#1b6ef3;color:#fff;border-color:#1b6ef3;}}
.collection-choice.suitable.active{{background:#dcfce7;color:#08765a;border-color:#34d399;}}
.collection-choice.unsuitable.active{{background:#ffe4e6;color:#9f1239;border-color:#fb7185;}}
.note{{color:#555;font-size:13px;margin:6px 0;}}
.note.warn{{color:#b00020;}}
.note.ok{{color:#13653f;}}
.preflight-box{{background:#fbfcff;border:1px solid #d7deea;border-radius:10px;padding:10px;margin-bottom:14px;}}
.preflight-title{{font-size:13px;font-weight:600;margin-bottom:4px;}}
.preflight-list{{margin:0;padding-left:18px;font-size:12px;color:#455;}}
.preflight-list li{{margin:2px 0;}}
.status-card{{margin-top:0;}}
.status-log{{max-height:180px;overflow:auto;font-size:12px;color:#333;background:#fafafa;border-radius:8px;padding:8px;border:1px solid #e1e4e8;}}
.status-log div{{padding:2px 0;border-bottom:1px solid #eceff3;}}
.status-log div:last-child{{border-bottom:0;}}
.section-title{{font-size:14px;font-weight:600;margin:10px 0 6px;}}
.thumb-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(var(--thumb-size),1fr));gap:10px;}}
.thumb{{width:var(--thumb-size);height:var(--thumb-size);object-fit:contain;border-radius:6px;border:1px solid #e1e4e8;display:block;background:#fff;cursor:pointer;}}
.thumb.selected{{outline:2px solid #1b6ef3;}}
.data-card{{display:flex;flex-direction:column;gap:6px;}}
.meta{{font-size:12px;color:#444;line-height:1.2;}}
.foil-group{{border-top:1px solid #eef0f3;padding-top:10px;margin-top:10px;}}
.foil-row{{display:flex;align-items:flex-start;gap:12px;}}
.data-block{{flex:1;}}
.foil-overlay-wrap{{position:relative;width:100%;max-width:var(--img-size);line-height:0;}}
.foil-overlay-wrap #overlayimg{{width:100%;max-width:none;height:auto;}}
.foil-hover-layer{{position:absolute;inset:0;pointer-events:none;}}
.foil-hit{{position:absolute;width:30px;height:30px;transform:translate(-50%,-50%);border:2px solid rgba(255,255,255,.2);border-radius:50%;background:rgba(37,99,235,.03);pointer-events:auto;cursor:crosshair;padding:0;}}
.foil-hit:hover,.foil-hit:focus-visible{{border-color:#5eead4;background:rgba(94,234,212,.22);box-shadow:0 0 0 4px rgba(15,23,42,.42);outline:none;}}
.image-hover-preview{{position:fixed;display:none;z-index:100;width:250px;padding:8px;background:#fff;border:1px solid #cfd8e6;border-radius:12px;box-shadow:0 18px 45px rgba(10,20,38,.3);pointer-events:none;line-height:1.25;}}
.image-hover-preview.visible{{display:block;}}
.image-hover-preview img{{display:block;width:232px;height:232px;object-fit:contain;background:#101827;border-radius:8px;}}
.image-hover-label{{padding:7px 3px 2px;font-size:11px;font-weight:700;color:#334155;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}}
textarea{{width:100%;max-width:100%;border:1px solid #c9ced6;border-radius:8px;padding:8px;font-size:14px;}}
.submit-row{{margin-top:10px;}}
.atlas-placeholder{{border:1px dashed #cfd6e4;border-radius:10px;padding:10px;background:#fdfdfd;color:#445;}}
.atlas-placeholder .placeholder-title{{font-weight:600;margin-bottom:4px;}}
.atlas-placeholder code{{background:#eef1f6;padding:2px 4px;border-radius:4px;}}
.shortcut-list{{margin:4px 0 0;padding-left:18px;color:#555;font-size:12px;}}
.shortcut-list li{{margin:2px 0;}}
.autosave-state{{font-size:12px;min-height:16px;}}
#loading-overlay{{position:fixed;inset:0;background:rgba(245,246,248,0.96);display:flex;flex-direction:column;align-items:center;justify-content:center;font-size:18px;color:#111;z-index:9999;transition:opacity 0.3s;}}
#loading-overlay.hidden{{opacity:0;pointer-events:none;}}
.spinner{{width:40px;height:40px;border:4px solid #d0d7e7;border-top-color:#1b6ef3;border-radius:50%;animation:spin 0.8s linear infinite;margin-bottom:12px;}}
@keyframes spin{{to{{transform:rotate(360deg);}}}}
@media(max-width:1050px){{.layout{{grid-template-columns:1fr;}}.right{{position:static;}}}}
@media(max-width:700px){{:root{{--img-size:420px;--thumb-size:140px;}}.page{{padding:12px;}}.header{{margin:-12px -12px 14px;padding:12px;align-items:flex-start;gap:10px;}}.header-actions{{flex-wrap:wrap;justify-content:flex-end;}}.subtitle{{max-width:220px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}}}}
</style>
</head>
<body>
<div id=\"loading-overlay\"><div class=\"spinner\"></div><div>Loading images…</div></div>
<div id=\"hole-hover-preview\" class=\"image-hover-preview\" role=\"tooltip\"><img id=\"hole-hover-image\" alt=\"Data image preview\"><div id=\"hole-hover-label\" class=\"image-hover-label\"></div></div>
<div class=\"page\">
<div class=\"header\"><div><div class=\"title\">GridSquare {item['id']}</div><div class=\"subtitle\">{item['name']}</div>{category_subtitle_html}</div><div class=\"header-actions\"><a class=\"btn nav-btn\" href=\"/\">Atlas overview</a>{prev_nav_html}<div class=\"progress\" id=\"progress\">{idx + 1} / {total_len}</div>{next_nav_html}</div></div>
{overlay_banner}
{nodata_html}
{preflight_html}
<div class=\"layout\">
<div class=\"left\">
{grid_section_html}
{thumb_card_html}
</div>
<div class=\"right\">
<div class=\"card\">
<div class=\"section-title\">Atlas</div>
{atlas_html}
{atlas_note_html}
</div>
<div class=\"card status-card\">
<div class=\"section-title\">Background tasks</div>
<div id=\"status-log\" class=\"status-log\"><div>Gathering status…</div></div>
</div>
<div class=\"card\">
<div class=\"section-title\">Rating</div>
<div class=\"note\">EPU category score: {category_text}</div>
<div class=\"rate-buttons\">
<button type=\"button\" class=\"rate\" data-v=\"1\">1</button>
<button type=\"button\" class=\"rate\" data-v=\"2\">2</button>
<button type=\"button\" class=\"rate\" data-v=\"3\">3</button>
<button type=\"button\" class=\"rate\" data-v=\"4\">4</button>
<button type=\"button\" class=\"rate\" data-v=\"5\">5</button>
<button type=\"button\" id=\"skip\" class=\"btn\">Skip</button>
</div>
<div class=\"section-title\">Selected image</div>
<div id=\"selected-image\" class=\"note\">{default_label}</div>
<div class=\"actions\">
<button type=\"button\" id=\"show-jpeg\" class=\"btn\">Show PNG preview</button>
<button type=\"button\" id=\"show-mrc\" class=\"btn\">Show MRC for selected image</button>
<button type=\"button\" id=\"zoom-out\" class=\"btn\">Zoom -</button>
<button type=\"button\" id=\"zoom-in\" class=\"btn\">Zoom +</button>
<button type=\"button\" id=\"zoom-reset\" class=\"btn\">Reset zoom</button>
</div>
<div id=\"zoom-level\" class=\"note\">Zoom: 100%</div>
{grid_mrc_note}
<div class=\"note\">Viewer defaults to the Atlas when available. Click Atlas, GridSquare, FoilHole, or Data images to switch what is shown here.</div>
<div class=\"note\">Scroll over the viewer to zoom. Drag whenever zoomed; double-click to reset. No tool switching is needed.</div>
<div id=\"contrast-panel\" style=\"display:none;margin-bottom:8px;\">
<div>Low: <span id=\"lowv\">2</span>% <input type=\"range\" id=\"low\" min=\"0\" max=\"99\" value=\"2\"></div>
<div>High: <span id=\"highv\">98</span>% <input type=\"range\" id=\"high\" min=\"1\" max=\"100\" value=\"98\"></div>
</div>
<div class=\"section-title\">Keyboard shortcuts</div>
<label class=\"note\"><input type=\"checkbox\" id=\"hotkeys-enabled\" checked> Enable keyboard shortcuts</label>
<ul class=\"shortcut-list\">
<li>1-5: set rating</li>
<li>Ctrl/Cmd+Enter: submit current GridSquare</li>
</ul>
<div class=\"section-title\">Collection</div>
<div class=\"actions\"><button type=\"button\" id=\"review-suitable\" class=\"btn collection-choice suitable\">Mark suitable for collection</button><button type=\"button\" id=\"review-unsuitable\" class=\"btn collection-choice unsuitable\">Mark unsuitable for collection</button><button type=\"button\" id=\"review-clear-collection\" class=\"btn\">Clear</button></div>
<div class=\"note\">The collection decision is saved immediately and included in structured exports and PDF summaries.</div>
<div class=\"section-title\">Report</div>
<label class=\"note\"><input type=\"checkbox\" id=\"include-report\" checked> Include this GridSquare in the final report</label>
<div class=\"note\">Clear this to leave the GridSquare out of the PDF.</div>
<div>Selected rating: <span id=\"selected\">3</span></div>
<div>Comments:</div>
<textarea id=\"comment\" rows=\"4\"></textarea>
<div id=\"autosave-state\" class=\"note autosave-state\"></div>
<div class=\"submit-row\"><button type=\"button\" id=\"submit\" class=\"btn primary\">Save & continue (Ctrl+Enter)</button></div>
<div id=\"submit-status\" class=\"note\"></div>
</div>
</div>
</div>
<script>
const IDX = {idx};
const TOTAL_GRIDS = {total_len};
const NEXT_IDX = {next_idx_val};
const PREV_IDX = {prev_idx_val};
const GRID_HAS_MRC = {grid_mrc_json};
const ATLAS_HAS_MRC = {atlas_mrc_json};
const DEFAULT_KIND = {json.dumps(default_kind)};
const DEFAULT_HAS_MRC = {default_has_mrc_json};
const SESSION_STORAGE_KEY = {json.dumps(session_storage_key)};
const STORAGE_KEY = 'review_state_' + SESSION_STORAGE_KEY + '_' + IDX;
const LAST_IDX_KEY = 'last_idx_' + SESSION_STORAGE_KEY;
const HOTKEYS_KEY = 'hotkeys_enabled_' + SESSION_STORAGE_KEY;
const FOIL_HOVER_MARKERS = {foil_hover_markers_json};
localStorage.setItem(LAST_IDX_KEY, IDX);
const commentEl = document.getElementById('comment');
const includeEl = document.getElementById('include-report');
let collectionStatus = '';
const hotkeysEl = document.getElementById('hotkeys-enabled');
const autosaveEl = document.getElementById('autosave-state');
let rating = 3;
let selectedKind = DEFAULT_KIND;
let selectedName = '';
let selectedHasMrc = DEFAULT_HAS_MRC;
let zoomLevel = 1.0;
let panX = 0;
let panY = 0;
let isDragging = false;
let dragStartX = 0;
let dragStartY = 0;
let dragPointerStartX = 0;
let dragPointerStartY = 0;
let suppressNextGridClick = false;
let allowPersist = false;
let hotkeysEnabled = true;
let persistTimer = null;
let saveInFlight = false;
let pendingSave = false;
function hideLoading(){{
  const overlay = document.getElementById('loading-overlay');
  overlay.classList.add('hidden');
  setTimeout(()=>overlay.remove(),300);
}}
window.addEventListener('load', hideLoading);
function setRating(v){{
  rating = v;
  document.getElementById('selected').textContent = String(v);
  document.querySelectorAll('.rate').forEach(b=>b.classList.toggle('active', parseInt(b.dataset.v) === v));
  if (allowPersist) persistState();
}}
async function submitReview(){{
  const statusEl = document.getElementById('submit-status');
  statusEl.textContent = 'Submitting...';
  if (persistTimer) {{
    clearTimeout(persistTimer);
    persistTimer = null;
  }}
  try {{
    const payload = {{idx: IDX, rating: rating, comment: document.getElementById('comment').value, include: document.getElementById('include-report').checked, collection_status: collectionStatus, collect: collectionStatus === 'suitable'}};
    const res = await fetch('/submit', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify(payload)}});
    const text = await res.text();
    if (!res.ok) {{
      statusEl.textContent = 'Submit failed: ' + res.status;
      alert(text);
      return;
    }}
    let data;
    try {{
      data = JSON.parse(text);
    }} catch (e) {{
      statusEl.textContent = 'Submit failed: bad response';
      alert(text);
      return;
    }}
    localStorage.removeItem(STORAGE_KEY);
    setAutosaveState('');
    if (data.next === null) {{ window.location = '/done'; }}
    else {{ window.location = '/review/' + data.next; }}
  }} catch (e) {{
    statusEl.textContent = 'Submit failed';
    alert(String(e));
  }}
}}
Array.from(document.querySelectorAll('.rate')).forEach(b=>{{ b.onclick = () => setRating(parseInt(b.dataset.v)); }});
document.getElementById('skip').onclick = () => {{ rating = 0; submitReview(); }};
setRating(3);
function jpgUrl(kind,name){{
  return '/preview.png?idx=' + IDX + '&kind=' + kind + (name ? '&name=' + encodeURIComponent(name) : '') + '&size=1600&t=' + Date.now();
}}
function mrcUrl(){{
  const low = document.getElementById('low').value;
  const high = document.getElementById('high').value;
  return '/mrc_file?idx=' + IDX + '&kind=' + selectedKind + '&name=' + encodeURIComponent(selectedName) + '&low=' + low + '&high=' + high + '&t=' + Date.now();
}}
function updateButtons(){{
  document.getElementById('show-mrc').disabled = !selectedHasMrc;
}}
function clampPan(){{
  const img = document.getElementById('gridimg');
  const viewport = document.getElementById('viewer-viewport');
  if (!img || !viewport) return;
  const baseW = img.clientWidth || 0;
  const baseH = img.clientHeight || 0;
  if (baseW <= 0 || baseH <= 0) {{
    panX = 0;
    panY = 0;
    return;
  }}
  const scaledW = baseW * zoomLevel;
  const scaledH = baseH * zoomLevel;
  const maxPanX = Math.max(0, (scaledW - viewport.clientWidth) / 2);
  const maxPanY = Math.max(0, (scaledH - viewport.clientHeight) / 2);
  panX = Math.max(-maxPanX, Math.min(maxPanX, panX));
  panY = Math.max(-maxPanY, Math.min(maxPanY, panY));
}}
function applyZoom(){{
  const img = document.getElementById('gridimg');
  const viewport = document.getElementById('viewer-viewport');
  clampPan();
  img.style.transform = 'translate(' + panX.toFixed(1) + 'px,' + panY.toFixed(1) + 'px) scale(' + zoomLevel.toFixed(3) + ')';
  document.getElementById('zoom-level').textContent = 'Zoom: ' + Math.round(zoomLevel * 100) + '%';
  if (viewport) viewport.classList.toggle('zoomed', zoomLevel > 1.001);
}}
function setZoom(value, clientX=null, clientY=null){{
  const next = Math.max(0.5, Math.min(4.0, value));
  const ratio = zoomLevel > 0 ? (next / zoomLevel) : 1.0;
  if (clientX !== null && clientY !== null) {{
    const viewport = document.getElementById('viewer-viewport');
    const rect = viewport.getBoundingClientRect();
    const pointerX = clientX - rect.left - rect.width / 2;
    const pointerY = clientY - rect.top - rect.height / 2;
    panX = pointerX - (pointerX - panX) * ratio;
    panY = pointerY - (pointerY - panY) * ratio;
  }} else {{
    panX *= ratio;
    panY *= ratio;
  }}
  zoomLevel = next;
  if (zoomLevel <= 1.0) {{
    panX = 0;
    panY = 0;
  }}
  applyZoom();
}}
function resetViewerTransform(){{
  zoomLevel = 1.0;
  panX = 0;
  panY = 0;
  applyZoom();
}}
function selectionLabel(kind,name){{
  if (kind === 'grid') return 'GridSquare';
  if (kind === 'atlas') return 'Atlas';
  if (kind === 'foil') return name ? ('FoilHole: ' + name) : 'FoilHole';
  if (kind === 'data') return name ? ('Data image: ' + name) : 'Data image';
  return name ? (kind + ': ' + name) : kind;
}}
function selectImage(kind,name,hasMrc){{
  selectedKind = kind;
  selectedName = name || '';
  selectedHasMrc = !!hasMrc;
  const label = selectionLabel(kind, name);
  document.getElementById('selected-image').textContent = label;
  const viewerCaption = document.getElementById('viewer-caption');
  if (viewerCaption) {{
    viewerCaption.textContent = 'Viewer: ' + label + ' (last clicked image)';
  }}
  document.getElementById('gridimg').src = jpgUrl(kind,name);
  document.getElementById('contrast-panel').style.display = 'none';
  resetViewerTransform();
  updateButtons();
  document.querySelectorAll('.thumb').forEach(t=>t.classList.toggle('selected', t.dataset.kind === kind && t.dataset.name === name));
  const atlasImg = document.getElementById('atlasimg');
  if (atlasImg) {{
    atlasImg.classList.toggle('selected', kind === 'atlas');
  }}
}}
Array.from(document.querySelectorAll('.thumb')).forEach(t=>{{
  t.onclick = () => selectImage(t.dataset.kind, t.dataset.name, t.dataset.hasMrc === '1');
}});
const holeHoverPreview = document.getElementById('hole-hover-preview');
const holeHoverImage = document.getElementById('hole-hover-image');
const holeHoverLabel = document.getElementById('hole-hover-label');
function positionHolePreview(event){{
  if (!holeHoverPreview) return;
  const gap = 16;
  const width = 250;
  const height = 282;
  let left = event.clientX + gap;
  let top = event.clientY + gap;
  if (left + width > window.innerWidth - 8) left = event.clientX - width - gap;
  if (top + height > window.innerHeight - 8) top = window.innerHeight - height - 8;
  holeHoverPreview.style.left = Math.max(8, left) + 'px';
  holeHoverPreview.style.top = Math.max(8, top) + 'px';
}}
function showHolePreview(src,label,event){{
  if (!src || !holeHoverPreview) return;
  holeHoverImage.src = src;
  holeHoverLabel.textContent = label || 'Data image preview';
  holeHoverPreview.classList.add('visible');
  positionHolePreview(event);
}}
function hideHolePreview(){{if (holeHoverPreview) holeHoverPreview.classList.remove('visible');}}
function bindHoleHover(element,src,label){{
  if (!element || !src) return;
  element.addEventListener('pointerenter',event=>showHolePreview(src,label,event));
  element.addEventListener('pointermove',positionHolePreview);
  element.addEventListener('pointerleave',hideHolePreview);
}}
const foilHoverLayer = document.getElementById('foil-hover-layer');
if (foilHoverLayer){{
  FOIL_HOVER_MARKERS.filter(marker=>marker.preview).forEach(marker=>{{
    const hotspot = document.createElement('button');
    hotspot.type = 'button';
    hotspot.className = 'foil-hit';
    hotspot.style.left = marker.x + '%';
    hotspot.style.top = marker.y + '%';
    hotspot.setAttribute('aria-label','Preview Data image for FoilHole ' + marker.foil_id);
    bindHoleHover(hotspot,marker.preview,'FoilHole ' + marker.foil_id + ' · Data preview');
    hotspot.onclick = ()=>selectImage('data',marker.data_name,Boolean(marker.data_has_mrc));
    foilHoverLayer.appendChild(hotspot);
  }});
}}
document.querySelectorAll('.foil-thumb[data-hover-preview]').forEach(thumb=>{{
  bindHoleHover(thumb,thumb.dataset.hoverPreview,thumb.dataset.hoverLabel);
}});
const viewerImg = document.getElementById('gridimg');
viewerImg.onclick = (e) => {{
  if (suppressNextGridClick) {{
    suppressNextGridClick = false;
    e.preventDefault();
    return;
  }}
  if (selectedKind !== 'grid') {{
    selectImage('grid','',GRID_HAS_MRC);
  }}
}};
viewerImg.addEventListener('load', () => {{
  if (zoomLevel <= 1.0) {{
    panX = 0;
    panY = 0;
  }}
  applyZoom();
}});
viewerImg.onpointerdown = (e) => {{
  if (zoomLevel <= 1.0 || e.button !== 0) return;
  isDragging = true;
  suppressNextGridClick = false;
  dragStartX = e.clientX - panX;
  dragStartY = e.clientY - panY;
  dragPointerStartX = e.clientX;
  dragPointerStartY = e.clientY;
  viewerImg.classList.add('dragging');
  try {{ viewerImg.setPointerCapture(e.pointerId); }} catch (_err) {{}}
  e.preventDefault();
}};
viewerImg.onpointermove = (e) => {{
  if (!isDragging) return;
  panX = e.clientX - dragStartX;
  panY = e.clientY - dragStartY;
  if (Math.abs(e.clientX - dragPointerStartX) > 2 || Math.abs(e.clientY - dragPointerStartY) > 2) {{
    suppressNextGridClick = true;
  }}
  applyZoom();
}};
function stopPanDrag(e){{
  if (!isDragging) return;
  isDragging = false;
  viewerImg.classList.remove('dragging');
  try {{
    if (viewerImg.hasPointerCapture(e.pointerId)) {{
      viewerImg.releasePointerCapture(e.pointerId);
    }}
  }} catch (_err) {{}}
}}
viewerImg.onpointerup = stopPanDrag;
viewerImg.onpointercancel = stopPanDrag;
document.getElementById('viewer-viewport').addEventListener('wheel', (e) => {{
  e.preventDefault();
  setZoom(zoomLevel * Math.exp(-e.deltaY * 0.0015), e.clientX, e.clientY);
}}, {{passive:false}});
document.getElementById('viewer-viewport').addEventListener('dblclick', (e) => {{
  e.preventDefault();
  resetViewerTransform();
}});
const atlasImg = document.getElementById('atlasimg');
if (atlasImg) {{
  atlasImg.onclick = () => selectImage('atlas', '', ATLAS_HAS_MRC);
}}
if (DEFAULT_KIND === 'atlas' && atlasImg) {{
  atlasImg.classList.add('selected');
}}
function updateContrast(){{
  const lowEl = document.getElementById('low');
  const highEl = document.getElementById('high');
  let low = parseInt(lowEl.value);
  let high = parseInt(highEl.value);
  if (low >= high) {{
    if (low > 0) {{ low = high - 1; lowEl.value = String(low); }}
    else {{ high = low + 1; highEl.value = String(high); }}
  }}
  document.getElementById('lowv').textContent = String(low);
  document.getElementById('highv').textContent = String(high);
  document.getElementById('gridimg').src = mrcUrl();
}}
document.getElementById('show-mrc').onclick = () => {{
  if (!selectedHasMrc) return;
  document.getElementById('contrast-panel').style.display = 'block';
  updateContrast();
}};
document.getElementById('show-jpeg').onclick = () => {{
  document.getElementById('gridimg').src = jpgUrl(selectedKind, selectedName);
}};
document.getElementById('zoom-in').onclick = () => setZoom(zoomLevel * 1.25);
document.getElementById('zoom-out').onclick = () => setZoom(zoomLevel / 1.25);
document.getElementById('zoom-reset').onclick = () => resetViewerTransform();
function persistState(){{
  const nowTs = Date.now() / 1000.0;
  const data = {{
    rating,
    comment: commentEl.value,
    include: includeEl.checked,
    collection_status: collectionStatus,
    collect: collectionStatus === 'suitable',
    updated_at: nowTs
  }};
  localStorage.setItem(STORAGE_KEY, JSON.stringify(data));
  queueServerPersist();
}}
function setAutosaveState(message, isError=false){{
  if (!autosaveEl) return;
  autosaveEl.textContent = message || '';
  autosaveEl.classList.toggle('warn', !!isError);
  autosaveEl.classList.toggle('ok', !isError && !!message);
}}
async function saveDraftToServer(){{
  if (saveInFlight) {{
    pendingSave = true;
    return;
  }}
  saveInFlight = true;
  setAutosaveState('Saving draft…');
  try {{
    const payload = {{
      idx: IDX,
      rating,
      comment: commentEl.value,
      include: includeEl.checked,
      collection_status: collectionStatus,
      collect: collectionStatus === 'suitable',
      updated_at: Date.now() / 1000.0
    }};
    const res = await fetch('/draft', {{
      method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify(payload)
    }});
    if (!res.ok) {{
      throw new Error('HTTP ' + res.status);
    }}
    const data = await res.json();
    if (data && data.draft) {{
      localStorage.setItem(STORAGE_KEY, JSON.stringify(data.draft));
    }}
    setAutosaveState('Draft saved');
  }} catch (_err) {{
    setAutosaveState('Draft save failed (local cache kept).', true);
  }} finally {{
    saveInFlight = false;
    if (pendingSave) {{
      pendingSave = false;
      queueServerPersist();
    }}
  }}
}}
function queueServerPersist(){{
  if (!allowPersist) return;
  if (persistTimer) clearTimeout(persistTimer);
  persistTimer = setTimeout(() => {{
    persistTimer = null;
    saveDraftToServer();
  }}, 500);
}}
function applyState(data){{
  if (!data || typeof data !== 'object') return;
  if (typeof data.comment === 'string') commentEl.value = data.comment;
  if (typeof data.include === 'boolean') includeEl.checked = data.include;
  if (typeof data.collection_status === 'string') setReviewCollectionStatus(data.collection_status, false);
  else if (data.collect === true) setReviewCollectionStatus('suitable', false);
  if (typeof data.rating === 'number' && data.rating >= 1 && data.rating <= 5) setRating(data.rating);
}}
function restoreLocalState(){{
  const saved = localStorage.getItem(STORAGE_KEY);
  if (!saved) return null;
  try {{
    const data = JSON.parse(saved);
    applyState(data);
    return data;
  }} catch (_err) {{
    return null;
  }}
}}
async function restoreServerState(localData){{
  let localTs = 0;
  try {{
    localTs = localData && typeof localData.updated_at === 'number' ? localData.updated_at : 0;
  }} catch (_err) {{}}
  try {{
    const res = await fetch('/draft?idx=' + IDX + '&t=' + Date.now());
    if (!res.ok) return;
    const payload = await res.json();
    if (!payload || !payload.draft) return;
    const remote = payload.draft;
    const remoteTs = typeof remote.updated_at === 'number' ? remote.updated_at : 0;
    if (!localData || remoteTs >= localTs) {{
      applyState(remote);
      localStorage.setItem(STORAGE_KEY, JSON.stringify(remote));
    }}
  }} catch (_err) {{}}
}}
function initHotkeysSetting(){{
  const saved = localStorage.getItem(HOTKEYS_KEY);
  hotkeysEnabled = saved !== '0';
  hotkeysEl.checked = hotkeysEnabled;
  hotkeysEl.addEventListener('change', () => {{
    hotkeysEnabled = !!hotkeysEl.checked;
    localStorage.setItem(HOTKEYS_KEY, hotkeysEnabled ? '1' : '0');
  }});
}}
initHotkeysSetting();
(async () => {{
  const localDraft = restoreLocalState();
  if (localDraft) setAutosaveState('Draft restored.');
  await restoreServerState(localDraft);
  allowPersist = true;
}})();
commentEl.addEventListener('input', persistState);
includeEl.addEventListener('change', persistState);
function setReviewCollectionStatus(status, save=true){{
  collectionStatus = status === 'suitable' || status === 'unsuitable' ? status : '';
  document.getElementById('review-suitable').classList.toggle('active', collectionStatus === 'suitable');
  document.getElementById('review-unsuitable').classList.toggle('active', collectionStatus === 'unsuitable');
  if (save) persistState();
}}
document.getElementById('review-suitable').onclick = () => setReviewCollectionStatus('suitable');
document.getElementById('review-unsuitable').onclick = () => setReviewCollectionStatus('unsuitable');
document.getElementById('review-clear-collection').onclick = () => setReviewCollectionStatus('');
document.getElementById('low').oninput = updateContrast;
document.getElementById('high').oninput = updateContrast;
updateButtons();
applyZoom();
document.getElementById('submit').onclick = submitReview;
document.addEventListener('keydown', (e)=>{{
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {{
    e.preventDefault();
    submitReview();
    return;
  }}
  if (!hotkeysEnabled) return;
  const activeEl = document.activeElement;
  if (activeEl) {{
    const tagName = (activeEl.tagName || '').toLowerCase();
    if (tagName === 'textarea' || tagName === 'input') return;
  }}
  if (e.key >= '1' && e.key <= '5') {{ setRating(parseInt(e.key)); }}
}});
async function refreshStatus(){{
  try {{
    const res = await fetch('/status?t=' + Date.now());
    if (!res.ok) return;
    const data = await res.json();
    const logEl = document.getElementById('status-log');
    if (data.events && data.events.length) {{
      logEl.innerHTML = data.events.map(ev => '<div>' + new Date(ev.ts * 1000).toLocaleTimeString() + ' — ' + ev.message + '</div>').join('');
    }} else {{
      logEl.innerHTML = '<div>Idle</div>';
    }}
    if (typeof data.total === 'number' && typeof data.loaded === 'number') {{
      document.getElementById('progress').textContent = (IDX + 1) + ' / ' + data.total;
    }}
  }} catch (e) {{}}
}}
refreshStatus();
setInterval(refreshStatus, 5000);
</script>
</body></html>"""

    @app.get("/")
    def root():
        grid_summaries = _dashboard_grid_summaries()
        unscreened_summaries = _unscreened_atlas_summaries()
        reviewed_count = sum(1 for entry in grid_summaries if entry["reviewed"])
        mapped_count = sum(1 for entry in grid_summaries if entry["position"])
        image_count = sum(1 + entry["foil_count"] + entry["data_count"] for entry in grid_summaries)
        atlas_source_text = atlas_preview_path.name if atlas_preview_path else "No atlas selected"
        root_html = """<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>EPU Mapper dashboard</title>
<style>
:root{color-scheme:light;--ink:#172033;--muted:#68758b;--line:#dfe5ee;--soft:#f4f7fb;--panel:#fff;--nav:#101a2f;--nav-muted:#aeb9cc;--brand:#5eead4;--accent:#2563eb;--accent-dark:#1d4ed8;--good:#0f9f74;--warn:#b45309;--shadow:0 10px 30px rgba(26,39,67,.08);}
*{box-sizing:border-box}body{margin:0;font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:var(--soft);color:var(--ink)}button,input{font:inherit}.shell{min-height:100vh;display:grid;grid-template-columns:236px minmax(0,1fr)}
.sidebar{position:sticky;top:0;height:100vh;background:var(--nav);color:#fff;padding:24px 18px;display:flex;flex-direction:column;z-index:5}.brand{display:flex;align-items:center;gap:11px;font-size:17px;font-weight:750;letter-spacing:-.02em;margin:0 8px 28px}.brand-mark{width:32px;height:32px;border-radius:9px;background:linear-gradient(145deg,#5eead4,#3b82f6);display:grid;place-items:center;color:#10213b;font-weight:900}.nav-label{font-size:10px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:#71809a;margin:15px 10px 7px}.nav-item{display:flex;align-items:center;gap:10px;color:var(--nav-muted);text-decoration:none;padding:10px;border-radius:9px;font-size:13px;margin:2px 0}.nav-item.active{background:#1c2943;color:#fff}.nav-dot{width:7px;height:7px;border-radius:50%;background:#53617a}.nav-item.active .nav-dot{background:var(--brand);box-shadow:0 0 0 4px rgba(94,234,212,.12)}.session-health{margin-top:auto;border-top:1px solid #27344d;padding:16px 8px 0}.health-row{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--nav-muted)}.health-light{width:8px;height:8px;border-radius:50%;background:#f59e0b}.health-light.ok{background:#34d399}.health-light.err{background:#fb7185}.session-name{font-size:11px;color:#76859e;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:8px}
.main{min-width:0}.topbar{height:70px;background:rgba(255,255,255,.94);border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;padding:0 30px;position:sticky;top:0;z-index:4;backdrop-filter:blur(10px)}.top-title{font-size:17px;font-weight:700;letter-spacing:-.015em}.top-subtitle{font-size:12px;color:var(--muted);margin-top:3px}.top-actions{display:flex;gap:9px}.button{border:1px solid #cfd7e4;background:#fff;color:#314059;border-radius:9px;padding:9px 13px;font-size:12px;font-weight:650;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;justify-content:center;gap:7px}.button:hover{border-color:#aeb9ca;background:#f9fbfd}.button.primary{background:var(--accent);border-color:var(--accent);color:#fff}.button.primary:hover{background:var(--accent-dark)}
.content{padding:24px 30px 40px;max-width:1800px;margin:0 auto}.stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-bottom:16px}.stat{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:13px 16px;display:flex;align-items:baseline;justify-content:space-between}.stat-value{font-size:22px;font-weight:750;letter-spacing:-.04em}.stat-label{font-size:11px;color:var(--muted);font-weight:650}.workspace{display:grid;grid-template-columns:minmax(0,1.55fr) minmax(350px,.75fr);gap:16px;align-items:start}.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;box-shadow:0 1px 2px rgba(24,36,60,.03);overflow:hidden}.panel-head{min-height:57px;padding:13px 16px;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;gap:12px}.panel-title{font-size:13px;font-weight:750}.panel-note{font-size:11px;color:var(--muted);margin-top:3px}.segmented{display:flex;padding:3px;background:#eef2f7;border-radius:9px}.segmented button{border:0;background:transparent;color:#66748a;border-radius:7px;padding:7px 9px;font-size:11px;font-weight:650;cursor:pointer}.segmented button.active{background:#fff;color:#1c2940;box-shadow:0 1px 3px rgba(18,31,53,.12)}
.atlas-viewport{height:clamp(500px,62vh,760px);background:#111827;overflow:hidden;position:relative;touch-action:none;cursor:grab}.atlas-viewport.dragging{cursor:grabbing}.atlas-content{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;transform-origin:center center;will-change:transform}.atlas-image-wrap{position:relative;display:inline-block;line-height:0;max-width:100%;max-height:100%}.atlas-image{display:block;max-width:100%;max-height:clamp(500px,62vh,760px);width:auto;height:auto;user-select:none;-webkit-user-drag:none}.marker-layer{position:absolute;inset:0;pointer-events:none}.grid-marker{position:absolute;width:28px;height:28px;transform:translate(-50%,-50%);border-radius:50%;border:2px solid rgba(255,255,255,.9);background:#2563eb;color:#fff;font-size:10px;font-weight:800;line-height:1;cursor:pointer;pointer-events:auto;box-shadow:0 3px 10px rgba(0,0,0,.4);transition:transform .12s,background .12s}.grid-marker:hover,.grid-marker.active{transform:translate(-50%,-50%) scale(1.18);background:#0f9f74;z-index:2}.grid-marker.reviewed{background:#0f9f74}.grid-marker.excluded{background:#64748b}.grid-marker.collection{background:#d97706;box-shadow:0 0 0 4px rgba(245,158,11,.3),0 3px 10px rgba(0,0,0,.4)}.atlas-empty{height:100%;display:grid;place-items:center;color:#cbd5e1;text-align:center;padding:30px}.atlas-tools{position:absolute;right:12px;bottom:12px;display:flex;align-items:center;gap:5px;background:rgba(13,22,38,.86);padding:5px;border-radius:9px;color:#d8e0ec}.atlas-tools button{width:31px;height:31px;border:0;border-radius:7px;background:transparent;color:#fff;font-size:16px;cursor:pointer}.atlas-tools button:hover{background:rgba(255,255,255,.12)}.atlas-zoom{font-size:10px;min-width:40px;text-align:center}.atlas-help{position:absolute;left:12px;bottom:12px;background:rgba(13,22,38,.78);color:#d8e0ec;font-size:10px;padding:7px 9px;border-radius:7px;pointer-events:none}
.inspector{min-height:620px}.inspector-empty{min-height:520px;display:grid;place-items:center;text-align:center;color:var(--muted);padding:42px}.empty-target{width:48px;height:48px;border-radius:50%;border:1px dashed #9cabc0;margin:0 auto 12px;display:grid;place-items:center;color:#789}.inspector-body{display:none}.inspector-body.visible{display:block}.selected-summary{padding:15px 16px;border-bottom:1px solid var(--line);display:flex;align-items:flex-start;justify-content:space-between;gap:12px}.selected-title{font-size:17px;font-weight:760;letter-spacing:-.025em}.selected-file{font-size:10px;color:var(--muted);margin-top:4px;max-width:270px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.badges{display:flex;gap:5px;flex-wrap:wrap;margin-top:9px}.badge{font-size:10px;font-weight:650;color:#4f5f76;background:#edf2f7;padding:4px 7px;border-radius:999px}.inspector-gallery{padding:12px;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px;max-height:540px;overflow:auto}.gallery-item{border:1px solid var(--line);background:#fff;border-radius:10px;overflow:hidden;padding:0;cursor:pointer;text-align:left;color:var(--ink)}.gallery-item:hover{border-color:#94a3b8}.gallery-item img{width:100%;aspect-ratio:1/1;object-fit:contain;background:#101827;display:block}.gallery-caption{padding:8px;font-size:10px;font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.gallery-meta{padding:0 8px 8px;color:var(--muted);font-size:9px;line-height:1.35}.inspector-actions{padding:12px 16px;border-top:1px solid var(--line);display:flex;gap:8px}.inspector-actions .button{flex:1}
.workspace{display:flex;flex-direction:column;gap:16px}.inspector{min-height:0}.inspector-empty{min-height:260px}.selected-detail-layout{display:grid;grid-template-columns:minmax(0,1.55fr) minmax(320px,.65fr);gap:16px;padding:16px}.selected-media-card,.review-controls,.comparison-panel{border:1px solid var(--line);border-radius:12px;background:#fbfcfe;padding:12px}.selected-grid-image{display:block;width:100%;max-height:760px;object-fit:contain;background:#101827;border-radius:10px}.media-actions{display:flex;gap:8px;margin-top:10px}.selected-overlay-wrap{position:relative;margin:14px auto 0;width:min(100%,760px);line-height:0}.selected-overlay-image{display:block;width:100%;height:auto;background:#101827;border-radius:10px}.dashboard-hole-layer{position:absolute;inset:0;pointer-events:none}.dashboard-hole-hit{position:absolute;width:34px;height:34px;transform:translate(-50%,-50%);border:2px solid rgba(255,255,255,.28);border-radius:50%;background:rgba(37,99,235,.04);pointer-events:auto;cursor:crosshair}.dashboard-hole-hit:hover,.dashboard-hole-hit.active{border-color:#5eead4;background:rgba(94,234,212,.25);box-shadow:0 0 0 4px rgba(15,23,42,.42)}.review-controls{align-self:start;position:sticky;top:88px}.control-title{font-size:12px;font-weight:750;margin:14px 0 7px}.dashboard-ratings,.collection-decisions{display:flex;gap:7px;flex-wrap:wrap}.dashboard-rating{width:39px;height:36px;border:1px solid #cbd5e1;border-radius:8px;background:#fff;cursor:pointer}.dashboard-rating.active{background:#2563eb;color:#fff;border-color:#2563eb}.collection-decision.suitable.active{background:#dcfce7;color:#08765a;border-color:#34d399}.collection-decision.unsuitable.active{background:#ffe4e6;color:#9f1239;border-color:#fb7185}.dashboard-comment{width:100%;min-height:120px;border:1px solid #cbd5e1;border-radius:9px;padding:9px;resize:vertical}.save-state{font-size:11px;color:var(--muted);min-height:18px;margin-top:8px}.hole-comparison{display:none;padding:0 16px 16px}.hole-comparison.visible{display:block}.comparison-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.comparison-panel img{display:block;width:100%;height:clamp(360px,44vw,680px);object-fit:contain;background:#101827;border-radius:9px}.comparison-label{font-size:12px;font-weight:750;margin-bottom:8px}.comparison-meta{font-size:10px;color:var(--muted);margin-top:7px}.grid-marker.unsuitable{background:#dc2626}.grid-card.unsuitable .grid-card-index{background:#ffe4e6;color:#9f1239}
.browser-panel{margin-top:16px}.browser-head{display:flex;align-items:center;justify-content:space-between;gap:12px}.search{width:230px;border:1px solid #d3dbe7;border-radius:8px;padding:8px 10px;font-size:11px;outline:none}.search:focus{border-color:#5b8def;box-shadow:0 0 0 3px rgba(37,99,235,.08)}.grid-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:9px;padding:12px;max-height:300px;overflow:auto}.grid-card{border:1px solid var(--line);border-radius:10px;background:#fff;padding:10px;display:flex;align-items:center;gap:9px;cursor:pointer;text-align:left;color:var(--ink)}.grid-card:hover,.grid-card.active{border-color:#6b93e7;background:#f7faff}.grid-card-index{width:29px;height:29px;flex:0 0 auto;border-radius:8px;background:#eaf0fb;color:#2856ad;display:grid;place-items:center;font-size:10px;font-weight:800}.grid-card.reviewed .grid-card-index{background:#daf5ec;color:#08765a}.grid-card.collection .grid-card-index{background:#fff0d5;color:#a84f05}.grid-card-title{font-size:11px;font-weight:700}.grid-card-meta{font-size:9px;color:var(--muted);margin-top:3px}.button.collection.active{border-color:#d97706;background:#fff7e8;color:#9a4b08}.preflight-pop{display:none;margin:0 30px 16px;padding:10px 13px;border:1px solid #f1d19b;background:#fff9ed;color:#85530d;border-radius:10px;font-size:11px}.preflight-pop.show{display:block}.preflight-pop.err{border-color:#f3b8c1;background:#fff4f5;color:#9f1239}
.lightbox{position:fixed;inset:0;background:rgba(5,10,20,.88);z-index:20;display:none;align-items:center;justify-content:center;padding:30px}.lightbox.open{display:flex}.lightbox img{max-width:92vw;max-height:88vh;object-fit:contain}.lightbox-close{position:absolute;right:22px;top:18px;border:0;background:rgba(255,255,255,.12);color:#fff;border-radius:8px;width:38px;height:38px;font-size:20px;cursor:pointer}
.grid-hover-preview{position:fixed;display:none;z-index:15;width:min(560px,calc(100vw - 24px));padding:9px;background:#fff;border:1px solid #cfd8e6;border-radius:14px;box-shadow:0 18px 45px rgba(7,15,30,.34);pointer-events:none}.grid-hover-preview.visible{display:block}.grid-hover-preview img{display:block;width:542px;max-width:100%;height:min(542px,calc(100vh - 100px));object-fit:contain;background:#101827;border-radius:9px}.grid-hover-caption{padding:8px 3px 2px;font-size:12px;font-weight:750;color:#334155;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.portable-dialog{width:min(560px,calc(100vw - 32px));border:1px solid var(--line);border-radius:14px;padding:0;color:var(--ink);box-shadow:0 24px 70px rgba(15,23,42,.28)}.portable-dialog::backdrop{background:rgba(15,23,42,.46);backdrop-filter:blur(2px)}.portable-dialog-head{display:flex;align-items:center;justify-content:space-between;padding:16px 18px;border-bottom:1px solid var(--line)}.portable-dialog-head h2{font-size:16px;margin:0}.portable-dialog-close{border:0;background:transparent;color:var(--muted);font-size:22px;cursor:pointer}.portable-dialog-body{padding:18px}.portable-dialog-body label{display:block;font-size:11px;font-weight:700;margin-bottom:7px}.portable-destination{width:100%;padding:10px 11px;border:1px solid #cfd7e4;border-radius:9px;font-size:12px}.portable-warning{margin-top:10px;color:var(--muted);font-size:11px;line-height:1.45}.portable-status{display:none;margin-top:14px;padding:11px;border-radius:9px;background:#f1f5f9;color:#475569;font-size:11px;line-height:1.45;white-space:pre-wrap;overflow-wrap:anywhere}.portable-status.visible{display:block}.portable-status.error{background:#fff1f2;color:#9f1239}.portable-progress{height:5px;background:#dbe3ee;border-radius:99px;margin-top:9px;overflow:hidden}.portable-progress span{display:block;height:100%;width:0;background:var(--accent);transition:width .2s}.portable-dialog-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:16px}
.selected-detail-layout{grid-template-columns:minmax(0,1fr) minmax(320px,.34fr)}.selected-visual-workspace{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(340px,.7fr);gap:16px;min-width:0}.selected-grid-image,#selected-grid-png,#selected-grid-mrc{display:none}.selected-overlay-wrap{width:100%;margin-top:9px}.inline-rating-panel{border-top:1px solid var(--line);margin-top:14px;padding-top:12px}.inline-rating-panel .control-title{margin-top:0}.hole-comparison{display:block;padding:0;min-width:0}.hole-comparison .panel-head{padding:0 0 10px;border-bottom:0}.comparison-grid{grid-template-columns:1fr}.comparison-panel img{height:clamp(280px,28vw,460px)}.comparison-panel.data-panel img{height:clamp(320px,33vw,540px)}.hole-preview-placeholder{min-height:220px;display:grid;place-items:center;text-align:center;color:var(--muted);border:1px dashed #cbd5e1;border-radius:10px;padding:20px;background:#fff}.hole-comparison.has-selection .hole-preview-placeholder{display:none}.comparison-panel{display:none}.hole-comparison.has-selection .comparison-panel{display:block}.review-image-viewport{position:relative;width:100%;height:clamp(280px,28vw,460px);overflow:hidden;background:#101827;border-radius:9px;touch-action:none;cursor:default}.data-panel .review-image-viewport{height:clamp(320px,33vw,540px)}.review-image-viewport.zoomable{cursor:grab}.review-image-viewport.dragging{cursor:grabbing}.review-image-viewport img{width:100%;height:100%;object-fit:contain;transform-origin:center center;will-change:transform}.mrc-viewer-controls{display:none;margin-top:9px;padding:9px;border:1px solid #dbe3ee;border-radius:9px;background:#fff}.mrc-viewer-controls.visible{display:block}.mrc-control-row{display:flex;align-items:center;gap:7px;flex-wrap:wrap}.mrc-control-row+.mrc-control-row{margin-top:7px}.mrc-control-row label{font-size:10px;color:var(--muted);display:flex;align-items:center;gap:5px;flex:1;min-width:140px}.mrc-control-row input[type=range]{min-width:90px;flex:1}.mrc-zoom-value{font-size:10px;color:var(--muted);min-width:42px;text-align:center}.atlas-mrc-contrast{display:none;position:absolute;right:12px;bottom:58px;width:min(360px,calc(100% - 24px));padding:9px;background:rgba(13,22,38,.93);border-radius:9px;color:#fff}.atlas-mrc-contrast.visible{display:block}.atlas-mrc-contrast label{display:flex;align-items:center;gap:7px;font-size:10px}.atlas-mrc-contrast label+label{margin-top:7px}.atlas-mrc-contrast input{flex:1}.grid-marker.no-data{background:#475569;border-color:#fbbf24;box-shadow:0 0 0 4px rgba(251,191,36,.35),0 3px 10px rgba(0,0,0,.4)}.grid-marker.no-data::after{content:'!';position:absolute;right:-7px;top:-9px;width:16px;height:16px;border-radius:50%;display:grid;place-items:center;background:#fbbf24;color:#422006;font-size:10px;font-weight:900;border:1px solid #fff}.grid-card.no-data{border-color:#f2c96d;background:#fffbeb}.grid-card.no-data .grid-card-index{background:#fef3c7;color:#92400e}
@media(max-width:1100px){.shell{grid-template-columns:76px minmax(0,1fr)}.sidebar{padding:22px 12px}.brand{margin:0 auto 28px}.brand span:last-child,.nav-item span:last-child,.nav-label,.session-health{display:none}.nav-item{justify-content:center}.selected-detail-layout{grid-template-columns:1fr}.review-controls{position:static}.inspector{min-height:0}.inspector-empty{min-height:220px}.inspector-gallery{max-height:none;grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:1250px){.selected-visual-workspace{grid-template-columns:1fr}}
@media(max-width:720px){.shell{display:block}.sidebar{display:none}.topbar{padding:0 15px}.content{padding:15px}.stats{grid-template-columns:1fr}.top-subtitle{display:none}.workspace{display:block}.inspector{margin-top:12px}.atlas-viewport{height:56vh}.atlas-image{max-height:56vh}.selected-detail-layout{padding:10px}.comparison-grid{grid-template-columns:1fr}.comparison-panel img,.comparison-panel.data-panel img,.review-image-viewport,.data-panel .review-image-viewport{height:70vw}.inspector-gallery{grid-template-columns:repeat(2,minmax(0,1fr))}.browser-head{align-items:stretch;flex-direction:column}.search{width:100%}}
/* Linked 2x2 review workspace: Atlas | GridSquare, FoilHole | Data. */
.workspace{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;align-items:start}.workspace>.panel{min-width:0}
.atlas-viewport{height:clamp(470px,42vw,690px)}.atlas-image{max-height:clamp(470px,42vw,690px)}
.atlas-help{left:14px;bottom:14px;max-width:calc(100% - 160px);font-size:13px;font-weight:750;line-height:1.4;padding:10px 13px;background:rgba(13,22,38,.9);border:1px solid rgba(255,255,255,.2);box-shadow:0 8px 24px rgba(0,0,0,.24)}
.atlas-header-tools{display:flex;align-items:center;justify-content:flex-end;gap:8px;flex-wrap:wrap}.atlas-status-legend{position:static;display:flex;gap:8px;flex-wrap:wrap;padding:6px 8px;border:1px solid #dbe3ee;border-radius:9px;background:#f8fafc;color:#334155;font-size:10px;font-weight:750;pointer-events:none}.legend-item{display:flex;align-items:center;gap:5px}.legend-swatch{width:18px;height:18px;border-radius:50%;border:3px solid #64748b;background:#2563eb;display:inline-grid;place-items:center;color:#fff;font-size:8px;font-weight:900}.legend-swatch.suitable{background:#fff;border-color:#059669;color:#059669}.legend-swatch.unsuitable{background:#fff;border-color:#dc2626;color:#dc2626}.legend-swatch.unmarked{background:#fff;border-color:#64748b;color:#64748b}.legend-swatch.no-data{background:#475569;border-color:#fbbf24;color:#fef3c7}.legend-swatch.rating-1{background:#dc2626}.legend-swatch.rating-2{background:#f97316}.legend-swatch.rating-3{background:#facc15;color:#172033}.legend-swatch.rating-4{background:#84cc16;color:#172033}.legend-swatch.rating-5{background:#2e7d32}
.grid-marker::before{content:attr(data-status-label);position:absolute;right:-8px;top:-9px;width:15px;height:15px;border-radius:50%;display:grid;place-items:center;background:var(--status-color,#64748b);color:#fff;font-size:8px;font-weight:900;border:1px solid #fff}.grid-marker.no-data::after{left:-8px;right:auto}
.grid-marker.collection{background:#0f9f74;box-shadow:0 0 0 4px rgba(15,159,116,.32),0 3px 10px rgba(0,0,0,.4)}
.inspector{min-height:0}.inspector-empty{min-height:690px}.selected-summary{padding:12px 14px}.selected-detail-layout{display:block;padding:12px}.selected-media-card{padding:12px;background:#fff}.selected-media-card>.panel-title{font-size:14px}.selected-overlay-wrap{width:100%;margin:9px 0 0}.selected-overlay-image{width:100%;height:100%;object-fit:contain}.selected-grid-image{display:none!important}#selected-grid-png,#selected-grid-mrc{display:inline-flex}
.review-controls{position:static;margin-top:12px;padding:12px;background:#f8fafc}.review-controls>.panel-title{font-size:14px}.dashboard-comment{min-height:76px}
.interaction-guide{margin:7px 2px 2px;padding:0;border:0;background:transparent;color:var(--muted);font-size:10px;font-weight:500;line-height:1.35}.hole-nav{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-top:8px;padding:6px;border:1px solid var(--line);border-radius:9px;background:#fff}.hole-nav-status{font-size:10px;font-weight:650;color:var(--muted);text-align:center;flex:1}.hole-nav .button{min-width:94px}
.hole-comparison{display:none!important}.linked-image-panel{display:block!important;padding:12px;min-height:540px}.linked-image-panel .comparison-label{font-size:15px}.linked-image-panel .linked-empty{height:clamp(390px,38vw,620px);display:grid;place-items:center;text-align:center;color:var(--muted);border:1px dashed #cbd5e1;border-radius:10px;background:#fff;padding:24px}.linked-image-panel .review-image-viewport{height:clamp(390px,38vw,620px)}body.has-hole .linked-image-panel .linked-empty{display:none}.linked-image-panel.no-image .linked-empty{display:grid!important}.linked-image-panel img{display:none}.has-hole .linked-image-panel:not(.no-image) img{display:block}.linked-image-panel.no-image .review-image-viewport{display:none}
.review-image-stage{position:relative;width:100%;height:100%;transform-origin:center center;will-change:transform}.review-image-stage img{width:100%;height:100%;object-fit:contain}.review-image-stage .dashboard-hole-layer{inset:0}.review-image-viewport{height:clamp(390px,38vw,620px);cursor:grab}.review-image-viewport.dragging{cursor:grabbing}.review-image-viewport.zoomable{cursor:grab}
.mrc-viewer-controls{display:block;margin-top:5px;padding:4px 2px;border:0;background:transparent;opacity:.6;transition:opacity .15s}.mrc-viewer-controls:hover,.mrc-viewer-controls:focus-within{opacity:1}.mrc-viewer-controls .mrc-contrast-row{display:none}.mrc-viewer-controls.mrc-active .mrc-contrast-row{display:flex}.mrc-viewer-controls .button{padding:5px 8px;border-color:#e2e8f0;background:#f8fafc;font-size:10px}.mrc-viewer-controls .panel-note{font-size:9px}.mrc-control-row{gap:5px}.comparison-meta{min-height:18px}
@media(max-width:1250px){.workspace{grid-template-columns:1fr}.atlas-viewport,.atlas-image{height:clamp(500px,68vw,760px);max-height:clamp(500px,68vw,760px)}.inspector-empty{min-height:260px}}
@media(max-width:720px){.workspace{display:grid;grid-template-columns:1fr}.atlas-help{max-width:calc(100% - 28px);bottom:58px;font-size:12px}.atlas-status-legend{font-size:9px}.linked-image-panel{margin-top:0;min-height:360px}.linked-image-panel .review-image-viewport{height:70vw}.hole-nav .button{min-width:0}.interaction-guide{font-size:13px}}
/* Review cockpit: GridSquare rail | linked viewers | review rail. */
.content{max-width:none;padding:18px 22px 36px}.workspace{display:grid;grid-template-columns:220px minmax(680px,1fr) 310px;gap:14px;align-items:start}.workspace>.panel{min-width:0}
.visual-dashboard{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;align-items:start;min-width:0}.visual-dashboard>.panel{min-width:0}
.browser-panel{margin:0;position:sticky;top:86px;max-height:calc(100vh - 104px);overflow:hidden}.browser-head{display:block;padding:13px}.browser-head .search{width:100%;margin-top:10px}.grid-list{display:grid;grid-template-columns:1fr;gap:8px;padding:10px;max-height:calc(100vh - 205px);overflow:auto}.grid-card{padding:9px}.grid-card-title{font-size:10px}.grid-card-meta{font-size:9px;line-height:1.25}
.review-rail{position:sticky;top:86px;max-height:calc(100vh - 104px);overflow:auto}.review-controls{position:static;margin:0;padding:16px;border:0;background:#fff}.review-controls>.panel-title{font-size:15px}.dashboard-comment{min-height:150px}.collection-decisions{display:grid;grid-template-columns:1fr}.collection-decisions .button{width:100%}
.atlas-viewport{height:clamp(330px,27vw,500px)}.atlas-image{height:auto;max-height:clamp(330px,27vw,500px)}.inspector-empty{min-height:390px}.selected-media-card{padding:0;border:0;background:#fff}.selected-media-card .review-image-viewport{height:clamp(330px,27vw,500px)}
.linked-image-panel{min-height:410px}.linked-image-panel .linked-empty,.linked-image-panel .review-image-viewport{height:330px}.review-image-viewport{height:330px}
.manual-target-marker{position:absolute;width:18px;height:18px;transform:translate(-50%,-50%) rotate(45deg);border:2px solid #a5f3fc;background:#0891b2;pointer-events:auto;cursor:pointer;box-shadow:0 0 0 3px rgba(34,211,238,.28),0 2px 7px rgba(0,0,0,.4)}.manual-target-marker.candidate{width:34px;height:34px;border:0;background:transparent;box-shadow:none;transform:translate(-50%,-50%);border-radius:7px}.manual-target-marker.selected{background:#0891b2;border-color:#fff}.manual-target-button.active{background:#ecfeff;border-color:#06b6d4;color:#155e75}.legend-swatch.target{border-radius:3px;transform:rotate(45deg);background:#0891b2;border-color:#a5f3fc}
.atlas-tools{opacity:.58;background:rgba(13,22,38,.48);transition:opacity .15s}.atlas-tools:hover,.atlas-tools:focus-within{opacity:1}.atlas-tools button{width:27px;height:27px;font-size:13px}.atlas-zoom{font-size:9px}.media-actions{gap:5px;margin-top:6px}.media-actions .button{padding:6px 9px;font-size:10px;border-color:#dde4ee;color:#526176}
.visual-dashboard>.panel:nth-child(-n+2)>.panel-head{min-height:92px}.selected-detail-layout{padding:0}.selected-summary{display:none;border:0;padding:0}.inspector.has-selection .selected-summary{display:flex}.inspector.has-selection .inspector-head-copy{display:none}.selected-media-card>.panel-title{display:none}.selected-overlay-wrap{margin:0}.selected-media-card .review-image-viewport,.atlas-viewport{height:clamp(330px,27vw,500px)}
@media(max-width:1450px){.workspace{grid-template-columns:200px minmax(560px,1fr) 285px}.shell{grid-template-columns:76px minmax(0,1fr)}.sidebar{padding:22px 12px}.brand{margin:0 auto 28px}.brand span:last-child,.nav-item span:last-child,.nav-label,.session-health{display:none}.nav-item{justify-content:center}}
@media(max-width:1120px){.workspace{grid-template-columns:200px minmax(0,1fr)}.visual-dashboard{grid-template-columns:1fr}.review-rail{grid-column:2;position:static;max-height:none}.browser-panel{grid-row:1 / span 2}.atlas-viewport,.atlas-image,.selected-media-card .review-image-viewport{height:clamp(420px,62vw,680px);max-height:clamp(420px,62vw,680px)}}
@media(max-width:720px){.workspace{grid-template-columns:1fr}.browser-panel,.review-rail{position:static;max-height:none;grid-column:1;grid-row:auto}.grid-list{max-height:300px}.visual-dashboard{grid-template-columns:1fr}.linked-image-panel .review-image-viewport{height:70vw}}
/* Single-page shell and precisely aligned primary viewers. */
.shell{display:block;min-height:100vh}.main{width:100%;min-width:0}.top-health{display:inline-flex;align-items:center;gap:6px;color:var(--muted);font-size:10px;white-space:nowrap}.top-health .health-light{display:inline-block}.primary-viewer-panel{align-self:start}.primary-viewer-panel>.panel-head{height:108px;min-height:108px;padding:12px 14px;overflow:hidden}.primary-viewer-panel .atlas-viewport,.primary-viewer-panel .selected-media-card .review-image-viewport{height:clamp(360px,28vw,520px)}.primary-viewer-panel .inspector-empty{height:clamp(360px,28vw,520px);min-height:0}.primary-viewer-panel .selected-overlay-wrap{height:auto!important}
/* Fit media to its true aspect ratio; the stage and hit layer share exact bounds. */
.review-image-viewport{position:relative;display:block;overflow:hidden}.review-image-stage{position:absolute;left:50%;top:50%;width:1px;height:1px;transform-origin:center center;will-change:transform}.review-image-stage img{display:block;width:100%!important;height:100%!important;max-width:none;max-height:none;object-fit:fill}.review-image-stage .dashboard-hole-layer{position:absolute;inset:0}
.dashboard-hole-hit{width:32px;height:32px;border:0;background:transparent;box-shadow:none;cursor:pointer}.dashboard-hole-hit:hover,.dashboard-hole-hit:focus-visible{border:1px solid #5eead4;background:rgba(94,234,212,.12);box-shadow:0 0 0 3px rgba(15,23,42,.25)}.dashboard-hole-hit.active:not(:hover):not(:focus-visible){border:0;background:transparent;box-shadow:none}.linked-image-panel .review-image-viewport{background:#101827}.linked-image-panel .review-image-stage{max-width:none;max-height:none}
.grid-control-dock{display:block!important;visibility:visible!important;position:relative;z-index:3;padding:8px 10px 10px;background:#fff;border-top:1px solid var(--line)}.grid-control-dock>.media-actions{display:flex!important;visibility:visible!important;padding:0 0 6px;margin:0}.selected-media-card #selected-grid-png,.selected-media-card #selected-grid-mrc{display:inline-flex!important;visibility:visible!important;opacity:1!important;font-size:11px;padding:7px 10px}.grid-control-dock>.grid-viewer-controls{display:block!important;visibility:visible!important;margin:0 0 8px;padding:8px 9px;border:1px solid #dbe3ee;border-radius:9px;background:#f8fafc;opacity:1!important}.grid-viewer-controls .viewer-controls-title{display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;color:#475569;font-size:10px;font-weight:750}.grid-viewer-controls .mrc-contrast-row{display:flex!important}.grid-viewer-controls .mrc-contrast-row.disabled{opacity:.42}.grid-viewer-controls .mrc-control-row:last-child{margin-top:5px}.grid-viewer-controls .panel-note{font-size:9px}.grid-control-dock>.grid-nav{display:flex!important;visibility:visible!important;align-items:center;justify-content:space-between;gap:10px;margin:4px 0 0;padding:8px 0 0;border-top:1px solid var(--line)}.grid-nav-status{font-size:10px;font-weight:650;color:var(--muted);text-align:center;flex:1}.grid-nav .button{min-width:112px}
@media(max-width:1450px){.workspace{grid-template-columns:220px minmax(560px,1fr) 300px}}
@media(max-width:1120px){.workspace{grid-template-columns:200px minmax(0,1fr)}.primary-viewer-panel>.panel-head{height:96px;min-height:96px}.primary-viewer-panel .atlas-viewport,.primary-viewer-panel .selected-media-card .review-image-viewport,.primary-viewer-panel .inspector-empty{height:clamp(420px,62vw,680px)}}
</style></head><body>
<div class="shell">
<main class="main"><header class="topbar"><div><div class="top-title">EPU Mapper · Screening dashboard</div><div class="top-subtitle">Atlas → GridSquare → FoilHole → Data; hover or use Previous/Next</div></div><div class="top-actions"><span class="top-health"><span id="health-light" class="health-light"></span><span id="health-text">Checking session</span></span><button id="portable-export-dashboard" type="button" class="button">Export portable session</button><a class="button" href="/report.html">Export HTML</a><a class="button" href="/done">Reports & export</a></div></header><div id="preflight" class="preflight-pop"></div>
<div class="content"><section class="stats"><div class="stat"><span><span class="stat-value">__TOTAL__</span></span><span class="stat-label">Screened GridSquares</span></div><div class="stat"><span class="stat-value">__REVIEWED__</span><span class="stat-label">Reviewed</span></div><div class="stat"><span class="stat-value">__IMAGES__</span><span class="stat-label">Associated images</span></div></section>
<section class="workspace"><div class="panel"><div class="panel-head"><div><div class="panel-title">Interactive grid atlas</div><div class="panel-note">__MAPPED__ mapped squares · source: __ATLAS_SOURCE__ · fast PNG by default · load MRC only on request</div></div>__ATLAS_MODES__</div><div id="atlas-viewport" class="atlas-viewport"><div id="atlas-content" class="atlas-content">__ATLAS_CONTENT__</div>__ATLAS_AUX__</div></div>
<aside id="selected-detail" class="panel inspector"><div class="panel-head"><div class="inspector-head-copy"><div class="panel-title">Selected GridSquare review</div><div class="panel-note">PNG previews load by default; MRC is loaded only when requested</div></div></div><div id="inspector-empty" class="inspector-empty"><div><div class="empty-target">＋</div><div>Select a numbered atlas marker<br>or a GridSquare on the left</div></div></div><div id="inspector-body" class="inspector-body"><div class="selected-summary"><div><div id="selected-title" class="selected-title"></div><div id="selected-file" class="selected-file"></div><div id="selected-badges" class="badges"></div></div></div><div class="selected-detail-layout"><div class="selected-media-card"><div class="panel-title">GridSquare</div><img id="selected-grid-image" class="selected-grid-image" alt="Selected GridSquare"><div class="media-actions"><button id="selected-grid-png" type="button" class="button active">PNG preview</button><button id="selected-grid-mrc" type="button" class="button">Load GridSquare MRC</button></div><div id="selected-overlay-wrap" class="selected-overlay-wrap"><img id="selected-overlay-image" class="selected-overlay-image" alt="Screened FoilHole overlay"><div id="dashboard-hole-layer" class="dashboard-hole-layer"></div></div><div id="overlay-note" class="panel-note">Hover a screened FoilHole to compare its FoilHole and Data images.</div></div><aside class="review-controls"><div class="panel-title">Review decision</div><div class="control-title">Rating</div><div id="dashboard-ratings" class="dashboard-ratings"><button type="button" class="dashboard-rating" data-rating="1">1</button><button type="button" class="dashboard-rating" data-rating="2">2</button><button type="button" class="dashboard-rating" data-rating="3">3</button><button type="button" class="dashboard-rating" data-rating="4">4</button><button type="button" class="dashboard-rating" data-rating="5">5</button></div><div class="control-title">Collection suitability</div><div class="collection-decisions"><button id="mark-suitable" type="button" class="button collection-decision suitable">Suitable for collection</button><button id="mark-unsuitable" type="button" class="button collection-decision unsuitable">Unsuitable for collection</button><button id="clear-collection" type="button" class="button collection-decision">Clear</button></div><div class="control-title">Comment</div><textarea id="dashboard-comment" class="dashboard-comment" placeholder="Add notes for this GridSquare…"></textarea><label class="panel-note"><input id="dashboard-include" type="checkbox" checked> Include in final report</label><div id="dashboard-save-state" class="save-state"></div></aside></div><section id="hole-comparison" class="hole-comparison"><div class="panel-head"><div><div id="hole-comparison-title" class="panel-title">FoilHole comparison</div><div class="panel-note">Large PNG previews; request MRC only when needed</div></div></div><div class="comparison-grid"><div class="comparison-panel"><div class="comparison-label">FoilHole image</div><img id="comparison-foil" alt="FoilHole preview"><div class="media-actions"><button id="comparison-foil-png" type="button" class="button">PNG</button><button id="comparison-foil-mrc" type="button" class="button">Load MRC</button></div></div><div class="comparison-panel"><div class="comparison-label">Data image</div><img id="comparison-data" alt="Data preview"><div id="comparison-meta" class="comparison-meta"></div><div class="media-actions"><button id="comparison-data-png" type="button" class="button">PNG</button><button id="comparison-data-mrc" type="button" class="button">Load MRC</button></div></div></div></section></div></aside></section>
<section class="panel browser-panel"><div class="panel-head browser-head"><div><div class="panel-title">All screened GridSquares</div><div class="panel-note">Acquisition order</div></div><input id="grid-search" class="search" type="search" placeholder="Find GridSquare ID…" aria-label="Find GridSquare"></div><div id="grid-list" class="grid-list"></div></section></div></main></div>
<div id="lightbox" class="lightbox" role="dialog" aria-modal="true" aria-label="Image preview"><button type="button" id="lightbox-close" class="lightbox-close" aria-label="Close">×</button><img id="lightbox-image" alt="Selected microscopy image"></div>
<div id="grid-hover-preview" class="grid-hover-preview" role="tooltip"><img id="grid-hover-image" alt="GridSquare preview"><div id="grid-hover-caption" class="grid-hover-caption"></div></div>
<dialog id="portable-dialog" class="portable-dialog"><div class="portable-dialog-head"><h2>Export portable EPU session</h2><button id="portable-dialog-close" type="button" class="portable-dialog-close" aria-label="Close">×</button></div><div class="portable-dialog-body"><label for="portable-destination">Destination folder on this Mac</label><input id="portable-destination" class="portable-destination" type="text" spellcheck="false"><div class="portable-warning">This copies the complete EPU session and Atlas data, so the export can be very large. The resulting <strong>EPUMapperSession.epumap</strong> can be opened from the launcher on another computer.</div><div id="portable-status" class="portable-status"><span id="portable-status-text"></span><div class="portable-progress"><span id="portable-progress-bar"></span></div></div><div class="portable-dialog-actions"><button id="portable-cancel" type="button" class="button">Close</button><button id="portable-start" type="button" class="button primary">Start export</button></div></div></dialog>
<script>
const GRIDS=__GRIDS_JSON__;const UNSCREENED=__UNSCREENED_JSON__;const HAS_ATLAS=__HAS_ATLAS__;const SESSION_STORAGE_KEY=__SESSION_KEY__;const CACHE_KEY=__CACHE_KEY__;const PORTABLE_DEFAULT=__PORTABLE_DEFAULT__;const LAST_IDX_KEY='last_idx_'+SESSION_STORAGE_KEY;const atlasViewport=document.getElementById('atlas-viewport');const atlasContent=document.getElementById('atlas-content');const markerLayer=document.getElementById('marker-layer');let selectedIdx=null;let atlasScale=1,atlasX=0,atlasY=0,atlasDragging=false,atlasStartX=0,atlasStartY=0,atlasMode='screened',atlasLow=1,atlasHigh=99,atlasMrcTimer=null,targetMode=new URLSearchParams(location.search).get('targeting')==='1';
function esc(value){return String(value??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]))}
const gridHoverPreview=document.getElementById('grid-hover-preview'),gridHoverImage=document.getElementById('grid-hover-image'),gridHoverCaption=document.getElementById('grid-hover-caption');let hoveredGridIdx=null;
function positionGridHover(event){const gap=18,width=Math.min(560,window.innerWidth-24),height=Math.min(590,window.innerHeight-24);let left=event.clientX+gap,top=event.clientY+gap;if(left+width>window.innerWidth-8)left=event.clientX-width-gap;if(top+height>window.innerHeight-8)top=window.innerHeight-height-8;gridHoverPreview.style.left=Math.max(8,left)+'px';gridHoverPreview.style.top=Math.max(8,top)+'px'}
function showGridHover(target,event){const idx=Number(target.dataset.idx),grid=GRIDS.find(entry=>entry.idx===idx);if(!grid)return;if(hoveredGridIdx!==idx){hoveredGridIdx=idx;gridHoverImage.src='/preview.png?idx='+idx+'&kind=grid&size=1100&session='+encodeURIComponent(CACHE_KEY);const decision=grid.collection_status==='suitable'?' · suitable':grid.collection_status==='unsuitable'?' · unsuitable':'';gridHoverCaption.textContent='GridSquare '+grid.id+decision}gridHoverPreview.classList.add('visible');positionGridHover(event)}
function hideGridHover(){hoveredGridIdx=null;gridHoverPreview.classList.remove('visible')}
document.addEventListener('pointerover',event=>{const target=event.target.closest('.grid-marker,.grid-card');if(!target||target.contains(event.relatedTarget))return;showGridHover(target,event)});
document.addEventListener('pointermove',event=>{if(gridHoverPreview.classList.contains('visible'))positionGridHover(event)});
document.addEventListener('pointerout',event=>{const target=event.target.closest('.grid-marker,.grid-card');if(target&&!target.contains(event.relatedTarget))hideGridHover()});
document.addEventListener('pointerdown',hideGridHover);
function renderMarkers(){if(!markerLayer)return;markerLayer.innerHTML='';GRIDS.filter(g=>g.position).forEach(g=>{const noData=g.data_count===0,b=document.createElement('button');b.type='button';b.className='grid-marker'+(g.reviewed?' reviewed':'')+(!g.include?' excluded':'')+(g.collection_status==='suitable'?' collection':'')+(g.collection_status==='unsuitable'?' unsuitable':'')+(noData?' no-data':'');b.dataset.idx=g.idx;b.style.left=g.position.x+'%';b.style.top=g.position.y+'%';b.textContent=String(g.idx+1);const decision=g.collection_status==='suitable'?' · suitable for collection':g.collection_status==='unsuitable'?' · unsuitable for collection':'',availability=noData?' · NO SCREENING DATA':'';b.title='GridSquare '+g.id+availability+decision;b.setAttribute('aria-label','Open GridSquare '+g.id+(noData?', no screening data':''));b.addEventListener('pointerdown',e=>e.stopPropagation());b.onclick=e=>{e.stopPropagation();selectGrid(g.idx)};markerLayer.appendChild(b)})}
function renderGridList(filter=''){const list=document.getElementById('grid-list');const needle=filter.trim().toLowerCase();list.innerHTML='';GRIDS.filter(g=>!needle||g.id.toLowerCase().includes(needle)||g.name.toLowerCase().includes(needle)).forEach(g=>{const noData=g.data_count===0,b=document.createElement('button');b.type='button';b.className='grid-card'+(g.reviewed?' reviewed':'')+(g.collection_status==='suitable'?' collection':'')+(g.collection_status==='unsuitable'?' unsuitable':'')+(noData?' no-data':'')+(g.idx===selectedIdx?' active':'');b.dataset.idx=g.idx;const decision=g.collection_status==='suitable'?' · suitable':g.collection_status==='unsuitable'?' · unsuitable':'',availability=noData?'No screening data':g.foil_count+' foils · '+g.data_count+' data';b.innerHTML='<span class="grid-card-index">'+(g.idx+1)+'</span><span><span class="grid-card-title">GridSquare '+esc(g.id)+'</span><span class="grid-card-meta">'+availability+(g.reviewed?' · reviewed':'')+decision+'</span></span>';b.onclick=()=>selectGrid(g.idx);list.appendChild(b)})}
let selectedGridData=null,dashboardSaveTimer=null,activeHole=null,activeHoleIndex=-1,dashboardHoles=[];
function setDashboardRating(value,save=true){document.querySelectorAll('.dashboard-rating').forEach(button=>button.classList.toggle('active',Number(button.dataset.rating)===Number(value)));if(selectedGridData)selectedGridData.rating=Number(value)||0;if(save)queueDashboardSave()}
function setDashboardCollectionStatus(status,save=true){if(selectedGridData){selectedGridData.collection_status=status;selectedGridData.collect=status==='suitable'}document.getElementById('mark-suitable').classList.toggle('active',status==='suitable');document.getElementById('mark-unsuitable').classList.toggle('active',status==='unsuitable');if(save)queueDashboardSave()}
function queueDashboardSave(){if(selectedIdx===null||!selectedGridData)return;const state=document.getElementById('dashboard-save-state');state.textContent='Saving…';if(dashboardSaveTimer)clearTimeout(dashboardSaveTimer);dashboardSaveTimer=setTimeout(saveDashboardReview,450)}
async function saveDashboardReview(){if(selectedIdx===null||!selectedGridData)return;const payload={idx:selectedIdx,rating:selectedGridData.rating||0,comment:document.getElementById('dashboard-comment').value,include:document.getElementById('dashboard-include').checked,collection_status:selectedGridData.collection_status||'',collect:selectedGridData.collection_status==='suitable'};const state=document.getElementById('dashboard-save-state');try{const response=await fetch('/review_state',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});if(!response.ok)throw new Error('HTTP '+response.status);const result=await response.json(),grid=GRIDS.find(entry=>entry.idx===selectedIdx);if(grid){grid.reviewed=true;grid.rating=payload.rating;grid.include=payload.include;grid.collect=payload.collect;grid.collection_status=payload.collection_status}state.textContent='Saved';renderMarkers();renderGridList(document.getElementById('grid-search').value)}catch(error){state.textContent='Save failed — retry after reconnecting'}}
function pngUrl(kind,name,size=1400){return '/preview.png?idx='+selectedIdx+'&kind='+kind+(name?'&name='+encodeURIComponent(name):'')+'&size='+size+'&session='+encodeURIComponent(CACHE_KEY)}
function mrcPreviewUrl(kind,name,low=2,high=98){return '/mrc_file?idx='+selectedIdx+'&kind='+kind+'&name='+encodeURIComponent(name||'')+'&low='+encodeURIComponent(low)+'&high='+encodeURIComponent(high)+'&t='+Date.now()}
function arrangeSelectedWorkspace(){const layout=document.querySelector('.selected-detail-layout'),media=document.querySelector('.selected-media-card'),comparison=document.getElementById('hole-comparison');if(!layout||!media||!comparison)return;media.querySelector('.panel-title').textContent='GridSquare with screened FoilHoles';const ratings=document.getElementById('dashboard-ratings'),ratingTitle=ratings.previousElementSibling,ratingPanel=document.createElement('div');ratingPanel.className='inline-rating-panel';ratingPanel.appendChild(ratingTitle);ratingPanel.appendChild(ratings);media.appendChild(ratingPanel);const visual=document.createElement('div');visual.className='selected-visual-workspace';layout.insertBefore(visual,media);visual.appendChild(media);visual.appendChild(comparison);const foilPanel=document.getElementById('comparison-foil').closest('.comparison-panel'),dataPanel=document.getElementById('comparison-data').closest('.comparison-panel'),grid=comparison.querySelector('.comparison-grid');dataPanel.classList.add('data-panel');grid.appendChild(dataPanel);grid.appendChild(foilPanel);const placeholder=document.createElement('div');placeholder.className='hole-preview-placeholder';placeholder.innerHTML='<div><strong>Hover a screened FoilHole</strong><br><span>Its Data image will appear beside the GridSquare, with the FoilHole image underneath.</span></div>';grid.insertBefore(placeholder,grid.firstChild)}
function installMrcViewer(imgId,pngButtonId,mrcButtonId,kind,getSource){const img=document.getElementById(imgId),pngButton=document.getElementById(pngButtonId),mrcButton=document.getElementById(mrcButtonId);if(!img||!pngButton||!mrcButton)return;const viewport=document.createElement('div');viewport.className='review-image-viewport';img.parentNode.insertBefore(viewport,img);viewport.appendChild(img);const controls=document.createElement('div');controls.className='mrc-viewer-controls';controls.innerHTML='<div class="mrc-control-row"><label>Low <span class="mrc-low-value">2</span>% <input class="mrc-low" type="range" min="0" max="99" value="2"></label><label>High <span class="mrc-high-value">98</span>% <input class="mrc-high" type="range" min="1" max="100" value="98"></label></div><div class="mrc-control-row"><button type="button" class="button mrc-minus">−</button><span class="mrc-zoom-value">100%</span><button type="button" class="button mrc-plus">+</button><button type="button" class="button mrc-reset">Reset</button><span class="panel-note">Scroll to zoom · drag to pan</span></div>';mrcButton.closest('.media-actions').after(controls);const low=controls.querySelector('.mrc-low'),high=controls.querySelector('.mrc-high'),lowValue=controls.querySelector('.mrc-low-value'),highValue=controls.querySelector('.mrc-high-value'),zoomValue=controls.querySelector('.mrc-zoom-value');let scale=1,x=0,y=0,dragging=false,startX=0,startY=0,renderTimer=null,mrcActive=false;function apply(){img.style.transform='translate('+x.toFixed(1)+'px,'+y.toFixed(1)+'px) scale('+scale.toFixed(3)+')';zoomValue.textContent=Math.round(scale*100)+'%';viewport.classList.toggle('zoomable',mrcActive)}function setScale(value){const next=Math.max(1,Math.min(8,value)),ratio=next/scale;x*=ratio;y*=ratio;scale=next;if(scale===1){x=0;y=0}apply()}function reset(){scale=1;x=0;y=0;apply()}function renderMrc(){const source=getSource();if(!source||!source.name||!source.hasMrc)return;const lowNumber=Number(low.value),highNumber=Number(high.value);lowValue.textContent=low.value;highValue.textContent=high.value;img.style.display='block';img.src=mrcPreviewUrl(kind,source.name,lowNumber,highNumber)}function queueRender(changed){if(Number(low.value)>=Number(high.value)){if(changed===low)high.value=Math.min(100,Number(low.value)+1);else low.value=Math.max(0,Number(high.value)-1)}lowValue.textContent=low.value;highValue.textContent=high.value;if(renderTimer)clearTimeout(renderTimer);renderTimer=setTimeout(renderMrc,250)}low.oninput=()=>queueRender(low);high.oninput=()=>queueRender(high);mrcButton.onclick=()=>{const source=getSource();if(!source||!source.hasMrc)return;mrcActive=true;controls.classList.add('visible');renderMrc();reset()};pngButton.onclick=()=>{const source=getSource();if(!source||!source.png)return;mrcActive=false;controls.classList.remove('visible');img.style.display='block';img.src=source.png;reset()};controls.querySelector('.mrc-minus').onclick=()=>setScale(scale/1.3);controls.querySelector('.mrc-plus').onclick=()=>setScale(scale*1.3);controls.querySelector('.mrc-reset').onclick=reset;viewport.addEventListener('wheel',event=>{if(!mrcActive)return;event.preventDefault();setScale(scale*Math.exp(-event.deltaY*.0015))},{passive:false});viewport.addEventListener('pointerdown',event=>{if(!mrcActive||scale<=1||event.button!==0)return;dragging=true;startX=event.clientX-x;startY=event.clientY-y;viewport.classList.add('dragging');viewport.setPointerCapture(event.pointerId)});viewport.addEventListener('pointermove',event=>{if(!dragging)return;x=event.clientX-startX;y=event.clientY-startY;apply()});viewport.addEventListener('pointerup',()=>{dragging=false;viewport.classList.remove('dragging')});viewport.addEventListener('pointercancel',()=>{dragging=false;viewport.classList.remove('dragging')});apply()}
function positionDashboardHolePreview(hole){activeHole=hole;const section=document.getElementById('hole-comparison');section.classList.add('has-selection');document.getElementById('hole-comparison-title').textContent='FoilHole '+hole.foil_id;document.getElementById('comparison-meta').innerHTML=hole.data_preview?(hole.meta||[]).map(esc).join('<br>'):'No matching Data image';document.getElementById('comparison-foil-mrc').disabled=!hole.foil_has_mrc;document.getElementById('comparison-data-mrc').disabled=!hole.data_has_mrc;document.getElementById('comparison-foil-png').click();if(hole.data_preview)document.getElementById('comparison-data-png').click();else document.getElementById('comparison-data').style.display='none'}
function renderDashboardHoles(holes){const layer=document.getElementById('dashboard-hole-layer');layer.innerHTML='';(holes||[]).forEach(hole=>{const button=document.createElement('button');button.type='button';button.className='dashboard-hole-hit';button.style.left=hole.x+'%';button.style.top=hole.y+'%';button.title='FoilHole '+hole.foil_id;button.setAttribute('aria-label','Preview FoilHole '+hole.foil_id);button.onpointerenter=()=>positionDashboardHolePreview(hole);button.onfocus=()=>positionDashboardHolePreview(hole);button.onclick=()=>positionDashboardHolePreview(hole);layer.appendChild(button)})}
function syncSelectedGridAnnotation(){if(selectedIdx===null||!selectedGridData)return;const grid=GRIDS.find(entry=>entry.idx===selectedIdx);if(grid){grid.rating=Number(selectedGridData.rating)||0;grid.collect=Boolean(selectedGridData.collect);grid.collection_status=selectedGridData.collection_status||''}renderMarkers();renderGridList(document.getElementById('grid-search').value)}
function setDashboardRating(value,save=true){document.querySelectorAll('.dashboard-rating').forEach(button=>button.classList.toggle('active',Number(button.dataset.rating)===Number(value)));if(selectedGridData){selectedGridData.rating=Number(value)||0;syncSelectedGridAnnotation()}if(save)queueDashboardSave()}
function setDashboardCollectionStatus(status,save=true){const normalized=status==='suitable'||status==='unsuitable'?status:'';if(selectedGridData){selectedGridData.collection_status=normalized;selectedGridData.collect=normalized==='suitable';syncSelectedGridAnnotation()}document.getElementById('mark-suitable').classList.toggle('active',normalized==='suitable');document.getElementById('mark-unsuitable').classList.toggle('active',normalized==='unsuitable');if(save)queueDashboardSave()}
function renderMarkers(){
  if(!markerLayer)return;markerLayer.innerHTML='';
  const ratingColors={0:'#2563eb',1:'#dc2626',2:'#f97316',3:'#facc15',4:'#84cc16',5:'#2e7d32'};
  GRIDS.filter(grid=>grid.position).forEach(grid=>{
    const noData=grid.data_count===0,button=document.createElement('button'),status=grid.collection_status||'',statusColor=status==='suitable'?'#059669':status==='unsuitable'?'#dc2626':'#64748b';
    button.type='button';button.className='grid-marker'+(grid.reviewed?' reviewed':'')+(!grid.include?' excluded':'')+(noData?' no-data':'')+(grid.idx===selectedIdx?' active':'');button.dataset.idx=grid.idx;button.dataset.statusLabel=status==='suitable'?'S':status==='unsuitable'?'U':'-';button.style.setProperty('--status-color',statusColor);button.style.left=grid.position.x+'%';button.style.top=grid.position.y+'%';button.style.background=ratingColors[Number(grid.rating)||0];button.style.color=[3,4].includes(Number(grid.rating))?'#172033':'#fff';button.style.borderColor=statusColor;button.style.boxShadow=noData?'0 0 0 4px rgba(251,191,36,.55),0 3px 10px rgba(0,0,0,.4)':'0 0 0 3px '+statusColor+'55,0 3px 10px rgba(0,0,0,.4)';button.textContent=String(grid.idx+1);
    const decision=status==='suitable'?' · suitable for collection':status==='unsuitable'?' · unsuitable for collection':' · collection status unmarked',availability=noData?' · NO SCREENING DATA':'';button.title='GridSquare '+grid.id+' · rating '+(grid.rating||0)+availability+decision;button.setAttribute('aria-label','Open GridSquare '+grid.id+', rating '+(grid.rating||0)+decision+(noData?', no screening data':''));button.addEventListener('pointerdown',event=>event.stopPropagation());button.onclick=event=>{event.stopPropagation();selectGrid(grid.idx)};markerLayer.appendChild(button);
  });
  UNSCREENED.filter(target=>target.selected||targetMode).forEach(target=>{const button=document.createElement('button');button.type='button';button.className='manual-target-marker '+(target.selected?'selected':'candidate');button.style.left=target.position.x+'%';button.style.top=target.position.y+'%';button.title=targetMode?((target.selected?'Remove':'Add')+' unscreened GridSquare '+target.id+' as a collection target'):('Manual collection target · GridSquare '+target.id);button.setAttribute('aria-label',button.title);button.addEventListener('pointerdown',event=>event.stopPropagation());button.onclick=async event=>{event.stopPropagation();if(!targetMode&&target.selected)return;button.disabled=true;try{const response=await fetch('/manual_target',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:target.key,selected:!target.selected})});if(!response.ok)throw new Error('HTTP '+response.status);const payload=await response.json();target.selected=Boolean(payload.selected);renderMarkers()}catch(error){alert('Could not save target: '+error.message)}};markerLayer.appendChild(button)});
  const targetButton=document.getElementById('manual-target-toggle');if(targetButton){const count=UNSCREENED.filter(target=>target.selected).length;targetButton.classList.toggle('active',targetMode);targetButton.textContent=(targetMode?'Finish target selection':'Add unscreened targets')+(count?' · '+count:'')}
  const atlasHelp=document.getElementById('atlas-help');if(atlasHelp)atlasHelp.textContent=targetMode?'Target mode: click an unscreened square; only selected targets will be marked.':'Scroll to zoom · drag to pan · double-click to reset';
}
function arrangeSelectedWorkspace(){
  const workspace=document.querySelector('.workspace'),atlasPanel=workspace?workspace.querySelector(':scope > .panel'):null,browser=document.querySelector('.browser-panel'),inspector=document.getElementById('selected-detail'),layout=document.querySelector('.selected-detail-layout'),media=document.querySelector('.selected-media-card'),controls=document.querySelector('.review-controls'),comparison=document.getElementById('hole-comparison');
  if(!workspace||!atlasPanel||!browser||!inspector||!layout||!media||!controls||!comparison)return;
  atlasPanel.classList.add('primary-viewer-panel');inspector.classList.add('primary-viewer-panel');
  media.querySelector('.panel-title').textContent='GridSquare with screened FoilHoles';
  const summary=inspector.querySelector('.selected-summary'),inspectorHead=inspector.querySelector('.panel-head');if(summary&&inspectorHead)inspectorHead.appendChild(summary);
  const guide=document.createElement('div');guide.className='interaction-guide';guide.id='grid-hover-guide';guide.textContent='Tip: hover a numbered FoilHole or use Previous/Next below the Data image.';
  const overlayWrap=document.getElementById('selected-overlay-wrap'),mediaActions=media.querySelector('.media-actions'),overlayNote=document.getElementById('overlay-note');media.insertBefore(overlayWrap,mediaActions);
  const controlDock=document.createElement('div');controlDock.id='grid-control-dock';controlDock.className='grid-control-dock';media.insertBefore(controlDock,overlayNote);controlDock.appendChild(mediaActions);
  const gridNav=document.createElement('div');gridNav.className='grid-nav';gridNav.innerHTML='<button id="grid-prev" type="button" class="button">← Previous GridSquare</button><div id="grid-nav-status" class="grid-nav-status">Select a GridSquare</div><button id="grid-next" type="button" class="button">Next GridSquare →</button>';controlDock.appendChild(gridNav);media.insertBefore(guide,overlayNote);
  const foilPanel=document.getElementById('comparison-foil').closest('.comparison-panel');
  const dataPanel=document.getElementById('comparison-data').closest('.comparison-panel');
  foilPanel.classList.add('panel','linked-image-panel','no-image');dataPanel.classList.add('panel','linked-image-panel','data-panel','no-image');
  foilPanel.insertAdjacentHTML('afterbegin','<div class="linked-empty"><div><strong>No FoilHole selected</strong><br>Choose a screened hole in the GridSquare above.</div></div>');
  dataPanel.insertAdjacentHTML('afterbegin','<div class="linked-empty"><div><strong>No Data image selected</strong><br>The matching Data image will appear here without changing the layout size.</div></div>');
  const nav=document.createElement('div');nav.className='hole-nav';nav.innerHTML='<button id="hole-prev" type="button" class="button">← Previous hole</button><div id="hole-nav-status" class="hole-nav-status">Select a GridSquare</div><button id="hole-next" type="button" class="button">Next hole →</button>';dataPanel.appendChild(nav);
  const visual=document.createElement('div');visual.className='visual-dashboard';workspace.insertBefore(browser,workspace.firstChild);workspace.insertBefore(visual,inspector);visual.appendChild(atlasPanel);visual.appendChild(inspector);visual.appendChild(foilPanel);visual.appendChild(dataPanel);comparison.classList.add('comparison-storage');
  const reviewRail=document.createElement('aside');reviewRail.className='panel review-rail';reviewRail.appendChild(controls);workspace.appendChild(reviewRail);
  layout.appendChild(media);
  document.getElementById('hole-prev').onclick=()=>showDashboardHoleByIndex(activeHoleIndex-1);
  document.getElementById('hole-next').onclick=()=>showDashboardHoleByIndex(activeHoleIndex+1);
  document.getElementById('grid-prev').onclick=()=>{if(selectedIdx!==null&&selectedIdx>0)selectGrid(selectedIdx-1,false)};
  document.getElementById('grid-next').onclick=()=>{if(selectedIdx!==null&&selectedIdx<GRIDS.length-1)selectGrid(selectedIdx+1,false)};
  updateGridNavigation();
}
function installMrcViewer(imgId,pngButtonId,mrcButtonId,kind,getSource){
  const img=document.getElementById(imgId),pngButton=document.getElementById(pngButtonId),mrcButton=document.getElementById(mrcButtonId);if(!img||!pngButton||!mrcButton)return;
  const parent=img.parentNode,viewport=document.createElement('div');viewport.className='review-image-viewport';parent.insertBefore(viewport,img);
  const stage=document.createElement('div');stage.className='review-image-stage';viewport.appendChild(stage);stage.appendChild(img);if(kind==='grid'){const layer=document.getElementById('dashboard-hole-layer');if(layer)stage.appendChild(layer)}
  const controls=document.createElement('div');controls.className='mrc-viewer-controls'+(kind==='grid'?' grid-viewer-controls':'');const controlsTitle=kind==='grid'?'<div class="viewer-controls-title"><span>GridSquare display controls</span><span>Contrast activates with MRC</span></div>':'';controls.innerHTML=controlsTitle+'<div class="mrc-control-row mrc-contrast-row"><label>Low <span class="mrc-low-value">2</span>% <input class="mrc-low" type="range" min="0" max="99" value="2"></label><label>High <span class="mrc-high-value">98</span>% <input class="mrc-high" type="range" min="1" max="100" value="98"></label></div><div class="mrc-control-row"><button type="button" class="button mrc-minus">−</button><span class="mrc-zoom-value">100%</span><button type="button" class="button mrc-plus">+</button><button type="button" class="button mrc-reset">Reset</button><span class="panel-note">Scroll to zoom · drag to pan</span></div>';
  mrcButton.closest('.media-actions').after(controls);
  if(kind==='grid'){const gridNav=document.getElementById('grid-nav');if(gridNav)controls.after(gridNav)}
  const low=controls.querySelector('.mrc-low'),high=controls.querySelector('.mrc-high'),lowValue=controls.querySelector('.mrc-low-value'),highValue=controls.querySelector('.mrc-high-value'),zoomValue=controls.querySelector('.mrc-zoom-value'),contrastRow=controls.querySelector('.mrc-contrast-row');let scale=1,x=0,y=0,dragging=false,startX=0,startY=0,renderTimer=null,mrcActive=false;if(kind==='grid'){low.disabled=true;high.disabled=true;contrastRow.classList.add('disabled')}
  function syncFit(){const iw=img.naturalWidth||1,ih=img.naturalHeight||1,vw=Math.max(1,viewport.clientWidth),vh=Math.max(1,viewport.clientHeight),ratio=Math.min(vw/iw,vh/ih);stage.style.width=Math.max(1,iw*ratio).toFixed(2)+'px';stage.style.height=Math.max(1,ih*ratio).toFixed(2)+'px'}
  function apply(){stage.style.transform='translate(-50%,-50%) translate('+x.toFixed(1)+'px,'+y.toFixed(1)+'px) scale('+scale.toFixed(3)+')';zoomValue.textContent=Math.round(scale*100)+'%';viewport.classList.toggle('zoomable',scale>1)}
  function setScale(value){const next=Math.max(1,Math.min(8,value)),ratio=next/scale;x*=ratio;y*=ratio;scale=next;if(scale===1){x=0;y=0}apply()}
  function reset(){scale=1;x=0;y=0;apply()}
  function renderMrc(){const source=getSource();if(!source||!source.hasMrc)return;lowValue.textContent=low.value;highValue.textContent=high.value;img.src=mrcPreviewUrl(kind,source.name||'',Number(low.value),Number(high.value))}
  function queueRender(changed){if(Number(low.value)>=Number(high.value)){if(changed===low)high.value=Math.min(100,Number(low.value)+1);else low.value=Math.max(0,Number(high.value)-1)}lowValue.textContent=low.value;highValue.textContent=high.value;if(renderTimer)clearTimeout(renderTimer);renderTimer=setTimeout(renderMrc,250)}
  low.oninput=()=>queueRender(low);high.oninput=()=>queueRender(high);
  mrcButton.onclick=()=>{const source=getSource();if(!source||!source.hasMrc)return;mrcActive=true;controls.classList.add('mrc-active');low.disabled=false;high.disabled=false;contrastRow.classList.remove('disabled');pngButton.classList.remove('active');mrcButton.classList.add('active');renderMrc();reset()};
  pngButton.onclick=()=>{const source=getSource();if(!source||!source.png)return;mrcActive=false;controls.classList.remove('mrc-active');if(kind==='grid'){low.disabled=true;high.disabled=true;contrastRow.classList.add('disabled')}mrcButton.classList.remove('active');pngButton.classList.add('active');img.src=source.png;reset()};
  controls.querySelector('.mrc-minus').onclick=()=>setScale(scale/1.3);controls.querySelector('.mrc-plus').onclick=()=>setScale(scale*1.3);controls.querySelector('.mrc-reset').onclick=reset;
  viewport.addEventListener('wheel',event=>{event.preventDefault();setScale(scale*Math.exp(-event.deltaY*.0015))},{passive:false});
  viewport.addEventListener('pointerdown',event=>{if(scale<=1||event.button!==0||event.target.closest('.dashboard-hole-hit'))return;dragging=true;startX=event.clientX-x;startY=event.clientY-y;viewport.classList.add('dragging');viewport.setPointerCapture(event.pointerId)});
  viewport.addEventListener('pointermove',event=>{if(!dragging)return;x=event.clientX-startX;y=event.clientY-startY;apply()});viewport.addEventListener('pointerup',()=>{dragging=false;viewport.classList.remove('dragging')});viewport.addEventListener('pointercancel',()=>{dragging=false;viewport.classList.remove('dragging')});
  img.addEventListener('load',()=>{syncFit();reset()});if(typeof ResizeObserver!=='undefined')new ResizeObserver(()=>{syncFit();apply()}).observe(viewport);img._viewerReset=reset;img._viewerShowPng=()=>pngButton.click();syncFit();apply();
}
function updateHoleNavigation(){
  const count=dashboardHoles.length,status=document.getElementById('hole-nav-status'),prev=document.getElementById('hole-prev'),next=document.getElementById('hole-next');if(!status||!prev||!next)return;
  status.textContent=count?(activeHoleIndex>=0?'Hole '+(activeHoleIndex+1)+' of '+count:count+' screened holes'):'No screened holes available';prev.disabled=activeHoleIndex<=0;next.disabled=activeHoleIndex<0||activeHoleIndex>=count-1;
}
function updateGridNavigation(){
  const status=document.getElementById('grid-nav-status'),prev=document.getElementById('grid-prev'),next=document.getElementById('grid-next');if(!status||!prev||!next)return;
  status.textContent=selectedIdx===null?'Select a GridSquare':'GridSquare '+(selectedIdx+1)+' of '+GRIDS.length;prev.disabled=selectedIdx===null||selectedIdx<=0;next.disabled=selectedIdx===null||selectedIdx>=GRIDS.length-1;
}
function positionDashboardHolePreview(hole){
  if(!hole)return;activeHole=hole;activeHoleIndex=dashboardHoles.indexOf(hole);document.body.classList.add('has-hole');
  document.querySelectorAll('.dashboard-hole-hit').forEach((button,index)=>button.classList.toggle('active',index===activeHoleIndex));
  const foilPanel=document.getElementById('comparison-foil').closest('.linked-image-panel'),dataPanel=document.getElementById('comparison-data').closest('.linked-image-panel');foilPanel.classList.remove('no-image');dataPanel.classList.toggle('no-image',!hole.data_preview);
  foilPanel.querySelector('.comparison-label').textContent='FoilHole '+hole.foil_id;dataPanel.querySelector('.comparison-label').textContent=hole.data_preview?'Data · FoilHole '+hole.foil_id:'Data · no matching image';
  document.getElementById('comparison-meta').innerHTML=hole.data_preview?(hole.meta||[]).map(esc).join('<br>'):'No matching Data image for this FoilHole.';
  document.getElementById('comparison-foil-mrc').disabled=!hole.foil_has_mrc;document.getElementById('comparison-data-mrc').disabled=!hole.data_has_mrc;
  document.getElementById('comparison-foil-png').click();if(hole.data_preview)document.getElementById('comparison-data-png').click();
  updateHoleNavigation();
}
function showDashboardHoleByIndex(index){if(!dashboardHoles.length)return;const safe=Math.max(0,Math.min(dashboardHoles.length-1,index));positionDashboardHolePreview(dashboardHoles[safe])}
function renderDashboardHoles(holes){
  dashboardHoles=Array.isArray(holes)?holes:[];activeHole=null;activeHoleIndex=-1;document.body.classList.remove('has-hole');document.querySelectorAll('.linked-image-panel').forEach(panel=>panel.classList.add('no-image'));document.getElementById('comparison-meta').textContent='';const layer=document.getElementById('dashboard-hole-layer');layer.innerHTML='';
  dashboardHoles.forEach((hole,index)=>{const button=document.createElement('button');button.type='button';button.className='dashboard-hole-hit';button.style.left=hole.x+'%';button.style.top=hole.y+'%';button.dataset.markerLabel=hole.marker_label||String(index+1);button.title='FoilHole '+hole.foil_id;button.setAttribute('aria-label','Show FoilHole '+hole.foil_id);button.onpointerenter=()=>showDashboardHoleByIndex(index);button.onfocus=()=>showDashboardHoleByIndex(index);button.onclick=()=>showDashboardHoleByIndex(index);layer.appendChild(button)});updateHoleNavigation();
}
async function selectGrid(idx,scrollToReview=true){
  if(dashboardSaveTimer){clearTimeout(dashboardSaveTimer);dashboardSaveTimer=null;await saveDashboardReview()}
  selectedIdx=idx;activeHole=null;updateGridNavigation();
  document.querySelectorAll('.grid-marker,.grid-card').forEach(el=>el.classList.toggle('active',Number(el.dataset.idx)===idx));
  const inspector=document.getElementById('selected-detail'),empty=document.getElementById('inspector-empty'),body=document.getElementById('inspector-body');
  inspector.classList.add('has-selection');empty.style.display='none';body.classList.add('visible');
  document.getElementById('dashboard-save-state').textContent='Loading…';
  document.getElementById('hole-comparison')?.classList.remove('has-selection');
  document.getElementById('comparison-foil').removeAttribute('src');
  document.getElementById('comparison-data').removeAttribute('src');
  try{
    const response=await fetch('/grid_details?idx='+idx+'&t='+Date.now(),{cache:'no-store'});
    if(!response.ok)throw new Error('HTTP '+response.status);
    if(idx!==selectedIdx)return;
    const data=await response.json();selectedGridData=data;
    const grid=GRIDS.find(entry=>entry.idx===idx);
    if(grid){grid.collect=Boolean(data.collect);grid.collection_status=data.collection_status||''}
    document.getElementById('selected-title').textContent='GridSquare '+data.id;
    document.getElementById('selected-file').textContent=data.name;
    const decision=data.collection_status==='suitable'?'<span class="badge">Suitable</span>':data.collection_status==='unsuitable'?'<span class="badge">Unsuitable</span>':'';
    document.getElementById('selected-badges').innerHTML='<span class="badge">EPU '+esc(data.category??'N/A')+'</span><span class="badge">'+data.foil_count+' foils</span><span class="badge">'+data.data_count+' data</span>'+decision;
    const overlayWrap=document.getElementById('selected-overlay-wrap'),overlayImage=document.getElementById('selected-overlay-image');
    overlayWrap.style.display='block';
    overlayImage.onerror=()=>{if(overlayImage.dataset.fallback!=='1'){overlayImage.dataset.fallback='1';overlayImage.src=data.grid_preview+'&t='+Date.now();document.getElementById('overlay-note').textContent='The FoilHole overlay could not be loaded; showing the current GridSquare PNG.'}};
    overlayImage.dataset.fallback='0';overlayImage.src=(data.overlay_preview||data.grid_preview)+'&t='+Date.now();
    document.getElementById('selected-grid-mrc').disabled=!data.grid_has_mrc;
    document.getElementById('selected-grid-png').click();
    document.getElementById('overlay-note').textContent=data.overlay_preview?'Hover or click a screened FoilHole, or use Previous/Next below.':'Foil overlay unavailable; showing the GridSquare image without hole targets.';
    renderDashboardHoles(data.holes);if(data.holes&&data.holes.length)showDashboardHoleByIndex(0);setDashboardRating(data.rating||0,false);setDashboardCollectionStatus(data.collection_status||'',false);
    document.getElementById('dashboard-comment').value=data.comment||'';
    document.getElementById('dashboard-include').checked=data.include!==false;
    document.getElementById('dashboard-save-state').textContent=data.reviewed?'Saved review loaded':'Not yet reviewed';
    renderGridList(document.getElementById('grid-search').value);
    if(scrollToReview)document.getElementById('selected-detail').scrollIntoView({behavior:'smooth',block:'start'});
  }catch(error){selectedGridData=null;renderDashboardHoles([]);document.getElementById('dashboard-save-state').textContent='GridSquare could not be loaded: '+error.message}
}
function openLightbox(src,label){const box=document.getElementById('lightbox');document.getElementById('lightbox-image').src=src;document.getElementById('lightbox-image').alt=label;box.classList.add('open')}
function closeLightbox(){document.getElementById('lightbox').classList.remove('open');document.getElementById('lightbox-image').src=''}
function applyAtlas(){atlasContent.style.transform='translate('+atlasX.toFixed(1)+'px,'+atlasY.toFixed(1)+'px) scale('+atlasScale.toFixed(3)+')';document.getElementById('atlas-zoom').textContent=Math.round(atlasScale*100)+'%'}
function setAtlasScale(value,clientX=null,clientY=null){const next=Math.max(1,Math.min(8,value)),ratio=next/atlasScale;if(clientX!==null){const r=atlasViewport.getBoundingClientRect(),px=clientX-r.left-r.width/2,py=clientY-r.top-r.height/2;atlasX=px-(px-atlasX)*ratio;atlasY=py-(py-atlasY)*ratio}else{atlasX*=ratio;atlasY*=ratio}atlasScale=next;if(next===1){atlasX=0;atlasY=0}applyAtlas()}
function resetAtlas(){atlasScale=1;atlasX=0;atlasY=0;applyAtlas()}
function loadAtlasMrc(){const img=document.getElementById('atlas-image');if(!img||atlasMode!=='mrc')return;img.src='/atlas_overview_mrc?low='+encodeURIComponent(atlasLow)+'&high='+encodeURIComponent(atlasHigh)+'&session='+encodeURIComponent(CACHE_KEY)+'&t='+Date.now()}
function installAtlasMrcControls(){if(!atlasViewport)return;const panel=document.createElement('div');panel.className='atlas-mrc-contrast';panel.innerHTML='<label>Low <span class="atlas-low-value">1</span>% <input class="atlas-low" type="range" min="0" max="99" value="1"></label><label>High <span class="atlas-high-value">99</span>% <input class="atlas-high" type="range" min="1" max="100" value="99"></label>';atlasViewport.appendChild(panel);const low=panel.querySelector('.atlas-low'),high=panel.querySelector('.atlas-high'),lowValue=panel.querySelector('.atlas-low-value'),highValue=panel.querySelector('.atlas-high-value');function queue(changed){if(Number(low.value)>=Number(high.value)){if(changed===low)high.value=Math.min(100,Number(low.value)+1);else low.value=Math.max(0,Number(high.value)-1)}atlasLow=Number(low.value);atlasHigh=Number(high.value);lowValue.textContent=low.value;highValue.textContent=high.value;if(atlasMrcTimer)clearTimeout(atlasMrcTimer);atlasMrcTimer=setTimeout(loadAtlasMrc,300)}low.oninput=()=>queue(low);high.oninput=()=>queue(high);return panel}
if(HAS_ATLAS){const atlasContrast=installAtlasMrcControls();renderMarkers();atlasViewport.addEventListener('wheel',e=>{e.preventDefault();setAtlasScale(atlasScale*Math.exp(-e.deltaY*.0015),e.clientX,e.clientY)},{passive:false});atlasViewport.addEventListener('pointerdown',e=>{if(e.button!==0||e.target.closest('.grid-marker,.manual-target-marker,.atlas-tools,.atlas-mrc-contrast'))return;atlasDragging=true;atlasStartX=e.clientX-atlasX;atlasStartY=e.clientY-atlasY;atlasViewport.classList.add('dragging');atlasViewport.setPointerCapture(e.pointerId)});atlasViewport.addEventListener('pointermove',e=>{if(!atlasDragging)return;atlasX=e.clientX-atlasStartX;atlasY=e.clientY-atlasStartY;applyAtlas()});atlasViewport.addEventListener('pointerup',()=>{atlasDragging=false;atlasViewport.classList.remove('dragging')});atlasViewport.addEventListener('pointercancel',()=>{atlasDragging=false;atlasViewport.classList.remove('dragging')});atlasViewport.addEventListener('dblclick',e=>{if(!e.target.closest('.grid-marker,.manual-target-marker'))resetAtlas()});document.getElementById('atlas-plus').onclick=()=>setAtlasScale(atlasScale*1.3);document.getElementById('atlas-minus').onclick=()=>setAtlasScale(atlasScale/1.3);document.getElementById('atlas-reset').onclick=resetAtlas;document.querySelectorAll('.atlas-mode').forEach(b=>b.onclick=()=>{document.querySelectorAll('.atlas-mode').forEach(x=>x.classList.toggle('active',x===b));const img=document.getElementById('atlas-image');if(!img)return;atlasMode=b.dataset.mode;atlasContrast.classList.toggle('visible',atlasMode==='mrc');if(atlasMode==='mrc')loadAtlasMrc();else img.src=(atlasMode==='categories'?'/atlas_overview_categories':'/atlas_overview_raw')+'?session='+encodeURIComponent(CACHE_KEY);if(markerLayer)markerLayer.style.display=atlasMode==='screened'?'block':'none';resetAtlas()})}
const manualTargetToggle=document.getElementById('manual-target-toggle');if(manualTargetToggle)manualTargetToggle.onclick=()=>{targetMode=!targetMode;if(targetMode&&atlasMode!=='screened')document.querySelector('.atlas-mode[data-mode="screened"]').click();renderMarkers()};
arrangeSelectedWorkspace();
renderGridList();
document.getElementById('grid-search').addEventListener('input',event=>renderGridList(event.target.value));
document.querySelectorAll('.dashboard-rating').forEach(button=>button.onclick=()=>setDashboardRating(button.dataset.rating));
document.getElementById('mark-suitable').onclick=()=>setDashboardCollectionStatus('suitable');
document.getElementById('mark-unsuitable').onclick=()=>setDashboardCollectionStatus('unsuitable');
document.getElementById('mark-suitable').textContent='Mark suitable for collection';
document.getElementById('mark-unsuitable').textContent='Mark unsuitable for collection';
document.getElementById('clear-collection').onclick=()=>setDashboardCollectionStatus('');
document.getElementById('dashboard-comment').addEventListener('input',queueDashboardSave);
document.getElementById('dashboard-include').addEventListener('change',queueDashboardSave);
document.getElementById('selected-grid-png').onclick=()=>{if(selectedGridData)document.getElementById('selected-grid-image').src=selectedGridData.grid_preview};
document.getElementById('selected-grid-mrc').onclick=()=>{if(selectedGridData&&selectedGridData.grid_has_mrc)document.getElementById('selected-grid-image').src=mrcPreviewUrl('grid','')};
document.getElementById('comparison-foil-png').onclick=()=>{if(activeHole)document.getElementById('comparison-foil').src=activeHole.foil_preview};
document.getElementById('comparison-data-png').onclick=()=>{if(activeHole&&activeHole.data_preview)document.getElementById('comparison-data').src=activeHole.data_preview};
document.getElementById('comparison-foil-mrc').onclick=()=>{if(activeHole&&activeHole.foil_has_mrc)document.getElementById('comparison-foil').src=mrcPreviewUrl('foil',activeHole.foil_name)};
document.getElementById('comparison-data-mrc').onclick=()=>{if(activeHole&&activeHole.data_has_mrc)document.getElementById('comparison-data').src=mrcPreviewUrl('data',activeHole.data_name)};
installMrcViewer('comparison-foil','comparison-foil-png','comparison-foil-mrc','foil',()=>activeHole?{name:activeHole.foil_name,png:activeHole.foil_preview,hasMrc:activeHole.foil_has_mrc}:null);
installMrcViewer('comparison-data','comparison-data-png','comparison-data-mrc','data',()=>activeHole?{name:activeHole.data_name,png:activeHole.data_preview,hasMrc:activeHole.data_has_mrc}:null);
installMrcViewer('selected-overlay-image','selected-grid-png','selected-grid-mrc','grid',()=>selectedGridData?{name:'',png:selectedGridData.overlay_preview||selectedGridData.grid_preview,hasMrc:selectedGridData.grid_has_mrc}:null);
document.getElementById('lightbox-close').onclick=closeLightbox;
document.getElementById('lightbox').onclick=event=>{if(event.target.id==='lightbox')closeLightbox()};
const portableDialog=document.getElementById('portable-dialog'),portableDestination=document.getElementById('portable-destination'),portableStart=document.getElementById('portable-start'),portableStatus=document.getElementById('portable-status'),portableStatusText=document.getElementById('portable-status-text'),portableProgressBar=document.getElementById('portable-progress-bar');let portablePollTimer=null;
portableDestination.value=PORTABLE_DEFAULT;
function closePortableDialog(){if(portableDialog.open)portableDialog.close()}
function updatePortableStatus(job){const progress=Math.max(0,Math.min(100,Number(job.progress)||0));portableStatus.classList.add('visible');portableStatus.classList.toggle('error',job.status==='error');portableStatusText.textContent=(job.message||job.status||'Working…')+(job.path?'\\n'+job.path:'');portableProgressBar.style.width=progress+'%';if(job.status==='done'||job.status==='error'){portableStart.disabled=false;portableStart.textContent='Start export';if(portablePollTimer){clearTimeout(portablePollTimer);portablePollTimer=null}}}
async function pollPortableExport(jobId){try{const response=await fetch('/portable_export/'+encodeURIComponent(jobId)+'?t='+Date.now(),{cache:'no-store'});const job=await response.json();if(!response.ok)throw new Error(job.error||('HTTP '+response.status));updatePortableStatus(job);if(job.status!=='done'&&job.status!=='error')portablePollTimer=setTimeout(()=>pollPortableExport(jobId),900)}catch(error){updatePortableStatus({status:'error',progress:100,message:'Could not read export progress: '+error.message})}}
document.getElementById('portable-export-dashboard').onclick=()=>{portableStatus.classList.remove('visible','error');portableProgressBar.style.width='0%';if(typeof portableDialog.showModal==='function')portableDialog.showModal();else portableDialog.setAttribute('open','')};
document.getElementById('portable-dialog-close').onclick=closePortableDialog;document.getElementById('portable-cancel').onclick=closePortableDialog;
portableStart.onclick=async()=>{const destination=portableDestination.value.trim();if(!destination){updatePortableStatus({status:'error',progress:100,message:'Choose a destination folder.'});return}portableStart.disabled=true;portableStart.textContent='Exporting…';updatePortableStatus({status:'queued',progress:0,message:'Saving current review and preparing export…'});try{if(dashboardSaveTimer){clearTimeout(dashboardSaveTimer);dashboardSaveTimer=null;await saveDashboardReview()}const response=await fetch('/portable_export',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({destination})});const payload=await response.json();if(!response.ok)throw new Error(payload.error||('HTTP '+response.status));updatePortableStatus(payload.job||{status:'queued',progress:0,message:'Queued…'});pollPortableExport(payload.job_id)}catch(error){updatePortableStatus({status:'error',progress:100,message:'Could not start portable export: '+error.message})}};
document.addEventListener('keydown',async event=>{if(event.key==='Escape'){closeLightbox();return}if(event.key==='Enter'&&(event.metaKey||event.ctrlKey)&&selectedIdx!==null){event.preventDefault();if(dashboardSaveTimer){clearTimeout(dashboardSaveTimer);dashboardSaveTimer=null}await saveDashboardReview();if(selectedIdx<GRIDS.length-1)await selectGrid(selectedIdx+1,true)}});
let serverWasDisconnected=false;
function showServerDisconnected(message='The local EPU Mapper server is not responding. Return to the launcher and click Start review.'){serverWasDisconnected=true;const health=document.getElementById('health-light'),text=document.getElementById('health-text'),box=document.getElementById('preflight');if(health)health.className='health-light err';if(text)text.textContent='Server disconnected';box.className='preflight-pop show err';box.innerHTML='<strong>Dashboard disconnected:</strong> '+esc(message)}
const dashboardAtlasImage=document.getElementById('atlas-image');if(dashboardAtlasImage)dashboardAtlasImage.addEventListener('error',()=>showServerDisconnected('The requested atlas image could not be loaded. If this tab was already open, restart the server in the launcher.'));
async function monitorServer(){try{const response=await fetch('/status?t='+Date.now(),{cache:'no-store'});if(!response.ok)throw new Error('HTTP '+response.status);if(serverWasDisconnected)location.reload()}catch(_error){showServerDisconnected()}}
monitorServer();setInterval(monitorServer,5000);
fetch('/preflight?t='+Date.now()).then(r=>r.json()).then(data=>{const level=data.level||'ok',health=document.getElementById('health-light'),text=document.getElementById('health-text'),box=document.getElementById('preflight');if(health)health.className='health-light '+(level==='ok'?'ok':level==='error'?'err':'');if(text)text.textContent=level==='ok'?'Session ready':level==='error'?'Session issue':'Ready with warnings';const rows=(data.errors||[]).concat(data.warnings||[]);if(rows.length){box.className='preflight-pop show'+(level==='error'?' err':'');box.innerHTML='<strong>'+(level==='error'?'Session issue':'Preflight note')+':</strong> '+rows.slice(0,3).map(esc).join(' · ')}}).catch(()=>{const text=document.getElementById('health-text');if(text)text.textContent='Status unavailable'});
</script></body></html>"""
        if atlas_preview_path:
            atlas_content = (
                "<div class=\"atlas-image-wrap\"><img id=\"atlas-image\" class=\"atlas-image\" "
                f"src=\"/atlas_overview_raw?session={urllib.parse.quote(session_cache_key)}\" alt=\"Grid atlas\"><div id=\"marker-layer\" class=\"marker-layer\"></div></div>"
            )
            atlas_modes = (
                "<div class=\"atlas-header-tools\"><div class=\"segmented\"><button type=\"button\" class=\"atlas-mode active\" data-mode=\"screened\">Screened</button>"
                "<button type=\"button\" class=\"atlas-mode\" data-mode=\"categories\">EPU categories</button>"
                "<button type=\"button\" class=\"atlas-mode\" data-mode=\"raw\">PNG atlas</button>"
                "<button type=\"button\" class=\"atlas-mode\" data-mode=\"mrc\">Load MRC</button></div>"
                "<button type=\"button\" id=\"manual-target-toggle\" class=\"button manual-target-button\">Add unscreened targets</button>"
                "<div class=\"atlas-status-legend\" aria-label=\"Atlas marker legend\">"
                "<span class=\"legend-item\">Rating:</span><span class=\"legend-swatch rating-1\">1</span><span class=\"legend-swatch rating-2\">2</span><span class=\"legend-swatch rating-3\">3</span><span class=\"legend-swatch rating-4\">4</span><span class=\"legend-swatch rating-5\">5</span>"
                "<span class=\"legend-item\"><span class=\"legend-swatch suitable\">S</span>Suitable</span>"
                "<span class=\"legend-item\"><span class=\"legend-swatch unsuitable\">U</span>Not suitable</span>"
                "<span class=\"legend-item\"><span class=\"legend-swatch unmarked\">-</span>Unmarked</span>"
                "<span class=\"legend-item\"><span class=\"legend-swatch no-data\">!</span>No screening data</span>"
                "<span class=\"legend-item\"><span class=\"legend-swatch target\"></span>Unscreened target</span></div></div>"
            )
            atlas_aux = (
                "<div id=\"atlas-help\" class=\"atlas-help\">Scroll to zoom · drag to pan · double-click to reset</div>"
                "<div class=\"atlas-tools\"><button type=\"button\" id=\"atlas-minus\" aria-label=\"Zoom out\">−</button>"
                "<span id=\"atlas-zoom\" class=\"atlas-zoom\">100%</span>"
                "<button type=\"button\" id=\"atlas-plus\" aria-label=\"Zoom in\">+</button>"
                "<button type=\"button\" id=\"atlas-reset\" aria-label=\"Reset view\">↺</button></div>"
            )
        else:
            atlas_content = "<div class=\"atlas-empty\"><div><strong>Atlas unavailable</strong><br><span>Add the EPU Atlas folder when launching to enable spatial navigation.</span></div></div>"
            atlas_modes = ""
            atlas_aux = ""
        safe_grids_json = json.dumps(grid_summaries, separators=(",", ":")).replace("</", "<\\/")
        safe_unscreened_json = json.dumps(unscreened_summaries, separators=(",", ":")).replace("</", "<\\/")
        root_html = root_html.replace("__GRIDS_JSON__", safe_grids_json)
        root_html = root_html.replace("__UNSCREENED_JSON__", safe_unscreened_json)
        root_html = root_html.replace("__HAS_ATLAS__", "true" if atlas_preview_path else "false")
        root_html = root_html.replace("__SESSION_KEY__", json.dumps(session_storage_key))
        root_html = root_html.replace("__CACHE_KEY__", json.dumps(session_cache_key))
        portable_default = Path.home() / "Downloads"
        if not portable_default.is_dir():
            portable_default = Path.home()
        root_html = root_html.replace("__PORTABLE_DEFAULT__", json.dumps(str(portable_default)))
        root_html = root_html.replace("__SESSION_PATH__", str(base_dir).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        root_html = root_html.replace("__TOTAL__", str(len(grid_summaries)))
        root_html = root_html.replace("__REVIEWED__", str(reviewed_count))
        root_html = root_html.replace("__IMAGES__", str(image_count))
        root_html = root_html.replace("__MAPPED__", str(mapped_count))
        root_html = root_html.replace("__ATLAS_SOURCE__", atlas_source_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        root_html = root_html.replace("__ATLAS_CONTENT__", atlas_content)
        root_html = root_html.replace("__ATLAS_MODES__", atlas_modes)
        root_html = root_html.replace("__ATLAS_AUX__", atlas_aux)
        return HTMLResponse(root_html)

    @app.get("/review/{idx}")
    def review(idx: int):
        if idx < 0 or idx >= len(items):
            return HTMLResponse("<html><body>Invalid index</body></html>", status_code=404)
        return HTMLResponse(review_html(idx))

    @app.get("/grid_details")
    def grid_details(idx: int):
        if idx < 0 or idx >= len(items):
            raise HTTPException(status_code=404)
        item = items[idx]
        response = _normalize_review_entry(responses.get(item["dir"].name, {}), default_include=True)
        images = [
            {
                "kind": "grid",
                "name": "",
                "label": "GridSquare",
                "src": f"/grid?idx={idx}",
                "thumb": f"/thumb?idx={idx}&kind=grid&size=420",
                "has_mrc": bool(item.get("mrc")),
                "meta": [],
            }
        ]
        if item.get("atlas"):
            images.insert(
                0,
                {
                    "kind": "atlas",
                    "name": "",
                    "label": "Atlas location",
                    "src": f"/atlas?idx={idx}",
                    "thumb": f"/thumb?idx={idx}&kind=atlas&size=420",
                    "has_mrc": bool(item.get("atlas_mrc")),
                    "meta": [],
                },
            )
        if item.get("overlay"):
            images.append(
                {
                    "kind": "overlay",
                    "name": "",
                    "label": "Foil overlay",
                    "src": f"/overlay?idx={idx}",
                    "thumb": f"/thumb?idx={idx}&kind=overlay&size=420",
                    "has_mrc": False,
                    "meta": [],
                }
            )
        for foil in item["foils"]:
            encoded = urllib.parse.quote(foil["path"].name)
            images.append(
                {
                    "kind": "foil",
                    "name": foil["path"].name,
                    "label": f"FoilHole {foil['id']}",
                    "src": f"/foil?idx={idx}&name={encoded}",
                    "thumb": f"/thumb?idx={idx}&kind=foil&name={encoded}&size=420",
                    "has_mrc": bool(foil.get("mrc")),
                    "meta": [],
                }
            )
        for data_image in item["data"]:
            encoded = urllib.parse.quote(data_image["path"].name)
            images.append(
                {
                    "kind": "data",
                    "name": data_image["path"].name,
                    "label": f"Data {data_image['id']}",
                    "src": f"/data?idx={idx}&name={encoded}",
                    "thumb": f"/thumb?idx={idx}&kind=data&name={encoded}&size=420",
                    "has_mrc": bool(data_image.get("mrc")),
                    "meta": data_image.get("meta") or [],
                }
            )
        return JSONResponse(
            {
                "idx": idx,
                "id": str(item["id"]),
                "name": item["name"],
                "category": item.get("epu_category_score"),
                "foil_count": len(item["foils"]),
                "data_count": len(item["data"]),
                "collect": response["collect"],
                "collection_status": response["collection_status"],
                "rating": response["rating"],
                "comment": response["comment"],
                "include": response["include"],
                "reviewed": response["reviewed"] if item["dir"].name in responses else False,
                "grid_preview": f"/preview.png?idx={idx}&kind=grid&size=1800&session={session_cache_key}",
                "grid_has_mrc": bool(item.get("mrc")),
                "overlay_preview": f"/preview.png?idx={idx}&kind=overlay&size=1800&session={session_cache_key}" if item.get("overlay") else "",
                "holes": _hole_preview_records(idx, item),
                "review_url": f"/review/{idx}",
                "images": images,
            }
        )

    @app.get("/preflight")
    def preflight():
        level = "error" if preflight_state["errors"] else ("warn" if preflight_state["warnings"] else "ok")
        return JSONResponse(
            {
                "level": level,
                "errors": preflight_state["errors"],
                "warnings": preflight_state["warnings"],
                "info": preflight_state["info"],
            }
        )

    @app.get("/manual_targets")
    def get_manual_targets():
        return JSONResponse(
            {
                "targets": list(manual_targets.values()),
                "unscreened": _unscreened_atlas_summaries(),
            }
        )

    @app.post("/manual_target")
    async def set_manual_target(request: Request):
        try:
            payload = await request.json()
            key = str(payload.get("key", "")).strip()
            selected = bool(payload.get("selected", True))
        except Exception:
            return JSONResponse({"error": "invalid request"}, status_code=400)
        available = {entry["key"]: entry for entry in _unscreened_atlas_summaries()}
        target = available.get(key)
        if target is None:
            return JSONResponse({"error": "Atlas GridSquare is not an unscreened target"}, status_code=404)
        if selected:
            manual_targets[key] = {
                "key": key,
                "gridsquare_id": target["id"],
                "category": target.get("category"),
                "position": target["position"],
                "selected_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
        else:
            manual_targets.pop(key, None)
        _save_json_dict(manual_targets_file, manual_targets)
        return JSONResponse({"ok": True, "key": key, "selected": selected, "count": len(manual_targets)})

    @app.get("/grid")
    def grid(idx: int):
        if idx < 0 or idx >= len(items):
            raise HTTPException(status_code=404)
        return FileResponse(items[idx]["grid_img"], media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/atlas")
    def atlas(idx: int):
        if idx < 0 or idx >= len(items):
            raise HTTPException(status_code=404)
        item = items[idx]
        atlas_path = item["atlas"]
        if not atlas_path or not atlas_path.is_file():
            raise HTTPException(status_code=404)
        atlas_center_key = item.get("atlas_center_key")
        atlas_centers = item.get("atlas_centers") or {}
        if atlas_center_key and atlas_center_key in atlas_centers:
            payload = _render_atlas_overlay(
                atlas_path,
                atlas_centers,
                atlas_center_key,
                item.get("atlas_ref_w"),
                item.get("atlas_ref_h"),
                f"GridSquare {item['id']}",
            )
            if payload:
                return Response(content=payload, media_type="image/png", headers={"Cache-Control": "no-store"})
        return FileResponse(atlas_path, headers={"Cache-Control": "no-store"})

    @app.get("/atlas_overview_screened")
    def atlas_overview_screened():
        if atlas_screened_preview:
            return Response(content=atlas_screened_preview, media_type="image/png", headers={"Cache-Control": "no-store"})
        if atlas_preview_path and atlas_preview_path.is_file():
            return FileResponse(atlas_preview_path, headers={"Cache-Control": "no-store"})
        raise HTTPException(status_code=404)

    @app.get("/atlas_overview_categories")
    def atlas_overview_categories():
        if atlas_category_preview:
            return Response(content=atlas_category_preview, media_type="image/png", headers={"Cache-Control": "no-store"})
        if atlas_preview_path and atlas_preview_path.is_file():
            return FileResponse(atlas_preview_path, headers={"Cache-Control": "no-store"})
        raise HTTPException(status_code=404)

    @app.get("/atlas_overview_raw")
    def atlas_overview_raw():
        if atlas_raw_preview:
            return Response(content=atlas_raw_preview, media_type="image/png", headers={"Cache-Control": "no-store"})
        if atlas_preview_path and atlas_preview_path.is_file():
            return FileResponse(atlas_preview_path, headers={"Cache-Control": "no-store"})
        raise HTTPException(status_code=404)

    @app.get("/atlas_overview_mrc")
    def atlas_overview_mrc(low: float = 1.0, high: float = 99.0):
        safe_low = max(0.0, min(99.0, float(low)))
        safe_high = max(safe_low + 0.1, min(100.0, float(high)))
        cache_key = (round(safe_low, 1), round(safe_high, 1))
        payload = atlas_mrc_previews.get(cache_key)
        if payload is None and atlas_preview_path and atlas_preview_path.is_file():
            atlas_mrc_path = _find_atlas_mrc(atlas_preview_path)
            atlas_rgb = _mrc_to_image(atlas_mrc_path, safe_low, safe_high) if atlas_mrc_path else None
            if atlas_rgb is not None:
                buf = io.BytesIO()
                atlas_rgb.convert("RGB").save(buf, format="PNG", compress_level=3)
                payload = buf.getvalue()
                if len(atlas_mrc_previews) >= 12:
                    atlas_mrc_previews.pop(next(iter(atlas_mrc_previews)))
                atlas_mrc_previews[cache_key] = payload
        if payload:
            return Response(content=payload, media_type="image/png", headers={"Cache-Control": "no-store"})
        raise HTTPException(status_code=404)

    @app.get("/overlay")
    def overlay(idx: int):
        if idx < 0 or idx >= len(items):
            raise HTTPException(status_code=404)
        overlay_path = items[idx].get("overlay")
        if not overlay_path or not overlay_path.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(overlay_path, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.get("/thumb")
    def thumb(idx: int, kind: str, name: str = "", size: int = _THUMB_DEFAULT_SIZE):
        if idx < 0 or idx >= len(items):
            raise HTTPException(status_code=404)
        safe_size = max(96, min(1024, int(size)))
        source = _resolve_media_path(items[idx], kind, name)
        if source is None or not source.is_file():
            raise HTTPException(status_code=404)
        cached = _build_thumb(source, safe_size)
        if cached and cached.is_file():
            return FileResponse(cached, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
        return FileResponse(source, headers={"Cache-Control": "no-store"})

    @app.get("/preview.png")
    def png_preview(idx: int, kind: str, name: str = "", size: int = 1600):
        if idx < 0 or idx >= len(items):
            raise HTTPException(status_code=404)
        source = _resolve_media_path(items[idx], kind, name)
        if source is None or not source.is_file():
            raise HTTPException(status_code=404)
        cached = _build_png_preview(source, size)
        if cached and cached.is_file():
            return FileResponse(cached, media_type="image/png", headers={"Cache-Control": "no-store"})
        raise HTTPException(status_code=404)

    @app.get("/data")
    def data(idx: int, name: str):
        if idx < 0 or idx >= len(items):
            raise HTTPException(status_code=404)
        for p in items[idx]["data"]:
            if p["path"].name == name and p["path"].is_file():
                return FileResponse(p["path"], headers={"Cache-Control": "no-store"})
        raise HTTPException(status_code=404)

    @app.get("/foil")
    def foil(idx: int, name: str):
        if idx < 0 or idx >= len(items):
            raise HTTPException(status_code=404)
        for p in items[idx]["foils"]:
            if p["path"].name == name and p["path"].is_file():
                return FileResponse(p["path"], headers={"Cache-Control": "no-store"})
        raise HTTPException(status_code=404)

    @app.get("/mrc_file")
    def mrc_file(idx: int, kind: str, name: str, low: float = 2.0, high: float = 98.0):
        if idx < 0 or idx >= len(items):
            raise HTTPException(status_code=404)
        mrc_path = None
        if kind == "grid":
            mrc_path = items[idx]["mrc"]
        elif kind == "atlas":
            mrc_path = items[idx].get("atlas_mrc")
        elif kind == "foil":
            for p in items[idx]["foils"]:
                if p["path"].name == name:
                    mrc_path = p["mrc"]
                    break
        elif kind == "data":
            for p in items[idx]["data"]:
                if p["path"].name == name:
                    mrc_path = p["mrc"]
                    break
        if not mrc_path or not mrc_path.is_file():
            raise HTTPException(status_code=404)
        img = _mrc_to_image(mrc_path, low, high)
        if img is None:
            raise HTTPException(status_code=404)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return Response(content=buf.getvalue(), media_type="image/png")

    @app.get("/status")
    def status():
        return JSONResponse(
            {
                "total": status_state["total"],
                "loaded": status_state["loaded"],
                "events": list(_OVERLAY_EVENTS)[:20],
            }
        )

    @app.get("/mrc")
    def mrc(idx: int, low: float = 2.0, high: float = 98.0):
        if idx < 0 or idx >= len(items):
            raise HTTPException(status_code=404)
        mrc_path = items[idx]["mrc"]
        if not mrc_path or not mrc_path.is_file():
            raise HTTPException(status_code=404)
        img = _mrc_to_image(mrc_path, low, high)
        if img is None:
            raise HTTPException(status_code=404)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return Response(content=buf.getvalue(), media_type="image/png")

    @app.get("/draft")
    def draft(idx: int):
        if idx < 0 or idx >= len(items):
            raise HTTPException(status_code=404)
        item_name = _item_key(idx)
        with drafts_lock:
            entry = drafts.get(item_name)
            if not isinstance(entry, dict):
                entry = responses.get(item_name)
        if isinstance(entry, dict):
            entry = _normalize_review_entry(entry, default_include=True)
        return JSONResponse({"draft": entry})

    @app.post("/collection")
    async def set_collection(request: Request):
        try:
            payload = await request.json()
            idx = int(payload.get("idx", -1))
        except Exception:
            return JSONResponse({"error": "invalid request"}, status_code=400)
        if idx < 0 or idx >= len(items):
            return JSONResponse({"error": "invalid idx"}, status_code=400)
        name = _item_key(idx)
        existing = responses.get(name)
        if isinstance(existing, dict):
            entry = _normalize_review_entry(existing, default_include=True)
        else:
            entry = _normalize_review_entry({}, default_include=True)
            entry["reviewed"] = False
        requested_status = str(payload.get("collection_status", "") or "").strip().lower()
        if requested_status not in ("suitable", "unsuitable"):
            requested_status = "suitable" if bool(payload.get("collect", False)) else ""
        entry["collection_status"] = requested_status
        entry["collect"] = requested_status == "suitable"
        entry["updated_at"] = time.time()
        responses[name] = entry
        _save_responses(responses)
        with drafts_lock:
            draft_entry = drafts.get(name)
            if isinstance(draft_entry, dict):
                draft_entry = _normalize_review_entry(draft_entry, default_include=True)
                draft_entry["collect"] = entry["collect"]
                draft_entry["collection_status"] = entry["collection_status"]
                draft_entry["updated_at"] = entry["updated_at"]
                drafts[name] = draft_entry
                _save_drafts(drafts)
        return JSONResponse(
            {"ok": True, "collect": entry["collect"], "collection_status": entry["collection_status"]}
        )

    @app.post("/draft")
    async def save_draft(request: Request):
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        try:
            idx = int(payload.get("idx", -1))
        except Exception:
            idx = -1
        if idx < 0 or idx >= len(items):
            return JSONResponse({"error": "invalid idx"}, status_code=400)
        entry = _normalize_review_entry(payload, default_include=True)
        item_name = _item_key(idx)
        with drafts_lock:
            drafts[item_name] = entry
            _save_drafts(drafts)
        return JSONResponse({"ok": True, "draft": entry})

    @app.post("/submit")
    async def submit(request: Request):
        try:
            data = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        try:
            try:
                idx = int(data.get("idx", -1))
            except Exception:
                idx = -1
            if idx < 0 or idx >= len(items):
                return JSONResponse({"next": None})
            normalized = _normalize_review_entry(data, default_include=True)
            rating = normalized["rating"]
            comment = normalized["comment"]
            include = normalized["include"]
            collect = normalized["collect"]
            collection_status = normalized["collection_status"]
            name = items[idx]["dir"].name
            responses[name] = {
                "rating": rating,
                "comment": comment,
                "include": include,
                "collect": collect,
                "collection_status": collection_status,
                "reviewed": True,
                "updated_at": time.time(),
            }
            _save_responses(responses)
            responses.update(_load_responses())
            with drafts_lock:
                if name in drafts:
                    drafts.pop(name, None)
                    _save_drafts(drafts)
            next_idx = idx + 1
            if next_idx >= len(items):
                return JSONResponse({"next": None})
            return JSONResponse({"next": next_idx})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/review_state")
    async def save_review_state(request: Request):
        try:
            payload = await request.json()
            idx = int(payload.get("idx", -1))
        except Exception:
            return JSONResponse({"error": "invalid request"}, status_code=400)
        if idx < 0 or idx >= len(items):
            return JSONResponse({"error": "invalid idx"}, status_code=400)
        normalized = _normalize_review_entry(payload, default_include=True)
        normalized["reviewed"] = True
        normalized["updated_at"] = time.time()
        name = _item_key(idx)
        responses[name] = normalized
        _save_responses(responses)
        with drafts_lock:
            drafts.pop(name, None)
            _save_drafts(drafts)
        return JSONResponse({"ok": True, "review": normalized})

    @app.get("/summary")
    def summary():
        return JSONResponse({"summary": summary_state["text"]})

    @app.post("/summary")
    async def set_summary(request: Request):
        try:
            data = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        raw_summary = data.get("summary", "")
        if not isinstance(raw_summary, str):
            raw_summary = str(raw_summary)
        normalized = _save_review_summary(base_dir, raw_summary)
        summary_state["text"] = normalized
        return JSONResponse({"summary": normalized})

    def _report_paths(all_screened_images: bool = False) -> tuple[Path, Path]:
        if report_file:
            report = report_file
            details = report_file.with_name(f"{report_file.stem}_details.pdf")
        else:
            prefix = label_prefix
            report_name = f"{prefix}Screening_report.pdf"
            details_name = f"{prefix}Screening_details.pdf"
            report = base_dir / report_name
            details = base_dir / details_name
        if all_screened_images:
            report = report.with_name(f"{report.stem}_all_screened{report.suffix}")
            details = details.with_name(f"{details.stem}_all_screened{details.suffix}")
        return report, details

    def _temp_report_path(filename: str) -> Path:
        temp_root = Path(tempfile.gettempdir()) / "EPUMapperReview"
        temp_root.mkdir(parents=True, exist_ok=True)
        return temp_root / filename

    @app.get("/export.json")
    def export_json():
        filename = f"{label_prefix}review_export.json" if label_prefix else "review_export.json"
        payload = _export_payload()
        text = json.dumps(payload, indent=2)
        return Response(
            content=text,
            media_type="application/json",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )

    @app.get("/export.csv")
    def export_csv():
        filename = f"{label_prefix}review_export.csv" if label_prefix else "review_export.csv"
        rows = _export_rows()
        columns = [
            "index",
            "gridsquare_id",
            "gridsquare_dir",
            "gridsquare_image",
            "include",
            "collect",
            "collection_status",
            "rating",
            "comment",
            "foil_count",
            "data_count",
            "epu_category_score",
            "atlas_available",
            "overlay_available",
        ]
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        return Response(
            content=buf.getvalue(),
            media_type="text/csv",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )

    @app.get("/report.html")
    def embedded_html_report(scope: str = "representative"):
        if scope not in {"representative", "all_screened"}:
            return JSONResponse({"error": "scope must be 'representative' or 'all_screened'"}, status_code=400)
        all_screened_images = scope == "all_screened"
        suffix = "Screening_report_all_screened.html" if all_screened_images else "Screening_report.html"
        filename = f"{label_prefix}{suffix}" if label_prefix else suffix
        target = base_dir / filename
        try:
            write_embedded_html_report(
                base_dir,
                target,
                atlas_name,
                responses,
                overlay=overlay_enabled,
                atlas_overlay=atlas_overlay,
                global_summary=summary_state["text"],
                skip_foil_processing=skip_foil_processing,
                all_screened_images=all_screened_images,
            )
        except (PermissionError, OSError):
            target = _temp_report_path(filename)
            write_embedded_html_report(
                base_dir,
                target,
                atlas_name,
                responses,
                overlay=overlay_enabled,
                atlas_overlay=atlas_overlay,
                global_summary=summary_state["text"],
                skip_foil_processing=skip_foil_processing,
                all_screened_images=all_screened_images,
            )
        return FileResponse(target, media_type="text/html", filename=filename, headers={"Cache-Control": "no-store"})

    @app.post("/report_jobs")
    async def create_report_job(request: Request):
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                payload = {}
        except Exception:
            payload = {}
        kind = str(payload.get("kind", "full")).strip().lower()
        scope = str(payload.get("scope", "representative")).strip().lower()
        if kind == "overview":
            kind = "full"
        if kind not in {"full", "details"}:
            return JSONResponse({"error": "kind must be 'full' or 'details'"}, status_code=400)
        if scope not in {"representative", "all_screened"}:
            return JSONResponse({"error": "scope must be 'representative' or 'all_screened'"}, status_code=400)
        job_id = secrets.token_urlsafe(8)
        now = time.time()
        with report_jobs_lock:
            report_jobs[job_id] = {
                "id": job_id,
                "kind": kind,
                "scope": scope,
                "status": "queued",
                "progress": 0,
                "message": "Queued...",
                "created_at": now,
                "updated_at": now,
            }
        threading.Thread(target=_run_report_job, args=(job_id, kind, scope), daemon=True).start()
        return JSONResponse({"job_id": job_id, "job": _job_state(job_id)})

    @app.get("/report_jobs/{job_id}")
    def report_job_status(job_id: str):
        state = _job_state(job_id)
        if state is None:
            raise HTTPException(status_code=404)
        return JSONResponse(state)

    @app.get("/report_jobs/{job_id}/download")
    def report_job_download(job_id: str):
        state = _job_state(job_id)
        if state is None:
            raise HTTPException(status_code=404)
        if state.get("status") != "done":
            return JSONResponse({"error": "report not ready"}, status_code=409)
        with report_jobs_lock:
            raw = report_jobs.get(job_id, {}).get("path")
            filename = report_jobs.get(job_id, {}).get("filename", f"{job_id}.pdf")
        if not raw:
            raise HTTPException(status_code=404)
        path = Path(raw)
        if not path.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(path, media_type="application/pdf", filename=filename, headers={"Cache-Control": "no-store"})

    @app.post("/portable_export")
    async def create_portable_export(request: Request):
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                payload = {}
        except Exception:
            payload = {}
        destination_value = str(payload.get("destination", "") or "").strip()
        if not destination_value:
            return JSONResponse({"error": "Choose a destination folder."}, status_code=400)
        destination = Path(destination_value).expanduser().resolve()
        if not destination.is_dir():
            return JSONResponse({"error": f"Destination folder not found: {destination}"}, status_code=400)
        job_id = secrets.token_urlsafe(8)
        now = time.time()
        with portable_jobs_lock:
            portable_jobs[job_id] = {
                "id": job_id,
                "status": "queued",
                "progress": 0,
                "message": "Queued…",
                "created_at": now,
                "updated_at": now,
            }
        threading.Thread(target=_run_portable_job, args=(job_id, destination), daemon=True).start()
        return JSONResponse({"job_id": job_id, "job": _portable_job_state(job_id)})

    @app.get("/portable_export/{job_id}")
    def portable_export_status(job_id: str):
        state = _portable_job_state(job_id)
        if state is None:
            raise HTTPException(status_code=404)
        return JSONResponse(state)

    @app.get("/done")
    def done():
        summary_json = json.dumps(summary_state["text"])
        done_html = """<html><head><meta charset="utf-8"><title>Review complete</title>
<style>
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:#f5f6f8;color:#111;}
.page{max-width:600px;margin:0 auto;padding:36px;}
.card{background:#fff;border:1px solid #e1e4e8;border-radius:12px;padding:24px;box-shadow:0 1px 3px rgba(0,0,0,0.08);}
.title{font-size:22px;font-weight:600;margin-bottom:8px;}
.note{color:#555;font-size:14px;margin-bottom:12px;}
.summary-label{display:block;font-weight:600;margin:14px 0 6px;font-size:14px;}
textarea{width:100%;max-width:100%;border:1px solid #c9ced6;border-radius:8px;padding:8px;font-size:14px;box-sizing:border-box;}
.btn{display:inline-block;margin-top:10px;border:1px solid #1b6ef3;background:#1b6ef3;color:#fff;border-radius:8px;padding:10px 14px;font-size:14px;text-decoration:none;margin-right:8px;}
.btn.secondary{background:#fff;color:#1b6ef3;}
#done-status{margin-top:12px;font-size:13px;color:#1b6ef3;}
.progress-wrap{margin-top:10px;border:1px solid #d7deea;background:#fbfcff;border-radius:10px;padding:10px;display:none;}
.progress-label{font-size:13px;color:#445;margin-bottom:8px;}
.progress-track{height:8px;border-radius:999px;background:#dfe5f1;overflow:hidden;}
.progress-bar{height:100%;width:0%;background:#1b6ef3;transition:width 0.2s linear;}
.scope-card{margin:14px 0;padding:12px;border:1px solid #d7deea;border-radius:10px;background:#f8faff;}
.scope-title{font-size:14px;font-weight:700;margin-bottom:8px;}
.scope-option{display:block;padding:7px 4px;font-size:14px;cursor:pointer;}
.scope-option input{margin-right:7px;}
.scope-help{display:block;margin:3px 0 0 25px;color:#667085;font-size:12px;}
</style>
</head><body><div class="page"><div class="card">
<div class="title">All GridSquares reviewed</div>
<div class="note">Before generating reports, optionally add one session-level summary sentence.</div>
<label class="summary-label" for="global-summary">Session summary (one sentence, optional)</label>
<textarea id="global-summary" rows="2" maxlength="__SUMMARY_MAX_LEN__"></textarea>
<div><button type="button" class="btn" id="save-summary">Save summary</button></div>
<div class="note">You can now add unscreened Atlas GridSquares as manual collection targets.</div>
<a class="btn secondary" href="/?targeting=1">Select unscreened Atlas targets</a>
<div class="scope-card">
  <div class="scope-title">Screening images to include</div>
  <label class="scope-option"><input type="radio" name="report-scope" value="representative" checked>One highest-rated suitable GridSquare<span class="scope-help">Recommended compact report.</span></label>
  <label class="scope-option"><input type="radio" name="report-scope" value="all_screened">All screened GridSquares and images<span class="scope-help">Explicit full-session export; may produce a very large PDF or HTML file.</span></label>
</div>
<a class="btn" id="report-link" href="#">Generate full PDF report</a>
<a class="btn secondary" id="html-report-link" href="#">Download self-contained HTML report</a>
<div class="note">Export structured review data:</div>
<a class="btn secondary" id="export-csv" href="/export.csv">Download CSV</a>
<a class="btn secondary" id="export-json" href="/export.json">Download JSON</a>
<div id="report-progress-wrap" class="progress-wrap">
  <div id="report-progress-label" class="progress-label">Preparing report…</div>
  <div class="progress-track"><div id="report-progress-bar" class="progress-bar"></div></div>
</div>
<div id="done-status"></div>
</div></div>
<script>
const SUMMARY_INITIAL = __SUMMARY_JSON__;
const summaryEl = document.getElementById('global-summary');
const doneStatus = document.getElementById('done-status');
const progressWrap = document.getElementById('report-progress-wrap');
const progressLabel = document.getElementById('report-progress-label');
const progressBar = document.getElementById('report-progress-bar');
summaryEl.value = SUMMARY_INITIAL || '';

async function saveSummary(showStatus=true){
  const payload = {summary: summaryEl.value || ''};
  const res = await fetch('/summary', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify(payload)
  });
  if (!res.ok){
    const txt = await res.text();
    throw new Error(txt || ('Failed to save summary (' + res.status + ')'));
  }
  const data = await res.json();
  summaryEl.value = data.summary || '';
  if (showStatus){
    doneStatus.textContent = 'Summary saved.';
  }
}

document.getElementById('save-summary').addEventListener('click', async ()=>{
  doneStatus.textContent = 'Saving summary…';
  try{
    await saveSummary(true);
  }catch(err){
    doneStatus.textContent = String(err);
  }
});

function setProgress(visible, label, pct){
  progressWrap.style.display = visible ? 'block' : 'none';
  if (label) progressLabel.textContent = label;
  if (typeof pct === 'number'){
    const clamped = Math.max(0, Math.min(100, pct));
    progressBar.style.width = String(clamped) + '%';
  }
}

async function pollReportJob(jobId){
  while (true){
    const res = await fetch('/report_jobs/' + encodeURIComponent(jobId) + '?t=' + Date.now());
    if (!res.ok){
      throw new Error('Failed to fetch report status (' + res.status + ')');
    }
    const job = await res.json();
    setProgress(true, job.message || 'Generating report…', job.progress || 0);
    if (job.status === 'done'){
      doneStatus.textContent = 'Report ready. Download starting…';
      const dlUrl = (job.download_url || ('/report_jobs/' + encodeURIComponent(jobId) + '/download')) + '?t=' + Date.now();
      window.location = dlUrl;
      return;
    }
    if (job.status === 'error'){
      throw new Error(job.message || 'Report generation failed.');
    }
    await new Promise(resolve => setTimeout(resolve, 800));
  }
}

function selectedReportScope(){
  const selected = document.querySelector('input[name="report-scope"]:checked');
  return selected ? selected.value : 'representative';
}

async function startReport(kind, msg, scope){
  doneStatus.textContent = msg;
  setProgress(true, 'Submitting report job…', 5);
  try{
    await saveSummary(false);
  }catch(err){
    setProgress(false, '', 0);
    doneStatus.textContent = String(err);
    return;
  }
  try{
    const res = await fetch('/report_jobs', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({kind, scope})
    });
    const payload = await res.json();
    if (!res.ok || !payload.job_id){
      throw new Error(payload.error || ('Failed to create report job (' + res.status + ')'));
    }
    await pollReportJob(payload.job_id);
  }catch(err){
    doneStatus.textContent = String(err);
    setProgress(true, 'Report generation failed.', 100);
  }
}

document.getElementById('report-link').addEventListener('click', (ev) => {
  ev.preventDefault();
  const scope = selectedReportScope();
  const message = scope === 'all_screened' ? 'Generating all-screened PDF report…' : 'Generating compact PDF report…';
  startReport('full', message, scope);
});
document.getElementById('html-report-link').addEventListener('click', async (ev) => {
  ev.preventDefault();
  doneStatus.textContent = 'Preparing HTML report…';
  try{
    await saveSummary(false);
    window.location = '/report.html?scope=' + encodeURIComponent(selectedReportScope());
  }catch(err){
    doneStatus.textContent = String(err);
  }
});
	const SESSION_STORAGE_KEY = __SESSION_STORAGE_KEY_JSON__;
	localStorage.removeItem('last_idx_' + SESSION_STORAGE_KEY);
	</script>
	</body></html>"""
        done_html = done_html.replace("__SUMMARY_JSON__", summary_json)
        done_html = done_html.replace("__SUMMARY_MAX_LEN__", str(_SUMMARY_MAX_LEN))
        done_html = done_html.replace("__SESSION_STORAGE_KEY_JSON__", json.dumps(session_storage_key))
        return HTMLResponse(done_html)

    @app.get("/report")
    def report(scope: str = "representative"):
        if scope not in {"representative", "all_screened"}:
            return JSONResponse({"error": "scope must be 'representative' or 'all_screened'"}, status_code=400)
        all_screened_images = scope == "all_screened"
        report_path, _details_path = _report_paths(all_screened_images=all_screened_images)
        target_path = report_path
        try:
            write_combined_report(
                base_dir,
                target_path,
                atlas_name,
                responses,
                overlay=overlay_enabled,
                atlas_overlay=atlas_overlay,
                global_summary=summary_state["text"],
                skip_foil_processing=skip_foil_processing,
                all_screened_images=all_screened_images,
            )
        except (PermissionError, OSError):
            # Common on read-only/network session folders; fall back to a writable temp directory.
            target_path = _temp_report_path(report_path.name)
            write_combined_report(
                base_dir,
                target_path,
                atlas_name,
                responses,
                overlay=overlay_enabled,
                atlas_overlay=atlas_overlay,
                global_summary=summary_state["text"],
                skip_foil_processing=skip_foil_processing,
                all_screened_images=all_screened_images,
            )
        except Exception as exc:
            return JSONResponse({"error": f"failed to generate full report: {exc}"}, status_code=500)
        return FileResponse(target_path, media_type="application/pdf", filename=target_path.name, headers={"Cache-Control": "no-store"})

    @app.get("/selected_report")
    def selected_report():
        _overview_path, details_path = _report_paths()
        target_path = details_path
        try:
            write_selected_report(
                base_dir,
                target_path,
                atlas_name,
                responses,
                overlay=overlay_enabled,
                atlas_overlay=atlas_overlay,
                global_summary=summary_state["text"],
                skip_foil_processing=skip_foil_processing,
            )
        except (PermissionError, OSError):
            # Common on read-only/network session folders; fall back to a writable temp directory.
            target_path = _temp_report_path(details_path.name)
            write_selected_report(
                base_dir,
                target_path,
                atlas_name,
                responses,
                overlay=overlay_enabled,
                atlas_overlay=atlas_overlay,
                global_summary=summary_state["text"],
                skip_foil_processing=skip_foil_processing,
            )
        except Exception as exc:
            return JSONResponse({"error": f"failed to generate selected report: {exc}"}, status_code=500)
        return FileResponse(target_path, media_type="application/pdf", filename=target_path.name, headers={"Cache-Control": "no-store"})

    threading.Thread(target=_prime_thumbnail_cache, daemon=True).start()

    return app


def generate_details_report(
    base_dir: Path,
    atlas_name: str | None,
    session_label: str | None,
    details_output: Path | None,
    overlay: bool,
    overlay_transform: str | None,
    atlas_overlay: bool = True,
    skip_foil_processing: bool = False,
) -> Path:
    base_dir = base_dir.resolve()
    _configure_overlay_transform(overlay_transform)
    summary_text = _load_review_summary(base_dir)
    grids = _collect_grids(base_dir)
    if not grids:
        raise RuntimeError(f"no GridSquare directories found in {base_dir}")
    responses = {
        gdir.name: {"include": True, "collect": False, "collection_status": "", "rating": 0, "comment": ""}
        for _gid, gdir in grids
    }
    if details_output:
        target_path = details_output
    else:
        prefix = _prefix_from_label(session_label)
        name = f"{prefix}Screening_details.pdf"
        target_path = base_dir / name
    write_selected_report(
        base_dir,
        target_path,
        atlas_name,
        responses,
        overlay=overlay,
        atlas_overlay=atlas_overlay,
        global_summary=summary_text,
        skip_foil_processing=skip_foil_processing,
    )
    return target_path


def main():
    parser = argparse.ArgumentParser(description="Web review app for GridSquare folders")
    parser.add_argument("grid_dir", type=Path, help="path to a GridSquare directory, Images-Disc*, or session root")
    parser.add_argument(
        "--atlas",
        type=str,
        help="atlas image path/name, or an atlas directory containing Atlas_*.jpg/.png",
    )
    parser.add_argument("--report", type=Path, help="output PDF path")
    label_env_default = os.environ.get("SESSION_LABEL") or os.environ.get("GRID_LABEL") or os.environ.get("REPORT_PREFIX")
    parser.add_argument(
        "--session-label",
        "--grid-label",
        dest="session_label",
        type=str,
        default=label_env_default,
        help="name prefixed to generated PDF filenames (defaults to SESSION_LABEL / GRID_LABEL / REPORT_PREFIX env vars if set)",
    )
    parser.add_argument(
        "--details-only",
        "--export-all-details",
        dest="details_only",
        action="store_true",
        help="generate the detailed PDF for every GridSquare and exit (skips launching the web app)",
    )
    parser.add_argument("--details-output", type=Path, help="custom output path when using --details-only / --export-all-details")
    parser.add_argument(
        "--overlay",
        dest="overlay",
        action="store_true",
        default=True,
        help="display foil_overlay.png images beside each GridSquare and include them in the selected PDF report (default: on)",
    )
    parser.add_argument(
        "--no-overlay",
        dest="overlay",
        action="store_false",
        help="disable foil overlays even if metadata is available",
    )
    parser.add_argument(
        "--overlay-transform",
        choices=["auto", "identity", "rot90", "rot180", "rot270", "mirror_x", "mirror_y", "mirror_diag", "mirror_diag_inv"],
        default="identity",
        help="Overlay rotation/mirror transform when --overlay is enabled (default: identity; choose 'auto' to detect)",
    )
    parser.add_argument(
        "--skip-foil-processing",
        action="store_true",
        help="skip FoilHole/data discovery and only map GridSquares onto the atlas for faster loading on large sessions",
    )
    parser.add_argument(
        "--atlas-overlay",
        dest="atlas_overlay",
        action="store_true",
        default=True,
        help="highlight the current GridSquare on the atlas when Atlas.dm metadata is available (default: on)",
    )
    parser.add_argument(
        "--no-atlas-overlay",
        dest="atlas_overlay",
        action="store_false",
        help="disable atlas GridSquare highlighting",
    )
    parser.add_argument(
        "--images-subdir",
        type=str,
        help="Name of the Images-Disc* subdirectory when pointing at a session root (defaults to IMAGES_SUBDIR env or auto-detect)",
    )
    parser.add_argument("--host", type=str, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--open", action="store_true", help="automatically open browser")
    args = parser.parse_args()
    preferred_disc = args.images_subdir or os.environ.get("IMAGES_SUBDIR")
    try:
        grid_root = _resolve_grid_root(args.grid_dir, preferred_disc)
    except RuntimeError as exc:
        print(f"[review_app] {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    overlay_transform = args.overlay_transform if args.overlay else None
    if args.details_only:
        try:
            details_path = generate_details_report(
                grid_root,
                args.atlas,
                args.session_label,
                args.details_output,
                args.overlay,
                overlay_transform,
                atlas_overlay=args.atlas_overlay,
                skip_foil_processing=args.skip_foil_processing,
            )
        except RuntimeError as exc:
            print(f"[review_app] {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
        except Exception as exc:
            print(f"[review_app] Failed to build detailed PDF: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
        print(f"[review_app] Detailed PDF written to {details_path}")
        return
    app = create_app(
        grid_root,
        args.atlas,
        args.report,
        args.overlay,
        overlay_transform,
        session_label=args.session_label,
        atlas_overlay=args.atlas_overlay,
        skip_foil_processing=args.skip_foil_processing,
    )
    if args.open:
        url = f"http://{args.host}:{args.port}"
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            print(
                f"[review_app] Cannot start server: {args.host}:{args.port} is already in use. "
                "Use --port to choose a free port or stop the other process.",
                file=sys.stderr,
            )
            raise SystemExit(2) from exc
        raise


if __name__ == "__main__":
    main()
