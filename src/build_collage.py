#!/usr/bin/env python3
"""Generate a PDF collage for one or more GridSquare folders.

When pointed at a directory containing subfolders named
"GridSquare_<ID>" the script will sort them by numeric ID (lowest first)
and produce a single multi-page PDF.  Each GridSquare is treated as a
"section": the first page of a section shows the grid overview (with
index), optionally accompanied by an atlas screenshot named
`Atlas_w_order.jpg` if present alongside the grid image, and subsequent
pages list foil-hole and data images.  If no foil/data images exist a
placeholder text page will note the absence of screening data.

Usage:
    python build_collage.py /path/to/base_directory [-o output.pdf]

You may also supply a single GridSquare directory directly; it will still be
handled correctly.  The output file defaults to `<base>.pdf`.
"""

import argparse
import http.server
import io
import json
import queue
import socketserver
import threading
import urllib.parse
import webbrowser
import textwrap
import xml.etree.ElementTree as ET
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

import numpy as np
import mrcfile
import tempfile
from PIL import Image, ImageDraw, ImageFont
from reportlab.pdfgen import canvas as pdf_canvas
from reportlab.lib.utils import ImageReader
from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# cryo-EM grid JPEGs can easily exceed Pillow's protective pixel limit;
# disable it so we can safely load very large images from trusted sources.
Image.MAX_IMAGE_PIXELS = None

_PDF_PAGE_WIDTH = 2550
_PDF_MIN_HEIGHT = 2000
_PDF_FONTS_READY = False
_PDF_FONT_REGULAR = "Helvetica"
_PDF_FONT_BOLD = "Helvetica-Bold"
_PDF_FONT_SMALL = "Helvetica"


def _first_existing(paths):
    for candidate in paths:
        try:
            if candidate and candidate.is_file():
                return candidate
        except Exception:
            continue
    return None


def _ensure_pdf_fonts():
    """Register TTF fonts for ReportLab output if available."""
    global _PDF_FONTS_READY, _PDF_FONT_REGULAR, _PDF_FONT_BOLD, _PDF_FONT_SMALL
    if _PDF_FONTS_READY:
        return
    script_dir = Path(__file__).resolve().parent
    candidates_regular = [
        script_dir / "DejaVuSans.ttf",
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path.home() / "Library/Fonts/Arial.ttf",
    ]
    candidates_bold = [
        script_dir / "DejaVuSans-Bold.ttf",
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
        Path.home() / "Library/Fonts/Arial Bold.ttf",
    ]
    regular_path = _first_existing(candidates_regular)
    bold_path = _first_existing(candidates_bold)
    if regular_path:
        try:
            pdfmetrics.registerFont(TTFont("EPUMapperSans", str(regular_path)))
            _PDF_FONT_REGULAR = "EPUMapperSans"
            _PDF_FONT_SMALL = "EPUMapperSans"
            if bold_path:
                pdfmetrics.registerFont(TTFont("EPUMapperSansBold", str(bold_path)))
                _PDF_FONT_BOLD = "EPUMapperSansBold"
            else:
                _PDF_FONT_BOLD = _PDF_FONT_REGULAR
        except Exception:
            _PDF_FONT_REGULAR = "Helvetica"
            _PDF_FONT_BOLD = "Helvetica-Bold"
            _PDF_FONT_SMALL = "Helvetica"
    _PDF_FONTS_READY = True


def _pdf_color(r: int, g: int, b: int, alpha: float = 1.0):
    return colors.Color(r / 255.0, g / 255.0, b / 255.0, alpha=alpha)


def _pil_to_reader(img: Image.Image | None) -> ImageReader | None:
    if img is None:
        return None
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    return ImageReader(img)


def _wrap_text_lines(text: str, font_size: float, max_width: float, min_chars: int = 6) -> list[str]:
    """Return wrapped text approximating ReportLab string width."""
    if not text:
        return []
    approx_chars = max(min_chars, int(max_width / (font_size * 0.55)))
    return textwrap.wrap(text, width=approx_chars) or [text]


def _format_category_score(value) -> str:
    if value is None:
        return "N/A"
    try:
        return str(int(value))
    except Exception:
        return str(value)


def find_grid_image(grid_dir: Path) -> Path:
    for entry in grid_dir.iterdir():
        if entry.is_file() and entry.suffix.lower() in (".jpg", ".jpeg"):
            if entry.name.startswith("GridSquare_"):
                return entry
    raise FileNotFoundError(f"no grid square JPG found in {grid_dir}")


def find_grid_mrc(grid_dir: Path) -> Path | None:
    for entry in grid_dir.iterdir():
        if entry.is_file() and entry.suffix.lower() == ".mrc":
            if entry.name.startswith("GridSquare_"):
                return entry
    return None


def _mrc_to_image(path: Path, low: float = 2.0, high: float = 98.0) -> Image.Image | None:
    try:
        with mrcfile.open(path, permissive=True) as mrc:
            data = mrc.data
        arr = np.asarray(data)
        arr = np.squeeze(arr)
        if arr.ndim > 2:
            arr = arr[0]
        arr = arr.astype(np.float32, copy=False)
        lo = np.percentile(arr, low)
        hi = np.percentile(arr, high)
        if not np.isfinite(lo) or not np.isfinite(hi):
            return None
        if hi <= lo:
            hi = lo + 1.0
        scaled = (arr - lo) / (hi - lo)
        scaled = np.clip(scaled, 0, 1) * 255.0
        return Image.fromarray(scaled.astype(np.uint8), mode="L")
    except Exception:
        return None


def _timestamp_from_filename(path: Path) -> str:
    parts = path.stem.split("_")
    if len(parts) >= 3:
        return "_".join(parts[2:])
    return ""


def _overlay_prefixes(grid_dir: Path) -> list[str]:
    """Return filename prefixes commonly used for this GridSquare."""
    prefixes: list[str] = []
    name = grid_dir.name
    prefixes.append(name)
    if name.lower().startswith("gridsquare_"):
        suffix = name.split("_", 1)[1]
        if suffix:
            prefixes.append(suffix)
    digits = "".join(ch for ch in name if ch.isdigit())
    if digits:
        prefixes.append(digits)
    seen: set[str] = set()
    ordered: list[str] = []
    for prefix in prefixes:
        if prefix and prefix not in seen:
            seen.add(prefix)
            ordered.append(prefix)
    return ordered


def _find_overlay_image(grid_dir: Path, base_dir: Path | None = None) -> Path | None:
    """Locate a foil overlay PNG for `grid_dir`, optionally matching prefixed outputs."""
    overlay_names = ("foil_overlay.png",)
    overlay_patterns = ("*foil_overlay*.png", "*FoilOverlay*.png")

    for name in overlay_names:
        candidate = grid_dir / name
        if candidate.is_file():
            return candidate
    for pattern in overlay_patterns:
        for candidate in sorted(grid_dir.glob(pattern)):
            if candidate.is_file():
                return candidate

    search_dirs: list[Path] = []
    if base_dir and base_dir not in search_dirs:
        search_dirs.append(base_dir)
    parent = grid_dir.parent
    if parent not in search_dirs:
        search_dirs.append(parent)

    prefixes = _overlay_prefixes(grid_dir)
    for directory in search_dirs:
        if not directory or not directory.is_dir():
            continue
        for prefix in prefixes:
            for name in overlay_names:
                for suffix in (f"{prefix}_{name}", f"{prefix}{name}"):
                    candidate = directory / suffix
                    if candidate.is_file():
                        return candidate
        for prefix in prefixes:
            for pattern in overlay_patterns:
                combined_pattern = f"{prefix}{pattern}"
                for candidate in sorted(directory.glob(combined_pattern)):
                    if candidate.is_file():
                        return candidate
    return None


def gather_foil_and_data(grid_dir: Path) -> tuple[dict[str, list[Path]], dict[str, list[Path]]]:
    foil_dir = grid_dir / "FoilHoles"
    data_dir = grid_dir / "Data"
    foils: dict[str, list[Path]] = defaultdict(list)
    datas: dict[str, list[Path]] = defaultdict(list)

    if foil_dir.is_dir():
        for f in foil_dir.glob("*.jpg"):
            parts = f.stem.split("_")
            if len(parts) >= 2 and parts[0] == "FoilHole":
                foil_id = parts[1]
                foils[foil_id].append(f)
    if data_dir.is_dir():
        for f in data_dir.glob("*.jpg"):
            parts = f.stem.split("_")
            if len(parts) >= 3 and parts[0] == "FoilHole" and parts[2] == "Data":
                foil_id = parts[1]
                datas[foil_id].append(f)

    def _sort_entries(d: dict[str, list[Path]]) -> dict[str, list[Path]]:
        sorted_dict: dict[str, list[Path]] = {}
        for foil_id, paths in d.items():
            sorted_dict[foil_id] = sorted(paths, key=lambda path: (_timestamp_from_filename(path), path.name))
        return sorted_dict

    return _sort_entries(foils), _sort_entries(datas)


def _latest_only(d: dict[str, list[Path]]) -> dict[str, list[Path]]:
    latest: dict[str, list[Path]] = {}
    for key, paths in d.items():
        if paths:
            latest[key] = [paths[-1]]
    return latest


def _grid_timestamp_from_name(name: str) -> tuple[str, str] | None:
    """Parse `GridSquare_YYYYMMDD_HHMMSS.*` style timestamps from a filename."""
    stem = Path(name).stem
    parts = stem.split("_")
    if len(parts) < 3:
        return None
    if parts[0].lower() != "gridsquare":
        return None
    date_part = parts[1]
    time_part = parts[2]
    if len(date_part) != 8 or not date_part.isdigit():
        return None
    digits = "".join(ch for ch in time_part if ch.isdigit())
    if len(digits) < 6:
        return None
    return date_part, digits[:6]


def _grid_acquisition_key(grid_dir: Path) -> tuple[int, str, str]:
    """Return a sortable key that approximates EPU acquisition order for a GridSquare."""
    timestamp_candidates: list[tuple[str, str]] = []
    try:
        for entry in grid_dir.iterdir():
            if not entry.is_file():
                continue
            if entry.suffix.lower() not in (".jpg", ".jpeg"):
                continue
            ts = _grid_timestamp_from_name(entry.name)
            if ts is not None:
                timestamp_candidates.append(ts)
    except Exception:
        pass
    if timestamp_candidates:
        # Use the earliest timestamp in this GridSquare directory.
        # This is closest to the acquisition sequence shown by EPU.
        first = min(timestamp_candidates)
        return (0, first[0], first[1])
    # Fall back to legacy ordering for datasets without parseable timestamps.
    return (1, "", "")


def _collect_grids(base_dir: Path):
    grids: list[tuple[int | float, Path, tuple[int, str, str]]] = []
    for entry in base_dir.iterdir():
        if entry.is_dir() and entry.name.startswith("GridSquare_"):
            parts = entry.name.split("_")
            try:
                gid = int(parts[1])
            except Exception:
                gid = float("inf")
            grids.append((gid, entry, _grid_acquisition_key(entry)))
    if not grids and base_dir.name.startswith("GridSquare_"):
        try:
            gid = int(base_dir.name.split("_")[1])
        except Exception:
            gid = float("inf")
        grids.append((gid, base_dir, _grid_acquisition_key(base_dir)))
    grids.sort(key=lambda row: (row[2], row[0], row[1].name))
    return [(gid, entry) for gid, entry, _ in grids]


def _load_image(path: Path, mode: str | None = None) -> Image.Image | None:
    try:
        with Image.open(path) as im:
            if mode:
                return im.convert(mode)
            return im.copy()
    except Exception:
        return None


_ATLAS_MAPPING_CACHE: dict[Path, tuple[dict[str, dict], float | None, float | None]] = {}
_EPU_CATEGORY_COLORS: dict[int, tuple[int, int, int]] = {
    -1: (148, 163, 184),
    0: (96, 165, 250),
    1: (74, 222, 128),
    2: (250, 204, 21),
    3: (251, 146, 60),
    4: (239, 68, 68),
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


def _parse_atlas_dm_nodes(dm_path: Path) -> dict[str, dict]:
    nodes: dict[str, dict] = {}
    try:
        root = ET.parse(dm_path).getroot()
    except Exception:
        return nodes

    for parent in root.iter():
        if not _local_tag(parent.tag).startswith("keyvaluepairofintnodexml"):
            continue
        key_node = None
        value_node = None
        for child in list(parent):
            child_name = _local_tag(child.tag)
            if child_name == "key":
                key_node = child
            elif child_name == "value":
                value_node = child
        if key_node is None or value_node is None or not key_node.text:
            continue
        key = key_node.text.strip()
        if not key:
            continue
        category_value = None
        quality_value = None
        for node in list(value_node):
            if _local_tag(node.tag) == "category":
                category_float = _as_float(node.text)
                if category_float is not None:
                    category_value = int(round(category_float))
                break
        pos_node = None
        for node in value_node.iter():
            if _local_tag(node.tag) == "positionontheatlas":
                pos_node = node
                break
        center = None
        if pos_node is not None:
            center_node = None
            for node in list(pos_node):
                if _local_tag(node.tag) == "center":
                    center_node = node
                    break
            center_x = None
            center_y = None
            if center_node is not None:
                for node in list(center_node):
                    name = _local_tag(node.tag)
                    if name == "x":
                        center_x = _as_float(node.text)
                    elif name == "y":
                        center_y = _as_float(node.text)
            if center_x is not None and center_y is not None:
                center = (center_x, center_y)
            for node in pos_node.iter():
                if _local_tag(node.tag) == "quality":
                    quality_value = _as_float(node.text)
                    break
        nodes[key] = {
            "center": center,
            "category": category_value,
            "quality": quality_value,
        }
    return nodes


def _atlas_reference_dimensions(atlas_path: Path, nodes: dict[str, dict]) -> tuple[float | None, float | None]:
    atlas_mrc = atlas_path.with_suffix(".mrc")
    if atlas_mrc.is_file():
        try:
            with mrcfile.open(atlas_mrc, permissive=True) as mrc:
                width = float(mrc.header.nx)
                height = float(mrc.header.ny)
            if width > 0 and height > 0:
                return width, height
        except Exception:
            pass
    centers = [entry.get("center") for entry in nodes.values() if isinstance(entry, dict)]
    centers = [center for center in centers if center is not None]
    if centers:
        max_x = max(center[0] for center in centers)
        max_y = max(center[1] for center in centers)
        return max_x + 1.0, max_y + 1.0
    return None, None


def _load_atlas_mapping(atlas_path: Path) -> tuple[dict[str, dict], float | None, float | None]:
    key = atlas_path.resolve()
    cached = _ATLAS_MAPPING_CACHE.get(key)
    if cached is not None:
        return cached
    dm_path = next((path for path in _atlas_dm_candidates(key) if path.is_file()), None)
    if dm_path is None:
        result = ({}, None, None)
        _ATLAS_MAPPING_CACHE[key] = result
        return result
    nodes = _parse_atlas_dm_nodes(dm_path)
    if not nodes:
        result = ({}, None, None)
        _ATLAS_MAPPING_CACHE[key] = result
        return result
    ref_w, ref_h = _atlas_reference_dimensions(key, nodes)
    result = (nodes, ref_w, ref_h)
    _ATLAS_MAPPING_CACHE[key] = result
    return result


def _atlas_lookup_keys(grid_dir: Path, grid_id: int | float | None) -> list[str]:
    keys: list[str] = []
    if grid_id is not None:
        try:
            keys.append(str(int(grid_id)))
        except Exception:
            pass
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


def _atlas_center_for_grid(
    nodes: dict[str, dict],
    grid_dir: Path,
    grid_id: int | float | None,
) -> tuple[float, float] | None:
    for key in _atlas_lookup_keys(grid_dir, grid_id):
        entry = nodes.get(key)
        if isinstance(entry, dict):
            center = entry.get("center")
            if center is not None:
                return center
    return None


def _atlas_category_for_grid(
    nodes: dict[str, dict],
    grid_dir: Path,
    grid_id: int | float | None,
) -> int | None:
    for key in _atlas_lookup_keys(grid_dir, grid_id):
        entry = nodes.get(key)
        if not isinstance(entry, dict):
            continue
        category_value = entry.get("category")
        if category_value is not None:
            try:
                return int(category_value)
            except Exception:
                continue
    return None


def _category_marker_color(category: int | None) -> tuple[int, int, int]:
    if category is None:
        return (148, 163, 184)
    return _EPU_CATEGORY_COLORS.get(category, (99, 102, 241))


def _atlas_with_grid_markers(
    atlas_img: Image.Image,
    atlas_path: Path | None,
    marker_items: list[tuple[int, Path, int | float | None, bool]],
) -> Image.Image:
    if atlas_path is None or not marker_items:
        return atlas_img
    nodes, ref_w, ref_h = _load_atlas_mapping(atlas_path)
    if not nodes:
        return atlas_img

    rendered = atlas_img.convert("RGB").copy()
    width, height = rendered.size
    scale_x = width / ref_w if ref_w and ref_w > 0 else 1.0
    scale_y = height / ref_h if ref_h and ref_h > 0 else 1.0
    draw = ImageDraw.Draw(rendered, "RGBA")
    radius = max(12, int(min(width, height) * 0.018))
    ring_width = max(2, radius // 5)
    font = _get_font(max(16, int(radius * 1.2))) or ImageFont.load_default()

    for label_idx, grid_dir, grid_id, highlight in marker_items:
        center = _atlas_center_for_grid(nodes, grid_dir, grid_id)
        if center is None:
            continue
        cx = center[0] * scale_x
        cy = center[1] * scale_y
        if not (0 <= cx < width and 0 <= cy < height):
            continue
        fill_rgba = (220, 55, 55, 128) if highlight else (36, 109, 217, 128)
        edge_rgba = (145, 24, 24, 128) if highlight else (14, 68, 151, 128)
        draw.ellipse(
            (cx - radius, cy - radius, cx + radius, cy + radius),
            fill=fill_rgba,
            outline=edge_rgba,
            width=ring_width,
        )
        text = str(label_idx)
        if hasattr(draw, "textbbox"):
            box = draw.textbbox((0, 0), text, font=font)
            text_w = box[2] - box[0]
            text_h = box[3] - box[1]
        else:
            text_w, text_h = font.getsize(text)
        tx = cx - text_w / 2
        ty = cy - text_h / 2
        draw.text((tx + 1, ty + 1), text, fill=(0, 0, 0, 128), font=font)
        draw.text((tx, ty), text, fill=(255, 255, 255, 128), font=font)

    return rendered


def _atlas_with_category_markers(
    atlas_img: Image.Image,
    atlas_path: Path | None,
) -> Image.Image:
    if atlas_path is None:
        return atlas_img
    nodes, ref_w, ref_h = _load_atlas_mapping(atlas_path)
    if not nodes:
        return atlas_img

    rendered = atlas_img.convert("RGB").copy()
    width, height = rendered.size
    scale_x = width / ref_w if ref_w and ref_w > 0 else 1.0
    scale_y = height / ref_h if ref_h and ref_h > 0 else 1.0
    draw = ImageDraw.Draw(rendered, "RGBA")
    radius = max(8, int(min(width, height) * 0.009))
    outline_width = max(1, radius // 4)
    font = _get_font(max(11, int(radius * 1.1))) or ImageFont.load_default()

    seen_categories: set[int | None] = set()
    for entry in nodes.values():
        if not isinstance(entry, dict):
            continue
        center = entry.get("center")
        if center is None:
            continue
        cx = center[0] * scale_x
        cy = center[1] * scale_y
        if not (0 <= cx < width and 0 <= cy < height):
            continue
        category_value = entry.get("category")
        try:
            category_value = int(category_value) if category_value is not None else None
        except Exception:
            category_value = None
        seen_categories.add(category_value)
        r, g, b = _category_marker_color(category_value)
        draw.ellipse(
            (cx - radius, cy - radius, cx + radius, cy + radius),
            fill=(r, g, b, 128),
            outline=(20, 28, 44, 128),
            width=outline_width,
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

    if seen_categories:
        legend_items = sorted(
            [cat for cat in seen_categories if cat is not None],
            key=lambda value: int(value),
        )
        if None in seen_categories:
            legend_items.append(None)
        legend_font = _get_font(max(14, int(radius * 1.3))) or ImageFont.load_default()
        pad = max(10, int(radius * 1.4))
        row_h = max(18, int(radius * 2.0))
        legend_w = max(230, int(width * 0.26))
        legend_h = pad * 2 + row_h * len(legend_items)
        x0 = max(8, width - legend_w - 14)
        y0 = max(8, height - legend_h - 14)
        if hasattr(draw, "rounded_rectangle"):
            draw.rounded_rectangle(
                (x0, y0, x0 + legend_w, y0 + legend_h),
                radius=10,
                fill=(255, 255, 255, 215),
                outline=(173, 184, 204, 235),
                width=2,
            )
        else:
            draw.rectangle((x0, y0, x0 + legend_w, y0 + legend_h), fill=(255, 255, 255, 215), outline=(173, 184, 204, 235), width=2)
        for idx, category_value in enumerate(legend_items):
            y = y0 + pad + idx * row_h
            r, g, b = _category_marker_color(category_value)
            sw = max(10, int(radius * 1.2))
            draw.rectangle((x0 + pad, y + 2, x0 + pad + sw, y + sw), fill=(r, g, b, 220), outline=(20, 28, 44, 230), width=1)
            label = "N/A" if category_value is None else str(category_value)
            draw.text((x0 + pad + sw + 8, y), f"EPU {label}", fill=(20, 28, 44, 255), font=legend_font)

    return rendered


@lru_cache(maxsize=16)
def _get_font(size: int = 18) -> ImageFont.ImageFont:
    """Return a reasonably sized font; prefer a truetype if available."""
    try:
        # common in conda environments
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except Exception:
        try:
            return ImageFont.load_default()
        except Exception:
            return None  # ultimately drawing will ignore font


def _label_image(img: Image.Image, label: str) -> Image.Image:
    """Return a copy of `img` with `label` drawn on a subtle glassy banner."""
    font = _get_font(28) or ImageFont.load_default()
    base = img.convert("RGBA")
    draw = ImageDraw.Draw(base)
    if hasattr(draw, "textbbox"):
        bbox = draw.textbbox((0, 0), label, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
    else:
        text_w, text_h = font.getsize(label)
    pad_x, pad_y = 24, 14
    banner_h = text_h + pad_y * 2
    banner = Image.new("RGBA", (base.width, banner_h), (15, 23, 38, 170))
    base.alpha_composite(banner, (0, 0))
    text_x = pad_x
    text_y = (banner_h - text_h) // 2
    draw.text((text_x, text_y), label, fill=(255, 255, 255, 255), font=font)
    return base.convert(img.mode)


def make_collage(
    grid_img: Image.Image,
    foil_img: Image.Image,
    data_img: Image.Image | None,
    grid_label: str,
    foil_label: str,
    data_label: str | None = None,
) -> Image.Image:
    # ensure all images are same height; if not, we'll pad/resize in future
    w, h = grid_img.size
    total_w = w * 3
    collage = Image.new("L", (total_w, h))
    # label each copy before pasting
    grid_labeled = _label_image(grid_img.copy(), grid_label)
    foil_labeled = _label_image(foil_img.copy(), foil_label)
    collage.paste(grid_labeled, (0, 0))
    collage.paste(foil_labeled, (w, 0))
    if data_img is not None:
        data_labeled = _label_image(data_img.copy(), data_label or "data")
        collage.paste(data_labeled, (w * 2, 0))
    return collage


def make_text_page(text: str, size=(512, 512)) -> Image.Image:
    """Create an image with centered text using a larger font for readability."""
    img = Image.new("L", size, color=255)
    draw = ImageDraw.Draw(img)
    font = _get_font(22)
    # wrap text if necessary
    lines = text.split("\n")
    # calculate total height
    line_heights = []
    for line in lines:
        if hasattr(draw, "textbbox"):
            bbox = draw.textbbox((0, 0), line, font=font)
            h = bbox[3] - bbox[1]
        else:
            h = font.getsize(line)[1] if font else 10
        line_heights.append(h)
    total_h = sum(line_heights)
    y = (size[1] - total_h) // 2
    for i, line in enumerate(lines):
        if hasattr(draw, "textbbox"):
            bbox = draw.textbbox((0, 0), line, font=font)
            w = bbox[2] - bbox[0]
        else:
            w = font.getsize(line)[0] if font else len(line) * 6
        x = (size[0] - w) // 2
        draw.text((x, y), line, fill=0, font=font)
        y += line_heights[i]
    return img


def make_section_page(title: str, subtitle: str | None = None, size=(1700, 2200)) -> Image.Image:
    """Create a section header page with a title and optional subtitle."""
    img = Image.new("RGB", size, color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    title_font = _get_font(48)
    subtitle_font = _get_font(24)
    if hasattr(draw, "textbbox"):
        tb = draw.textbbox((0, 0), title, font=title_font)
        tw = tb[2] - tb[0]
        th = tb[3] - tb[1]
    else:
        tw = title_font.getsize(title)[0] if title_font else len(title) * 16
        th = title_font.getsize(title)[1] if title_font else 28
    x = (size[0] - tw) // 2
    y = int(size[1] * 0.28)
    draw.text((x, y), title, fill=0, font=title_font)
    if subtitle:
        if hasattr(draw, "textbbox"):
            sb = draw.textbbox((0, 0), subtitle, font=subtitle_font)
            sw = sb[2] - sb[0]
        else:
            sw = subtitle_font.getsize(subtitle)[0] if subtitle_font else len(subtitle) * 10
        sx = (size[0] - sw) // 2
        draw.text((sx, y + th + 24), subtitle, fill=0, font=subtitle_font)
    return img


def _draw_grid_summary_page(
    c: pdf_canvas.Canvas,
    grid_img: Image.Image,
    atlas_img: Image.Image | None,
    foils: dict[str, Path],
    datas: dict[str, Path],
    resp: dict | None,
    heading: str,
    grid_image_name: str,
    overlay_img: Image.Image | None = None,
    category_score: int | None = None,
) -> None:
    """Render a GridSquare summary directly onto a ReportLab canvas."""
    _ensure_pdf_fonts()
    base_margin = 90
    section_gap = 60
    card_inner_pad = 56
    hero_panel_gap = 56
    row_gap = 52
    hero_panel_h = 1100
    thumb_h = 1050
    thumb_gap = 64
    bg_color = _pdf_color(244, 246, 251)
    card_color = _pdf_color(255, 255, 255)
    muted_color = _pdf_color(110, 120, 148)
    text_color = _pdf_color(20, 28, 44)

    def _rows() -> list[dict]:
        rows: list[dict] = []
        hole_indices = {fid: idx + 1 for idx, fid in enumerate(sorted(foils.keys()))}
        for foil_id in sorted(foils.keys()):
            foil_paths = foils[foil_id]
            data_paths = datas.get(foil_id, [])
            slots = max(len(foil_paths), len(data_paths), 1)
            for idx_row in range(slots):
                foil_path = foil_paths[idx_row] if idx_row < len(foil_paths) else None
                data_path = data_paths[idx_row] if idx_row < len(data_paths) else None
                meta_lines: list[str] = []
                if data_path:
                    xml_path = data_path.with_suffix('.xml')
                    if xml_path.is_file():
                        meta = parse_metadata(xml_path)
                        for key in ('pixel_size', 'exposure', 'dose', 'defocus'):
                            if key in meta:
                                meta_lines.append(f"{key.replace('_', ' ').title()}: {meta[key]}")
                rows.append(
                    {
                        'foil_reader': _pil_to_reader(_load_image(foil_path, 'L')) if foil_path else None,
                        'data_reader': _pil_to_reader(_load_image(data_path, 'L')) if data_path else None,
                        'hole_index': hole_indices.get(foil_id),
                        'shot_index': idx_row + 1 if len(foil_paths) > 1 else None,
                        'data_path': data_path,
                        'foil_name': foil_path.name if foil_path else None,
                        'data_name': data_path.name if data_path else None,
                        'meta_lines': meta_lines,
                    }
                )
        return rows

    row_entries = _rows()

    def _row_height(entry: dict) -> float:
        meta_block = len(entry['meta_lines']) * 36
        return card_inner_pad * 2 + 60 + thumb_h + meta_block

    rows_height = (
        sum((_row_height(entry) + row_gap) for entry in row_entries) - (row_gap if row_entries else 0)
    )
    if rows_height < 0:
        rows_height = 0

    rating = '—' if not resp else str(resp.get('rating', '—'))
    category_text = _format_category_score(category_score)
    comment_raw = '' if not resp else str(resp.get('comment', '')).strip()
    comment_lines = textwrap.wrap(comment_raw, width=110) if comment_raw else []
    info_card_height = max(320, card_inner_pad * 2 + 180 + len(comment_lines) * 34)
    hero_card_height = hero_panel_h + card_inner_pad * 2 + 80
    total_height = (
        base_margin
        + 210
        + section_gap
        + info_card_height
        + section_gap
        + hero_card_height
        + section_gap
        + (rows_height if row_entries else 320)
        + base_margin
    )
    page_height = max(total_height, _PDF_MIN_HEIGHT)
    c.setPageSize((_PDF_PAGE_WIDTH, page_height))
    c.setFillColor(bg_color)
    c.rect(0, 0, _PDF_PAGE_WIDTH, page_height, fill=1, stroke=0)

    y = page_height - base_margin
    # Separate GridSquare label and file name for better emphasis.
    square_label, _, file_name = heading.partition(":")
    c.setFont(_PDF_FONT_BOLD, 96)
    c.setFillColor(text_color)
    c.drawString(base_margin, y - 96, square_label.strip())
    c.setFont(_PDF_FONT_REGULAR, 48)
    c.setFillColor(muted_color)
    c.drawString(base_margin, y - 160, file_name.strip() or grid_image_name)
    y -= 210

    card_width = _PDF_PAGE_WIDTH - 2 * base_margin
    c.setFillColor(card_color)
    c.roundRect(base_margin, y - info_card_height, card_width, info_card_height, 32, stroke=0, fill=1)
    c.setFont(_PDF_FONT_REGULAR, 46)
    c.setFillColor(text_color)
    c.drawString(base_margin + card_inner_pad, y - card_inner_pad - 46, f"Rating: {rating}")
    c.setFont(_PDF_FONT_REGULAR, 42)
    c.drawString(base_margin + card_inner_pad, y - card_inner_pad - 102, f"EPU category score: {category_text}")
    text_y = y - card_inner_pad - 180
    c.setFillColor(muted_color)
    c.setFont(_PDF_FONT_REGULAR, 34)
    if comment_lines:
        c.drawString(base_margin + card_inner_pad, text_y, 'Reviewer notes:')
        text_y -= 38
        c.setFillColor(text_color)
        for line in comment_lines:
            c.drawString(base_margin + card_inner_pad, text_y, line)
            text_y -= 34
    else:
        c.drawString(base_margin + card_inner_pad, text_y, 'No reviewer notes provided.')
    y -= info_card_height + section_gap

    c.setFillColor(card_color)
    c.roundRect(base_margin, y - hero_card_height, card_width, hero_card_height, 34, stroke=0, fill=1)
    panels: list[tuple[ImageReader | None, str]] = [(_pil_to_reader(grid_img), grid_image_name)]
    if atlas_img:
        panels.insert(0, (_pil_to_reader(atlas_img), 'Atlas overview'))
    if overlay_img is not None:
        panels.append((_pil_to_reader(overlay_img), 'Foil overlay'))
    inner_width = card_width - 2 * card_inner_pad
    panel_width = (inner_width - hero_panel_gap * (len(panels) - 1)) / max(len(panels), 1)
    img_y = y - hero_card_height + card_inner_pad
    for idx, (reader, label) in enumerate(panels):
        x = base_margin + card_inner_pad + idx * (panel_width + hero_panel_gap)
        if reader:
            c.drawImage(reader, x, img_y + 60, width=panel_width, height=hero_panel_h, preserveAspectRatio=True, mask='auto')
        else:
            c.setFillColor(_pdf_color(230, 233, 241))
            c.rect(x, img_y + 60, panel_width, hero_panel_h, fill=1, stroke=0)
        c.setFillColor(muted_color)
        c.setFont(_PDF_FONT_REGULAR, 32)
        c.drawString(x, img_y + 30, label)
    y -= hero_card_height + section_gap

    thumb_w = (card_width - 2 * card_inner_pad - thumb_gap) / 2
    if not row_entries:
        c.setFillColor(card_color)
        c.roundRect(base_margin, y - 280, card_width, 280, 30, stroke=0, fill=1)
        c.setFillColor(text_color)
        c.setFont(_PDF_FONT_REGULAR, 42)
        c.drawString(base_margin + card_inner_pad, y - 180, 'No FoilHole imagery available for this GridSquare.')
        y -= 280 + row_gap
    else:
        for entry in row_entries:
            card_height = _row_height(entry)
            c.setFillColor(card_color)
            c.roundRect(base_margin, y - card_height, card_width, card_height, 34, stroke=0, fill=1)
            label_y = y - card_inner_pad - 60
            c.setFont(_PDF_FONT_BOLD, 46)
            c.setFillColor(text_color)
            hole_idx = entry['hole_index']
            shot_idx = entry['shot_index']
            foil_caption = f"Hole #{hole_idx}" if hole_idx else 'FoilHole'
            if shot_idx:
                foil_caption += f" · shot {shot_idx}"
            data_caption = f"Data · hole #{hole_idx}" if hole_idx else 'Screening data'
            foil_x = base_margin + card_inner_pad
            data_x = base_margin + card_inner_pad + thumb_w + thumb_gap
            c.drawString(foil_x, label_y, foil_caption)
            c.drawString(data_x, label_y, data_caption)
            img_y = y - card_inner_pad - 80 - thumb_h
            placeholder_color = _pdf_color(233, 236, 244)
            if entry['foil_reader']:
                c.drawImage(entry['foil_reader'], foil_x, img_y, width=thumb_w, height=thumb_h, preserveAspectRatio=True, mask='auto')
            else:
                c.setFillColor(placeholder_color)
                c.rect(foil_x, img_y, thumb_w, thumb_h, fill=1, stroke=0)
                c.setFillColor(muted_color)
                c.setFont(_PDF_FONT_SMALL, 32)
                c.drawCentredString(foil_x + thumb_w / 2, img_y + thumb_h / 2, "Foil image missing")
            if entry['data_reader']:
                c.drawImage(entry['data_reader'], data_x, img_y, width=thumb_w, height=thumb_h, preserveAspectRatio=True, mask='auto')
            else:
                c.setFillColor(placeholder_color)
                c.rect(data_x, img_y, thumb_w, thumb_h, fill=1, stroke=0)
                c.setFillColor(muted_color)
                c.setFont(_PDF_FONT_SMALL, 32)
                c.drawCentredString(data_x + thumb_w / 2, img_y + thumb_h / 2, "Data image missing")
            c.setFillColor(muted_color)
            c.setFont(_PDF_FONT_SMALL, 30)
            foil_label_lines = _wrap_text_lines(entry.get('foil_name', '') or '', 30, thumb_w)
            data_label_lines = _wrap_text_lines(entry.get('data_name', '') or '', 30, thumb_w)
            label_spacing = 32
            offset = 22
            for idx, line in enumerate(foil_label_lines):
                c.drawString(foil_x, img_y - offset - idx * label_spacing, line)
            for idx, line in enumerate(data_label_lines):
                c.drawString(data_x, img_y - offset - idx * label_spacing, line)
            extra_offset = offset + max(len(foil_label_lines), len(data_label_lines)) * label_spacing + 10
            if entry['meta_lines']:
                meta_y = img_y - extra_offset
                c.setFont(_PDF_FONT_SMALL, 32)
                for line in entry['meta_lines']:
                    c.drawString(data_x, meta_y, line)
                    meta_y -= 34
            y -= card_height + row_gap

    c.showPage()


def _draw_pdf_message_page(c: pdf_canvas.Canvas, lines: list[str], title: str = "Notice") -> None:
    _ensure_pdf_fonts()
    base_margin = 120
    line_height = 48
    body_height = max(200, len(lines) * line_height)
    page_height = base_margin * 2 + 120 + body_height
    page_height = max(page_height, _PDF_MIN_HEIGHT // 2)
    c.setPageSize((_PDF_PAGE_WIDTH, page_height))
    c.setFillColor(_pdf_color(244, 246, 251))
    c.rect(0, 0, _PDF_PAGE_WIDTH, page_height, fill=1, stroke=0)
    c.setFillColor(_pdf_color(20, 28, 44))
    c.setFont(_PDF_FONT_BOLD, 80)
    c.drawString(base_margin, page_height - base_margin - 20, title)
    c.setFont(_PDF_FONT_REGULAR, 40)
    text_y = page_height - base_margin - 120
    for line in lines:
        c.drawString(base_margin, text_y, line)
        text_y -= line_height
    c.showPage()


def make_grid_page(grid_img: Image.Image, label: str, atlas_img: Image.Image | None = None, markers: list[tuple] | None = None) -> Image.Image:
    """Return a page showing the atlas (left) and grid (right).

    If no atlas image is provided a placeholder is generated instead.
    The atlas is shown at full resolution rather than being cropped.
    """
    w_g, h_g = grid_img.size
    # prepare atlas or placeholder
    if atlas_img is None:
        atlas_canvas = Image.new("L", (w_g, h_g), color=200)
        atlas_labeled = _label_image(atlas_canvas, "no atlas")
    else:
        # resize atlas to fit within grid dimensions without cropping
        atlas_resized = atlas_img.copy()
        atlas_resized.thumbnail((w_g, h_g), Image.LANCZOS)
        # place resized atlas on white background of grid size
        mode = atlas_resized.mode
        if mode == "L":
            atlas_canvas = Image.new("L", (w_g, h_g), color=255)
        else:
            atlas_canvas = Image.new("RGB", (w_g, h_g), color=(255,255,255))
        offset = ((w_g - atlas_resized.width) // 2, (h_g - atlas_resized.height) // 2)
        atlas_canvas.paste(atlas_resized, offset)
        atlas_labeled = _label_image(atlas_canvas, "atlas")
    # grid page always same size as grid image copy
    grid_labeled = _label_image(grid_img.convert(atlas_labeled.mode).copy(), label)

    # draw markers after conversion so visibility is preserved regardless of mode
    if markers:
        # convert base images to RGB so we can composite an RGBA overlay with transparent fill
        atlas_rgb = atlas_labeled.convert('RGB')
        grid_rgb = grid_labeled.convert('RGB')

        # create overlay with transparent fill and green outline
        overlay = Image.new('RGBA', grid_rgb.size, (0, 0, 0, 0))
        ovdraw = ImageDraw.Draw(overlay)
        font = _get_font(12)
        for item in markers:
            # support both old (px,py,in_bounds) and new (px,py,in_bounds,label)
            try:
                px, py, in_bounds, mlabel = item
            except Exception:
                try:
                    px, py, in_bounds = item
                except Exception:
                    px, py = item
                    in_bounds = (0 <= px < grid_rgb.width) and (0 <= py < grid_rgb.height)
                mlabel = None

            r = max(10, int(min(grid_rgb.width, grid_rgb.height) * 0.05))
            if in_bounds:
                left = px - r
                top = py - r
                right = px + r
                bottom = py + r
                # transparent fill (alpha 0) and green outline (alpha 255)
                ovdraw.ellipse((left, top, right, bottom), fill=(0, 255, 0, 0), outline=(0, 200, 0, 255), width=6)
                # small black crosshair for extra contrast
                ovdraw.line((px - r, py, px + r, py), fill=(0,0,0,255), width=3)
                ovdraw.line((px, py - r, px, py + r), fill=(0,0,0,255), width=3)
                # numeric label
                if mlabel is not None:
                    tx = px + r + 4
                    ty = max(4, py - 8)
                    ovdraw.text((tx, ty), str(mlabel), font=font, fill=(0,200,0,220))
            else:
                # draw an arrow/marker at the edge pointing toward the off-image coordinate
                ex = min(max(px, 0), grid_rgb.width - 1)
                ey = min(max(py, 0), grid_rgb.height - 1)
                # draw small circle at edge and a short arrow pointing outward
                rr = max(6, int(min(grid_rgb.width, grid_rgb.height) * 0.03))
                ovdraw.ellipse((ex-rr, ey-rr, ex+rr, ey+rr), fill=(0,0,0,0), outline=(0,200,0,255), width=5)
                # arrow direction
                ax = px - ex
                ay = py - ey
                # normalize and scale
                import math
                mag = math.hypot(ax, ay)
                if mag == 0:
                    ux, uy = 0, -1
                else:
                    ux, uy = ax/mag, ay/mag
                # arrow tail from edge point outward
                tail = (ex + ux*rr, ey + uy*rr)
                head = (ex + ux*(rr*2.5), ey + uy*(rr*2.5))
                ovdraw.line((tail, head), fill=(0,200,0,255), width=4)
                # label overflow distance and optional index
                overflow_px = int(mag)
                lbl = f"{overflow_px}px"
                if mlabel is not None:
                    lbl = f"#{mlabel} {lbl}"
                ovdraw.text((ex + ux*(rr*3), ey + uy*(rr*3)), lbl, fill=(0,200,0,255), font=font)

        # composite overlay onto grid
        grid_rgba = grid_rgb.convert('RGBA')
        grid_composited = Image.alpha_composite(grid_rgba, overlay)
        # ensure atlas is RGB and compose final two-panel RGB image
        atlas_rgb = atlas_labeled.convert('RGB')
        grid_final = grid_composited.convert('RGB')

        combined = Image.new('RGB', (w_g * 2, h_g), color=(255,255,255))
        combined.paste(atlas_rgb, (0, 0))
        combined.paste(grid_final, (w_g, 0))
        return combined

    combined = Image.new(atlas_labeled.mode, (w_g * 2, h_g), color=(255,255,255) if atlas_labeled.mode == "RGB" else 255)
    combined.paste(atlas_labeled, (0, 0))
    combined.paste(grid_labeled, (w_g, 0))
    return combined


def parse_metadata(xml_path: Path) -> dict[str, str]:
    """Extract simple metadata values from a Data XML file.

    Returns a dict containing any of the keys 'pixel_size', 'exposure', 'dose',
    'dose_on_camera', and 'defocus' when they can be located.  If 'dose' is
    not explicitly present we will attempt to compute it from dose-on-camera
    and exposure time.
    """
    try:
        import xml.etree.ElementTree as ET
        tree = ET.parse(xml_path)
        root = tree.getroot()
    except Exception:
        return {}
    info: dict[str, str] = {}
    parent_map = {}
    for parent in root.iter():
        for child in parent:
            parent_map[child] = parent

    def _local(tag: str | None) -> str:
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

    # helper to iterate ignoring namespace
    def iter_tags(tagname):
        for e in root.iter():
            if e.tag.lower().endswith(tagname.lower()):
                yield e
    # pixel size (numericValue underneath pixelSize)
    pixel_size_angstrom: float | None = None
    for ps in iter_tags("pixelsize"):
        nv = None
        for child in ps.iter():
            if child.tag.lower().endswith("numericvalue"):
                nv = child
                break
        if nv is not None and nv.text:
            size_m = _as_float(nv.text)
            if size_m is not None:
                pixel_size_angstrom = size_m * 1e10
                break
    if pixel_size_angstrom is not None:
        info["pixel_size"] = f"{pixel_size_angstrom:.2f} Å"
    # exposure time – prefer the <camera> node, fall back to any exposure
    exposure_val: float | None = None
    for e in iter_tags("exposuretime"):
        val = _as_float(e.text)
        if val is None:
            continue
        parent = parent_map.get(e)
        parent_name = _local(parent.tag if parent is not None else None)
        if parent_name == "camera":
            exposure_val = val
            break
        if exposure_val is None:
            exposure_val = val
    # look for exposure in CustomData key/value pairs if still missing
    for kv in root.findall('.//{http://schemas.microsoft.com/2003/10/Serialization/Arrays}KeyValueOfstringanyType'):
        key = kv.find('{http://schemas.microsoft.com/2003/10/Serialization/Arrays}Key')
        val = kv.find('{http://schemas.microsoft.com/2003/10/Serialization/Arrays}Value')
        if key is not None and key.text:
            k = key.text.lower()
            if 'dose' in k and val is not None and val.text:
                info['dose'] = val.text
            if 'doseoncamera' in k and val is not None and val.text:
                info['dose_on_camera'] = val.text
            if 'exposuretime' in k and exposure_val is None and val is not None and val.text:
                maybe = _as_float(val.text)
                if maybe is not None:
                    exposure_val = maybe
    if exposure_val is not None:
        info['exposure'] = f"{exposure_val:.2f} s"
    # if explicit dose missing and we have rate+exposure, compute
    if 'dose' not in info and 'dose_on_camera' in info and exposure_val is not None:
        try:
            rate = float(info['dose_on_camera'])
            info['dose'] = str(rate * exposure_val)
        except Exception:
            pass
    # convert dose to e-/Å^2 if necessary
    if 'dose' in info:
        try:
            d = float(info['dose'])
            # if it's >1e3 assume value in e-/m^2 or similar -> convert
            if d > 1e3:
                info['dose'] = f"{d/1e20:.2f}"  # m^2 -> Å^2
            else:
                # already small value; show with 2 decimals
                info['dose'] = f"{d:.2f}"
        except Exception:
            pass
    # defocus – prefer AppliedDefocus custom data, fall back to optics/defocus
    defocus_val: float | None = None
    for kv in root.findall('.//{http://schemas.microsoft.com/2003/10/Serialization/Arrays}KeyValueOfstringanyType'):
        key = kv.find('{http://schemas.microsoft.com/2003/10/Serialization/Arrays}Key')
        val = kv.find('{http://schemas.microsoft.com/2003/10/Serialization/Arrays}Value')
        if key is not None and key.text and 'applieddefocus' in key.text.lower() and val is not None and val.text:
            maybe = _as_float(val.text)
            if maybe is not None:
                defocus_val = maybe
                break
    if defocus_val is None:
        for d in iter_tags('defocus'):
            val = _as_float(d.text)
            if val is not None:
                defocus_val = val
                break
    if defocus_val is not None:
        info['defocus'] = f"{defocus_val * 1e6:.2f} µm"
    return info


def make_foil_page(
    foil_img: Image.Image,
    data_img: Image.Image | None,
    foil_label: str,
    data_label: str | None,
    metadata: dict | None = None,
    index_label: int | None = None,
) -> Image.Image:
    """Return a page with foil image (left) and data image (right).
    Labels are applied individually.  If metadata is provided it will be
    rendered as text below the data image."""
    w, h = foil_img.size
    meta_lines: list[str] = []
    if metadata:
        for key in ("pixel_size", "exposure", "dose", "defocus"):
            if key in metadata:
                txt = metadata[key]
                if key == "dose":
                    txt += " e-/Å²"
                meta_lines.append(f"{key.replace('_', ' ')}: {txt}")
    # if we need to accommodate metadata, increase page height
    extra_h = 0
    if meta_lines:
        extra_h = 20 * len(meta_lines)
    total_w = w * (1 + (1 if data_img is not None else 0))
    page = Image.new("L", (total_w, h + extra_h), color=255)
    foil_labeled = _label_image(foil_img.copy(), foil_label)
    # draw index label on the foil image if provided
    if index_label is not None:
        try:
            draw_idx = ImageDraw.Draw(foil_labeled)
            font_idx = _get_font(14)
            mode = foil_labeled.mode
            r_idx = max(10, int(min(foil_labeled.width, foil_labeled.height) * 0.04))
            cx = 6 + r_idx
            cy = 6 + r_idx
            if mode == 'L':
                draw_idx.ellipse((cx-r_idx, cy-r_idx, cx+r_idx, cy+r_idx), fill=0, outline=255, width=2)
                # white text
                txt = str(index_label)
                draw_idx.text((cx - r_idx/2, cy - r_idx/2), txt, fill=255, font=font_idx)
            else:
                draw_idx.ellipse((cx-r_idx, cy-r_idx, cx+r_idx, cy+r_idx), fill=(0,0,0), outline=(0,200,0), width=3)
                txt = str(index_label)
                draw_idx.text((cx - r_idx/2, cy - r_idx/2), txt, fill=(255,255,255), font=font_idx)
        except Exception:
            pass
    page.paste(foil_labeled, (0, 0))
    if data_img is not None:
        data_labeled = _label_image(data_img.copy(), data_label or "data")
        page.paste(data_labeled, (w, 0))
        if meta_lines:
            draw = ImageDraw.Draw(page)
            try:
                font = ImageFont.load_default()
            except Exception:
                font = None
            y = h + 2
            for line in meta_lines:
                draw.text((w + 2, y), line, fill=0, font=font)
                y += 18
    return page


def parse_grid_info(xml_path: Path) -> dict:
    """Read grid xml and return pixel size and stage X,Y coords."""
    info = {}
    try:
        import xml.etree.ElementTree as ET
        tree = ET.parse(xml_path)
        root = tree.getroot()
    except Exception:
        return info
    def iter_tags(tagname):
        for e in root.iter():
            if e.tag.lower().endswith(tagname.lower()):
                yield e
    for e in iter_tags("readoutarea"):
        dims = {}
        for c in e.iter():
            tag = c.tag.lower()
            if tag.endswith("width") and c.text:
                try:
                    dims["readout_width"] = int(float(c.text))
                except Exception:
                    pass
            if tag.endswith("height") and c.text:
                try:
                    dims["readout_height"] = int(float(c.text))
                except Exception:
                    pass
        if dims:
            info.update(dims)
            break
    for e in iter_tags("pixelsize"):
        for child in e.iter():
            if child.tag.lower().endswith("numericvalue") and child.text:
                info["pixel_size"] = float(child.text)
                break
        if "pixel_size" in info:
            break
    stage_coords = None
    for e in root.iter():
        if e.tag.lower().endswith("stage"):
            for c in e.iter():
                if c.tag.lower().endswith("position"):
                    coords = {}
                    for cc in c:
                        tag = cc.tag.lower()
                        if tag.endswith("x") and cc.text:
                            coords["x"] = float(cc.text)
                        if tag.endswith("y") and cc.text:
                            coords["y"] = float(cc.text)
                    if "x" in coords and "y" in coords:
                        stage_coords = coords
                        break
            if stage_coords:
                break
    if stage_coords:
        info["stage_x"] = stage_coords["x"]
        info["stage_y"] = stage_coords["y"]
    else:
        for e in iter_tags("position"):
            coords = {}
            for c in e:
                tag = c.tag.lower()
                if tag.endswith("x") and c.text:
                    coords["x"] = float(c.text)
                if tag.endswith("y") and c.text:
                    coords["y"] = float(c.text)
            if "x" in coords and "y" in coords:
                info["stage_x"] = coords["x"]
                info["stage_y"] = coords["y"]
                break
    for e in iter_tags("matrix"):
        vals = {}
        for c in e:
            tag = c.tag.lower()
            if tag.endswith("_m11") and c.text:
                vals["m11"] = float(c.text)
            if tag.endswith("_m12") and c.text:
                vals["m12"] = float(c.text)
            if tag.endswith("_m21") and c.text:
                vals["m21"] = float(c.text)
            if tag.endswith("_m22") and c.text:
                vals["m22"] = float(c.text)
        if all(k in vals for k in ("m11", "m12", "m21", "m22")):
            info["ref_matrix"] = (vals["m11"], vals["m12"], vals["m21"], vals["m22"])
            break
    return info


def parse_foil_position(xml_path: Path) -> dict:
    """Extract foil stage X,Y and center results if present."""
    info = {}
    try:
        import xml.etree.ElementTree as ET
        tree = ET.parse(xml_path)
        root = tree.getroot()
    except Exception:
        return info
    def iter_tags(tagname):
        for e in root.iter():
            if e.tag.lower().endswith(tagname.lower()):
                yield e
    # stage position
    for e in iter_tags("position"):
        coords = {}
        for c in e:
            tag = c.tag.lower()
            if tag.endswith("x") and c.text:
                coords["stage_x"] = float(c.text)
            if tag.endswith("y") and c.text:
                coords["stage_y"] = float(c.text)
        if "stage_x" in coords and "stage_y" in coords:
            info.update(coords)
            break
    # foil center
    for e in iter_tags("center"):
        coords = {}
        for c in e:
            tag = c.tag.lower()
            if tag.endswith("x") and c.text:
                coords["center_x"] = float(c.text)
            if tag.endswith("y") and c.text:
                coords["center_y"] = float(c.text)
        if coords:
            info.update(coords)
            break
    # rotation (if present in FindFoilHoleCenterResults or similar)
    for e in iter_tags("rotation"):
        if e.text:
            try:
                info["rotation"] = float(e.text)
            except Exception:
                pass
            break
    return info


def build_pdf(base_dir: Path, output_file: Path, atlas_name: str | None, no_markers: bool = False):
    grids = _collect_grids(base_dir)
    if not grids:
        raise RuntimeError(f"no GridSquare directories found in {base_dir}")

    pages = []
    for index, (_gid, grid_dir) in enumerate(grids, start=1):
        try:
            grid_image_path = find_grid_image(grid_dir)
        except Exception:
            print(f"skipping {grid_dir} (no grid image)")
            continue
        grid_image = _load_image(grid_image_path)
        if grid_image is None:
            print(f"skipping {grid_dir} (failed to load grid image)")
            continue
        grid_meta = {}
        grid_xml = grid_dir / grid_image_path.with_suffix('.xml').name
        if grid_xml.is_file():
            grid_meta = parse_grid_info(grid_xml)
        foils, datas = gather_foil_and_data(grid_dir)
        foil_entries = [
            (fid, path)
            for fid in sorted(foils.keys())
            for path in foils[fid]
        ]
        foil_label_map = {}
        markers = None
        if (not no_markers) and grid_meta.get('pixel_size') and grid_meta.get('stage_x'):
            markers = []
            marker_idx = 1
            for foil_id, foil_path in foil_entries:
                xml_path = foil_path.with_suffix('.xml')
                if not xml_path.is_file():
                    continue
                fp = parse_foil_position(xml_path)
                fmeta = parse_grid_info(xml_path)
                if 'stage_x' not in fp or 'stage_y' not in fp:
                    continue

                if 'center_x' in fp and 'center_y' in fp and fmeta.get('pixel_size'):
                    try:
                        with Image.open(foil_path) as foil_img_for_calc:
                            fw, fh = foil_img_for_calc.size
                        foil_px = float(fp['center_x'])
                        foil_py = float(fp['center_y'])
                        dx_px = foil_px - (fw / 2.0)
                        dy_px = foil_py - (fh / 2.0)
                        dy_img_up = -dy_px
                        if 'rotation' in fp:
                            try:
                                import math
                                th = float(fp['rotation'])
                                c = math.cos(th)
                                s = math.sin(th)
                                rx_px = c * dx_px - s * dy_img_up
                                ry_px = s * dx_px + c * dy_img_up
                            except Exception:
                                rx_px, ry_px = dx_px, dy_img_up
                        else:
                            rx_px, ry_px = dx_px, dy_img_up
                        center_stage_x = fp['stage_x'] + rx_px * fmeta['pixel_size']
                        center_stage_y = fp['stage_y'] + ry_px * fmeta['pixel_size']
                    except Exception:
                        center_stage_x = fp['stage_x']
                        center_stage_y = fp['stage_y']
                else:
                    center_stage_x = fp['stage_x']
                    center_stage_y = fp['stage_y']

                dx = center_stage_x - grid_meta.get('stage_x', 0)
                dy = center_stage_y - grid_meta.get('stage_y', 0)
                try:
                    px = grid_image.width / 2.0 + dx / grid_meta['pixel_size']
                    py = grid_image.height / 2.0 - dy / grid_meta['pixel_size']
                except Exception:
                    continue

                in_bounds = (0 <= px < grid_image.width) and (0 <= py < grid_image.height)
                labels_this = marker_idx
                markers.append((px, py, in_bounds, labels_this))
                foil_label_map[(foil_id, foil_path.name)] = labels_this
                marker_idx += 1
                status = "in" if in_bounds else "out"
                print(f"computed marker for foil {foil_id} at px={px:.1f}, py={py:.1f} ({status}-of-bounds) label={labels_this}")
        atlas_img = None
        if atlas_name:
            atlas_path = _resolve_atlas_path(atlas_name, grid_dir, base_dir)
            if atlas_path:
                atlas_img = _load_image(atlas_path)
        grid_label = f"GridSquare {_gid}: {grid_image_path.name}"
        pages.append(make_grid_page(grid_image, grid_label, atlas_img, markers))

        if not foil_entries:
            pages.append(make_text_page("no screening data available for this square"))
        else:
            data_remaining = {fid: list(paths) for fid, paths in datas.items()}
            for foil_id, foil_path in foil_entries:
                foil_image = _load_image(foil_path, "L")
                if foil_image is None:
                    continue
                data_candidate = None
                if foil_id in data_remaining and data_remaining[foil_id]:
                    data_candidate = data_remaining[foil_id].pop(0)
                data_image = _load_image(data_candidate, "L") if data_candidate else None
                foil_lbl = foil_path.name
                data_lbl = data_candidate.name if data_candidate else None
                meta = {}
                if data_candidate:
                    xml_path = data_candidate.with_suffix(".xml")
                    if xml_path.is_file():
                        meta = parse_metadata(xml_path)
                index_label = foil_label_map.get((foil_id, foil_path.name))
                pages.append(make_foil_page(foil_image, data_image, foil_lbl, data_lbl, meta, index_label))
    if pages:
        first, rest = pages[0], pages[1:]
        first.save(output_file, "PDF", save_all=True, append_images=rest, resolution=300)
        print(f"wrote PDF to {output_file}")


def _make_data_montage(data_paths: list[Path], thumb_size=(256, 256), cols: int = 3) -> Image.Image | None:
    """Create a simple montage of data images (L mode) or return None if no data."""
    if not data_paths:
        return None
    thumbs = []
    for p in data_paths:
        try:
            im = Image.open(p).convert('L')
            im.thumbnail(thumb_size, Image.LANCZOS)
            # pad to thumb_size
            bg = Image.new('L', thumb_size, 255)
            offs = ((thumb_size[0] - im.width) // 2, (thumb_size[1] - im.height) // 2)
            bg.paste(im, offs)
            thumbs.append(bg)
        except Exception:
            continue
    if not thumbs:
        return None
    rows = (len(thumbs) + cols - 1) // cols
    montage = Image.new('L', (cols * thumb_size[0], rows * thumb_size[1]), 255)
    for idx, t in enumerate(thumbs):
        x = (idx % cols) * thumb_size[0]
        y = (idx // cols) * thumb_size[1]
        montage.paste(t, (x, y))
    return montage


def _resolve_atlas_path(atlas_name: str, grid_dir: Path, base_dir: Path) -> Path | None:
    """Resolve an atlas path robustly.

    Tries (in order):
    - if atlas_name is absolute image path -> that path
    - if atlas_name is an absolute directory -> pick Atlas_*.jpg/png from it
    - grid_dir / atlas_name
    - base_dir / atlas_name
    - any file named atlas_name anywhere under base_dir (first match)
    - atlas_name relative to CWD
    Returns Path or None.
    """

    def _choose_from_dir(directory: Path) -> Path | None:
        if not directory.is_dir():
            return None
        patterns = (
            "Atlas_*.jpg",
            "Atlas_*.jpeg",
            "Atlas_*.png",
            "atlas_*.jpg",
            "atlas_*.jpeg",
            "atlas_*.png",
            "*.jpg",
            "*.jpeg",
            "*.png",
        )
        matches: list[Path] = []
        for pattern in patterns:
            matches = [p for p in directory.glob(pattern) if p.is_file()]
            if matches:
                break
        if not matches:
            return None
        def _mtime(path: Path) -> float:
            try:
                return path.stat().st_mtime
            except Exception:
                return 0.0
        matches.sort(key=lambda p: (_mtime(p), p.name), reverse=True)
        return matches[0]

    def _resolve_candidate(candidate: Path) -> Path | None:
        if candidate.is_file():
            return candidate
        if candidate.is_dir():
            return _choose_from_dir(candidate)
        return None

    if not atlas_name:
        return None
    p = Path(atlas_name)
    # absolute
    if p.is_absolute():
        resolved = _resolve_candidate(p)
        if resolved is not None:
            return resolved
    # relative to grid_dir
    cand = grid_dir / atlas_name
    resolved = _resolve_candidate(cand)
    if resolved is not None:
        return resolved
    # relative to base_dir
    cand = base_dir / atlas_name
    resolved = _resolve_candidate(cand)
    if resolved is not None:
        return resolved
    # search recursively under base_dir
    try:
        for f in base_dir.rglob(atlas_name):
            resolved = _resolve_candidate(f)
            if resolved is not None:
                return resolved
    except Exception:
        pass
    # finally try relative to cwd
    cand = Path(atlas_name)
    resolved = _resolve_candidate(cand)
    if resolved is not None:
        return resolved
    return None


def run_interactive_review(base_dir: Path, atlas_name: str | None = None, report_file: Path | None = None):
    """Interactive review using a temporary local HTTP server and HTML UI.

    For each gridsquare the script serves a simple HTML page with the grid
    and a data montage, the user selects a rating and optional comment. The
    page posts the result back to the local server which the script uses to
    collect responses and proceed to the next gridsquare. The page attempts
    to close itself after submission.
    """
    # collect grid dirs in acquisition order (same ordering as the web app)
    grids = _collect_grids(base_dir)

    responses: dict[str, dict] = {}

    # create temporary dir to host images/html
    tmpdir = Path(tempfile.mkdtemp(prefix="grid_review_"))

    # a queue where the HTTP handler will push submissions
    q: "queue.Queue[dict]" = queue.Queue()

    class _Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, directory=None, **kwargs):
            super().__init__(*args, directory=tmpdir, **kwargs)

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != '/mrc':
                return super().do_GET()
            qs = urllib.parse.parse_qs(parsed.query)
            f = qs.get('file', [''])[0]
            low = qs.get('low', ['2'])[0]
            high = qs.get('high', ['98'])[0]
            try:
                low_f = float(low)
                high_f = float(high)
            except Exception:
                low_f = 2.0
                high_f = 98.0
            try:
                p = Path(urllib.parse.unquote(f))
            except Exception:
                self.send_error(404)
                return
            try:
                base = base_dir.resolve()
                p_resolved = p.resolve()
                if not p.is_file() or p.suffix.lower() != '.mrc':
                    self.send_error(404)
                    return
                try:
                    if not p_resolved.is_relative_to(base):
                        self.send_error(403)
                        return
                except Exception:
                    if base not in p_resolved.parents and p_resolved != base:
                        self.send_error(403)
                        return
            except Exception:
                self.send_error(404)
                return
            img = _mrc_to_image(p, low_f, high_f)
            if img is None:
                self.send_error(404)
                return
            buf = io.BytesIO()
            img.save(buf, format='PNG')
            payload = buf.getvalue()
            self.send_response(200)
            self.send_header('Content-Type', 'image/png')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            if self.path != '/submit':
                self.send_error(404)
                return
            length = int(self.headers.get('content-length', 0))
            body = self.rfile.read(length).decode('utf-8')
            try:
                data = json.loads(body)
            except Exception:
                data = {}
            # push into queue for main thread
            q.put(data)
            # respond with a small page that closes the window
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            resp_html = '<html><body><h3>Submitted.</h3></body></html>'
            self.wfile.write(resp_html.encode('utf-8'))

    # start server on an ephemeral port
    class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        allow_reuse_address = True

    server = ThreadedTCPServer(('127.0.0.1', 0), lambda *a, **kw: _Handler(*a, **kw))
    port = server.server_address[1]
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()

    try:
        total = len(grids)
        for idx, (_gid, grid_dir) in enumerate(grids, start=1):
            print(f"Reviewing {grid_dir.name} ({idx}/{total})")
            try:
                grid_image_path = find_grid_image(grid_dir)
            except Exception:
                print(f"skipping {grid_dir} (no grid image)")
                continue
            grid_image = _load_image(grid_image_path)
            if grid_image is None:
                print(f"skipping {grid_dir} (failed to load grid image)")
                continue
            atlas_img = None
            if atlas_name:
                atlas_path = _resolve_atlas_path(atlas_name, grid_dir, base_dir)
                if atlas_path:
                    atlas_img = _load_image(atlas_path)

            grid_page = make_grid_page(grid_image, f"GridSquare {_gid}: {grid_image_path.name}", atlas_img, markers=None)
            _, datas = gather_foil_and_data(grid_dir)
            data_paths: list[Path] = []
            for data_id in sorted(datas.keys()):
                for path in datas[data_id]:
                    if path.is_file():
                        data_paths.append(path)
                    if len(data_paths) >= 36:
                        break
                if len(data_paths) >= 36:
                    break
            montage = _make_data_montage(data_paths)
            has_data = bool(data_paths)
            mrc_path = find_grid_mrc(grid_dir)
            print(f"MRC for {grid_dir.name}: {mrc_path}")

            if montage:
                w = max(grid_page.width, montage.width)
                h = grid_page.height + montage.height + 8
                canvas = Image.new('RGB', (w, h), (255, 255, 255))
                canvas.paste(grid_page, ((w - grid_page.width)//2, 0))
                canvas.paste(montage.convert('RGB'), ((w - montage.width)//2, grid_page.height + 8))
            else:
                canvas = grid_page

            img_name = "grid.png"
            html_name = "grid.html"
            img_path = tmpdir / img_name
            html_path = tmpdir / html_name
            canvas.save(img_path)

            html_template = """<html><head><meta charset=\"utf-8\"><title>Review {GRID}</title></head>
<body style=\"font-family: sans-serif;\">
<h2>{GRID}</h2>
<div style=\"margin-bottom:6px;color:#555;\">{PROGRESS}</div>
{NODATA}
{MRCCTA}
<div id=\"contrast-panel\" style=\"display:none;margin-bottom:8px;\">
<div>Low: <span id=\"lowv\">2</span>% <input type=\"range\" id=\"low\" min=\"0\" max=\"50\" value=\"2\"></div>
<div>High: <span id=\"highv\">98</span>% <input type=\"range\" id=\"high\" min=\"50\" max=\"100\" value=\"98\"></div>
</div>
<img id=\"gridimg\" src=\"{IMG}?v={TS}\" style=\"max-width:100%;height:auto;display:block;margin-bottom:8px;\"/>
<div style=\"margin-bottom:8px;\">
<button type=\"button\" class=\"rate\" data-v=\"1\">1</button>
<button type=\"button\" class=\"rate\" data-v=\"2\">2</button>
<button type=\"button\" class=\"rate\" data-v=\"3\">3</button>
<button type=\"button\" class=\"rate\" data-v=\"4\">4</button>
<button type=\"button\" class=\"rate\" data-v=\"5\">5</button>
<button type=\"button\" id=\"skip\">Skip</button>
</div>
<div>Selected rating: <span id=\"selected\">3</span></div>
<div>Comments:</div>
<textarea id=\"comment\" rows=\"4\" cols=\"60\" style=\"width:100%;max-width:900px;\"></textarea><br/>
<button type=\"button\" id=\"submit\">Submit (Ctrl+Enter)</button>
<script>
const GRIDNAME = {GRIDJSON};
const STATIC_SRC = "{IMG}?v={TS}";
const MRCFILE = {MRCJSON};
let rating = 3;
function setRating(v){ rating = v; document.getElementById('selected').textContent = String(v); }
Array.from(document.querySelectorAll('.rate')).forEach(b=>{
  b.onclick = () => setRating(parseInt(b.dataset.v));
});
document.getElementById('skip').onclick = () => { rating = 0; submit(); };
function mrcUrl(){
  const low = document.getElementById('low').value;
  const high = document.getElementById('high').value;
  return '/mrc?file=' + encodeURIComponent(MRCFILE) + '&low=' + low + '&high=' + high + '&t=' + Date.now();
}
function updateContrast(){
  const lowEl = document.getElementById('low');
  const highEl = document.getElementById('high');
  let low = parseInt(lowEl.value);
  let high = parseInt(highEl.value);
  if (low >= high) {
    if (low > 0) { low = high - 1; lowEl.value = String(low); }
    else { high = low + 1; highEl.value = String(high); }
  }
  document.getElementById('lowv').textContent = String(low);
  document.getElementById('highv').textContent = String(high);
  document.getElementById('gridimg').src = mrcUrl();
}
const contrastBtn = document.getElementById('contrast');
const overviewBtn = document.getElementById('overview');
if (contrastBtn) {
  contrastBtn.onclick = () => {
    document.getElementById('contrast-panel').style.display = 'block';
    updateContrast();
  };
}
if (overviewBtn) {
  overviewBtn.onclick = () => {
    document.getElementById('gridimg').src = STATIC_SRC;
  };
}
if (MRCFILE) {
  document.getElementById('low').oninput = updateContrast;
  document.getElementById('high').oninput = updateContrast;
}
function submit(){
  const payload = {grid: GRIDNAME, rating: rating, comment: document.getElementById('comment').value};
  fetch('/submit', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)})
    .then(()=>{ document.body.innerHTML = '<h3>Submitted.</h3>'; setTimeout(()=>{ window.location = '/grid.html?t=' + Date.now(); }, 300); })
    .catch(()=>{ document.body.innerHTML = '<h3>Submitted.</h3>'; setTimeout(()=>{ window.location = '/grid.html?t=' + Date.now(); }, 300); });
}
document.getElementById('submit').onclick = submit;
document.addEventListener('keydown', (e)=>{
  if (e.key >= '1' && e.key <= '5') { setRating(parseInt(e.key)); }
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { submit(); }
});
</script>
</body></html>"""

            canvas = grid_page

            img_name = "grid.png"
            html_name = "grid.html"
            img_path = tmpdir / img_name
            html_path = tmpdir / html_name
            canvas.save(img_path)

            # build html with MRC-enabled UI: keyboard shortcuts, skip button, progress display
            html_template = """<html><head><meta charset="utf-8"><title>Review {GRID}</title></head>
<body style="font-family: sans-serif;">
<h2>{GRID}</h2>
<div style="margin-bottom:6px;color:#555;">{PROGRESS}</div>
{NODATA}
<img id="gridimg" src="{IMG}?v={TS}" style="max-width:100%;height:auto;display:block;margin-bottom:8px;"/>
<div style="margin-bottom:8px;">
<button type="button" class="rate" data-v="1">1</button>
<button type="button" class="rate" data-v="2">2</button>
<button type="button" class="rate" data-v="3">3</button>
<button type="button" class="rate" data-v="4">4</button>
<button type="button" class="rate" data-v="5">5</button>
<button type="button" id="skip">Skip</button>
</div>
{MRCCTA}
<div id="contrast-panel" style="display:none;margin-bottom:8px;">
<div>Low: <span id="lowv">2</span>% <input type="range" id="low" min="0" max="99" value="2"></div>
<div>High: <span id="highv">98</span>% <input type="range" id="high" min="1" max="100" value="98"></div>
</div>
<div>Selected rating: <span id="selected">3</span></div>
<div>Comments:</div>
<textarea id="comment" rows="4" cols="60" style="width:100%;max-width:900px;"></textarea><br/>
<button type="button" id="submit">Submit (Ctrl+Enter)</button>
<script>
const GRIDNAME = {GRIDJSON};
const STATIC_SRC = "{IMG}?v={TS}";
const MRCFILE = {MRCJSON};
let rating = 3;
function setRating(v){ rating = v; document.getElementById('selected').textContent = String(v); }
Array.from(document.querySelectorAll('.rate')).forEach(b=>{
  b.onclick = () => setRating(parseInt(b.dataset.v));
});
document.getElementById('skip').onclick = () => { rating = 0; submit(); };
function mrcUrl(){
  const low = document.getElementById('low').value;
  const high = document.getElementById('high').value;
  return '/mrc?file=' + encodeURIComponent(MRCFILE) + '&low=' + low + '&high=' + high + '&t=' + Date.now();
}
function updateContrast(){
  const lowEl = document.getElementById('low');
  const highEl = document.getElementById('high');
  let low = parseInt(lowEl.value);
  let high = parseInt(highEl.value);
  if (low >= high) {
    if (low > 0) { low = high - 1; lowEl.value = String(low); }
    else { high = low + 1; highEl.value = String(high); }
  }
  document.getElementById('lowv').textContent = String(low);
  document.getElementById('highv').textContent = String(high);
  document.getElementById('gridimg').src = mrcUrl();
}
const contrastBtn = document.getElementById('contrast');
const overviewBtn = document.getElementById('overview');
if (contrastBtn) {
  contrastBtn.onclick = () => {
    if (!MRCFILE) { return; }
    document.getElementById('contrast-panel').style.display = 'block';
    updateContrast();
  };
}
if (overviewBtn) {
  overviewBtn.onclick = () => {
    document.getElementById('gridimg').src = STATIC_SRC;
  };
}
if (MRCFILE) {
  document.getElementById('low').oninput = updateContrast;
  document.getElementById('high').oninput = updateContrast;
}
function submit(){
  const payload = {grid: GRIDNAME, rating: rating, comment: document.getElementById('comment').value};
  fetch('/submit', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)})
    .then(()=>{ document.body.innerHTML = '<h3>Submitted.</h3>'; setTimeout(()=>{ window.location = '/grid.html?t=' + Date.now(); }, 300); })
    .catch(()=>{ document.body.innerHTML = '<h3>Submitted.</h3>'; setTimeout(()=>{ window.location = '/grid.html?t=' + Date.now(); }, 300); });
}
document.getElementById('submit').onclick = submit;
document.addEventListener('keydown', (e)=>{
  if (e.key >= '1' && e.key <= '5') { setRating(parseInt(e.key)); }
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { submit(); }
});
</script>
</body></html>"""

            nodata_html = "" if mrc_path else "<div style=\"margin-bottom:8px;color:#b00;\">No screening data available for this GridSquare.</div>"
            if mrc_path:
                mrc_html = "<div style=\"margin-bottom:8px;\"><button type=\"button\" id=\"contrast\">Adjust contrast</button> <button type=\"button\" id=\"overview\">Show overview</button></div>"
                mrc_json = json.dumps(str(mrc_path))
            else:
                mrc_html = "<div style=\"margin-bottom:8px;color:#555;\">No MRC available for this GridSquare.</div>"
                mrc_json = "null"
            html = html_template.replace('{IMG}', img_name).replace('{GRID}', grid_image_path.name).replace('{GRIDJSON}', json.dumps(grid_image_path.name)).replace('{PROGRESS}', f"{idx} / {total}").replace('{NODATA}', nodata_html).replace('{TS}', str(idx)).replace('{MRCCTA}', mrc_html).replace('{MRCJSON}', mrc_json)
            html_path.write_text(html, encoding='utf-8')

            url = f'http://127.0.0.1:{port}/{html_name}?t={idx}'
            if idx == 1:
                webbrowser.open(url)

            # wait for submission for this grid, validating grid name in response
            try:
                while True:
                    data = q.get(timeout=None)
                    if data.get('grid') == grid_image_path.name:
                        break
            except Exception:
                print("no response received; skipping")
                continue
            # store keyed by grid image filename
            responses[grid_image_path.name] = {"rating": int(data.get('rating', 0)), "comment": data.get('comment','')}

    finally:
        # shutdown server and cleanup
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass
        try:
            for f in tmpdir.iterdir():
                try:
                    f.unlink()
                except Exception:
                    pass
            tmpdir.rmdir()
        except Exception:
            pass

    # write final report PDF
    report_out = report_file or (base_dir / "Screening_overview.pdf")
    write_review_report(base_dir, report_out, atlas_name, responses)
    print(f"Wrote review report to {report_out}")


def _build_overview_page_image(
    base_dir: Path,
    atlas_name: str | None,
    responses: dict,
    atlas_overlay: bool = True,
    global_summary: str | None = None,
) -> Image.Image:
    grids = _collect_grids(base_dir)
    if not grids:
        raise RuntimeError(f"no GridSquare directories found in {base_dir}")

    def _ensure_font(size: int, fallback=None):
        font = _get_font(size)
        if font is None:
            try:
                return fallback or ImageFont.load_default()
            except Exception:
                return fallback
        return font

    page_w, page_h = 2400, 2300
    margin = 40
    atlas_gap = 24
    atlas_panel_w = int((page_w - 2 * margin - atlas_gap * 2) / 3)
    atlas_box_h = 520
    page = Image.new("RGB", (page_w, page_h), color=(255, 255, 255))
    draw = ImageDraw.Draw(page)
    fonts = {
        "title": _ensure_font(64),
        "body": _ensure_font(24),
        "small": _ensure_font(18),
        "table": _ensure_font(22),
    }
    line_h = (fonts["body"].size + 6) if getattr(fonts["body"], "size", None) else 24
    y_offset = margin

    atlas_img = None
    atlas_path_for_report = None
    atlas_nodes: dict[str, dict] = {}
    if atlas_name:
        for _, g in grids:
            atlas_path = _resolve_atlas_path(atlas_name, g, base_dir)
            if atlas_path:
                atlas_img = _load_image(atlas_path, "RGB")
                if atlas_img is not None:
                    atlas_path_for_report = atlas_path
                    break
    if atlas_path_for_report is not None:
        atlas_nodes, _ref_w, _ref_h = _load_atlas_mapping(atlas_path_for_report)

    atlas_screened_panel = Image.new("RGB", (atlas_panel_w, atlas_box_h), color=(245, 245, 245))
    atlas_category_panel = Image.new("RGB", (atlas_panel_w, atlas_box_h), color=(245, 245, 245))
    atlas_raw_panel = Image.new("RGB", (atlas_panel_w, atlas_box_h), color=(245, 245, 245))
    if atlas_img is not None:
        atlas_raw = atlas_img.convert("RGB")
        atlas_raw.thumbnail((atlas_panel_w - 20, atlas_box_h - 20), Image.LANCZOS)
        ox = (atlas_panel_w - atlas_raw.width) // 2
        oy = (atlas_box_h - atlas_raw.height) // 2
        atlas_raw_panel.paste(atlas_raw, (ox, oy))

        atlas_screened = atlas_img.convert("RGB")
        if atlas_overlay and atlas_path_for_report is not None:
            marker_items = [
                (idx, gdir, gid, False)
                for idx, (gid, gdir) in enumerate(grids, start=1)
            ]
            atlas_screened = _atlas_with_grid_markers(atlas_screened, atlas_path_for_report, marker_items)
        atlas_screened.thumbnail((atlas_panel_w - 20, atlas_box_h - 20), Image.LANCZOS)
        ox = (atlas_panel_w - atlas_screened.width) // 2
        oy = (atlas_box_h - atlas_screened.height) // 2
        atlas_screened_panel.paste(atlas_screened, (ox, oy))

        atlas_category = _atlas_with_category_markers(atlas_img.convert("RGB"), atlas_path_for_report)
        atlas_category.thumbnail((atlas_panel_w - 20, atlas_box_h - 20), Image.LANCZOS)
        ox = (atlas_panel_w - atlas_category.width) // 2
        oy = (atlas_box_h - atlas_category.height) // 2
        atlas_category_panel.paste(atlas_category, (ox, oy))
    atlas_screened_panel = _label_image(
        atlas_screened_panel,
        "Atlas: screened GridSquares" if atlas_img is not None else "No atlas available",
    )
    atlas_category_panel = _label_image(
        atlas_category_panel,
        "Atlas: all squares by EPU color/category (arbitrary colors)" if atlas_img is not None else "No atlas available",
    )
    atlas_raw_panel = _label_image(
        atlas_raw_panel,
        "Atlas: raw (no overlay)" if atlas_img is not None else "No atlas available",
    )
    page.paste(atlas_screened_panel, (margin, margin))
    page.paste(atlas_category_panel, (margin + atlas_panel_w + atlas_gap, margin))
    page.paste(atlas_raw_panel, (margin + 2 * (atlas_panel_w + atlas_gap), margin))

    stats_y = margin + atlas_box_h + 24
    color_note = "Note: category colors are arbitrary and currently do not match the EPU GUI color code."
    draw.text((margin, stats_y), color_note, fill=(80, 88, 108), font=fonts["small"])
    stats_y += line_h
    reviewed_count = sum(1 for resp in responses.values() if resp)
    selected_count = sum(1 for resp in responses.values() if resp and bool(resp.get("include")))
    stats_lines = [
        f"Total GridSquares: {len(grids)}",
        f"Reviewed GridSquares: {reviewed_count}",
        f"Selected GridSquares: {selected_count}",
    ]
    for text in stats_lines:
        draw.text((margin, stats_y), text, fill=0, font=fonts["body"])
        stats_y += line_h

    rows = []
    for idx, (gid, gdir) in enumerate(grids, start=1):
        name = gdir.name
        resp = responses.get(name)
        rating = resp.get("rating", "—") if resp else "—"
        comment = (resp.get("comment", "") if resp else "").strip()
        include_flag = "Yes" if resp and resp.get("include") else "No"
        category_score = _atlas_category_for_grid(atlas_nodes, gdir, gid) if atlas_nodes else None
        rows.append(
            (
                f"GridSquare {idx}",
                _format_category_score(category_score),
                str(rating),
                include_flag,
                comment or "—",
            )
        )
    summary_text = (global_summary or "").strip()
    if summary_text:
        stats_y += line_h + 4
        draw.text((margin, stats_y), "Session summary:", fill=0, font=fonts["body"])
        stats_y += line_h
        summary_line_h = (fonts["small"].size + 4) if getattr(fonts["small"], "size", None) else 20
        for line in textwrap.wrap(summary_text, width=48):
            draw.text((margin, stats_y), line, fill=0, font=fonts["small"])
            stats_y += summary_line_h

    y_offset = stats_y + 18
    heading = "GridSquare Review Summary"
    draw.text((margin, y_offset), heading, fill=0, font=fonts["title"])
    if hasattr(draw, "textbbox") and fonts["title"]:
        bbox = draw.textbbox((margin, y_offset), heading, font=fonts["title"])
        y_offset = bbox[3] + 12
    else:
        y_offset += line_h

    legend = (
        "Each row lists the GridSquare order, EPU category score, rating (0 means skipped), "
        "reviewer notes, and whether it was marked for detailed review."
    )
    for chunk in textwrap.wrap(legend, width=120):
        draw.text((margin, y_offset), chunk, fill=0, font=fonts["body"])
        y_offset += line_h
    y_offset += 12

    grid_col = margin
    category_col = grid_col + 260
    rating_col = category_col + 340
    include_col = rating_col + 180
    comment_col = include_col + 220
    header_y = y_offset
    draw.text((grid_col, header_y), "GridSquare", fill=0, font=fonts["table"])
    draw.text((category_col, header_y), "EPU color / category", fill=0, font=fonts["table"])
    draw.text((rating_col, header_y), "Rating", fill=0, font=fonts["table"])
    draw.text((include_col, header_y), "Included?", fill=0, font=fonts["table"])
    draw.text((comment_col, header_y), "Reviewer comments", fill=0, font=fonts["table"])
    y_offset = header_y + (fonts["table"].size if hasattr(fonts["table"], "size") else 24) + 6
    row_height = (fonts["table"].size if hasattr(fonts["table"], "size") else 24) + 6
    for grid_label, category_text, rating_text, include_text, comment_text in rows:
        draw.text((grid_col, y_offset), grid_label, fill=0, font=fonts["table"])
        draw.text((category_col, y_offset), category_text, fill=0, font=fonts["table"])
        draw.text((rating_col, y_offset), rating_text, fill=0, font=fonts["table"])
        draw.text((include_col, y_offset), include_text, fill=0, font=fonts["table"])
        comment_lines = textwrap.wrap(comment_text, width=72) or ["—"]
        comment_y = y_offset
        for c_line in comment_lines:
            draw.text((comment_col, comment_y), c_line, fill=0, font=fonts["table"])
            comment_y += row_height
        y_offset = max(y_offset + row_height, comment_y)
    return page


def _append_pil_page(pdf: pdf_canvas.Canvas, page_image: Image.Image) -> None:
    page_rgb = page_image.convert("RGB")
    page_w, page_h = page_rgb.size
    pdf.setPageSize((float(page_w), float(page_h)))
    pdf.drawImage(ImageReader(page_rgb), 0, 0, width=page_w, height=page_h)
    pdf.showPage()


def _append_selected_report_pages(
    pdf: pdf_canvas.Canvas,
    base_dir: Path,
    atlas_name: str | None,
    responses: dict,
    overlay: bool = False,
    atlas_overlay: bool = True,
    global_summary: str | None = None,
    include_summary_page: bool = True,
) -> None:
    grids = _collect_grids(base_dir)
    if not grids:
        raise RuntimeError(f"no GridSquare directories found in {base_dir}")
    include_list = []
    for idx, (gid, gdir) in enumerate(grids, start=1):
        resp = responses.get(gdir.name)
        if resp and bool(resp.get("include")):
            include_list.append((idx, gid, gdir, resp))
    failed: list[tuple[str, str]] = []
    summary_text = (global_summary or "").strip()
    if include_summary_page and summary_text:
        summary_lines = textwrap.wrap(summary_text, width=100) or [summary_text]
        _draw_pdf_message_page(pdf, summary_lines, title="Session Summary")
    if not include_list:
        _draw_pdf_message_page(pdf, ["No GridSquares selected for this report."])
    else:
        for idx, gid, gdir, resp in include_list:
            grid_name = gdir.name
            try:
                grid_image_path = find_grid_image(gdir)
            except FileNotFoundError:
                failed.append((grid_name, "GridSquare JPEG not found"))
                print(f"[selected_report] skipping {grid_name}: grid image missing")
                continue
            grid_image = _load_image(grid_image_path)
            if grid_image is None:
                failed.append((grid_name, f"could not open {grid_image_path.name}"))
                print(f"[selected_report] skipping {grid_name}: unable to load grid image {grid_image_path}")
                continue
            atlas_img_local = None
            atlas_path_local = None
            atlas_nodes: dict[str, dict] = {}
            if atlas_name:
                atlas_path = _resolve_atlas_path(atlas_name, gdir, base_dir)
                if atlas_path:
                    atlas_path_local = atlas_path
                    atlas_img_local = _load_image(atlas_path, "RGB")
                    atlas_nodes, _ref_w, _ref_h = _load_atlas_mapping(atlas_path_local)
                    if atlas_overlay and atlas_img_local is not None:
                        atlas_img_local = _atlas_with_grid_markers(
                            atlas_img_local,
                            atlas_path_local,
                            [(idx, gdir, gid, True)],
                        )
            category_score = _atlas_category_for_grid(atlas_nodes, gdir, gid) if atlas_nodes else None
            overlay_img_local = None
            if overlay:
                overlay_path = _find_overlay_image(gdir, base_dir)
                if overlay_path:
                    overlay_img_local = _load_image(overlay_path, "RGB")
            heading = f"GridSquare {idx}: {grid_image_path.name}"
            foils, datas = gather_foil_and_data(gdir)
            foils = _latest_only(foils)
            datas = _latest_only(datas)
            try:
                _draw_grid_summary_page(
                    pdf,
                    grid_image,
                    atlas_img_local,
                    foils,
                    datas,
                    resp,
                    heading,
                    grid_image_path.name,
                    overlay_img_local,
                    category_score=category_score,
                )
            except Exception as exc:
                failed.append((grid_name, f"render error: {exc}"))
                print(f"[selected_report] skipping {grid_name}: {exc}")
                continue
    if failed:
        lines = ["Some selected GridSquares were skipped:"]
        for name, reason in failed[:12]:
            lines.append(f"- {name}: {reason}")
        if len(failed) > 12:
            lines.append("... see console for the full list")
        _draw_pdf_message_page(pdf, lines)


def write_review_report(
    base_dir: Path,
    report_file: Path,
    atlas_name: str | None,
    responses: dict,
    atlas_overlay: bool = True,
    global_summary: str | None = None,
):
    """Generate a single-page overview PDF."""
    page = _build_overview_page_image(
        base_dir,
        atlas_name,
        responses,
        atlas_overlay=atlas_overlay,
        global_summary=global_summary,
    )
    page.save(report_file, "PDF", resolution=300)


def write_selected_report(
    base_dir: Path,
    report_file: Path,
    atlas_name: str | None,
    responses: dict,
    overlay: bool = False,
    atlas_overlay: bool = True,
    global_summary: str | None = None,
):
    """Generate a detailed PDF with only the included GridSquares."""
    _ensure_pdf_fonts()
    pdf = pdf_canvas.Canvas(str(report_file))
    _append_selected_report_pages(
        pdf,
        base_dir,
        atlas_name,
        responses,
        overlay=overlay,
        atlas_overlay=atlas_overlay,
        global_summary=global_summary,
        include_summary_page=True,
    )
    pdf.save()


def write_combined_report(
    base_dir: Path,
    report_file: Path,
    atlas_name: str | None,
    responses: dict,
    overlay: bool = False,
    atlas_overlay: bool = True,
    global_summary: str | None = None,
):
    """Generate one merged PDF: overview first, then included GridSquare details."""
    _ensure_pdf_fonts()
    pdf = pdf_canvas.Canvas(str(report_file))
    overview_page = _build_overview_page_image(
        base_dir,
        atlas_name,
        responses,
        atlas_overlay=atlas_overlay,
        global_summary=global_summary,
    )
    _append_pil_page(pdf, overview_page)
    _append_selected_report_pages(
        pdf,
        base_dir,
        atlas_name,
        responses,
        overlay=overlay,
        atlas_overlay=atlas_overlay,
        global_summary=global_summary,
        include_summary_page=False,
    )
    pdf.save()


def _make_report_grid_page(grid_img: Image.Image, label: str, resp: dict | None) -> Image.Image:
    # add a text panel below the grid image with rating/comments
    w, h = grid_img.size
    extra_h = 100
    page = Image.new('RGB', (w, h + extra_h), (255, 255, 255))
    # label grid image
    grid_labeled = _label_image(grid_img.copy(), label)
    page.paste(grid_labeled.convert('RGB'), (0, 0))
    draw = ImageDraw.Draw(page)
    font = _get_font(14)
    y = h + 8
    if resp is None:
        draw.text((8, y), "No rating provided", fill=0, font=font)
    else:
        draw.text((8, y), f"Rating: {resp.get('rating')}", fill=0, font=font)
        draw.text((8, y + 24), f"Comments: {resp.get('comment','')}", fill=0, font=font)
    return page

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build TMP collage PDF for a GridSquare folder")
    parser.add_argument("grid_dir", type=Path, help="path to GridSquare directory")
    parser.add_argument("-o", "--output", type=Path, help="output PDF path")
    parser.add_argument("--atlas", type=str,
                        help="name of the atlas JPEG file to look for in each grid directory (default: none)")
    parser.add_argument("--no-markers", action='store_true', help="disable foil->grid marker mapping/drawing")
    parser.add_argument("--review", action='store_true', help="interactive review mode: display each gridsquare and collect rating/comments")
    parser.add_argument("--report", type=Path, help="output PDF path for the review report (when --review enabled)")
    args = parser.parse_args()
    out = args.output or args.grid_dir.with_suffix(".pdf")
    if args.review:
        run_interactive_review(args.grid_dir, args.atlas, args.report)
    else:
        build_pdf(args.grid_dir, out, args.atlas, no_markers=args.no_markers)
