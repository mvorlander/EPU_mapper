"""Non-destructive contrast and Gaussian low-pass processing of display previews."""
from __future__ import annotations

from pathlib import Path
import numpy as np
from PIL import Image, ImageOps


def adjust_array(array, low=1., high=99., gamma=1., sigma=0., mode="percentile", max_size=2048):
    values = np.asarray([low, high, gamma, sigma], dtype=float)
    if not np.all(np.isfinite(values)) or not (0 <= low < high <= 100 and .2 <= gamma <= 3 and 0 <= sigma <= 4):
        raise ValueError("Use 0 ≤ black < white ≤ 100, gamma 0.2–3, and low-pass sigma 0–4 pixels.")
    if mode not in ("percentile", "equalize"):
        raise ValueError("Unknown contrast routine")
    array = np.asarray(array, dtype=np.float32)
    if array.ndim not in (2, 3) or (array.ndim == 3 and array.shape[2] != 3):
        raise ValueError("Expected a grayscale or RGB image")
    finite = np.isfinite(array)
    fill = float(np.median(array[finite])) if finite.any() else 0.
    array = np.where(finite, array, fill)
    height, width = array.shape[:2]
    ratio = min(1., max_size / max(width, height))
    if ratio < 1:
        size = (max(1, round(width * ratio)), max(1, round(height * ratio)))
        def resize(plane):
            return np.asarray(Image.fromarray(plane).resize(size, Image.Resampling.BILINEAR))
        array = resize(array) if array.ndim == 2 else np.stack([resize(array[:, :, c]) for c in range(3)], axis=-1)
    if sigma:
        radius = max(1, int(np.ceil(sigma * 3)))
        coords = np.arange(-radius, radius + 1)
        kernel = np.exp(-coords**2 / (2 * sigma**2))
        kernel /= kernel.sum()
        for axis in (0, 1):
            padding = [(0, 0)] * array.ndim
            padding[axis] = (radius, radius)
            padded = np.pad(array, padding, mode="edge")
            array = np.apply_along_axis(lambda row: np.convolve(row, kernel, mode="valid"), axis, padded).astype(np.float32)
    luminance = array.mean(axis=2) if array.ndim == 3 else array
    lo, hi = np.percentile(luminance, [low, high])
    if hi <= lo:
        return Image.fromarray(np.full(array.shape, 127, dtype=np.uint8))
    normalized = np.clip((array - lo) / (hi - lo), 0, 1)
    image = Image.fromarray(np.round(normalized**(1 / gamma) * 255).astype(np.uint8))
    return ImageOps.equalize(image) if mode == "equalize" else image


def adjusted_preview(path: Path, **settings):
    if path.suffix.lower() in (".mrc", ".mrcs"):
        import mrcfile
        with mrcfile.open(path, permissive=True) as mrc:
            array = np.squeeze(mrc.data)
            if array.ndim > 2:
                array = array[0]
            return adjust_array(array, **settings)
    with Image.open(path) as image:
        return adjust_array(np.asarray(image.convert("RGB")), **settings)
