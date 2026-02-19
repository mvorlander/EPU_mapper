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
from functools import lru_cache
from pathlib import Path

import numpy as np
import mrcfile
import tempfile
from PIL import Image, ImageDraw, ImageFont

# cryo-EM grid JPEGs can easily exceed Pillow's protective pixel limit;
# disable it so we can safely load very large images from trusted sources.
Image.MAX_IMAGE_PIXELS = None


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


def gather_foil_and_data(grid_dir: Path):
    foil_dir = grid_dir / "FoilHoles"
    data_dir = grid_dir / "Data"
    foils: dict[str, Path] = {}
    datas: dict[str, Path] = {}

    if foil_dir.is_dir():
        for f in foil_dir.glob("*.jpg"):
            parts = f.stem.split("_")
            if len(parts) >= 2 and parts[0] == "FoilHole":
                foil_id = parts[1]
                ts = _timestamp_from_filename(f)
                if foil_id in foils:
                    old_ts = _timestamp_from_filename(foils[foil_id])
                    if ts > old_ts:
                        foils[foil_id] = f
                else:
                    foils[foil_id] = f
    if data_dir.is_dir():
        for f in data_dir.glob("*.jpg"):
            parts = f.stem.split("_")
            if len(parts) >= 3 and parts[0] == "FoilHole" and parts[2] == "Data":
                foil_id = parts[1]
                ts = _timestamp_from_filename(f)
                if foil_id in datas:
                    old_ts = _timestamp_from_filename(datas[foil_id])
                    if ts > old_ts:
                        datas[foil_id] = f
                else:
                    datas[foil_id] = f
    return foils, datas


def _collect_grids(base_dir: Path):
    grids = []
    for entry in base_dir.iterdir():
        if entry.is_dir() and entry.name.startswith("GridSquare_"):
            parts = entry.name.split("_")
            try:
                gid = int(parts[1])
            except Exception:
                gid = float("inf")
            grids.append((gid, entry))
    if not grids and base_dir.name.startswith("GridSquare_"):
        try:
            grids.append((int(base_dir.name.split("_")[1]), base_dir))
        except Exception:
            grids.append((float("inf"), base_dir))
    grids.sort(key=lambda x: x[0])
    return grids


def _load_image(path: Path, mode: str | None = None) -> Image.Image | None:
    try:
        with Image.open(path) as im:
            if mode:
                return im.convert(mode)
            return im.copy()
    except Exception:
        return None


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


def make_grid_summary_page(
    grid_img: Image.Image,
    atlas_img: Image.Image | None,
    foils: dict[str, Path],
    datas: dict[str, Path],
    resp: dict | None,
    heading: str,
    grid_image_name: str,
) -> Image.Image:
    """Create an illustrated summary page for a single GridSquare."""
    page_w, page_h = 1700, 2200
    margin = 50
    gap = 32
    page = Image.new("RGB", (page_w, page_h), color=(255, 255, 255))
    draw = ImageDraw.Draw(page)
    title_font = _get_font(72)
    body_font = _get_font(30)
    small_font = _get_font(22)
    y = margin
    draw.text((margin, y), heading, fill=0, font=title_font)
    y += (title_font.size if hasattr(title_font, "size") else 74) + 4
    rating = "—" if not resp else str(resp.get("rating", "—"))
    include_flag = "Yes" if resp and resp.get("include") else "No"
    comment = "" if not resp else str(resp.get("comment", "")).strip()
    draw.text((margin, y), f"Rating: {rating}    Included in selected report: {include_flag}", fill=0, font=body_font)
    y += body_font.size + 6 if hasattr(body_font, "size") else 34
    if comment:
        draw.text((margin, y), f"Reviewer notes: {comment}", fill=0, font=body_font)
        y += body_font.size + 4 if hasattr(body_font, "size") else 30
    y += 16
    top_h = 640
    box_w = (page_w - 2 * margin - gap) // 2

    def _fit(img: Image.Image | None, w: int, h: int, mode: str = "RGB") -> Image.Image:
        if img is None:
            return Image.new(mode, (w, h), color=(240, 240, 240) if mode == "RGB" else 240)
        copy = img.convert(mode).copy()
        copy.thumbnail((w, h), Image.LANCZOS)
        canvas = Image.new(mode, (w, h), color=(255, 255, 255) if mode == "RGB" else 255)
        ox = (w - copy.width) // 2
        oy = (h - copy.height) // 2
        canvas.paste(copy, (ox, oy))
        return canvas

    atlas_box = _fit(atlas_img, box_w, top_h, "RGB")
    grid_box = _fit(grid_img, box_w, top_h, "RGB")
    atlas_box = _label_image(atlas_box, "Atlas overview" if atlas_img else "Atlas missing")
    grid_box = _label_image(grid_box, grid_image_name)
    page.paste(atlas_box, (margin, y))
    page.paste(grid_box, (margin + box_w + gap, y))
    y += top_h + gap

    thumb_w = (page_w - 2 * margin - gap) // 2
    thumb_h = thumb_w
    if not foils:
        note = "No FoilHole imagery is available for this GridSquare."
        draw.text((margin, y), note, fill=0, font=body_font)
    else:
        for foil_id, foil_path in sorted(foils.items()):
            foil_img = _load_image(foil_path, "L")
            data_path = datas.get(foil_id)
            data_img = _load_image(data_path, "L") if data_path else None
            foil_box = _fit(foil_img, thumb_w, thumb_h, "L").convert("RGB")
            data_box = _fit(data_img, thumb_w, thumb_h, "L").convert("RGB")
            foil_box = _label_image(foil_box, f"FoilHole {foil_id}")
            data_label = data_path.name if data_path else "No screening data"
            data_box = _label_image(data_box, data_label)
            page.paste(foil_box, (margin, y))
            page.paste(data_box, (margin + thumb_w + gap, y))
            meta_lines: list[str] = []
            if data_path:
                xml_path = data_path.with_suffix(".xml")
                if xml_path.is_file():
                    meta = parse_metadata(xml_path)
                    for key in ("pixel_size", "exposure", "dose", "defocus"):
                        if key in meta:
                            txt = meta[key]
                            if key == "dose":
                                txt += " e-/Å²"
                            meta_lines.append(f"{key.replace('_', ' ')}: {txt}")
            if meta_lines:
                meta_y = y + thumb_h + 10
                for line in meta_lines:
                    draw.text((margin + thumb_w + gap, meta_y), line, fill=0, font=small_font)
                    meta_y += (small_font.size if hasattr(small_font, "size") else 22)
            y += thumb_h + gap + (small_font.size if hasattr(small_font, "size") else 20)
            if y + thumb_h + margin > page_h:
                break
    return page


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
    # helper to iterate ignoring namespace
    def iter_tags(tagname):
        for e in root.iter():
            if e.tag.lower().endswith(tagname.lower()):
                yield e
    # pixel size (numericValue underneath pixelSize)
    for ps in iter_tags("pixelsize"):
        nv = None
        for child in ps.iter():
            if child.tag.lower().endswith("numericvalue"):
                nv = child
                break
        if nv is not None and nv.text:
            info["pixel_size"] = nv.text
            break
    # exposure time
    for e in iter_tags("exposuretime"):
        if e.text:
            info["exposure"] = e.text
            break
    # look for dose in custom data key/value pairs
    for kv in root.findall('.//{http://schemas.microsoft.com/2003/10/Serialization/Arrays}KeyValueOfstringanyType'):
        key = kv.find('{http://schemas.microsoft.com/2003/10/Serialization/Arrays}Key')
        val = kv.find('{http://schemas.microsoft.com/2003/10/Serialization/Arrays}Value')
        if key is not None and key.text:
            k = key.text.lower()
            if 'dose' in k and val is not None and val.text:
                info['dose'] = val.text
            if 'doseoncamera' in k and val is not None and val.text:
                info['dose_on_camera'] = val.text
    # if explicit dose missing and we have rate+exposure, compute
    if 'dose' not in info and 'dose_on_camera' in info and 'exposure' in info:
        try:
            rate = float(info['dose_on_camera'])
            exp = float(info['exposure'])
            info['dose'] = str(rate * exp)
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
    # defocus (choose first defocus value encountered)
    for d in iter_tags('defocus'):
        if d.text:
            info['defocus'] = d.text
            break
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
    for e in iter_tags("pixelsize"):
        for child in e.iter():
            if child.tag.lower().endswith("numericvalue") and child.text:
                info["pixel_size"] = float(child.text)
                break
        if "pixel_size" in info:
            break
    # stage position
    for e in iter_tags("position"):
        # look for X and Y children
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
        foil_label_map = {}
        markers = None
        if (not no_markers) and grid_meta.get('pixel_size') and grid_meta.get('stage_x'):
            markers = []
            marker_idx = 1
            for foil_id, foil_path in sorted(foils.items()):
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
                foil_label_map[foil_id] = labels_this
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

        if not foils:
            pages.append(make_text_page("no screening data available for this square"))
        else:
            for foil_id, foil_path in sorted(foils.items()):
                foil_image = _load_image(foil_path, "L")
                if foil_image is None:
                    continue
                data_path = datas.get(foil_id)
                data_image = _load_image(data_path, "L") if data_path else None
                foil_lbl = foil_path.name
                data_lbl = data_path.name if data_path else None
                meta = {}
                if data_path:
                    xml_path = data_path.with_suffix(".xml")
                    if xml_path.is_file():
                        meta = parse_metadata(xml_path)
                index_label = foil_label_map.get(foil_id)
                pages.append(make_foil_page(foil_image, data_image, foil_lbl, data_lbl, meta, index_label))
    if pages:
        first, rest = pages[0], pages[1:]
        first.save(output_file, "PDF", save_all=True, append_images=rest, resolution=150)
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
    - if atlas_name is absolute and exists -> that path
    - grid_dir / atlas_name
    - base_dir / atlas_name
    - any file named atlas_name anywhere under base_dir (first match)
    - atlas_name relative to CWD
    Returns Path or None.
    """
    if not atlas_name:
        return None
    p = Path(atlas_name)
    # absolute
    if p.is_absolute() and p.is_file():
        return p
    # relative to grid_dir
    cand = grid_dir / atlas_name
    if cand.is_file():
        return cand
    # relative to base_dir
    cand = base_dir / atlas_name
    if cand.is_file():
        return cand
    # search recursively under base_dir
    try:
        for f in base_dir.rglob(atlas_name):
            if f.is_file():
                return f
    except Exception:
        pass
    # finally try relative to cwd
    cand = Path(atlas_name)
    if cand.is_file():
        return cand
    return None


def run_interactive_review(base_dir: Path, atlas_name: str | None = None, report_file: Path | None = None):
    """Interactive review using a temporary local HTTP server and HTML UI.

    For each gridsquare the script serves a simple HTML page with the grid
    and a data montage, the user selects a rating and optional comment. The
    page posts the result back to the local server which the script uses to
    collect responses and proceed to the next gridsquare. The page attempts
    to close itself after submission.
    """
    # collect grid dirs in order
    grids = []
    for entry in base_dir.iterdir():
        if entry.is_dir() and entry.name.startswith("GridSquare_"):
            parts = entry.name.split("_")
            try:
                gid = int(parts[1])
            except Exception:
                gid = float('inf')
            grids.append((gid, entry))
    if not grids and base_dir.name.startswith("GridSquare_"):
        try:
            grids.append((int(base_dir.name.split("_")[1]), base_dir))
        except Exception:
            grids.append((float('inf'), base_dir))
    grids.sort(key=lambda x: x[0])

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
            data_paths = [datas[k] for k in sorted(datas.keys()) if datas[k].is_file()][:36]
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
    report_out = report_file or (base_dir / "review_report.pdf")
    write_review_report(base_dir, report_out, atlas_name, responses)
    print(f"Wrote review report to {report_out}")


def write_review_report(base_dir: Path, report_file: Path, atlas_name: str | None, responses: dict):
    """Generate a single-page PDF with atlas (left) and ratings/comments (right) in a compact format."""
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

    page_w, page_h = 1700, 2200
    margin = 40
    column_gap = 36
    left_col_w = 720
    atlas_box_h = 720
    page = Image.new("RGB", (page_w, page_h), color=(255, 255, 255))
    draw = ImageDraw.Draw(page)
    fonts = {
        "title": _ensure_font(64),
        "body": _ensure_font(24),
        "small": _ensure_font(18),
        "table": _ensure_font(22),
    }
    line_h = (fonts["body"].size + 6) if getattr(fonts["body"], "size", None) else 24
    right_x = margin + left_col_w + column_gap
    y_offset = margin

    atlas_img = None
    if atlas_name:
        for _, g in grids:
            atlas_path = _resolve_atlas_path(atlas_name, g, base_dir)
            if atlas_path:
                atlas_img = _load_image(atlas_path, "RGB")
                if atlas_img is not None:
                    break

    atlas_panel = Image.new("RGB", (left_col_w, atlas_box_h), color=(245, 245, 245))
    if atlas_img is not None:
        atlas_copy = atlas_img.convert("RGB")
        atlas_copy.thumbnail((left_col_w - 20, atlas_box_h - 20), Image.LANCZOS)
        ox = (left_col_w - atlas_copy.width) // 2
        oy = (atlas_box_h - atlas_copy.height) // 2
        atlas_panel.paste(atlas_copy, (ox, oy))
    atlas_label = "Atlas overview" if atlas_img is not None else "No atlas available"
    atlas_panel = _label_image(atlas_panel, atlas_label)
    page.paste(atlas_panel, (margin, margin))

    stats_y = margin + atlas_box_h + 28
    reviewed_count = sum(1 for resp in responses.values() if resp)
    selected_count = sum(1 for resp in responses.values() if resp and bool(resp.get('include')))
    stats_lines = [
        f"Total GridSquares: {len(grids)}",
        f"Reviewed GridSquares: {reviewed_count}",
    ]
    for text in stats_lines:
        draw.text((margin, stats_y), text, fill=0, font=fonts["body"])
        stats_y += line_h

    rows = []
    for idx, (_gid, gdir) in enumerate(grids, start=1):
        name = gdir.name
        resp = responses.get(name)
        rating = resp.get('rating', '—') if resp else '—'
        comment = (resp.get('comment', '') if resp else '').strip()
        include_flag = "Yes" if resp and resp.get('include') else "No"
        rows.append((f"GridSquare {idx}", str(rating), include_flag, comment or "—"))
    draw.text((margin, stats_y), f"Selected GridSquares: {selected_count}", fill=0, font=fonts["body"])

    # place ratings/comments on right
    heading = "GridSquare Review Summary"
    draw.text((right_x, y_offset), heading, fill=0, font=fonts["title"])
    if hasattr(draw, "textbbox") and fonts["title"]:
        bbox = draw.textbbox((right_x, y_offset), heading, font=fonts["title"])
        y_offset = bbox[3] + 12
    else:
        y_offset += line_h

    legend = (
        "Each row lists the GridSquare order, its rating (0 means skipped), "
        "any reviewer notes, and whether it was marked for the Selected report."
    )
    for chunk in textwrap.wrap(legend, width=70):
        draw.text((right_x, y_offset), chunk, fill=0, font=fonts["body"])
        y_offset += line_h
    y_offset += 12

    grid_col = right_x
    rating_col = grid_col + 220
    include_col = rating_col + 150
    comment_col = include_col + 200
    header_y = y_offset
    draw.text((grid_col, header_y), "GridSquare", fill=0, font=fonts["table"])
    draw.text((rating_col, header_y), "Rating", fill=0, font=fonts["table"])
    draw.text((include_col, header_y), "Included?", fill=0, font=fonts["table"])
    draw.text((comment_col, header_y), "Reviewer comments", fill=0, font=fonts["table"])
    y_offset = header_y + (fonts["table"].size if hasattr(fonts["table"], "size") else 24) + 6
    row_height = (fonts["table"].size if hasattr(fonts["table"], "size") else 24) + 6
    for grid_label, rating_text, include_text, comment_text in rows:
        draw.text((grid_col, y_offset), grid_label, fill=0, font=fonts["table"])
        draw.text((rating_col, y_offset), rating_text, fill=0, font=fonts["table"])
        draw.text((include_col, y_offset), include_text, fill=0, font=fonts["table"])
        comment_lines = textwrap.wrap(comment_text, width=55) or ["—"]
        comment_y = y_offset
        for idx_line, c_line in enumerate(comment_lines):
            draw.text((comment_col, comment_y), c_line, fill=0, font=fonts["table"])
            comment_y += row_height
        y_offset = max(y_offset + row_height, comment_y)

    page.save(report_file, "PDF", resolution=150)


def write_selected_report(base_dir: Path, report_file: Path, atlas_name: str | None, responses: dict):
    """Generate a PDF with only the included GridSquares."""
    grids = _collect_grids(base_dir)
    if not grids:
        raise RuntimeError(f"no GridSquare directories found in {base_dir}")
    include_list = []
    for idx, (_gid, gdir) in enumerate(grids, start=1):
        resp = responses.get(gdir.name)
        if resp and bool(resp.get('include')):
            include_list.append((idx, gdir, resp))
    pages: list[Image.Image] = []
    failed: list[tuple[str, str]] = []
    if not include_list:
        pages.append(make_text_page("no GridSquares selected for this report"))
    else:
        for idx, gdir, resp in include_list:
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
            if atlas_name:
                atlas_path = _resolve_atlas_path(atlas_name, gdir, base_dir)
                if atlas_path:
                    atlas_img_local = _load_image(atlas_path, "RGB")
            heading = f"GridSquare {idx}: {grid_image_path.name}"
            foils, datas = gather_foil_and_data(gdir)
            try:
                pages.append(
                    make_grid_summary_page(
                        grid_image,
                        atlas_img_local,
                        foils,
                        datas,
                        resp,
                        heading,
                        grid_image_path.name,
                    )
                )
            except Exception as exc:
                failed.append((grid_name, f"render error: {exc}"))
                print(f"[selected_report] skipping {grid_name}: {exc}")
                continue
    if not pages:
        if include_list:
            lines = ["failed to render the selected GridSquares."]
            if failed:
                for name, reason in failed[:12]:
                    lines.append(f"- {name}: {reason}")
                if len(failed) > 12:
                    lines.append("...")
            pages.append(make_text_page("\n".join(lines)))
        else:
            pages.append(make_text_page("no GridSquares selected for this report"))
    elif failed:
        lines = ["some selected GridSquares were skipped:"]
        for name, reason in failed[:12]:
            lines.append(f"- {name}: {reason}")
        if len(failed) > 12:
            lines.append("... see console for the full list")
        pages.append(make_text_page("\n".join(lines)))
    first, rest = pages[0], pages[1:]
    first.save(report_file, "PDF", save_all=True, append_images=rest, resolution=150)


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
