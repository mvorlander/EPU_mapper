"""Atlas annotation tools and self-contained review exports for the unified UI."""
import json
import math
import re
import time
import uuid
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse, Response


def report_filename(label):
    """Portable download name; exclude separators, controls and Windows metacharacters."""
    name=re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', '_', str(label or 'EPU-session'))
    name=name.strip(' .')[:100].rstrip(' .') or 'EPU-session'
    return name+'-screening-report.html'


def atlas_targets(store, atlas_id):
    return [dict(value, id=key.split(':',1)[1]) for key,value in store.annotation_snapshot().items()
            if key.startswith('atlas-target:') and value.get('atlas_id')==atlas_id]


def build_report(store, label, scope='representative', high_resolution=True, transform='identity'):
    from PIL import Image
    from build_collage import _embedded_image_uri
    from collection_plan import PLAN_HTML
    from image_adjustments import adjusted_preview
    atlas=store.meta('atlas') or {}
    grids=store.grids()
    warnings=[];assets={}

    def embed(key):
        if not key:
            return ''
        if key not in assets:
            try:
                with Image.open(store.cache_file(key)) as image:
                    assets[key]=_embedded_image_uri(image,1800)
            except (OSError,ValueError,KeyError) as exc:
                warnings.append('Preview unavailable: '+str(exc))
                assets[key]=''
        return key

    atlas_uri=''
    if high_resolution and atlas.get('mrc'):
        try:
            atlas_uri=_embedded_image_uri(adjusted_preview(store.cache_file(atlas['mrc']),max_size=4096),4096)
        except (OSError,ValueError,KeyError) as exc:
            warnings.append('High-resolution Atlas unavailable; using JPEG/PNG preview. '+str(exc))
    if not atlas_uri and atlas.get('id'):
        try:
            with Image.open(store.cache_file(atlas['id'])) as image:
                atlas_uri=_embedded_image_uri(image,4096)
        except (OSError,ValueError,KeyError) as exc:
            warnings.append('Atlas unavailable: '+str(exc))
    selected=[g['id'] for g in grids if g['annotation'].get('status')=='suitable']
    if scope=='representative':
        selected=selected[:1]
    elif scope=='all_screened':
        selected=[g['id'] for g in grids if g['holes']]
    if not selected:
        warnings.append('No squares match the selected image scope. Atlas and annotations are included; mark a square suitable or explicitly choose all screened images.')
    records=[]
    for number,g in enumerate(grids,1):
        a=g['annotation'];pairs=[];hole_markers=[]
        if g['id'] in selected:
            offset=0
            while True:
                page=store.holes(g['id'],offset,100)
                for h in page['rows']:
                    exposure=store.exposures(g['id'],h['hole'])
                    rows=exposure['exposures'] or [dict(id='',foil=exposure['foil'],foil_name=exposure['foil_name'],name='Data not included' if store.ignore_data else 'No matching Data preview')]
                    for row in rows:
                        pairs.append(dict(id=h['hole'],foil=embed(row['foil']),data=embed(row['id']),foil_name=row['foil_name'],data_name=row['name']))
                offset+=len(page['rows'])
                if offset>=page['total'] or not page['rows']:
                    break
            try:
                for m in store.geometry(g['id'],transform)['markers']:
                    pair=next((p for p in pairs if str(p['id'])==str(m['hole'])),None)
                    if pair:
                        hole_markers.append(dict(x=m['x']*100,y=m['y']*100,foil_name=pair['foil_name'],id=m['hole'],label=m['hole']))
            except (OSError,ValueError,KeyError) as exc:
                warnings.append('Hole overlay unavailable for '+g['name']+': '+str(exc))
        position=None
        p=atlas.get('nodes',{}).get(g['name'].replace('GridSquare_',''),{}).get('center')
        if p and atlas.get('width') and atlas.get('height'):
            position=dict(x=p[0]/atlas['width']*100,y=p[1]/atlas['height']*100)
        records.append(dict(key=g['id'],number=number,label=g['name'],status=a.get('status',''),rating=a.get('rating',0),priority=a.get('priority','primary'),order=number,preferred='',comment=a.get('comment',''),included=True,screened=bool(g['holes']),position=position,grid=embed(g['image']) if g['id'] in selected else '',source=g['name'],pairs=pairs,hole_markers=hole_markers,details=g['id'] in selected))
    for record,g in zip(records,grids):
        record['atlas_key']=g['name'].replace('GridSquare_','')
    targets=atlas_targets(store,atlas.get('id'))
    for number,t in enumerate(targets,1):
        records.append(dict(key='target:'+t['id'],number='T'+str(number),label='Manual target '+str(number),status='target',rating=0,priority='needs_screening',order=number,preferred='',comment=t.get('comment',''),included=True,screened=False,position=dict(x=t['x']*100,y=t['y']*100),bounds=t.get('bounds'),grid='',source='Manually selected; verify before collection',pairs=[],hole_markers=[],details=False))
    from build_collage import _EPU_CATEGORY_COLORS
    categories=[]
    if atlas.get('width') and atlas.get('height'):
        categories=[dict(key=key,x=n['center'][0]/atlas['width'],y=n['center'][1]/atlas['height'],category=n.get('category')) for key,n in atlas.get('nodes',{}).items() if n.get('center')]
    payload=dict(title=label or store.source.name,created=time.strftime('%Y-%m-%d %H:%M %Z'),summary='\n'.join(warnings),scope=scope,atlas=atlas_uri,categories=atlas_uri,records=records,assets=assets,category_nodes=categories,category_colors={str(k):'#%02x%02x%02x'%v for k,v in _EPU_CATEGORY_COLORS.items()})
    serialized=json.dumps(payload,ensure_ascii=True).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    template=PLAN_HTML.replace("p.foil_name===m.foil_name", "m.id!=null?String(p.id)===String(m.id):p.foil_name===m.foil_name")
    return template.replace('__PLAN_DATA__',serialized).replace('</body>',REPORT_TOOLS+'</body>'),warnings


def install_review_tools(app,store,label=None,transform='identity'):
    @app.post('/api/annotations/clear')
    def clear_annotations(value:dict):
        if value.get('confirm')!='DELETE ALL ANNOTATIONS' or value.get('source')!=str(store.source):
            raise HTTPException(400,'Confirm deletion for this session.')
        result=store.clear_user_annotations()
        result['backup_url']='/api/annotations/backup/'+result['backup']
        return result

    @app.get('/api/annotations/backup/{key}')
    def annotation_backup(key:str):
        if len(key)!=32 or any(c not in '0123456789abcdef' for c in key):
            raise HTTPException(404,'Backup not found')
        path=store.root/'annotation-backups'/(key+'.json')
        if not path.is_file():
            raise HTTPException(404,'Backup not found')
        return FileResponse(path,media_type='application/json',filename='annotations-before-clear.json')

    @app.get('/review-tools.js')
    def javascript():
        return Response(REVIEW_TOOLS,media_type='text/javascript',headers={'Cache-Control':'no-store'})

    @app.get('/api/atlas-targets')
    def targets():
        return atlas_targets(store,(store.meta('atlas') or {}).get('id'))

    @app.post('/api/atlas-targets')
    def add_target(value:dict):
        atlas=store.meta('atlas') or {}
        if not atlas.get('id') or value.get('atlas_id')!=atlas['id']:
            raise HTTPException(409,'Atlas changed; select the target again.')
        try:
            x,y=float(value['x']),float(value['y'])
            if not all(math.isfinite(v) and 0<=v<=1 for v in (x,y)):
                raise ValueError()
            bounds=value.get('bounds')
            if bounds is not None:
                if len(bounds)!=4:
                    raise ValueError()
                bounds=[float(v) for v in bounds]
                if not all(math.isfinite(v) and 0<=v<=1 for v in bounds) or not(bounds[0]<bounds[2] and bounds[1]<bounds[3]):
                    raise ValueError()
            clean=dict(atlas_id=atlas['id'],x=x,y=y,bounds=bounds,comment=str(value.get('comment',''))[:10000])
        except (ValueError,TypeError,KeyError):
            raise HTTPException(400,'Target must lie inside the Atlas image.')
        key=uuid.uuid4().hex
        store.execute('INSERT INTO annotations VALUES (?,?)',('atlas-target:'+key,json.dumps(clean)))
        return dict(clean,id=key)

    @app.delete('/api/atlas-targets/{key}')
    def delete_target(key:str):
        if not any(t['id']==key for t in targets()):
            raise HTTPException(404,'Target not found on this Atlas')
        store.execute('DELETE FROM annotations WHERE key=?',('atlas-target:'+key,))
        return dict(deleted=True)

    @app.post('/api/report-html')
    def export(value:dict):
        scope=value.get('scope','representative')
        if scope not in ('representative','targets','all_screened'):
            raise HTTPException(400,'Unknown report scope')
        if store.scan_state['status'] in ('new','indexing'):
            raise HTTPException(409,'Wait for indexing to finish before exporting.')
        def work():
            html,warnings=build_report(store,label,scope,bool(value.get('high_resolution',True)),transform)
            key=uuid.uuid4().hex
            folder=store.root/'reports';folder.mkdir(exist_ok=True)
            (folder/(key+'.html')).write_text(html,encoding='utf-8')
            return dict(url='/api/report-html/'+key,warnings=warnings)
        return dict(job=store.submit('Building embedded HTML report',work,priority=10))

    @app.get('/api/report-html/{key}')
    def download(key:str):
        if len(key)!=32 or any(c not in '0123456789abcdef' for c in key):
            raise HTTPException(404,'Report not found')
        path=store.root/'reports'/(key+'.html')
        if not path.is_file():
            raise HTTPException(404,'Report not found')
        return FileResponse(path,media_type='text/html',filename=report_filename(label or store.source.name))


REVIEW_TOOLS = r'''
const atlasTools=document.createElement('div');atlasTools.className='controls';
const clearPanel=document.createElement('details');clearPanel.className='contrast';clearPanel.style.cssText='margin:10px 18px';
clearPanel.innerHTML='<summary>Session annotation settings</summary><p>Clear all ratings, comments, suitability decisions, follow-up flags, manual targets/areas, and observed targeting shifts in this local session. Source images, EPU categories, particle density, and existing exports stay unchanged. A JSON backup is saved locally first. Old screening reviews will not be imported again.</p><button id="clearAnnotations">Clear all session annotations…</button><span id="clearStatus" role="status"></span><a id="clearBackup" hidden download>Download annotation backup</a><button id="clearReload" hidden>Reload cleared session</button>';
document.querySelector('header').after(clearPanel);
$('clearReload').onclick=()=>location.reload();
$('clearAnnotations').onclick=async()=>{
 if(prompt('Clear ALL user annotations in this session? A local backup will be kept. Type DELETE ALL ANNOTATIONS to confirm.')!=='DELETE ALL ANNOTATIONS')return;
 if(!await saveIfDirty())return;
 const b=$('clearAnnotations');b.disabled=true;$('clearStatus').textContent='Backing up and clearing annotations…';
 try{const snapshot=await api('/api/annotations.json');const r=await api('/api/annotations/clear',{confirm:'DELETE ALL ANNOTATIONS',source:snapshot.source});dirty=false;
 $('clearStatus').textContent=r.deleted+' saved annotations cleared. Download the backup if needed, then reload to continue.';
 $('clearBackup').href=r.backup_url;$('clearBackup').hidden=false;$('clearReload').hidden=false;
 document.querySelector('main').inert=true;document.querySelector('main').style.opacity='.45';
 for(const panel of [reportPanel,targetPanel])panel.inert=true;
 }catch(e){$('clearStatus').textContent='Could not clear annotations: '+e.message;b.disabled=false}
};
atlasLegend.title='Collection suitability, ratings, or EPU metadata categories; not Data preview availability.';
$('legend').textContent=(config.mode==='foilhole'?'Data loading is off. ':'')+'Atlas: suitability / rating / EPU category layers · white ring = active square · dashed cyan = manual collection target. Scroll to zoom · drag to pan.';
atlasTools.innerHTML='<label>Atlas annotations <select id="atlasColorMode"><option value="status">Collection suitability</option><option value="rating">Rating</option><option value="categories">EPU categories</option><option value="raw">Raw Atlas</option></select></label><button id="manualTarget">Add target / area</button>';
views.atlas.card.querySelector('.viewer-options').after(atlasTools);
const categoryColors={'-1':'#94a3b8',0:'#40e0d0',1:'#f97316',2:'#3b82f6',3:'#facc15',4:'#ec4899',5:'#c084fc',6:'#d946ef'},ratingColors=['#64748b','#dc2626','#f97316','#ca8a04','#65a30d','#15803d'];
function renderAtlasLegend(mode){
 atlasLegend.replaceChildren();atlasLegend.style.gap='8px 14px';
 if(mode==='raw'){atlasLegend.textContent='Raw Atlas — no annotation overlays.';return}
 const swatch=(label,color,outline=false,dashed=false)=>{
  const item=document.createElement('span');item.style.cssText='display:inline-flex;align-items:center;gap:6px;white-space:nowrap';
  const icon=document.createElement('span');icon.setAttribute('aria-hidden','true');icon.style.cssText='display:inline-block;width:13px;height:13px;flex:none;border-radius:50%;box-sizing:border-box';
  icon.style.border='2px '+(dashed?'dashed':'solid')+' '+color;icon.style.background=outline?'transparent':color;
  if(color==='#fff')icon.style.boxShadow='0 0 0 2px #475569';
  const text=document.createElement('span');text.textContent=label;item.append(icon,text);atlasLegend.append(item);
 };
 if(mode==='rating')ratingColors.forEach((color,i)=>swatch(i?'Rating '+i:'Unrated',color));
 if(mode==='categories'){for(const [key,color] of Object.entries(categoryColors))swatch('Category '+key,color)}
 const outline=mode!=='status';
 swatch(outline?'Suitable outline':'Suitable','#059669',outline);
 swatch(outline?'Unsuitable outline':'Unsuitable','#dc2626',outline);
 swatch(outline?'Unmarked outline':'Unmarked','#64748b',outline);
 swatch('Active square','#fff',true);swatch('Manual target','#0891b2',true,true);
 if(mode==='categories'){const note=document.createElement('span');note.textContent='Category colors are display colors, not the EPU UI palette.';atlasLegend.append(note)}
}
let manualTargets=[],targetArmed=false,targetCorner=null,targetAtlas='';
const targetPanel=document.createElement('details');targetPanel.className='contrast';targetPanel.innerHTML='<summary>Manual collection targets</summary><p>Click an unscreened square to add a target. For an area, click two opposite corners. Targets are planning annotations, not microscope acquisition commands.</p><label><input type="checkbox" id="targetRectangle"> Rectangular area</label><input id="targetComment" aria-label="Manual target comment" placeholder="Target notes"><button id="cancelTarget">Cancel placement</button><p id="targetHint" role="status"></p><div id="targetList"></div>';
views.atlas.card.append(targetPanel);
$('targetComment').style.width='min(100%, 400px)';
const nativeRenderAtlas=renderAtlas;
renderAtlas=function(){
 nativeRenderAtlas();if(!atlas)return;
 const mode=$('atlasColorMode').value,svg=views.atlas.svg;
 if(mode==='raw'){svg.replaceChildren();renderAtlasLegend(mode);return}
 const ms=views.atlas.markers.map(m=>{const g=grids.find(g=>g.id===m.id),a=dirty&&grid?.id===m.id&&$('scope').value==='grid'?{rating:Number($('rating').value),status:$('suitability').value}:g?.annotation||{};return {...m,color:mode==='rating'?ratingColors[a.rating||0]:a.status==='suitable'?'#059669':a.status==='unsuitable'?'#dc2626':'#64748b',stroke:a.status==='suitable'?'#059669':a.status==='unsuitable'?'#dc2626':'#64748b'}});
 views.atlas.mark(ms,m=>selectGrid(m.id));
 if(mode==='categories'&&atlas.width&&atlas.height){
  const layer=document.createElementNS('http://www.w3.org/2000/svg','g');
  for(const [key,n] of Object.entries(atlas.nodes)){
   const c=document.createElementNS(layer.namespaceURI,'circle');c.setAttribute('cx',n.center[0]/atlas.width);c.setAttribute('cy',n.center[1]/atlas.height);c.setAttribute('r',.018);c.style.fill=categoryColors[n.category]||'#6366f1';c.style.stroke=c.style.fill;c.style.pointerEvents='none';layer.append(c);
  }
  svg.prepend(layer);for(const c of svg.querySelectorAll('circle.selected, circle')){if(c.parentElement===svg)c.style.fill='none'}
 }
 drawActiveGridOutline();
 for(const [i,t] of manualTargets.entries()){
  const shape=document.createElementNS('http://www.w3.org/2000/svg',t.bounds?'rect':'circle');
  if(t.bounds){const [x,y,x2,y2]=t.bounds;shape.setAttribute('x',x);shape.setAttribute('y',y);shape.setAttribute('width',x2-x);shape.setAttribute('height',y2-y)}else{shape.setAttribute('cx',t.x);shape.setAttribute('cy',t.y);shape.setAttribute('r',.022)}
  shape.style.fill='none';shape.style.stroke='#0891b2';shape.style.strokeWidth='2px';shape.style.strokeDasharray='5 3';shape.style.pointerEvents='none';shape.setAttribute('vector-effect','non-scaling-stroke');const title=document.createElementNS(shape.namespaceURI,'title');title.textContent='Manual target '+(i+1)+': '+t.comment;shape.append(title);svg.append(shape);
 }
 renderAtlasLegend(mode);
};
$('atlasColorMode').onchange=renderAtlas;
async function loadAtlasTargets(){const id=atlas?.id;if(!id)return;const rows=await api('/api/atlas-targets');if(id!==atlas?.id)return;manualTargets=rows;$('targetList').replaceChildren();for(const [i,t] of rows.entries()){const row=document.createElement('div');row.className='controls';const text=document.createElement('span');text.textContent='Target '+(i+1)+' · '+t.comment;row.append(text,button('Remove',async()=>{if(!confirm('Remove this manual target?'))return;const r=await fetch('/api/atlas-targets/'+t.id,{method:'DELETE'});if(!r.ok){fail(new Error(await r.text()));return}await loadAtlasTargets()}));$('targetList').append(row)}renderAtlas()}
function cancelAtlasTarget(){targetArmed=false;targetCorner=null;$('manualTarget').classList.remove('active');views.atlas.viewport.style.cursor='';$('targetHint').textContent=''}
$('manualTarget').onclick=()=>{targetArmed=!targetArmed;if(!targetArmed){cancelAtlasTarget();return}targetAtlas=atlas?.id;targetCorner=null;targetPanel.open=true;$('manualTarget').classList.add('active');views.atlas.viewport.style.cursor='crosshair';$('targetHint').textContent='Click '+($('targetRectangle').checked?'the first corner':'the target square')+' on the Atlas. Esc cancels.'};
$('cancelTarget').onclick=cancelAtlasTarget;document.addEventListener('keydown',e=>{if(e.key==='Escape')cancelAtlasTarget()});
views.atlas.viewport.addEventListener('pointerdown',e=>{if(targetArmed){e.preventDefault();e.stopImmediatePropagation()}},true);
views.atlas.viewport.addEventListener('click',async e=>{
 if(!targetArmed)return;e.preventDefault();e.stopImmediatePropagation();
 if(!atlas||atlas.id!==targetAtlas){cancelAtlasTarget();return}
 const matrix=views.atlas.svg.getScreenCTM();if(!matrix)return;const p=new DOMPoint(e.clientX,e.clientY).matrixTransform(matrix.inverse());if(p.x<0||p.x>1||p.y<0||p.y>1)return;
 if($('targetRectangle').checked&&!targetCorner){targetCorner=p;$('targetHint').textContent='Click the opposite corner. Esc cancels.';return}
 const bounds=$('targetRectangle').checked&&targetCorner?[Math.min(p.x,targetCorner.x),Math.min(p.y,targetCorner.y),Math.max(p.x,targetCorner.x),Math.max(p.y,targetCorner.y)]:null;
 try{await api('/api/atlas-targets',{atlas_id:atlas.id,x:bounds?(bounds[0]+bounds[2])/2:p.x,y:bounds?(bounds[1]+bounds[3])/2:p.y,bounds,comment:$('targetComment').value});cancelAtlasTarget();await loadAtlasTargets()}catch(e){$('targetHint').textContent=e.message}
},true);
for(const id of ['rating','suitability']){const original=$(id).oninput;$(id).oninput=e=>{original?.(e);if($('scope').value==='grid'&&grid){grid.annotation={...grid.annotation,rating:Number($('rating').value),status:$('suitability').value};renderAtlas()}}}
for(const [label,status] of [['Mark square suitable','suitable'],['Mark square unsuitable','unsuitable']]){document.querySelector('aside.review').append(button(label,async()=>{if(!grid||!await saveIfDirty())return;$('scope').value='grid';await loadAnnotation();$('suitability').value=status;$('suitability').oninput();await saveIfDirty()}))}
const reportPanel=document.createElement('details');reportPanel.className='contrast';reportPanel.style.cssText='margin:10px 18px;background:white;border:1px solid #ccd7e4;border-radius:8px';
reportPanel.innerHTML='<summary>Export HTML screening report</summary><label>Screening images <select id="reportScope"><option value="representative">One suitable GridSquare</option><option value="targets">All suitable GridSquares</option><option value="all_screened">ALL screened GridSquares / exposures (may be large)</option></select></label><label><input id="reportHigh" type="checkbox" checked> High-resolution Atlas (reads Atlas MRC if available; embeds JPEG only)</label><button id="reportBuild">Build portable HTML</button><span id="reportStatus" role="status"></span><a id="reportDownload" hidden download>Download HTML report</a>';
document.querySelector('header').after(reportPanel);
$('reportBuild').onclick=async()=>{if(!await saveIfDirty())return;if($('reportScope').value==='all_screened'&&!confirm('Include every indexed screening exposure? This may take time and produce a very large HTML file.'))return;const activity=beginActivity('Building portable HTML report');$('reportBuild').disabled=true;$('reportDownload').hidden=true;$('reportStatus').textContent='Preparing embedded images…';try{const j=await api('/api/report-html',{scope:$('reportScope').value,high_resolution:$('reportHigh').checked});const r=await waitJob(j.job);$('reportDownload').href=r.url;$('reportDownload').hidden=false;$('reportStatus').textContent=r.warnings.length?'Ready with '+r.warnings.length+' warnings (listed in report).':'Ready — opens offline in any browser.'}catch(e){$('reportStatus').textContent='Export failed: '+e.message}finally{activity.finish();$('reportBuild').disabled=false}};
let targetLoadedAtlas='';const reviewStatusApi=api;api=async function(path,data){const result=await reviewStatusApi(path,data);if(path==='/api/status'&&result.atlas?.id&&targetLoadedAtlas!==result.atlas.id){targetLoadedAtlas=result.atlas.id;setTimeout(()=>loadAtlasTargets().catch(fail),0)}return result};
if(atlas)loadAtlasTargets().catch(fail);renderAtlas();
'''

REPORT_TOOLS = r'''<script>
const opacityLabel=document.createElement('label');opacityLabel.innerHTML='Overlay opacity <input id="report-opacity" aria-label="Atlas overlay opacity" type="range" min="0" max="100" value="100"><output>100%</output>';$('atlas-mode').after(opacityLabel);
const baseMarkers=markers;
const atlasLegend=document.createElement('div');atlasLegend.className='legend';atlasLegend.id='atlas-legend';atlasLegend.setAttribute('aria-live','polite');$('atlas-card').append(atlasLegend);
document.querySelector('header .legend').style.display='none';
function metadataTitle(r){return r.label+'\nSuitability: '+(r.status==='target'?'Manual target — needs screening':r.status==='suitable'?'Suitable':r.status==='unsuitable'?'Not suitable':'Unmarked')+'\nRating: '+(r.rating?r.rating+' / 5':'Unrated')+'\nComment: '+(r.comment||'None')+(r.screened?'':'\nNo screening evidence')}
function describeMarker(element,r,category){element.title=(category!=null?'EPU category '+category+'\n':'')+metadataTitle(r);element.setAttribute('aria-label',element.title)}
function updateAtlasLegend(mode){
 atlasLegend.replaceChildren();
 const swatch=(label,color)=>{const s=document.createElement('span');s.textContent=label;s.style.setProperty('--color',color);atlasLegend.append(s)};
 if(mode==='raw'){atlasLegend.textContent='Raw Atlas — no annotation overlays.';return}
 if(mode==='rating'){ratings.forEach((color,i)=>swatch(i?'Rating '+i:'Unrated',color))}
 else if(mode==='categories'){for(const [key,color] of Object.entries(plan.category_colors||{}))swatch('EPU category '+key,color);const note=document.createElement('small');note.textContent='Category colors are display colors, not the EPU UI palette.';atlasLegend.append(note)}
 else{swatch('Suitable',colors.suitable);swatch('Not suitable',colors.unsuitable);swatch('Unmarked',colors.unmarked)}
 if(plan.records.some(r=>r.status==='target'))swatch('Manual target / dashed area',colors.target);
 const note=document.createElement('small');note.textContent='Cyan outline = selected square · ! = no screening evidence. Hover a marker for suitability, rating and comments.';atlasLegend.append(note);
}
markers=function(){baseMarkers();const mode=$('atlas-mode').value,layer=$('markers');layer.style.opacity=Number($('report-opacity').value)/100;
 const positioned=plan.records.filter(r=>r.position);[...layer.children].forEach((b,i)=>{describeMarker(b,positioned[i]);if(positioned[i].status==='target')b.style.setProperty('--color',colors.target)});
 if(mode==='categories'){layer.replaceChildren();layer.hidden=false;for(const n of plan.category_nodes||[]){const r=plan.records.find(r=>String(r.atlas_key)===String(n.key));const c=document.createElement('button');c.className='marker'+(r&&r===current?' selected':'');c.style.left=n.x*100+'%';c.style.top=n.y*100+'%';c.style.setProperty('--color',plan.category_colors[n.category]||'#6366f1');if(r){describeMarker(c,r,n.category);c.textContent=String(r.number)+(r.screened?'':'!');c.onclick=()=>select(r)}else{c.title='GridSquare '+n.key+'\nEPU category '+n.category+'\nSuitability: Unmarked\nRating: Unrated\nComment: None\nNo mapped screening record';c.setAttribute('aria-label',c.title)}layer.append(c)}}
 if(mode==='categories'){for(const r of plan.records.filter(r=>r.status==='target'&&r.position)){const b=document.createElement('button');b.className='marker manual'+(r===current?' selected':'');b.style.left=r.position.x+'%';b.style.top=r.position.y+'%';b.style.setProperty('--color','#0891b2');b.textContent=r.number;describeMarker(b,r);b.onclick=()=>select(r);layer.append(b)}}
 if(mode!=='raw'){for(const r of plan.records.filter(r=>r.bounds)){const b=document.createElement('div');b.style.cssText='position:absolute;border:2px dashed #0891b2;pointer-events:none';b.style.left=r.bounds[0]*100+'%';b.style.top=r.bounds[1]*100+'%';b.style.width=(r.bounds[2]-r.bounds[0])*100+'%';b.style.height=(r.bounds[3]-r.bounds[1])*100+'%';layer.append(b)}}
 updateAtlasLegend(mode);
};
$('report-opacity').oninput=()=>{opacityLabel.querySelector('output').textContent=$('report-opacity').value+'%';markers()};
markers();
</script>'''
