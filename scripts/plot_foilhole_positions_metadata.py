#!/usr/bin/env python3
"""Standalone visualizer that uses only XML metadata to map FoilHole positions."""
from __future__ import annotations

import argparse
import math
from pathlib import Path
from typing import Iterable
import xml.etree.ElementTree as ET

from PIL import Image, ImageDraw, ImageFont


def find_grid_image(grid_dir: Path) -> Path:
    for entry in sorted(grid_dir.iterdir()):
        if entry.is_file() and entry.suffix.lower() in {".jpg", ".jpeg"} and entry.name.startswith("GridSquare_"):
            return entry
    raise FileNotFoundError(f"No GridSquare JPEG found in {grid_dir}")


def load_xml(path: Path) -> ET.Element:
    try:
        tree = ET.parse(path)
        return tree.getroot()
    except Exception as exc:
        raise RuntimeError(f"Failed to parse XML {path}") from exc


def extract_pixel_size(root: ET.Element) -> float | None:
    for elem in root.iter():
        if elem.tag.lower().endswith("pixelsize"):
            for sub in elem.iter():
                if sub.tag.lower().endswith("numericvalue") and sub.text:
                    try:
                        return float(sub.text)
                    except ValueError:
                        continue
    return None


def extract_stage_xy(root: ET.Element) -> tuple[float | None, float | None]:
    for elem in root.iter():
        if elem.tag.lower().endswith("position"):
            x_val = y_val = None
            for child in elem:
                tag = child.tag.lower()
                if tag.endswith("x") and child.text:
                    try:
                        x_val = float(child.text)
                    except ValueError:
                        pass
                if tag.endswith("y") and child.text:
                    try:
                        y_val = float(child.text)
                    except ValueError:
                        pass
            if x_val is not None and y_val is not None:
                return x_val, y_val
    return None, None


def extract_center(root: ET.Element) -> tuple[float | None, float | None]:
    for elem in root.iter():
        if elem.tag.lower().endswith("center"):
            cx = cy = None
            for child in elem:
                tag = child.tag.lower()
                if tag.endswith("x") and child.text:
                    try:
                        cx = float(child.text)
                    except ValueError:
                        pass
                if tag.endswith("y") and child.text:
                    try:
                        cy = float(child.text)
                    except ValueError:
                        pass
            if cx is not None and cy is not None:
                return cx, cy
    return None, None


def extract_rotation(root: ET.Element) -> float | None:
    for elem in root.iter():
        if elem.tag.lower().endswith("rotation") and elem.text:
            try:
                return float(elem.text)
            except ValueError:
                return None
    return None


def compute_markers(grid_dir: Path) -> tuple[Image.Image, list[tuple[float, float, bool, int]]]:
    grid_image_path = find_grid_image(grid_dir)
    grid_img = Image.open(grid_image_path).convert("RGB")

    grid_xml_path = grid_dir / grid_image_path.with_suffix(".xml").name
    if not grid_xml_path.is_file():
        raise FileNotFoundError(f"GridSquare XML missing: {grid_xml_path}")
    grid_root = load_xml(grid_xml_path)
    grid_px_size = extract_pixel_size(grid_root)
    grid_stage_x, grid_stage_y = extract_stage_xy(grid_root)
    if grid_px_size is None or grid_stage_x is None or grid_stage_y is None:
        raise RuntimeError("Grid metadata missing pixel size or stage coordinates")

    markers: list[tuple[float, float, bool, int]] = []
    foils_dir = grid_dir / "FoilHoles"
    if not foils_dir.is_dir():
        raise RuntimeError("No FoilHoles directory found")

    idx = 1
    for foil_path in sorted(foils_dir.glob("*.jpg")):
        foil_xml = foil_path.with_suffix(".xml")
        if not foil_xml.is_file():
            continue
        foil_root = load_xml(foil_xml)
        foil_stage_x, foil_stage_y = extract_stage_xy(foil_root)
        if foil_stage_x is None or foil_stage_y is None:
            continue
        foil_pixel = extract_pixel_size(foil_root)
        center = extract_center(foil_root)
        rotation = extract_rotation(foil_root)

        center_stage_x = foil_stage_x
        center_stage_y = foil_stage_y

        if center[0] is not None and center[1] is not None and foil_pixel:
            with Image.open(foil_path) as foil_img:
                fw, fh = foil_img.size
            dx_px = center[0] - fw / 2.0
            dy_px = center[1] - fh / 2.0
            dy_img_up = -dy_px
            rx, ry = dx_px, dy_img_up
            if rotation is not None:
                c = math.cos(rotation)
                s = math.sin(rotation)
                rx, ry = c * dx_px - s * dy_img_up, s * dx_px + c * dy_img_up
            center_stage_x += rx * foil_pixel
            center_stage_y += ry * foil_pixel

        try:
            px = grid_img.width / 2.0 + (center_stage_x - grid_stage_x) / grid_px_size
            py = grid_img.height / 2.0 - (center_stage_y - grid_stage_y) / grid_px_size
        except Exception:
            continue

        in_bounds = 0 <= px < grid_img.width and 0 <= py < grid_img.height
        markers.append((px, py, in_bounds, idx))
        idx += 1

    if not markers:
        raise RuntimeError("No foil markers computed from metadata")
    return grid_img, markers


def draw_markers(img: Image.Image, markers: Iterable[tuple[float, float, bool, int]]) -> Image.Image:
    out = img.copy()
    draw = ImageDraw.Draw(out)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 28)
    except Exception:
        font = ImageFont.load_default()
    rad = max(12, int(min(out.size) * 0.015))
    for px, py, in_bounds, label in markers:
        color = (39, 174, 96) if in_bounds else (192, 57, 43)
        draw.ellipse((px - rad, py - rad, px + rad, py + rad), outline=color, width=4)
        draw.line((px - rad, py, px + rad, py), fill=color, width=2)
        draw.line((px, py - rad, px, py + rad), fill=color, width=2)
        draw.text((px + rad + 4, py - rad), str(label), fill=color, font=font)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Plot FoilHole positions using XML metadata only")
    parser.add_argument("grid_dir", type=Path, help="Path to a GridSquare directory")
    parser.add_argument("--output", type=Path, default=Path("metadata_overlay.png"), help="Output PNG")
    args = parser.parse_args()

    grid_dir = args.grid_dir.resolve()
    if not grid_dir.is_dir():
        raise SystemExit(f"Grid directory not found: {grid_dir}")

    grid_img, markers = compute_markers(grid_dir)
    out = draw_markers(grid_img, markers)
    out.save(args.output)
    print(f"Saved overlay to {args.output}")


if __name__ == "__main__":
    main()
