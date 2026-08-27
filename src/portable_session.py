"""Portable EPU Mapper session bundle helpers."""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path

PORTABLE_SESSION_FORMAT = "EPUMapperPortableSession"
PORTABLE_SESSION_VERSION = 1
PORTABLE_SESSION_FILENAME = "EPUMapperSession.epumap"


def path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def portable_session_source(selected_path: Path) -> Path:
    """Expand an Images-Disc selection to its complete EPU session root."""
    selected_path = selected_path.expanduser().resolve()
    for candidate in [selected_path, *list(selected_path.parents)[:5]]:
        if (candidate / "EpuSession.dm").is_file():
            return candidate
    return selected_path


def portable_bundle_name(label: str, session_source: Path) -> str:
    base = label.strip() or session_source.name or "EPU_session"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._-") or "EPU_session"
    return f"{safe}_EPUMapperSession"


def _portable_relative_path(bundle_root: Path, path: Path) -> str:
    return path.relative_to(bundle_root).as_posix()


def export_portable_session(
    session_path: Path,
    atlas_path: Path | None,
    atlas_mode: str,
    destination_parent: Path,
    label: str,
    options: dict,
    log,
) -> Path:
    """Copy a complete session and write a relocatable `.epumap` manifest."""
    session_source = portable_session_source(session_path)
    if not session_source.is_dir():
        raise RuntimeError(f"Session folder not found: {session_source}")
    atlas_source = atlas_path.expanduser().resolve() if atlas_path else None
    if atlas_source is not None and not atlas_source.exists():
        raise RuntimeError(f"Atlas path not found: {atlas_source}")
    destination_parent = destination_parent.expanduser().resolve()
    if not destination_parent.is_dir():
        raise RuntimeError(f"Destination folder not found: {destination_parent}")
    if path_is_within(destination_parent, session_source):
        raise RuntimeError("Choose a destination outside the EPU session being exported.")
    if atlas_source is not None and atlas_source.is_dir() and path_is_within(destination_parent, atlas_source):
        raise RuntimeError("Choose a destination outside the Atlas folder being exported.")

    bundle_name = portable_bundle_name(label, session_source)
    bundle_root = destination_parent / bundle_name
    if bundle_root.exists():
        raise RuntimeError(f"Destination already exists: {bundle_root}")
    partial_root = destination_parent / f".{bundle_name}.partial-{os.getpid()}-{int(time.time())}"
    if partial_root.exists():
        raise RuntimeError(f"Temporary export folder already exists: {partial_root}")

    data_root = partial_root / "data"
    copied_session = data_root / "session"
    log(f"Portable export: copying complete session from {session_source}\n")
    data_root.mkdir(parents=True)
    shutil.copytree(session_source, copied_session, copy_function=shutil.copy2)

    atlas_relative = ""
    if atlas_source is not None:
        if path_is_within(atlas_source, session_source):
            atlas_relative = _portable_relative_path(
                partial_root,
                copied_session / atlas_source.relative_to(session_source),
            )
            log("Portable export: Atlas is already contained in the copied session.\n")
        elif atlas_source.is_dir():
            copied_atlas = data_root / "atlas"
            log(f"Portable export: copying Atlas folder from {atlas_source}\n")
            shutil.copytree(atlas_source, copied_atlas, copy_function=shutil.copy2)
            atlas_relative = _portable_relative_path(partial_root, copied_atlas)
        else:
            copied_atlas_dir = data_root / "atlas"
            copied_atlas_dir.mkdir()
            copied_atlas = copied_atlas_dir / atlas_source.name
            shutil.copy2(atlas_source, copied_atlas)
            atlas_relative = _portable_relative_path(partial_root, copied_atlas)

    manifest = {
        "format": PORTABLE_SESSION_FORMAT,
        "version": PORTABLE_SESSION_VERSION,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "session_path": _portable_relative_path(partial_root, copied_session),
        "atlas_path": atlas_relative,
        "atlas_mode": atlas_mode,
        "session_label": label,
        "options": options,
    }
    manifest_path = partial_root / PORTABLE_SESSION_FILENAME
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (partial_root / "README.txt").write_text(
        "Portable EPU Mapper session\n\n"
        "Open EPUMapperSession.epumap from the EPU Mapper launcher. All paths in the manifest are relative to this folder.\n",
        encoding="utf-8",
    )
    partial_root.rename(bundle_root)
    final_manifest = bundle_root / PORTABLE_SESSION_FILENAME
    log(f"Portable export complete: {final_manifest}\n")
    return final_manifest


def load_portable_session(manifest_path: Path) -> dict:
    manifest_path = manifest_path.expanduser().resolve()
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Could not read portable session file: {exc}") from exc
    if manifest.get("format") != PORTABLE_SESSION_FORMAT or manifest.get("version") != PORTABLE_SESSION_VERSION:
        raise RuntimeError("This is not a supported EPU Mapper portable session file.")
    bundle_root = manifest_path.parent.resolve()

    def resolve_relative(value: str, *, required: bool) -> Path | None:
        if not value:
            if required:
                raise RuntimeError("Portable session manifest is missing its session path.")
            return None
        relative = Path(value)
        if relative.is_absolute():
            raise RuntimeError("Portable session paths must be relative to the bundle folder.")
        resolved = (bundle_root / relative).resolve()
        if not path_is_within(resolved, bundle_root):
            raise RuntimeError("Portable session contains an unsafe path outside its bundle folder.")
        if not resolved.exists():
            raise RuntimeError(f"Portable session data is missing: {relative}")
        return resolved

    session_resolved = resolve_relative(str(manifest.get("session_path", "")), required=True)
    atlas_resolved = resolve_relative(str(manifest.get("atlas_path", "")), required=False)
    return {
        "session_path": session_resolved,
        "atlas_path": atlas_resolved,
        "atlas_mode": manifest.get("atlas_mode", "epu"),
        "session_label": str(manifest.get("session_label", "") or ""),
        "options": manifest.get("options", {}) if isinstance(manifest.get("options"), dict) else {},
    }
