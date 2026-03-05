import argparse
import io
import errno
import json
import os
import re
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

    def _load_responses() -> dict[str, dict]:
        if responses_file.is_file():
            try:
                return json.loads(responses_file.read_text())
            except Exception:
                return {}
        return {}

    def _save_responses(current: dict[str, dict]) -> None:
        try:
            responses_file.write_text(json.dumps(current))
        except Exception:
            return

    responses = _load_responses()

    app = FastAPI()

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
            f"<img id=\"gridimg\" src=\"{default_src}\"/></div>"
        )
        overlay_html = ""
        overlay_inline_notice = ""
        if item.get("overlay"):
            overlay_html = (
                f"<div class=\"image-frame\"><div class=\"image-caption\">Foil overlay</div>"
                f"<img id=\"overlayimg\" src=\"/overlay?idx={idx}&t={ts}\"/></div>"
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
                foil_thumb = f"<img class=\"thumb\" data-kind=\"foil\" data-name=\"{f['path'].name}\" data-has-mrc=\"{1 if f['mrc'] else 0}\" src=\"/foil?idx={idx}&name={urllib.parse.quote(f['path'].name)}\"/>"
                data_imgs = []
                for p in data_by_id.get(f["id"], []):
                    meta_html = ""
                    if p.get("meta"):
                        meta_html = "<div class=\"meta\">" + "<br>".join(p["meta"]) + "</div>"
                    data_imgs.append(f"<div class=\"data-card\"><img class=\"thumb\" data-kind=\"data\" data-name=\"{p['path'].name}\" data-has-mrc=\"{1 if p['mrc'] else 0}\" src=\"/data?idx={idx}&name={urllib.parse.quote(p['path'].name)}\"/>{meta_html}</div>")
                data_block = f"<div class=\"thumb-grid\">{''.join(data_imgs)}</div>" if data_imgs else "<div class=\"note\">No data images for this FoilHole.</div>"
                groups.append(f"<div class=\"foil-group\"><div class=\"foil-row\">{foil_thumb}<div class=\"data-block\">{data_block}</div></div></div>")
            thumb_html = "<div class=\"section-title\">Foil holes and data</div>" + "".join(groups)
        else:
            thumb_html = "<div class=\"section-title\">Foil holes and data</div><div class=\"note\">No foil images found.</div>"
        overlay_banner = overlay_notice_html or ""
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
.image-frame img{{width:100%;max-width:var(--img-size);max-height:var(--img-size);height:auto;object-fit:contain;display:block;}}
.atlas-img{{max-width:100%;height:auto;display:block;}}
.atlas-img.selected{{outline:2px solid #1b6ef3;border-radius:6px;}}
#gridimg{{transform-origin:center center;transition:transform 0.15s ease;}}
.actions{{margin:8px 0;display:flex;gap:8px;flex-wrap:wrap;}}
.btn{{border:1px solid #c9ced6;background:#fff;border-radius:8px;padding:8px 10px;font-size:14px;cursor:pointer;}}
.btn:hover{{background:#f0f2f5;}}
.btn:disabled{{opacity:0.5;cursor:default;}}
.rate-buttons{{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0;}}
.rate{{border:1px solid #c9ced6;background:#fff;border-radius:8px;padding:8px 10px;font-size:14px;cursor:pointer;min-width:38px;}}
.rate.active{{background:#1b6ef3;color:#fff;border-color:#1b6ef3;}}
.note{{color:#555;font-size:13px;margin:6px 0;}}
.note.warn{{color:#b00020;}}
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
</div>
<div id=\"zoom-level\" class=\"note\">Zoom: 100%</div>
{grid_mrc_note}
<div class=\"note\">Viewer defaults to the Atlas when available. Click Atlas, GridSquare, FoilHole, or Data images to switch what is shown here.</div>
<div class=\"note\">Use MRC + sliders for contrast and zoom controls for detail inspection.</div>
<div id=\"contrast-panel\" style=\"display:none;margin-bottom:8px;\">
<div>Low: <span id=\"lowv\">2</span>% <input type=\"range\" id=\"low\" min=\"0\" max=\"99\" value=\"2\"></div>
<div>High: <span id=\"highv\">98</span>% <input type=\"range\" id=\"high\" min=\"1\" max=\"100\" value=\"98\"></div>
</div>
<div class=\"section-title\">Report</div>
<label class=\"note\"><input type=\"checkbox\" id=\"include-report\"> Include this GridSquare in the final report</label>
<div>Selected rating: <span id=\"selected\">3</span></div>
<div>Comments:</div>
<textarea id=\"comment\" rows=\"4\"></textarea>
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
const STORAGE_KEY = 'review_state_' + IDX;
localStorage.setItem('last_idx', IDX);
const commentEl = document.getElementById('comment');
const includeEl = document.getElementById('include-report');
let rating = 3;
let selectedKind = DEFAULT_KIND;
let selectedName = '';
let selectedHasMrc = DEFAULT_HAS_MRC;
let zoomLevel = 1.0;
let allowPersist = false;
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
function applyZoom(){{
  const img = document.getElementById('gridimg');
  img.style.transform = 'scale(' + zoomLevel.toFixed(3) + ')';
  document.getElementById('zoom-level').textContent = 'Zoom: ' + Math.round(zoomLevel * 100) + '%';
}}
function setZoom(value){{
  zoomLevel = Math.max(0.5, Math.min(4.0, value));
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
  setZoom(1.0);
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
document.getElementById('gridimg').onclick = () => selectImage('grid','',GRID_HAS_MRC);
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
document.getElementById('zoom-reset').onclick = () => setZoom(1.0);
function persistState(){{
  const data = {{
    rating,
    comment: commentEl.value,
    include: includeEl.checked
  }};
  localStorage.setItem(STORAGE_KEY, JSON.stringify(data));
}}
function restoreState(){{
  const saved = localStorage.getItem(STORAGE_KEY);
  if (!saved) return;
  try {{
    const data = JSON.parse(saved);
    if (typeof data.comment === 'string') commentEl.value = data.comment;
    if (typeof data.include === 'boolean') includeEl.checked = data.include;
    if (typeof data.rating === 'number' && data.rating >=1 && data.rating <=5) setRating(data.rating);
  }} catch (e) {{}}
}}
restoreState();
allowPersist = true;
commentEl.addEventListener('input', persistState);
includeEl.addEventListener('change', persistState);
document.getElementById('low').oninput = updateContrast;
document.getElementById('high').oninput = updateContrast;
updateButtons();
applyZoom();
document.getElementById('submit').onclick = submitReview;
document.addEventListener('keydown', (e)=>{{
  if (e.key >= '1' && e.key <= '5') {{ setRating(parseInt(e.key)); }}
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {{ submitReview(); }}
  if (e.key === 'ArrowRight' && NEXT_IDX !== null) {{ window.location = '/review/' + NEXT_IDX; }}
  if (e.key === 'ArrowLeft' && PREV_IDX !== null) {{ window.location = '/review/' + PREV_IDX; }}
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
        return HTMLResponse("""<html><head><meta charset=\"utf-8\"><title>Grid review</title>
<style>
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:#f5f6f8;color:#111;}
.page{max-width:900px;margin:0 auto;padding:32px;}
.card{background:#fff;border:1px solid #e1e4e8;border-radius:12px;padding:20px;box-shadow:0 1px 2px rgba(0,0,0,0.04);}
.title{font-size:22px;font-weight:600;margin-bottom:8px;}
.note{color:#555;font-size:14px;line-height:1.4;margin-bottom:10px;}
.btn{display:inline-block;margin-top:14px;border:1px solid #1b6ef3;background:#1b6ef3;color:#fff;border-radius:8px;padding:10px 14px;font-size:14px;text-decoration:none;margin-right:8px;}
.btn.secondary{background:#fff;color:#1b6ef3;}
</style>
</head><body><div class=\"page\"><div class=\"card\"><div class=\"title\">Grid review</div>
<div class=\"note\">Review GridSquare, FoilHole, and Data images. Click any thumbnail to inspect it. Use "Show MRC" to adjust contrast when available. Rate each GridSquare and leave comments. A PDF report is generated at the end.</div>
<a class=\"btn\" id=\"start-btn\" href=\"/review/0\">Start review</a>
<a class=\"btn secondary\" id=\"resume-btn\" style=\"display:none;\" href=\"#\">Resume last visited</a>
</div></div>
<script>
const resumeBtn = document.getElementById('resume-btn');
const startBtn = document.getElementById('start-btn');
const lastIdx = localStorage.getItem('last_idx');
if (lastIdx !== null){{
  resumeBtn.style.display = 'inline-block';
  resumeBtn.href = '/review/' + lastIdx;
  resumeBtn.onclick = () => {{ window.location = '/review/' + lastIdx; return false; }};
}}
</script>
</body></html>""")

    @app.get("/review/{idx}")
    def review(idx: int):
        if idx < 0 or idx >= len(items):
            return HTMLResponse("<html><body>Invalid index</body></html>", status_code=404)
        return HTMLResponse(review_html(idx))

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
            try:
                rating = int(data.get("rating", 0))
            except Exception:
                try:
                    rating = int(float(data.get("rating", 0)))
                except Exception:
                    rating = 0
            comment = str(data.get("comment", ""))
            include = bool(data.get("include", False))
            if idx < 0 or idx >= len(items):
                return JSONResponse({"next": None})
            name = items[idx]["dir"].name
            responses[name] = {"rating": rating, "comment": comment, "include": include}
            _save_responses(responses)
            responses.update(_load_responses())
            next_idx = idx + 1
            if next_idx >= len(items):
                return JSONResponse({"next": None})
            return JSONResponse({"next": next_idx})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

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

    @app.get("/done")
    def done():
        return HTMLResponse(
            """<html><head><meta charset="utf-8"><title>Review complete</title>
<style>
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:#f5f6f8;color:#111;}
.page{max-width:600px;margin:0 auto;padding:36px;}
.card{background:#fff;border:1px solid #e1e4e8;border-radius:12px;padding:24px;box-shadow:0 1px 3px rgba(0,0,0,0.08);}
.title{font-size:22px;font-weight:600;margin-bottom:8px;}
.note{color:#555;font-size:14px;margin-bottom:12px;}
.btn{display:inline-block;margin-top:10px;border:1px solid #1b6ef3;background:#1b6ef3;color:#fff;border-radius:8px;padding:10px 14px;font-size:14px;text-decoration:none;margin-right:8px;}
#done-status{margin-top:12px;font-size:13px;color:#1b6ef3;}
</style>
</head><body><div class="page"><div class="card">
<div class="title">All GridSquares reviewed</div>
<div class="note">Download the PDF summaries below. You can reopen this session later to continue editing notes or regenerate the reports.</div>
<a class="btn" id="report-link" href="/report">Download overview</a>
<a class="btn" id="selected-link" href="/selected_report">Download details</a>
<div id="done-status"></div>
</div></div>
<script>
function prepLink(id,url,msg){
  const link=document.getElementById(id);
  link.href=url + '?t=' + Date.now();
  link.addEventListener('click',()=>{
    document.getElementById('done-status').textContent=msg;
  });
}
prepLink('report-link','/report','Generating overview PDF…');
prepLink('selected-link','/selected_report','Generating selected-report PDF…');
localStorage.removeItem('last_idx');
</script>
</body></html>"""
        )

    @app.get("/report")
    def report():
        overview_path, _details_path = _report_paths()
        target_path = overview_path
        try:
            write_review_report(base_dir, target_path, atlas_name, responses, atlas_overlay=atlas_overlay)
        except (PermissionError, OSError):
            # Common on read-only/network session folders; fall back to a writable temp directory.
            target_path = _temp_report_path(overview_path.name)
            write_review_report(base_dir, target_path, atlas_name, responses, atlas_overlay=atlas_overlay)
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
            )
        except Exception as exc:
            return JSONResponse({"error": f"failed to generate selected report: {exc}"}, status_code=500)
        return FileResponse(target_path, media_type="application/pdf", filename=target_path.name, headers={"Cache-Control": "no-store"})

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
    write_selected_report(base_dir, target_path, atlas_name, responses, overlay=overlay, atlas_overlay=atlas_overlay)
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
