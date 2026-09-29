"""Compare CryoSPARC relative ice thickness with EPU FoilHole intensity.

EPU's conventional hole filter stores transmitted ``PixelIntensityMean`` in
the GridSquare_*.dm target metadata: higher recorded intensity means thinner
ice. This is not necessarily calibrated physical ice thickness. CryoSPARC
stores its own relative estimate as ``ctf_stats/ice_thickness_rel`` on
exposure datasets, where higher values mean thicker ice.
"""

from __future__ import annotations

import csv
import json
import math
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

import numpy as np


FOIL_RE = re.compile(r"(?:^|[/\\])FoilHole_(\d+)(?:_|\.)")
GRID_RE = re.compile(r"GridSquare_(\d+)")


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _child(element, name):
    if element is None:
        return None
    return next((item for item in element if _local(item.tag) == name), None)


def _text(element, name, default=None):
    item = _child(element, name)
    value = (item.text or "").strip() if item is not None else ""
    return value or default


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _boolean(value):
    return str(value or "").strip().lower() == "true"


def _metadata_directory(epu_session):
    root = Path(epu_session).expanduser().resolve()
    candidates = (root / "Metadata", root)
    for candidate in candidates:
        if candidate.is_dir() and next(candidate.glob("GridSquare_*.dm"), None):
            return candidate
    raise FileNotFoundError(f"No Metadata/GridSquare_*.dm files found below {root}")


def _session_settings(epu_session):
    root = Path(epu_session).expanduser().resolve()
    paths = [root / "EpuSession.dm", root.parent / "EpuSession.dm"]
    path = next((item for item in paths if item.is_file()), None)
    result = {"path": str(path) if path else None, "minimum": None, "maximum": None,
              "ice_thickness_enabled": None}
    if not path:
        return result
    tree = ET.parse(path).getroot()
    for item in tree.iter():
        name = _local(item.tag)
        if name == "IceThicknessEnabled" and result["ice_thickness_enabled"] is None:
            result["ice_thickness_enabled"] = _boolean(item.text)
        if name == "FilterHolesSettings":
            result["minimum"] = _number(_text(item, "MinimumIntensity"))
            result["maximum"] = _number(_text(item, "MaximumIntensity"))
            break
    return result


def read_epu_targets(epu_session):
    """Return FoilHole target metadata keyed by globally unique FoilHole ID."""
    metadata = _metadata_directory(epu_session)
    targets = {}
    ambiguous = set()
    for path in sorted(metadata.glob("GridSquare_*.dm")):
        match = GRID_RE.search(path.name)
        grid_id = match.group(1) if match else path.stem
        records = []
        for pair in ET.parse(path).getroot().iter():
            if _local(pair.tag) != "KeyValuePairOfintTargetLocationXmlBpEWF4JT":
                continue
            target = _child(pair, "value")
            filt = _child(target, "FilterProperties")
            intensity = _number(_text(filt, "PixelIntensityMean"))
            if target is None or intensity is None:
                continue
            hole = _text(target, "Id") or _text(pair, "key")
            if not hole:
                continue
            primary = _text(target, "PrimaryId")
            primary = None if primary in (None, "0") else primary
            record = {
                "grid_id": grid_id,
                "hole_id": str(hole),
                "primary_id": primary,
                "intensity_mean": intensity,
                "intensity_stdev": _number(_text(filt, "PixelIntensityStDev")),
                "selected": _boolean(_text(target, "Selected")),
                "near_grid_bar": _boolean(_text(target, "IsNearGridBar")),
            }
            records.append(record)
        flags = {record["hole_id"]: record["selected"] for record in records}
        for record in records:
            record["epu_selected"] = flags.get(record["primary_id"], record["selected"]) \
                if record["primary_id"] else record["selected"]
            hole = record["hole_id"]
            if hole in targets and targets[hole]["grid_id"] != grid_id:
                ambiguous.add(hole)
            else:
                targets[hole] = record
    for hole in ambiguous:
        targets.pop(hole, None)
    return targets, ambiguous, _session_settings(epu_session)


def _load_cs(path):
    array = np.load(Path(path).expanduser(), mmap_mode="r", allow_pickle=False)
    if not array.dtype.names or "uid" not in array.dtype.names:
        raise ValueError(f"{path} is not a CryoSPARC table with a uid field")
    return array


def _source_for_field(main, passthrough, field):
    if field in main.dtype.names:
        return main, None
    if passthrough is not None and field in passthrough.dtype.names:
        main_uids = np.asarray(main["uid"])
        pass_uids = np.asarray(passthrough["uid"])
        if len(main_uids) == len(pass_uids) and np.array_equal(main_uids, pass_uids):
            return passthrough, None
        order = np.argsort(pass_uids)
        positions = np.searchsorted(pass_uids[order], main_uids)
        valid = positions < len(order)
        aligned = np.zeros(len(main), dtype=bool)
        indices = np.zeros(len(main), dtype=np.int64)
        indices[valid] = order[positions[valid]]
        aligned[valid] = pass_uids[indices[valid]] == main_uids[valid]
        if not aligned.all():
            raise ValueError(f"Passthrough table lacks {int((~aligned).sum())} main-table UIDs")
        return passthrough, indices
    raise ValueError(f"Required field {field!r} was not found in the main or passthrough table")


def _column(source, indices, field):
    return source[field] if indices is None else source[field][indices]


def _decode(value):
    if isinstance(value, (bytes, np.bytes_)):
        return value.decode("utf-8", errors="replace").rstrip("\x00")
    return str(value)


def join_exposures(epu_session, exposures_cs, passthrough_cs=None,
                   ice_field="ctf_stats/ice_thickness_rel", path_field=None):
    """Join CryoSPARC exposure rows to EPU targets through FoilHole IDs."""
    targets, ambiguous, settings = read_epu_targets(epu_session)
    main = _load_cs(exposures_cs)
    passthrough = _load_cs(passthrough_cs) if passthrough_cs else None
    if path_field is None:
        candidates = ("movie_blob/path", "micrograph_blob/path", "location/micrograph_path")
        path_field = next((field for field in candidates
                           if field in main.dtype.names or
                           (passthrough is not None and field in passthrough.dtype.names)), None)
        if path_field is None:
            raise ValueError("No movie, micrograph, or location path was found in the CryoSPARC table")
    path_source, path_indices = _source_for_field(main, passthrough, path_field)
    ice_source, ice_indices = _source_for_field(main, passthrough, ice_field)
    paths = _column(path_source, path_indices, path_field)
    ice = _column(ice_source, ice_indices, ice_field)
    low, high = settings["minimum"], settings["maximum"]
    rows = []
    unmatched = []
    for uid, raw_path, raw_ice in zip(main["uid"], paths, ice):
        path = _decode(raw_path)
        match = FOIL_RE.search(path)
        hole = match.group(1) if match else None
        target = targets.get(hole) if hole else None
        if target is None:
            unmatched.append({"uid": int(uid), "path": path, "hole_id": hole or ""})
            continue
        value = float(raw_ice)
        in_range = ((low is None or target["intensity_mean"] >= low) and
                    (high is None or target["intensity_mean"] <= high))
        rows.append({
            "uid": int(uid), "micrograph_path": path,
            "grid_id": target["grid_id"], "hole_id": target["hole_id"],
            "primary_id": target["primary_id"] or "",
            "epu_intensity_mean": target["intensity_mean"],
            "epu_intensity_stdev": target["intensity_stdev"],
            "epu_selected": bool(target["epu_selected"]),
            "epu_in_recorded_range": bool(in_range),
            "epu_near_grid_bar": bool(target["near_grid_bar"]),
            "cryosparc_ice_thickness_rel": value,
        })
    return rows, unmatched, {
        "source_rows": len(main), "targets": len(targets),
        "ambiguous_hole_ids": sorted(ambiguous), "settings": settings,
        "path_field": path_field, "ice_field": ice_field,
    }


def _pearson(x, y):
    if len(x) < 2 or np.ptp(x) == 0 or np.ptp(y) == 0:
        return None
    return float(np.corrcoef(x, y)[0, 1])


def _ranks(values):
    values = np.asarray(values)
    order = np.argsort(values, kind="mergesort")
    ranks = np.empty(len(values), dtype=float)
    start = 0
    while start < len(values):
        end = start + 1
        while end < len(values) and values[order[end]] == values[order[start]]:
            end += 1
        ranks[order[start:end]] = (start + end - 1) / 2.0
        start = end
    return ranks


def _correlations(x, y):
    return {"pearson": _pearson(x, y), "spearman": _pearson(_ranks(x), _ranks(y))}


def summarise(rows):
    finite = [row for row in rows if math.isfinite(row["cryosparc_ice_thickness_rel"])]
    intensities = np.asarray([row["epu_intensity_mean"] for row in finite], dtype=float)
    positive = np.isfinite(intensities) & (intensities > 0)
    reference = float(np.median(intensities[positive])) if positive.any() else None
    for row in finite:
        intensity = row["epu_intensity_mean"]
        row["epu_attenuation_proxy"] = (float(np.log(reference / intensity))
                                         if reference and intensity > 0 else float("nan"))
    grouped = defaultdict(list)
    for row in finite:
        grouped[(row["grid_id"], row["hole_id"])].append(row)
    holes = []
    for (grid, hole), group in sorted(grouped.items()):
        values = np.asarray([row["cryosparc_ice_thickness_rel"] for row in group])
        first = group[0]
        holes.append({
            "grid_id": grid, "hole_id": hole, "primary_id": first["primary_id"],
            "epu_intensity_mean": first["epu_intensity_mean"],
            "epu_attenuation_proxy": first["epu_attenuation_proxy"],
            "epu_intensity_stdev": first["epu_intensity_stdev"],
            "epu_selected": first["epu_selected"],
            "epu_in_recorded_range": first["epu_in_recorded_range"],
            "micrographs": len(group),
            "cryosparc_ice_median": float(np.median(values)),
            "cryosparc_ice_mean": float(np.mean(values)),
            "cryosparc_ice_stdev": float(np.std(values)),
        })
    def stats(records, x_key, y_key):
        x = np.asarray([row[x_key] for row in records], dtype=float)
        y = np.asarray([row[y_key] for row in records], dtype=float)
        return _correlations(x, y) if len(records) else {"pearson": None, "spearman": None}
    return finite, holes, {
        "epu_attenuation_reference": reference,
        "epu_attenuation_definition": "ln(session median PixelIntensityMean / PixelIntensityMean); higher = thicker",
        "micrograph_correlations": stats(finite, "epu_attenuation_proxy", "cryosparc_ice_thickness_rel"),
        "hole_median_correlations": stats(holes, "epu_attenuation_proxy", "cryosparc_ice_median"),
        "micrograph_raw_intensity_correlations": stats(finite, "epu_intensity_mean", "cryosparc_ice_thickness_rel"),
        "hole_median_raw_intensity_correlations": stats(holes, "epu_intensity_mean", "cryosparc_ice_median"),
    }


def _write_csv(path, rows):
    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)


def _plot(path_png, path_svg, micrographs, holes, settings, correlations, title):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 2, figsize=(12.5, 5.2))
    fig.subplots_adjust(left=.07, right=.985, bottom=.14, top=.79, wspace=.18)
    panels = ((axes[0], micrographs, "cryosparc_ice_thickness_rel", "Micrographs", 10, .22),
              (axes[1], holes, "cryosparc_ice_median", "FoilHole medians", 24, .65))
    low, high = settings.get("minimum"), settings.get("maximum")
    for axis, rows, y_key, label, size, alpha in panels:
        x = np.asarray([row["epu_attenuation_proxy"] for row in rows], dtype=float)
        y = np.asarray([row[y_key] for row in rows], dtype=float)
        colors = np.asarray([bool(row["epu_selected"]) for row in rows])
        axis.scatter(x[~colors], y[~colors], s=size, alpha=alpha, color="#94a3b8", linewidths=0,
                     label="not recorded as selected")
        axis.scatter(x[colors], y[colors], s=size, alpha=alpha, color="#0f766e", linewidths=0,
                     label="EPU selected")
        if low is not None and high is not None:
            reference = correlations["epu_attenuation_reference"]
            bounds = sorted((np.log(reference / low), np.log(reference / high)))
            axis.axvspan(*bounds, color="#f59e0b", alpha=.08, zorder=-2)
            axis.axvline(bounds[0], color="#d97706", lw=.8, ls="--")
            axis.axvline(bounds[1], color="#d97706", lw=.8, ls="--")
        if len(x) > 1 and np.ptp(x) > 0:
            slope, intercept = np.polyfit(x, y, 1)
            xx = np.linspace(x.min(), x.max(), 100)
            axis.plot(xx, slope * xx + intercept, color="#1e293b", lw=1.5)
        corr = correlations["micrograph_correlations" if y_key.endswith("rel") else "hole_median_correlations"]
        text = f"n = {len(rows):,}\nPearson r = {corr['pearson']:.3f}\nSpearman ρ = {corr['spearman']:.3f}"
        axis.text(.03, .97, text, transform=axis.transAxes, va="top", fontsize=9,
                  bbox={"boxstyle": "round,pad=.35", "facecolor": "white", "alpha": .9, "edgecolor": "#cbd5e1"})
        axis.set_title(label)
        axis.set_xlabel("EPU attenuation proxy, ln(median intensity / intensity)\n(higher = thicker)")
        axis.set_ylabel("CryoSPARC relative ice thickness (higher = thicker)")
        axis.grid(color="#e2e8f0", lw=.7)
    axes[0].legend(loc="lower right", frameon=True, fontsize=8)
    fig.suptitle(title, fontsize=14, fontweight="bold", y=.98)
    fig.text(.5, .90, "EPU recorded intensity was inverted to a thickness-aligned attenuation proxy; it is not calibrated physical thickness.",
             ha="center", fontsize=9, color="#475569")
    fig.savefig(path_png, dpi=300, facecolor="white")
    fig.savefig(path_svg, facecolor="white")
    plt.close(fig)


def run_comparison(epu_session, exposures_cs, output_dir, passthrough_cs=None,
                   ice_field="ctf_stats/ice_thickness_rel", path_field=None, title=None):
    output = Path(output_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    rows, unmatched, metadata = join_exposures(epu_session, exposures_cs, passthrough_cs,
                                               ice_field=ice_field, path_field=path_field)
    micrographs, holes, correlations = summarise(rows)
    _write_csv(output / "epu_cryosparc_ice_micrographs.csv", rows)
    _write_csv(output / "epu_cryosparc_ice_foilhole_medians.csv", holes)
    _write_csv(output / "epu_cryosparc_ice_unmatched.csv", unmatched)
    summary = {
        **metadata, **correlations, "matched_rows": len(rows),
        "finite_rows": len(micrographs), "unmatched_rows": len(unmatched),
        "matched_foils": len(holes), "exposures_cs": str(Path(exposures_cs).resolve()),
        "passthrough_cs": str(Path(passthrough_cs).resolve()) if passthrough_cs else None,
        "epu_session": str(Path(epu_session).resolve()),
    }
    (output / "epu_cryosparc_ice_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    if not micrographs:
        raise ValueError("No finite, matched CryoSPARC/EPU ice measurements were found")
    _plot(output / "epu_cryosparc_ice_comparison.png",
          output / "epu_cryosparc_ice_comparison.svg", micrographs, holes,
          metadata["settings"], correlations, title or "CryoSPARC versus EPU ice proxy")
    return summary
