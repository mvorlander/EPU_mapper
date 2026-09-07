"""Browser-only collection plans. No server, CDN, or absolute media paths."""
from __future__ import annotations

import json
import time
from pathlib import Path


def candidates(grids, responses, scope="targets"):
    import build_collage as bc
    rows = [(i, gid, path, responses.get(path.name, {})) for i, (gid, path) in enumerate(grids, 1)]
    if scope == "all_screened":
        return rows
    if scope == "representative":
        selected = bc._representative_suitable_grid(grids, responses)
        return [selected] if selected else []
    return sorted(
        [r for r in rows if r[3].get("include", True) and bc._normalized_collection_status(r[3]) == "suitable"],
        key=lambda r: ({"primary": 0, "backup": 1, "needs_screening": 2}.get(r[3].get("priority"), 0), r[3].get("target_order", r[0]), r[0]),
    )


def image_records(grid_dir: Path, all_images: bool):
    """Preserve every exposure, including data without a FoilHole preview.

    IDs establish associations, not list indices. With multiple acquisitions we
    leave unmatched slots explicit rather than invent an image correspondence.
    """
    import build_collage as bc
    foils, datas = bc.gather_foil_and_data(grid_dir)
    yield from pair_media(foils, datas, all_images)


def pair_media(foils, datas, all_images=True):
    """Pair by FoilHole ID and acquisition time, never by list position."""
    import build_collage as bc
    for fid in sorted(set(foils) | set(datas)):
        fs, ds = foils.get(fid, []), datas.get(fid, [])
        used = set()
        for data in ds if all_images else ds[-1:]:
            timestamp = bc._media_timestamp_from_name(data.name)
            preceding = [f for f in fs if timestamp and (t := bc._media_timestamp_from_name(f.name)) and t <= timestamp]
            foil = max(preceding, key=lambda f: bc._media_timestamp_from_name(f.name)) if preceding else fs[0] if len(fs) == 1 and not timestamp else None
            if foil:
                used.add(foil)
            yield fid, foil, data
        for foil in fs if all_images else fs[-1:] if not ds else []:
            if foil not in used:
                yield fid, foil, None


def append_pdf_checklist(pdf, base_dir, responses):
    """Human-readable handoff with stable EPU IDs, independently of detail scope."""
    import textwrap
    import build_collage as bc
    rows = []
    for idx, gid, directory, review in candidates(bc._collect_grids(base_dir), responses):
        rows.append((f"{idx}. GridSquare {gid}", str(review.get("priority", "primary")).replace("_", " ").title(), str(review.get("comment", "")), str(review.get("preferred_hole", ""))))
    for target in bc._load_manual_collection_targets(base_dir):
        rows.append((f"T. GridSquare {target.get('gridsquare_id') or target.get('key')}", "Needs screening (manual target)", str(target.get("comment", "")), ""))
    y = 0

    def new_page():
        pdf.setPageSize((595, 842))
        pdf.setFillColorRGB(.09, .13, .2)
        pdf.setFont(bc._PDF_FONT_BOLD, 20)
        pdf.drawString(36, 796, "Collection checklist")
        pdf.setFont(bc._PDF_FONT_REGULAR, 10)
        pdf.drawString(36, 774, "Use the EPU GridSquare ID to locate each target at the microscope.")
        pdf.drawString(36, 758, "Manual targets have no screening evidence and must be verified.")
        return 724

    y = new_page()
    if not rows:
        pdf.drawString(36, y, "No collection targets selected.")
    for label, priority, comment, preferred in rows:
        lines = textwrap.wrap(comment, 85)
        if preferred:
            lines.extend(textwrap.wrap("Preferred exposure: " + preferred, 85))
        content = [(label + " - " + priority, True)] + [(line, False) for line in lines]
        for line, bold in content:
            if y < 55:
                pdf.showPage()
                y = new_page()
            pdf.setFont(bc._PDF_FONT_BOLD if bold else bc._PDF_FONT_REGULAR, 10)
            pdf.drawString(36 if bold else 48, y, line)
            y -= 16
        y -= 14
    pdf.showPage()


def build_plan(base_dir, atlas_name, responses, *, overlay=False, atlas_overlay=True,
               global_summary=None, skip_foil_processing=False, all_screened_images=False,
               collection_targets_only=False):
    import build_collage as bc
    scope = "all_screened" if all_screened_images else "targets" if collection_targets_only else "representative"
    grids = bc._collect_grids(base_dir)
    selected = {r[0] for r in candidates(grids, responses, scope)}
    atlas_path = next((p for _, directory in grids if atlas_name and (p := bc._resolve_atlas_path(atlas_name, directory, base_dir))), None)
    atlas = bc._load_image(atlas_path, "RGB") if atlas_path else None
    nodes, width, height = bc._load_atlas_mapping(atlas_path) if atlas_path else ({}, None, None)
    records = []
    assets = {}

    def embed(path):
        if not path:
            return ""
        key = str(path)
        if key not in assets:
            assets[key] = ("a" + str(len(assets)), bc._embedded_path_uri(path))
        return assets[key][0]

    for idx, (gid, directory) in enumerate(grids, 1):
        review = responses.get(directory.name, {})
        center = bc._atlas_center_for_grid(nodes, directory, gid)
        try:
            grid_path = bc.find_grid_image(directory)
        except FileNotFoundError:
            grid_path = None
        pairs = []
        if idx in selected and not skip_foil_processing:
            for fid, foil, data in image_records(directory, all_screened_images):
                pairs.append(dict(id=fid, foil=embed(foil), data=embed(data), foil_name=foil.name if foil else "No matching FoilHole preview", data_name=data.name if data else "No matching Data image"))
        preferred = str(review.get("preferred_hole", ""))
        if idx in selected and preferred and not skip_foil_processing and not any(p["data_name"] == preferred for p in pairs):
            for fid, foil, data in image_records(directory, True):
                if data and data.name == preferred:
                    pairs.insert(0, dict(id=fid, foil=embed(foil), data=embed(data), foil_name=foil.name if foil else "No matching FoilHole preview", data_name=data.name))
        overlay_path, hole_markers = None, []
        if overlay and not skip_foil_processing and idx in selected:
            from review_app import _ensure_overlay_image
            overlay_path, _message, hole_markers = _ensure_overlay_image(directory, base_dir)
        fs, ds = bc.gather_foil_and_data(directory) if not skip_foil_processing else ({}, {})
        records.append(dict(key=str(gid), number=idx, label=f"GridSquare {gid}", status=bc._normalized_collection_status(review), rating=review.get("rating", 0), priority=review.get("priority", "primary"), order=review.get("target_order", idx), preferred=review.get("preferred_hole", ""), comment=review.get("comment", ""), included=review.get("include", True), screened=bool(ds), position=dict(x=center[0]/width*100, y=center[1]/height*100) if center and width and height else None, grid=embed(overlay_path or grid_path) if idx in selected else "", source=grid_path.name if grid_path else "GridSquare preview unavailable", pairs=pairs, hole_markers=hole_markers, details=idx in selected))
    for target in bc._load_manual_collection_targets(base_dir):
        gid = str(target.get("gridsquare_id") or target.get("key"))
        records.append(dict(key="target:"+gid, number="T"+gid, label="Unscreened GridSquare "+gid, status="target", priority="needs_screening", order=target.get("target_order", 999999), rating=0, comment=target.get("comment", ""), position=target.get("position"), grid="", source="Manually selected; screening evidence unavailable", pairs=[], details=False, screened=False, included=True))
    payload = dict(title=base_dir.parent.name if base_dir.name.lower().startswith("images-disc") else base_dir.name, created=time.strftime("%Y-%m-%d %H:%M %Z"), summary=global_summary or "", scope=scope, atlas=bc._embedded_image_uri(atlas, max_size=3000), categories=bc._embedded_image_uri(bc._atlas_with_category_markers(atlas, atlas_path)) if atlas is not None else "", records=records, assets={v[0]: v[1] for v in assets.values()})
    # JSON in script data must not be able to terminate the HTML element.
    serialized = json.dumps(payload, ensure_ascii=True).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return PLAN_HTML.replace("__PLAN_DATA__", serialized)


PLAN_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>EPU Mapper · Collection plan</title>
<style>
:root{font:15px system-ui,sans-serif;color:#172033;background:#f3f6fa}*{box-sizing:border-box}body{margin:0}header{padding:22px 28px;background:white;border-bottom:1px solid #dbe3ee}h1{margin:0 0 8px;font-size:25px}h2{font-size:17px;margin:0}p{white-space:pre-wrap}button,input,select{font:inherit;border:1px solid #c8d4e3;border-radius:7px;padding:8px;background:white;color:inherit}button{cursor:pointer}button:focus-visible{outline:3px solid #2563eb}button:disabled{opacity:.5;cursor:default}.muted{color:#59677c;font-size:13px}.legend,.tools{display:flex;flex-wrap:wrap;gap:10px;align-items:center;padding:10px 0}.legend span{border-left:8px solid var(--color);padding-left:6px}.workspace{display:grid;grid-template-columns:240px minmax(0,1fr);gap:18px;padding:18px}aside{background:white;padding:12px;border-radius:12px;align-self:start;position:sticky;top:10px;max-height:95vh;overflow:auto}aside input,aside select{width:100%;margin-bottom:8px}.target{width:100%;text-align:left;margin:4px 0;border-left:5px solid var(--color)}.target.active{background:#eaf2ff;border-color:#2563eb}.target small{display:block;margin-top:4px}.viewers{display:grid;grid-template-columns:1fr 1fr;gap:16px}.card{min-width:0;background:white;border-radius:12px;border:1px solid #dbe3ee;padding:12px}.viewport{height:clamp(300px,38vw,620px);overflow:hidden;background:#111827;position:relative;display:flex;align-items:center;justify-content:center;touch-action:none;border-radius:8px}.stage{position:relative;flex:none;transform-origin:center}.stage img{display:block;width:100%;height:100%;object-fit:contain;user-select:none;pointer-events:none}.marker{position:absolute;transform:translate(-50%,-50%);border:2px solid white;background:var(--color);color:white;border-radius:50%;width:28px;height:28px;font-size:11px;padding:0;box-shadow:0 0 0 1px #1118}.marker.selected{outline:3px solid #67e8f9}.marker.manual{border-radius:4px}.empty{color:#d7e1ef;text-align:center;padding:20px}.filename{overflow-wrap:anywhere;min-height:2.8em}.decision{padding:16px;margin:16px 0;background:white;border-radius:12px}.card:fullscreen{padding:20px;overflow:auto}.card:fullscreen .viewport{height:85vh}#summary{margin-bottom:0}#caption{margin-top:6px}body.raw .marker{display:none}@media(max-width:850px){.workspace{grid-template-columns:1fr}aside{position:static;max-height:240px}.viewers{grid-template-columns:1fr}}@media print{aside,.tools{display:none}.workspace{display:block}.viewport{height:340px}.card{break-inside:avoid}}
</style></head><body><header><h1>Collection plan · <span id="title"></span></h1><div class="muted" id="provenance"></div><p id="summary"></p><div class="legend"><span style="--color:#059669">Suitable</span><span style="--color:#dc2626">Unsuitable</span><span style="--color:#64748b">Unmarked</span><span style="--color:#0891b2">Unscreened target</span><span style="--color:#d97706">! No screening evidence</span></div></header>
<main class="workspace"><aside><h2>GridSquares & targets</h2><div class="tools"><input id="search" placeholder="Find ID or comment" aria-label="Search GridSquares"><select id="filter" aria-label="Filter GridSquares"><option value="targets">Collection shortlist</option><option value="all">All GridSquares</option><option value="primary">Primary</option><option value="backup">Backup</option><option value="needs_screening">Needs screening</option></select></div><div id="list"></div></aside><section><div class="viewers"><article class="card" id="atlas-card"><h2>Atlas</h2><div class="tools"><select id="atlas-mode" aria-label="Atlas annotations"><option value="annotated">Collection status</option><option value="rating">Ratings</option><option value="raw">Raw Atlas</option><option value="categories">EPU categories</option></select></div><div class="viewport" id="atlas-view"><div class="stage"><img alt="Atlas"><div id="markers"></div></div></div></article><article class="card" id="grid-card"><h2 id="grid-title">Select a GridSquare</h2><p class="muted filename" id="grid-source"></p><div class="viewport" id="grid-view"><div class="stage"><img alt="GridSquare"></div></div></article></div><div class="decision"><strong id="decision">Choose a target on the Atlas or in the list.</strong><p id="comment"></p><div id="caption" class="muted"></div></div><div class="viewers"><article class="card"><h2>FoilHole</h2><p id="foil-name" class="muted filename"></p><div class="viewport" id="foil-view"><div class="stage"><img alt="FoilHole"></div></div></article><article class="card"><h2>Data</h2><p id="data-name" class="muted filename"></p><div class="viewport" id="data-view"><div class="stage"><img alt="Data"></div></div><div class="tools"><button id="previous">← Previous exposure</button><span id="pair-count"></span><button id="next">Next exposure →</button></div></article></div></section></main>
<script type="application/json" id="plan-data">__PLAN_DATA__</script><script>
'use strict';const plan=JSON.parse(document.getElementById('plan-data').textContent),$=id=>document.getElementById(id),colors={suitable:'#059669',unsuitable:'#dc2626',target:'#0891b2',unmarked:'#64748b','':'#64748b'},ratings=['#64748b','#dc2626','#f97316','#ca8a04','#65a30d','#15803d'];let current=null,pairIndex=0;
$('title').textContent=plan.title;$('provenance').textContent='Offline, read-only snapshot · '+plan.created+' · Detail scope: '+plan.scope.replaceAll('_',' ')+' · EPU Mapper collection-plan format 1';$('summary').textContent=plan.summary;
function viewer(id){const view=$(id),stage=view.querySelector('.stage'),img=stage.querySelector('img');let scale=1,x=0,y=0,drag=null;const empty=document.createElement('div');empty.className='empty';view.append(empty);const tools=document.createElement('div');tools.className='tools';tools.innerHTML='<button aria-label="Zoom out">−</button><span>100%</span><button aria-label="Zoom in">+</button><button>Fit</button><button>Enlarge</button><span class="muted">Scroll to zoom · drag to pan</span>';view.after(tools);function apply(){stage.style.transform=`translate(${x}px,${y}px) scale(${scale})`;tools.querySelector('span').textContent=Math.round(scale*100)+'%'}function fit(){const ratio=Math.min(view.clientWidth/(img.naturalWidth||1),view.clientHeight/(img.naturalHeight||1));stage.style.width=(img.naturalWidth||1)*ratio+'px';stage.style.height=(img.naturalHeight||1)*ratio+'px';apply()}function zoom(value,cx=view.clientWidth/2,cy=view.clientHeight/2){const next=Math.max(1,Math.min(10,value)),r=next/scale;x=(cx-view.clientWidth/2)-(cx-view.clientWidth/2-x)*r;y=(cy-view.clientHeight/2)-(cy-view.clientHeight/2-y)*r;scale=next;if(scale===1)x=y=0;apply()}const bs=tools.querySelectorAll('button');bs[0].onclick=()=>zoom(scale/1.3);bs[1].onclick=()=>zoom(scale*1.3);bs[2].onclick=()=>zoom(1);bs[3].onclick=()=>view.closest('.card').requestFullscreen?.();view.onwheel=e=>{e.preventDefault();const r=view.getBoundingClientRect();zoom(scale*Math.exp(-e.deltaY*.0015),e.clientX-r.left,e.clientY-r.top)};view.onpointerdown=e=>{if(e.target.closest('button'))return;drag={px:e.clientX,py:e.clientY,x,y};view.setPointerCapture(e.pointerId)};view.onpointermove=e=>{if(drag){x=drag.x+e.clientX-drag.px;y=drag.y+e.clientY-drag.py;apply()}};view.onpointerup=view.onpointercancel=()=>drag=null;img.onload=fit;new ResizeObserver(fit).observe(view);return {set(uri,message='Image unavailable'){scale=1;x=y=0;stage.hidden=!uri;empty.hidden=!!uri;empty.textContent=message;if(uri)img.src=uri;else img.removeAttribute('src');fit()}}}
const av=viewer('atlas-view'),gv=viewer('grid-view'),fv=viewer('foil-view'),dv=viewer('data-view');const holeLayer=document.createElement('div');$('grid-view').querySelector('.stage').append(holeLayer);av.set(plan.atlas,'Atlas unavailable');
function markers(){const layer=$('markers');layer.replaceChildren();const mode=$('atlas-mode').value;plan.records.filter(r=>r.position).forEach(r=>{const b=document.createElement('button');b.className='marker'+(r.status==='target'?' manual':'')+(r===current?' selected':'');b.style.left=r.position.x+'%';b.style.top=r.position.y+'%';b.style.setProperty('--color',mode==='rating'?ratings[r.rating||0]:colors[r.status]||colors.unmarked);b.textContent=r.status==='target'?'T':String(r.number)+(r.screened?'':'!');b.title=r.label+(r.screened?'':' · No screening evidence');b.setAttribute('aria-label',b.title);b.onclick=()=>select(r);layer.append(b)});layer.hidden=mode==='raw'||mode==='categories'}
function list(){const q=$('search').value.toLowerCase(),filter=$('filter').value,rows=plan.records.filter(r=>(!q||(r.label+' '+r.comment).toLowerCase().includes(q))&&(filter==='all'||filter==='targets'&&(r.status==='target'||r.status==='suitable'&&r.included)||r.priority===filter&&(r.status==='target'||r.status==='suitable'&&r.included)));rows.sort((a,b)=>({primary:0,backup:1,needs_screening:2}[a.priority]??0)-({primary:0,backup:1,needs_screening:2}[b.priority]??0)||a.order-b.order);$('list').replaceChildren();rows.forEach(r=>{const b=document.createElement('button');b.className='target'+(r===current?' active':'');b.style.setProperty('--color',colors[r.status]||colors.unmarked);b.textContent=(r.status==='target'?'T':r.number)+' · '+r.label;const small=document.createElement('small');small.textContent=(r.priority||'primary').replaceAll('_',' ')+(r.screened?'':' · ! No screening evidence');b.append(small);b.onclick=()=>select(r);$('list').append(b)});if(!rows.length)$('list').textContent='No matching targets.'}
function pair(){const p=current?.pairs[pairIndex];fv.set(p?plan.assets[p.foil]:'','No FoilHole preview');dv.set(p?plan.assets[p.data]:'','No matching Data image');$('foil-name').textContent=p?.foil_name||'';$('data-name').textContent=p?.data_name||'';$('pair-count').textContent=p?(pairIndex+1)+' / '+current.pairs.length:'No exposures in this export';$('previous').disabled=!p||pairIndex===0;$('next').disabled=!p||pairIndex===current.pairs.length-1}
function select(r){current=r;holeLayer.replaceChildren();pairIndex=Math.max(0,r.pairs.findIndex(p=>p.data_name===r.preferred||p.id===r.preferred));$('grid-title').textContent=r.label;$('grid-source').textContent=r.source;gv.set(plan.assets[r.grid],r.details?'GridSquare unavailable':'Screening images not included in this scope');$('decision').textContent=(r.status==='target'?'Manual target · Needs screening':(r.status||'unmarked')+' · '+r.priority.replaceAll('_',' ')+' · Rating '+r.rating);$('comment').textContent=r.comment;$('caption').textContent=r.screened?(r.details?'Review snapshot. Source filenames are shown above each image.':'Images omitted by export scope; annotations remain available.'):'No screening evidence: this target still needs verification before collection.';(r.hole_markers||[]).forEach(m=>{const index=r.pairs.findIndex(p=>p.foil_name===m.foil_name);if(index<0)return;const b=document.createElement('button');b.className='marker';b.style.left=m.x+'%';b.style.top=m.y+'%';b.style.setProperty('--color','#059669');b.textContent=m.label||r.pairs[index].id;b.title='Show FoilHole '+r.pairs[index].id;b.onclick=()=>{pairIndex=index;pair()};holeLayer.append(b)});pair();markers();list()}
$('previous').onclick=()=>{pairIndex--;pair()};$('next').onclick=()=>{pairIndex++;pair()};$('search').oninput=$('filter').onchange=list;$('atlas-mode').onchange=()=>{av.set($('atlas-mode').value==='categories'?plan.categories:plan.atlas);markers()};list();markers();const initial=plan.records.find(r=>r.details)||plan.records[0];if(initial)select(initial);
</script></body></html>'''
