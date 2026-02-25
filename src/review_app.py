import argparse
import io
import errno
import json
import json
import os
import sys
import urllib.parse
import time
import tempfile
import webbrowser
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse, Response, JSONResponse
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
    try:
        grid_img, markers = compute_markers(gdir)
    except Exception as exc:
        print(f"[overlay] skipping {gdir.name}: {exc}")
        return None
    if not markers:
        print(f"[overlay] no FoilHole markers for {gdir.name}")
        return None
    out_path = gdir / "foil_overlay.png"
    try:
        plot_overlay(grid_img, markers, title=gdir.name, output=out_path, dpi=180)
    except Exception as exc:
        print(f"[overlay] failed to render overlay for {gdir.name}: {exc}")
        return None
    return out_path if out_path.is_file() else None


def _ensure_overlay_image(gdir: Path, base_dir: Path) -> Path | None:
    """Return a fresh overlay PNG path if generation succeeds, else fall back to cached copy."""
    generated = _generate_overlay_image(gdir)
    if generated:
        return generated
    return _find_overlay_image(gdir, base_dir)


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
) -> FastAPI:
    global _OVERLAY_TRANSFORM
    if overlay_transform == "auto":
        _OVERLAY_TRANSFORM = None
    elif overlay_transform in (None, ""):
        _OVERLAY_TRANSFORM = "identity"
    else:
        _OVERLAY_TRANSFORM = overlay_transform
    base_dir = base_dir.resolve()
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
    for _gid, gdir in grids:
        grid_img = find_grid_image(gdir)
        mrc_path = find_grid_mrc(gdir)
        atlas_path = _resolve_atlas_path(atlas_name, gdir, base_dir) if atlas_name else None
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
        overlay_path = _ensure_overlay_image(gdir, base_dir) if overlay_enabled else None
        items.append(
            {
                "id": _gid,
                "dir": gdir,
                "grid_img": grid_img,
                "name": grid_img.name,
                "mrc": mrc_path,
                "atlas": atlas_path,
                "overlay": overlay_path,
                "foils": foil_list,
                "data": data_list,
            }
        )

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
        grid_mrc_note = "" if grid_has_mrc else "<div class=\"note\">No grid MRC available.</div>"
        ts = int(time.time() * 1000)
        atlas_html = (
            f"<img id=\"atlasimg\" src=\"/atlas?idx={idx}&t={ts}\" class=\"atlas-img\"/>"
            if item["atlas"]
            else "<div class=\"note\">No atlas found.</div>"
        )
        grid_frame_html = (
            f"<div class=\"image-frame\"><div class=\"image-caption\">GridSquare view</div>"
            f"<img id=\"gridimg\" src=\"/grid?idx={idx}&t={ts}\"/></div>"
        )
        overlay_html = ""
        if item.get("overlay"):
            overlay_html = (
                f"<div class=\"image-frame\"><div class=\"image-caption\">Foil overlay</div>"
                f"<img id=\"overlayimg\" src=\"/overlay?idx={idx}&t={ts}\"/></div>"
            )
        grid_section_html = f"<div class=\"grid-panel\">{grid_frame_html}{overlay_html}</div>"
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
.actions{{margin:8px 0;display:flex;gap:8px;flex-wrap:wrap;}}
.btn{{border:1px solid #c9ced6;background:#fff;border-radius:8px;padding:8px 10px;font-size:14px;cursor:pointer;}}
.btn:hover{{background:#f0f2f5;}}
.btn:disabled{{opacity:0.5;cursor:default;}}
.rate-buttons{{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0;}}
.rate{{border:1px solid #c9ced6;background:#fff;border-radius:8px;padding:8px 10px;font-size:14px;cursor:pointer;min-width:38px;}}
.rate.active{{background:#1b6ef3;color:#fff;border-color:#1b6ef3;}}
.note{{color:#555;font-size:13px;margin:6px 0;}}
.note.warn{{color:#b00020;}}
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
</style>
</head>
<body>
<div class=\"page\">
<div class=\"header\"><div><div class=\"title\">GridSquare {item['id']}</div><div class=\"subtitle\">{item['name']}</div></div><div class=\"progress\">{idx + 1} / {len(items)}</div></div>
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
<div id=\"selected-image\" class=\"note\">GridSquare</div>
<div class=\"actions\">
<button type=\"button\" id=\"show-jpeg\" class=\"btn\">Show JPEG</button>
<button type=\"button\" id=\"show-mrc\" class=\"btn\">Show MRC to adjust contrast of last-clicked image</button>
</div>
{grid_mrc_note}
<div class=\"note\">When viewing MRCs you can fine-tune the current image using the sliders below.</div>
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
const GRID_HAS_MRC = {grid_mrc_json};
let rating = 3;
let selectedKind = 'grid';
let selectedName = '';
let selectedHasMrc = GRID_HAS_MRC;
function setRating(v){{
  rating = v;
  document.getElementById('selected').textContent = String(v);
  document.querySelectorAll('.rate').forEach(b=>b.classList.toggle('active', parseInt(b.dataset.v) === v));
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
function selectImage(kind,name,hasMrc){{
  selectedKind = kind;
  selectedName = name || '';
  selectedHasMrc = !!hasMrc;
  const label = kind === 'grid' ? 'GridSquare' : (kind + ': ' + name);
  document.getElementById('selected-image').textContent = label;
  document.getElementById('gridimg').src = jpgUrl(kind,name);
  document.getElementById('contrast-panel').style.display = 'none';
  updateButtons();
  document.querySelectorAll('.thumb').forEach(t=>t.classList.toggle('selected', t.dataset.kind === kind && t.dataset.name === name));
}}
Array.from(document.querySelectorAll('.thumb')).forEach(t=>{{
  t.onclick = () => selectImage(t.dataset.kind, t.dataset.name, t.dataset.hasMrc === '1');
}});
document.getElementById('gridimg').onclick = () => selectImage('grid','',GRID_HAS_MRC);
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
document.getElementById('low').oninput = updateContrast;
document.getElementById('high').oninput = updateContrast;
updateButtons();
document.getElementById('submit').onclick = submitReview;
document.addEventListener('keydown', (e)=>{{
  if (e.key >= '1' && e.key <= '5') {{ setRating(parseInt(e.key)); }}
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {{ submitReview(); }}
}});
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
.note{color:#555;font-size:14px;line-height:1.4;}
.btn{display:inline-block;margin-top:14px;border:1px solid #1b6ef3;background:#1b6ef3;color:#fff;border-radius:8px;padding:10px 14px;font-size:14px;text-decoration:none;}
</style>
</head><body><div class=\"page\"><div class=\"card\"><div class=\"title\">Grid review</div>
<div class=\"note\">Review GridSquare, FoilHole, and Data images. Click any thumbnail to inspect it. Use "Show MRC" to adjust contrast when available. Rate each GridSquare and leave comments. A PDF report is generated at the end.</div>
<a class=\"btn\" href=\"/review/0\">Start review</a>
</div></div></body></html>""")

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
        atlas_path = items[idx]["atlas"]
        if not atlas_path or not atlas_path.is_file():
            raise HTTPException(status_code=404)
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
            overview = base_dir / "Screening_overview.pdf"
            details = base_dir / "Screening_details.pdf"
        return overview, details

    def _temp_report_path(filename: str) -> Path:
        temp_root = Path(tempfile.gettempdir()) / "EPUMapperReview"
        temp_root.mkdir(parents=True, exist_ok=True)
        return temp_root / filename

    @app.get("/done")
    def done():
        return HTMLResponse("<html><body>All GridSquares reviewed. <a id=\"report-link\" href=\"/report\">Download screening overview</a> | <a id=\"selected-link\" href=\"/selected_report\">Download screening details</a><script>document.getElementById('report-link').href = '/report?t=' + Date.now();document.getElementById('selected-link').href = '/selected_report?t=' + Date.now();</script></body></html>")

    @app.get("/report")
    def report():
        overview_path, _details_path = _report_paths()
        target_path = overview_path
        try:
            write_review_report(base_dir, target_path, atlas_name, responses)
        except (PermissionError, OSError):
            # Common on read-only/network session folders; fall back to a writable temp directory.
            target_path = _temp_report_path(overview_path.name)
            write_review_report(base_dir, target_path, atlas_name, responses)
        except Exception as exc:
            return JSONResponse({"error": f"failed to generate overview report: {exc}"}, status_code=500)
        return FileResponse(target_path, media_type="application/pdf", filename=target_path.name, headers={"Cache-Control": "no-store"})

    @app.get("/selected_report")
    def selected_report():
        _overview_path, details_path = _report_paths()
        target_path = details_path
        try:
            write_selected_report(base_dir, target_path, atlas_name, responses, overlay=overlay_enabled)
        except (PermissionError, OSError):
            # Common on read-only/network session folders; fall back to a writable temp directory.
            target_path = _temp_report_path(details_path.name)
            write_selected_report(base_dir, target_path, atlas_name, responses, overlay=overlay_enabled)
        except Exception as exc:
            return JSONResponse({"error": f"failed to generate selected report: {exc}"}, status_code=500)
        return FileResponse(target_path, media_type="application/pdf", filename=target_path.name, headers={"Cache-Control": "no-store"})

    return app


def main():
    parser = argparse.ArgumentParser(description="Web review app for GridSquare folders")
    parser.add_argument("grid_dir", type=Path, help="path to a GridSquare directory, Images-Disc*, or session root")
    parser.add_argument("--atlas", type=str, help="atlas image name")
    parser.add_argument("--report", type=Path, help="output PDF path")
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
    app = create_app(grid_root, args.atlas, args.report, args.overlay, overlay_transform)
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
