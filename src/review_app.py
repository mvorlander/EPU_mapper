import argparse
import csv
import io
import errno
import hashlib
import json
import os
import re
import secrets
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
    write_review_report,
    write_selected_report,
    _resolve_atlas_path,
    parse_metadata,
    parse_grid_info,
)


def _find_mrc_for_jpg(path: Path) -> Path | None:
    cand = path.with_suffix(".mrc")
    if cand.is_file():
        return cand
    cand = path.with_suffix(".mrcs")
    if cand.is_file():
        return cand
    return None


def _format_meta(meta: dict) -> list[str]:
    lines = []
    for key in ("pixel_size", "exposure", "dose", "defocus"):
        if key in meta:
            txt = meta[key]
            if key == "dose":
                txt += " e-/Å²"
            lines.append(f"{key.replace('_', ' ')}: {txt}")
    return lines


_OVERLAY_TOOLS: tuple | None = None
_OVERLAY_TRANSFORM: str | None = None
_OVERLAY_EVENTS: deque = deque(maxlen=200)
_ATLAS_MAPPING_CACHE: dict[Path, tuple[dict[str, tuple[float, float]], float | None, float | None, str | None]] = {}


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


def _parse_atlas_dm_centers(dm_path: Path) -> dict[str, tuple[float, float]]:
    centers: dict[str, tuple[float, float]] = {}
    try:
        root = ET.parse(dm_path).getroot()
    except Exception:
        return centers

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
    return centers


def _atlas_reference_dimensions(atlas_path: Path, centers: dict[str, tuple[float, float]]) -> tuple[float | None, float | None]:
    atlas_mrc = atlas_path.with_suffix(".mrc")
    if atlas_mrc.is_file():
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


def _load_atlas_mapping(atlas_path: Path) -> tuple[dict[str, tuple[float, float]], float | None, float | None, str | None]:
    atlas_key = atlas_path.resolve()
    cached = _ATLAS_MAPPING_CACHE.get(atlas_key)
    if cached is not None:
        return cached

    dm_path = next((p for p in _atlas_dm_candidates(atlas_key) if p.is_file()), None)
    if dm_path is None:
        result = ({}, None, None, "Atlas marker unavailable: Atlas.dm metadata not found.")
        _ATLAS_MAPPING_CACHE[atlas_key] = result
        return result

    centers = _parse_atlas_dm_centers(dm_path)
    if not centers:
        result = ({}, None, None, f"Atlas marker unavailable: could not parse GridSquare centers from {dm_path.name}.")
        _ATLAS_MAPPING_CACHE[atlas_key] = result
        return result

    ref_w, ref_h = _atlas_reference_dimensions(atlas_key, centers)
    result = (centers, ref_w, ref_h, None)
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
    try:
        with Image.open(atlas_path) as atlas_image:
            atlas_rgb = atlas_image.convert("RGB")
    except Exception:
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
        fill=(220, 40, 40, 60),
        outline=(220, 40, 40, 240),
        width=ring_width,
    )
    cross = int(radius * 1.6)
    cross_width = max(2, radius // 6)
    draw.line((center_x - cross, center_y, center_x + cross, center_y), fill=(220, 40, 40, 240), width=cross_width)
    draw.line((center_x, center_y - cross, center_x, center_y + cross), fill=(220, 40, 40, 240), width=cross_width)

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
        draw.rectangle((text_x - 4, text_y - 3, text_x + text_w + 4, text_y + text_h + 3), fill=(255, 255, 255, 200))
        draw.text((text_x, text_y), text, fill=(150, 25, 25, 255), font=font)

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

    if overlay_requested and not overlay_enabled:
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
                centers, _ref_w, _ref_h, atlas_msg = _load_atlas_mapping(atlas_sample)
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


def _generate_overlay_image(gdir: Path) -> Path | None:
    """Generate foil_overlay.png inside `gdir` using the standalone helper."""
    tools = _overlay_tools()
    if not tools:
        return None
    compute_markers, plot_overlay = tools
    _record_status(f"[overlay] Generating overlay for {gdir.name}...")
    try:
        grid_img, markers = compute_markers(gdir)
    except Exception as exc:
        _record_status(f"[overlay] skipping {gdir.name}: {exc}")
        return None
    if not markers:
        _record_status(f"[overlay] no FoilHole markers for {gdir.name}")
        return None
    out_path = gdir / "foil_overlay.png"
    try:
        plot_overlay(grid_img, markers, title=gdir.name, output=out_path, dpi=180)
    except Exception as exc:
        _record_status(f"[overlay] failed to render overlay for {gdir.name}: {exc}")
        return None
    if out_path.is_file():
        _record_status(f"[overlay] Finished {gdir.name}")
        return out_path
    _record_status(f"[overlay] Overlay file missing for {gdir.name}")
    return None


def _ensure_overlay_image(gdir: Path, base_dir: Path) -> tuple[Path | None, str | None]:
    """Return a fresh overlay PNG path if generation succeeds, else fall back to cached copy."""
    generated = _generate_overlay_image(gdir)
    if generated:
        return generated, None
    cached = _find_overlay_image(gdir, base_dir)
    if cached:
        return cached, "Using cached overlay image (new generation failed)."
    return None, "Overlay unavailable for this GridSquare (missing metadata or generation failed)."


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
) -> FastAPI:
    _OVERLAY_EVENTS.clear()
    _configure_overlay_transform(overlay_transform)
    base_dir = base_dir.resolve()
    label_prefix = _prefix_from_label(session_label)
    overlay_enabled = bool(overlay)
    overlay_notice_html = ""
    if overlay_enabled:
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
        atlas_mrc_path = _find_mrc_for_jpg(atlas_path) if atlas_path else None
        atlas_centers: dict[str, tuple[float, float]] = {}
        atlas_ref_w: float | None = None
        atlas_ref_h: float | None = None
        atlas_center_key: str | None = None
        atlas_overlay_message: str | None = None
        if atlas_overlay and atlas_path and atlas_path.is_file():
            atlas_centers, atlas_ref_w, atlas_ref_h, atlas_overlay_message = _load_atlas_mapping(atlas_path)
            for lookup_key in _atlas_lookup_keys(gdir, _gid):
                if lookup_key in atlas_centers:
                    atlas_center_key = lookup_key
                    break
            if atlas_center_key is None and atlas_centers and atlas_overlay_message is None:
                atlas_overlay_message = "GridSquare not found in Atlas metadata."
        foils, datas = gather_foil_and_data(gdir)
        foils = _latest_only(foils)
        datas = _latest_only(datas)
        foil_list = []
        for foil_id in sorted(foils.keys()):
            for foil_path in foils[foil_id]:
                foil_list.append({"id": foil_id, "path": foil_path, "mrc": _find_mrc_for_jpg(foil_path)})
        data_list = []
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
        if overlay_enabled:
            overlay_path, overlay_message = _ensure_overlay_image(gdir, base_dir)
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
                "overlay": overlay_path,
                "overlay_message": overlay_message,
                "foils": foil_list,
                "data": data_list,
            }
        )
        status_state["loaded"] = idx_item

    responses_file = base_dir / "review_responses.json"
    drafts_file = _drafts_file_path(base_dir)
    summary_state = {"text": _load_review_summary(base_dir)}
    session_storage_key = hashlib.sha1(str(base_dir).encode("utf-8")).hexdigest()[:16]
    thumb_cache_dir = Path(tempfile.gettempdir()) / "EPUMapperThumbCache" / session_storage_key
    thumb_cache_dir.mkdir(parents=True, exist_ok=True)
    drafts_lock = threading.Lock()
    report_jobs_lock = threading.Lock()
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
    report_jobs: dict[str, dict] = {}

    app = FastAPI()

    def _item_key(idx: int) -> str:
        return items[idx]["dir"].name

    def _normalize_review_entry(payload: dict, default_include: bool = False) -> dict:
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
        updated_at_raw = payload.get("updated_at", time.time())
        try:
            updated_at = float(updated_at_raw)
        except Exception:
            updated_at = time.time()
        return {
            "rating": rating,
            "comment": comment,
            "include": include,
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
            response = _normalize_review_entry(responses.get(name, {}), default_include=False)
            rows.append(
                {
                    "index": idx_item + 1,
                    "gridsquare_id": item["id"],
                    "gridsquare_dir": name,
                    "gridsquare_image": item["name"],
                    "include": bool(response.get("include", False)),
                    "rating": int(response.get("rating", 0)),
                    "comment": str(response.get("comment", "")),
                    "foil_count": len(item["foils"]),
                    "data_count": len(item["data"]),
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
        }

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

    def _run_report_job(job_id: str, kind: str) -> None:
        _update_job(job_id, status="running", progress=10, message="Preparing report...")
        overview_path, details_path = _report_paths()
        target_path = overview_path if kind == "overview" else details_path

        def _write_target(path: Path) -> None:
            if kind == "overview":
                write_review_report(
                    base_dir,
                    path,
                    atlas_name,
                    responses,
                    atlas_overlay=atlas_overlay,
                    global_summary=summary_state["text"],
                )
            else:
                write_selected_report(
                    base_dir,
                    path,
                    atlas_name,
                    responses,
                    overlay=overlay_enabled,
                    atlas_overlay=atlas_overlay,
                    global_summary=summary_state["text"],
                )

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

    def review_html(idx: int) -> str:
        item = items[idx]
        has_data = bool(item["data"])
        nodata_html = "" if has_data else "<div class=\"note warn\">No screening data available for this GridSquare.</div>"
        grid_has_mrc = bool(item["mrc"])
        grid_mrc_json = "true" if grid_has_mrc else "false"
        atlas_has_mrc = bool(item.get("atlas_mrc"))
        atlas_mrc_json = "true" if atlas_has_mrc else "false"
        grid_mrc_note = "" if grid_has_mrc else "<div class=\"note\">No grid MRC available.</div>"
        ts = int(time.time() * 1000)
        default_kind = "atlas" if item["atlas"] else "grid"
        default_label = "Atlas" if default_kind == "atlas" else "GridSquare"
        default_src = f"/atlas?idx={idx}&t={ts}" if default_kind == "atlas" else f"/grid?idx={idx}&t={ts}"
        default_has_mrc_json = atlas_mrc_json if default_kind == "atlas" else grid_mrc_json
        atlas_note_html = ""
        if item["atlas"]:
            atlas_html = f"<img id=\"atlasimg\" src=\"/atlas?idx={idx}&t={ts}\" class=\"atlas-img\" data-kind=\"atlas\" data-has-mrc=\"{1 if atlas_has_mrc else 0}\"/>"
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
        overlay_html = ""
        overlay_inline_notice = ""
        if item.get("overlay"):
            overlay_html = (
                f"<div class=\"image-frame\"><div class=\"image-caption\">Foil overlay</div>"
                f"<img id=\"overlayimg\" class=\"frame-image\" src=\"/overlay?idx={idx}&t={ts}\"/></div>"
            )
        elif item.get("overlay_message"):
            overlay_inline_notice = f"<div class=\"note warn\">{item['overlay_message']}</div>"
        grid_section_html = f"<div class=\"grid-panel\">{grid_frame_html}{overlay_html}</div>{overlay_inline_notice}"
        data_by_id = {}
        for d in item["data"]:
            data_by_id.setdefault(d["id"], []).append(d)
        if item["foils"]:
            groups = []
            for f in item["foils"]:
                foil_thumb = (
                    f"<img class=\"thumb\" loading=\"lazy\" data-kind=\"foil\" data-name=\"{f['path'].name}\" "
                    f"data-has-mrc=\"{1 if f['mrc'] else 0}\" "
                    f"src=\"/thumb?idx={idx}&kind=foil&name={urllib.parse.quote(f['path'].name)}&size={_THUMB_DEFAULT_SIZE}\"/>"
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
        else:
            thumb_html = "<div class=\"section-title\">Foil holes and data</div><div class=\"note\">No foil images found.</div>"
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
        return f"""<html><head><meta charset=\"utf-8\"><title>Review GridSquare {item['id']}</title>
<style>
:root{{color-scheme:light;--img-size:420px;--thumb-size:280px;}}
body{{margin:0;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:#f5f6f8;color:#111;}}
.page{{max-width:1300px;margin:0 auto;padding:24px;}}
.header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;}}
.title{{font-size:20px;font-weight:600;}}
.subtitle{{color:#666;font-size:13px;margin-top:2px;}}
.progress{{color:#666;}}
.layout{{display:grid;grid-template-columns:1fr 340px;gap:16px;align-items:start;}}
.card{{background:#fff;border:1px solid #e1e4e8;border-radius:10px;padding:14px;box-shadow:0 1px 2px rgba(0,0,0,0.04);}}
.grid-panel{{display:flex;flex-wrap:wrap;gap:14px;margin-bottom:16px;}}
.image-frame{{background:#fff;border:1px solid #e1e4e8;border-radius:10px;padding:10px;display:flex;flex-direction:column;gap:8px;flex:1 1 360px;max-width:100%;}}
.image-caption{{font-size:13px;font-weight:600;color:#222;}}
.image-frame img.frame-image{{width:100%;max-width:var(--img-size);max-height:var(--img-size);height:auto;object-fit:contain;display:block;}}
.viewer-viewport{{position:relative;width:100%;max-width:var(--img-size);height:var(--img-size);overflow:hidden;border:1px solid #e1e4e8;border-radius:8px;background:#fbfcff;display:flex;align-items:center;justify-content:center;}}
.viewer-viewport.pan-enabled{{touch-action:none;}}
.atlas-img{{max-width:100%;height:auto;display:block;}}
.atlas-img.selected{{outline:2px solid #1b6ef3;border-radius:6px;}}
#gridimg{{max-width:100%;max-height:100%;width:auto;height:auto;object-fit:contain;transform-origin:center center;transition:transform 0.05s linear;cursor:pointer;user-select:none;-webkit-user-drag:none;}}
.viewer-viewport.pan-enabled #gridimg{{cursor:grab;}}
.viewer-viewport.pan-enabled #gridimg.dragging{{cursor:grabbing;}}
.actions{{margin:8px 0;display:flex;gap:8px;flex-wrap:wrap;}}
.btn{{border:1px solid #c9ced6;background:#fff;border-radius:8px;padding:8px 10px;font-size:14px;cursor:pointer;}}
.btn:hover{{background:#f0f2f5;}}
.btn.active{{background:#1b6ef3;color:#fff;border-color:#1b6ef3;}}
.btn:disabled{{opacity:0.5;cursor:default;}}
.rate-buttons{{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0;}}
.rate{{border:1px solid #c9ced6;background:#fff;border-radius:8px;padding:8px 10px;font-size:14px;cursor:pointer;min-width:38px;}}
.rate.active{{background:#1b6ef3;color:#fff;border-color:#1b6ef3;}}
.note{{color:#555;font-size:13px;margin:6px 0;}}
.note.warn{{color:#b00020;}}
.note.ok{{color:#13653f;}}
.preflight-box{{background:#fbfcff;border:1px solid #d7deea;border-radius:10px;padding:10px;margin-bottom:14px;}}
.preflight-title{{font-size:13px;font-weight:600;margin-bottom:4px;}}
.preflight-list{{margin:0;padding-left:18px;font-size:12px;color:#455;}}
.preflight-list li{{margin:2px 0;}}
.status-card{{margin-top:16px;}}
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
</style>
</head>
<body>
<div id=\"loading-overlay\"><div class=\"spinner\"></div><div>Loading images…</div></div>
<div class=\"page\">
<div class=\"header\"><div><div class=\"title\">GridSquare {item['id']}</div><div class=\"subtitle\">{item['name']}</div></div><div class=\"progress\" id=\"progress\">{idx + 1} / {total_len}</div></div>
{overlay_banner}
{nodata_html}
{preflight_html}
<div class=\"layout\">
<div class=\"left\">
{grid_section_html}
<div class=\"card\">{thumb_html}</div>
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
<button type=\"button\" id=\"show-jpeg\" class=\"btn\">Show JPEG</button>
<button type=\"button\" id=\"show-mrc\" class=\"btn\">Show MRC for selected image</button>
<button type=\"button\" id=\"zoom-out\" class=\"btn\">Zoom -</button>
<button type=\"button\" id=\"zoom-in\" class=\"btn\">Zoom +</button>
<button type=\"button\" id=\"zoom-reset\" class=\"btn\">Reset zoom</button>
<button type=\"button\" id=\"pan-toggle\" class=\"btn\">Pan: Off</button>
</div>
<div id=\"zoom-level\" class=\"note\">Zoom: 100%</div>
{grid_mrc_note}
<div class=\"note\">Viewer defaults to the Atlas when available. Click Atlas, GridSquare, FoilHole, or Data images to switch what is shown here.</div>
<div class=\"note\">Use MRC + sliders for contrast, then enable Pan to drag the zoomed image within the viewer.</div>
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
<div class=\"section-title\">Report</div>
<label class=\"note\"><input type=\"checkbox\" id=\"include-report\"> Include this GridSquare in the final report</label>
<div>Selected rating: <span id=\"selected\">3</span></div>
<div>Comments:</div>
<textarea id=\"comment\" rows=\"4\"></textarea>
<div id=\"autosave-state\" class=\"note autosave-state\"></div>
<div class=\"submit-row\"><button type=\"button\" id=\"submit\" class=\"btn\">Submit (Ctrl+Enter)</button></div>
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
localStorage.setItem(LAST_IDX_KEY, IDX);
const commentEl = document.getElementById('comment');
const includeEl = document.getElementById('include-report');
const hotkeysEl = document.getElementById('hotkeys-enabled');
const autosaveEl = document.getElementById('autosave-state');
let rating = 3;
let selectedKind = DEFAULT_KIND;
let selectedName = '';
let selectedHasMrc = DEFAULT_HAS_MRC;
let zoomLevel = 1.0;
let panEnabled = false;
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
    const payload = {{idx: IDX, rating: rating, comment: document.getElementById('comment').value, include: document.getElementById('include-report').checked}};
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
  if (kind === 'grid') return '/grid?idx=' + IDX + '&t=' + Date.now();
  if (kind === 'atlas') return '/atlas?idx=' + IDX + '&t=' + Date.now();
  if (kind === 'foil') return '/foil?idx=' + IDX + '&name=' + encodeURIComponent(name) + '&t=' + Date.now();
  return '/data?idx=' + IDX + '&name=' + encodeURIComponent(name) + '&t=' + Date.now();
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
  clampPan();
  img.style.transform = 'translate(' + panX.toFixed(1) + 'px,' + panY.toFixed(1) + 'px) scale(' + zoomLevel.toFixed(3) + ')';
  document.getElementById('zoom-level').textContent = 'Zoom: ' + Math.round(zoomLevel * 100) + '%';
}}
function updatePanUi(){{
  const btn = document.getElementById('pan-toggle');
  const viewport = document.getElementById('viewer-viewport');
  if (btn) {{
    btn.textContent = panEnabled ? 'Pan: On' : 'Pan: Off';
    btn.classList.toggle('active', panEnabled);
  }}
  if (viewport) {{
    viewport.classList.toggle('pan-enabled', panEnabled);
  }}
}}
function setZoom(value){{
  const next = Math.max(0.5, Math.min(4.0, value));
  const ratio = zoomLevel > 0 ? (next / zoomLevel) : 1.0;
  zoomLevel = next;
  panX *= ratio;
  panY *= ratio;
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
  panEnabled = false;
  updatePanUi();
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
  if (!panEnabled || zoomLevel <= 1.0) return;
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
const atlasImg = document.getElementById('atlasimg');
if (atlasImg) {{
  atlasImg.onclick = () => selectImage('atlas', '', ATLAS_HAS_MRC);
}}
if (DEFAULT_KIND === 'atlas' && atlasImg) {{
  selectImage('atlas', '', ATLAS_HAS_MRC);
}} else {{
  selectImage('grid', '', GRID_HAS_MRC);
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
document.getElementById('pan-toggle').onclick = () => {{
  panEnabled = !panEnabled;
  if (!panEnabled) {{
    isDragging = false;
    viewerImg.classList.remove('dragging');
  }}
  updatePanUi();
}};
function persistState(){{
  const nowTs = Date.now() / 1000.0;
  const data = {{
    rating,
    comment: commentEl.value,
    include: includeEl.checked,
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
document.getElementById('low').oninput = updateContrast;
document.getElementById('high').oninput = updateContrast;
updateButtons();
updatePanUi();
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
        root_html = """<html><head><meta charset=\"utf-8\"><title>Grid review</title>
<style>
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:#f5f6f8;color:#111;}
.page{max-width:900px;margin:0 auto;padding:32px;}
.card{background:#fff;border:1px solid #e1e4e8;border-radius:12px;padding:20px;box-shadow:0 1px 2px rgba(0,0,0,0.04);}
.title{font-size:22px;font-weight:600;margin-bottom:8px;}
.note{color:#555;font-size:14px;line-height:1.4;margin-bottom:10px;}
.btn{display:inline-block;margin-top:14px;border:1px solid #1b6ef3;background:#1b6ef3;color:#fff;border-radius:8px;padding:10px 14px;font-size:14px;text-decoration:none;margin-right:8px;}
.btn.secondary{background:#fff;color:#1b6ef3;}
.preflight{margin-top:14px;padding:10px;border-radius:10px;border:1px solid #d7deea;background:#fbfcff;font-size:13px;}
.preflight.ok{border-color:#b8d7c3;background:#f5fbf6;}
.preflight.warn{border-color:#e8d7ad;background:#fffaf0;}
.preflight.err{border-color:#e7b9b9;background:#fff6f6;}
.preflight ul{margin:6px 0 0;padding-left:18px;}
</style>
</head><body><div class=\"page\"><div class=\"card\"><div class=\"title\">Grid review</div>
<div class=\"note\">Review GridSquare, FoilHole, and Data images. Click any thumbnail to inspect it. Use "Show MRC" to adjust contrast when available. Rate each GridSquare and leave comments. A PDF report is generated at the end.</div>
<a class=\"btn\" id=\"start-btn\" href=\"/review/0\">Start review</a>
<a class=\"btn secondary\" id=\"resume-btn\" style=\"display:none;\" href=\"#\">Resume last visited</a>
<div id=\"preflight\" class=\"preflight\">Running preflight checks…</div>
</div></div>
<script>
const resumeBtn = document.getElementById('resume-btn');
const startBtn = document.getElementById('start-btn');
const preflightEl = document.getElementById('preflight');
const SESSION_STORAGE_KEY = __SESSION_STORAGE_KEY_JSON__;
const LAST_IDX_KEY = 'last_idx_' + SESSION_STORAGE_KEY;
const lastIdx = localStorage.getItem(LAST_IDX_KEY);
if (lastIdx !== null){{
  resumeBtn.style.display = 'inline-block';
  resumeBtn.href = '/review/' + lastIdx;
  resumeBtn.onclick = () => {{ window.location = '/review/' + lastIdx; return false; }};
}}
fetch('/preflight?t=' + Date.now()).then(r => r.json()).then(data => {{
  const level = data.level || 'ok';
  preflightEl.classList.remove('ok', 'warn', 'err');
  preflightEl.classList.add(level === 'ok' ? 'ok' : (level === 'warn' ? 'warn' : 'err'));
  const rows = (data.errors || []).concat(data.warnings || []).concat((data.info || []).slice(0, 2));
  if (!rows.length) {{
    preflightEl.textContent = 'Preflight checks passed.';
    return;
  }}
  preflightEl.innerHTML = '<strong>Preflight</strong><ul>' + rows.slice(0, 6).map(x => '<li>' + x + '</li>').join('') + '</ul>';
}}).catch(() => {{
  preflightEl.textContent = 'Preflight status unavailable.';
}});
</script>
</body></html>"""
        root_html = root_html.replace("__SESSION_STORAGE_KEY_JSON__", json.dumps(session_storage_key))
        return HTMLResponse(root_html)

    @app.get("/review/{idx}")
    def review(idx: int):
        if idx < 0 or idx >= len(items):
            return HTMLResponse("<html><body>Invalid index</body></html>", status_code=404)
        return HTMLResponse(review_html(idx))

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

    @app.get("/grid")
    def grid(idx: int):
        if idx < 0 or idx >= len(items):
            raise HTTPException(status_code=404)
        return FileResponse(items[idx]["grid_img"], media_type="image/jpeg")

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
                return Response(content=payload, media_type="image/png")
        return FileResponse(atlas_path)

    @app.get("/overlay")
    def overlay(idx: int):
        if idx < 0 or idx >= len(items):
            raise HTTPException(status_code=404)
        overlay_path = items[idx].get("overlay")
        if not overlay_path or not overlay_path.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(overlay_path, media_type="image/png")

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

    @app.get("/data")
    def data(idx: int, name: str):
        if idx < 0 or idx >= len(items):
            raise HTTPException(status_code=404)
        for p in items[idx]["data"]:
            if p["path"].name == name and p["path"].is_file():
                return FileResponse(p["path"])
        raise HTTPException(status_code=404)

    @app.get("/foil")
    def foil(idx: int, name: str):
        if idx < 0 or idx >= len(items):
            raise HTTPException(status_code=404)
        for p in items[idx]["foils"]:
            if p["path"].name == name and p["path"].is_file():
                return FileResponse(p["path"])
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
                entry = None
        return JSONResponse({"draft": entry})

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
        entry = _normalize_review_entry(payload, default_include=False)
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
            normalized = _normalize_review_entry(data, default_include=False)
            rating = normalized["rating"]
            comment = normalized["comment"]
            include = normalized["include"]
            name = items[idx]["dir"].name
            responses[name] = {"rating": rating, "comment": comment, "include": include}
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

    def _report_paths() -> tuple[Path, Path]:
        if report_file:
            overview = report_file
            details = report_file.with_name(f"{report_file.stem}_details.pdf")
        else:
            prefix = label_prefix
            overview_name = f"{prefix}Screening_overview.pdf"
            details_name = f"{prefix}Screening_details.pdf"
            overview = base_dir / overview_name
            details = base_dir / details_name
        return overview, details

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
            "rating",
            "comment",
            "foil_count",
            "data_count",
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

    @app.post("/report_jobs")
    async def create_report_job(request: Request):
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                payload = {}
        except Exception:
            payload = {}
        kind = str(payload.get("kind", "overview")).strip().lower()
        if kind not in {"overview", "details"}:
            return JSONResponse({"error": "kind must be 'overview' or 'details'"}, status_code=400)
        job_id = secrets.token_urlsafe(8)
        now = time.time()
        with report_jobs_lock:
            report_jobs[job_id] = {
                "id": job_id,
                "kind": kind,
                "status": "queued",
                "progress": 0,
                "message": "Queued...",
                "created_at": now,
                "updated_at": now,
            }
        threading.Thread(target=_run_report_job, args=(job_id, kind), daemon=True).start()
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
</style>
</head><body><div class="page"><div class="card">
<div class="title">All GridSquares reviewed</div>
<div class="note">Before generating PDFs, optionally add one session-level summary sentence.</div>
<label class="summary-label" for="global-summary">Session summary (one sentence, optional)</label>
<textarea id="global-summary" rows="2" maxlength="__SUMMARY_MAX_LEN__"></textarea>
<div><button type="button" class="btn" id="save-summary">Save summary</button></div>
<div class="note">Then generate PDF summaries below. You can reopen this session later to continue editing notes or regenerate reports.</div>
<a class="btn" id="report-link" href="#">Generate overview PDF</a>
<a class="btn" id="selected-link" href="#">Generate details PDF</a>
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

async function startReport(kind, msg){
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
      body: JSON.stringify({kind})
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
  startReport('overview', 'Generating overview PDF…');
});
document.getElementById('selected-link').addEventListener('click', (ev) => {
  ev.preventDefault();
  startReport('details', 'Generating detailed PDF…');
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
    def report():
        overview_path, _details_path = _report_paths()
        target_path = overview_path
        try:
            write_review_report(
                base_dir,
                target_path,
                atlas_name,
                responses,
                atlas_overlay=atlas_overlay,
                global_summary=summary_state["text"],
            )
        except (PermissionError, OSError):
            # Common on read-only/network session folders; fall back to a writable temp directory.
            target_path = _temp_report_path(overview_path.name)
            write_review_report(
                base_dir,
                target_path,
                atlas_name,
                responses,
                atlas_overlay=atlas_overlay,
                global_summary=summary_state["text"],
            )
        except Exception as exc:
            return JSONResponse({"error": f"failed to generate overview report: {exc}"}, status_code=500)
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
) -> Path:
    base_dir = base_dir.resolve()
    _configure_overlay_transform(overlay_transform)
    summary_text = _load_review_summary(base_dir)
    grids = _collect_grids(base_dir)
    if not grids:
        raise RuntimeError(f"no GridSquare directories found in {base_dir}")
    responses = {gdir.name: {"include": True, "rating": 0, "comment": ""} for _gid, gdir in grids}
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
