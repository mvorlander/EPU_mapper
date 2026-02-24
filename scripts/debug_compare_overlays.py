#!/usr/bin/env python3
"""Compare our FoilHole mapping logic against epubrowser-style stage math."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Dict, Iterable, Tuple

from build_collage import (
    gather_foil_and_data,
    find_grid_image,
    parse_grid_info,
    parse_foil_position,
    _load_image,
)

from plot_foilhole_positions import (
    _load_hole_positions,
    _load_dm_square_metadata,
    _epu_stage_payload,
    _project_marker_epu,
    _fit_to_frame,
)


def _select_paths(foils: Dict[str, list[Path]]) -> Iterable[tuple[str, Path, Path]]:
    for foil_id in sorted(foils.keys()):
        paths = foils[foil_id]
        if not paths:
            continue
        latest = paths[-1]
        position = latest if latest.with_suffix(".xml").is_file() else None
        if position is None:
            for candidate in reversed(paths):
                if candidate.with_suffix(".xml").is_file():
                    position = candidate
                    break
        if position is None:
            continue
        yield foil_id, position, latest


def _epu_reference(square_xml: Path, foil_xml: Path, base_w: float, base_h: float) -> tuple[float, float] | None:
    square = _epu_stage_payload(square_xml)
    foil = _epu_stage_payload(foil_xml)
    if not square or not foil:
        return None
    if square.get("pixel_size") is None or square.get("stage_x") is None or square.get("stage_y") is None:
        return None
    sq_px = square["pixel_size"]
    dx = square["stage_x"] - foil.get("stage_x", square["stage_x"])
    dy = square["stage_y"] - foil.get("stage_y", square["stage_y"])
    try:
        px = (base_w / 2.0) + (dx / sq_px)
        py = (base_h / 2.0) - (dy / sq_px)
        return px, py
    except Exception:
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare foil overlay mapping implementations")
    parser.add_argument("grid_dir", type=Path, help="Path to GridSquare directory")
    args = parser.parse_args()
    grid_dir = args.grid_dir.resolve()
    if not grid_dir.is_dir():
        raise SystemExit(f"GridSquare not found: {grid_dir}")

    grid_image_path = find_grid_image(grid_dir)
    grid_xml = grid_dir / grid_image_path.with_suffix(".xml").name
    if not grid_xml.is_file():
        raise SystemExit(f"GridSquare XML missing: {grid_xml}")
    grid_meta = parse_grid_info(grid_xml)
    base_w = float(grid_meta.get("readout_width") or 4096.0)
    base_h = float(grid_meta.get("readout_height") or 4096.0)
    grid_image = _load_image(grid_image_path)
    if grid_image is None:
        raise SystemExit(f"Unable to load grid image: {grid_image_path}")
    scale_x = grid_image.width / base_w
    scale_y = grid_image.height / base_h
    dm_square_meta = _load_dm_square_metadata(grid_dir)
    square_stage_x = dm_square_meta.get("stage_x", grid_meta.get("stage_x"))
    square_stage_y = dm_square_meta.get("stage_y", grid_meta.get("stage_y"))
    square_pixel_size = dm_square_meta.get("pixel_size", grid_meta.get("pixel_size"))

    hole_positions = _load_hole_positions(grid_dir)
    foils, _ = gather_foil_and_data(grid_dir)

    rows = []
    for foil_id, position_path, latest_path in _select_paths(foils):
        latest_name = latest_path.name
        xml_path = position_path.with_suffix(".xml")
        current_px = current_py = None
        current_src = ""
        in_bounds = False
        # DM metadata path
        if (
            foil_id in hole_positions
            and square_stage_x is not None
            and square_stage_y is not None
            and square_pixel_size
        ):
            hx, hy = hole_positions[foil_id]
            dx = hx - square_stage_x
            dy = hy - square_stage_y
            px = ((base_w / 2.0) + dx / square_pixel_size) * scale_x
            py = ((base_h / 2.0) - dy / square_pixel_size) * scale_y
            px, py, in_bounds = _fit_to_frame(px, py, grid_image.width, grid_image.height)
            current_px, current_py = px, py
            current_src = "dm"
        else:
            square_payload = _epu_stage_payload(grid_xml)
            foil_payload = _epu_stage_payload(xml_path)
            coords = None
            if square_payload and foil_payload and foil_payload.get("stage_x") is not None:
                coords = _project_marker_epu(square_payload, foil_payload, base_w, base_h, scale_x, scale_y)
            if coords:
                px, py = coords
                px, py, in_bounds = _fit_to_frame(px, py, grid_image.width, grid_image.height)
                current_px, current_py = px, py
                current_src = "epu"
            else:
                fp = parse_foil_position(xml_path)
                if (
                    "stage_x" in fp
                    and "stage_y" in fp
                    and square_stage_x is not None
                    and square_stage_y is not None
                    and square_pixel_size
                ):
                    dx = fp["stage_x"] - square_stage_x
                    dy = fp["stage_y"] - square_stage_y
                    px = (base_w / 2.0) + (dx / square_pixel_size)
                    py = (base_h / 2.0) - (dy / square_pixel_size)
                    px *= scale_x
                    py *= scale_y
                    px, py, in_bounds = _fit_to_frame(px, py, grid_image.width, grid_image.height)
                    current_px, current_py = px, py
                    current_src = "fallback"
        epu_px = epu_py = None
        if xml_path.is_file():
            ref = _epu_reference(grid_xml, xml_path, base_w, base_h)
            if ref:
                epu_px, epu_py = ref[0] * scale_x, ref[1] * scale_y
        rows.append((foil_id, latest_name, current_src, current_px, current_py, in_bounds, epu_px, epu_py))

    print(f"GridSquare: {grid_dir.name} (image {grid_image.width}x{grid_image.height})")
    print(f"{'FoilID':>10} {'Source':>8} {'InBounds':>8} {'Current(x,y)':>25} {'EPU(x,y)':>20}  Latest Image")
    for foil_id, latest_name, src, cpx, cpy, in_bounds, epx, epy in rows:
        cur_txt = "n/a" if cpx is None else f"({cpx:7.1f},{cpy:7.1f})"
        ref_txt = "n/a" if epx is None else f"({epx:7.1f},{epy:7.1f})"
        print(f"{foil_id:>10} {src:>8} {str(in_bounds):>8} {cur_txt:>25} {ref_txt:>20}  {latest_name}")


if __name__ == "__main__":
    main()
