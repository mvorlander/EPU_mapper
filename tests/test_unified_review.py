import contextlib
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT)]
from fastapi.testclient import TestClient
from acquisition_app import create_acquisition_app
from scripts.simulate_acquisition import generate
from unified_review import build_report,report_filename,REVIEW_TOOLS,REPORT_TOOLS


class UnifiedReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        with contextlib.redirect_stdout(io.StringIO()):
            self.source,self.atlas=generate(self.root/'fixture',images=12,squares=2,holes_per_square=2,grid_mrc=True)
        self.app=create_acquisition_app(self.source,self.atlas,cache_root=self.root/'cache')
        self.client=TestClient(self.app);self.client.__enter__()
        until=time.monotonic()+20
        while self.app.state.acquisition_store.scan_state['status'] in ('new','indexing') and time.monotonic()<until:
            time.sleep(.01)
        self.store=self.app.state.acquisition_store
        self.assertEqual(self.store.scan_state['status'],'ready')

    def tearDown(self):
        self.client.__exit__(None,None,None);self.temp.cleanup()

    def payload(self,html):
        return json.loads(re.search(r'<script type="application/json" id="plan-data">(.*?)</script>',html,re.S).group(1))

    def test_target_validation_persistence_and_deletion(self):
        atlas_id=self.store.meta('atlas')['id']
        self.assertEqual(self.client.post('/api/atlas-targets',json=dict(atlas_id='wrong',x=.2,y=.3)).status_code,409)
        self.assertEqual(self.client.post('/api/atlas-targets',json=dict(atlas_id=atlas_id,x=2,y=.3)).status_code,400)
        t=self.client.post('/api/atlas-targets',json=dict(atlas_id=atlas_id,x=.3,y=.4,bounds=[.2,.3,.4,.5],comment='target')).json()
        self.assertEqual(self.client.get('/api/atlas-targets').json()[0]['id'],t['id'])
        self.assertIn('atlas-target:'+t['id'],self.store.annotation_snapshot())
        self.assertEqual(self.client.delete('/api/atlas-targets/'+t['id']).status_code,200)
        self.assertEqual(self.client.get('/api/atlas-targets').json(),[])

    def test_clear_session_annotations_requires_confirmation_and_keeps_backup(self):
        g=self.store.grids()[0]
        self.store.annotate('grid:'+g['id'],dict(rating=5,status='suitable',comment='keep in backup'))
        self.store.annotate('hole:'+g['id']+':example',dict(flag=True))
        self.store.execute('INSERT INTO annotations VALUES (?,?)',('position:example',json.dumps(dict(note='shift'))))
        self.client.post('/api/atlas-targets',json=dict(atlas_id=self.store.meta('atlas')['id'],x=.3,y=.4))
        before=self.store.annotation_snapshot()
        self.assertEqual(self.client.post('/api/annotations/clear',json={}).status_code,400)
        self.assertEqual(self.store.annotation_snapshot(),before)
        self.store.set_meta('legacy-review:'+g['id'],dict(rating=5))
        body=dict(confirm='DELETE ALL ANNOTATIONS',source=str(self.source))
        result=self.client.post('/api/annotations/clear',json=body)
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['deleted'],4)
        backup=self.client.get(result.json()['backup_url'])
        self.assertEqual(backup.json()['annotations'],before)
        self.assertEqual(self.store.annotation_snapshot(),{})
        self.assertTrue(self.store.meta('user-annotations-cleared'))
        self.assertIsNone(self.store.meta('legacy-review:'+g['id']))
        self.assertEqual(len(self.store.grids()),2)
        self.assertTrue(self.store.meta('atlas')['nodes'])
        # Even a refresh must not restore the legacy source annotations.
        parent=Path(self.store.grid(g['id'])['path']).parent
        (parent/'review_responses.json').write_text(json.dumps({g['name']:dict(rating=5)}))
        self.store.import_screening_reviews()
        self.assertEqual(self.store.annotation_snapshot(),{})

    def test_failed_backup_does_not_clear_annotations(self):
        from unittest.mock import patch
        self.store.annotate('grid:test',dict(comment='preserve'))
        before=self.store.annotation_snapshot()
        with patch.object(Path,'write_text',side_effect=OSError('Disk full')):
            with self.assertRaises(OSError):
                self.store.clear_user_annotations()
        self.assertEqual(self.store.annotation_snapshot(),before)
        self.assertFalse(self.store.meta('user-annotations-cleared',False))

    def test_report_includes_live_reviews_targets_jpegs_and_opacity(self):
        g=self.store.grids()[0]
        self.store.annotate('grid:'+g['id'],dict(rating=5,status='suitable',comment='</script><script>bad()</script>'))
        self.client.post('/api/atlas-targets',json=dict(atlas_id=self.store.meta('atlas')['id'],x=.3,y=.4,bounds=[.2,.3,.4,.5],comment='manual'))
        html,warnings=build_report(self.store,'test')
        data=self.payload(html)
        self.assertEqual(sum(r['details'] for r in data['records']),1)
        self.assertEqual(data['records'][0]['rating'],5)
        self.assertTrue(data['records'][0]['pairs'])
        self.assertTrue(data['atlas'].startswith('data:image/jpeg;base64,'))
        self.assertTrue(all(v.startswith('data:image/jpeg;base64,') for v in data['assets'].values() if v))
        self.assertEqual(data['records'][-1]['bounds'],[.2,.3,.4,.5])
        self.assertNotIn('</script><script>bad()',html)
        self.assertIn('Atlas overlay opacity',html)
        self.assertTrue(data['category_nodes'])
        self.assertIn('Raw Atlas',html)
        self.assertEqual(warnings,[])
        all_html,_=build_report(self.store,'test','all_screened',False)
        self.assertEqual(sum(len(r['pairs']) for r in self.payload(all_html)['records']),12)

    def test_export_job_download_and_script_syntax(self):
        response=self.client.post('/api/report-html',json=dict(scope='all_screened',high_resolution=False)).json()
        until=time.monotonic()+20
        while time.monotonic()<until:
            job=self.client.get('/api/jobs/'+response['job']).json()
            if job['status'] in ('done','error'):
                break
            time.sleep(.02)
        self.assertEqual(job['status'],'done',job)
        download=self.client.get(job['result']['url'])
        self.assertEqual(download.status_code,200)
        self.assertIn(self.source.name+'-screening-report.html',download.headers['content-disposition'])
        self.assertEqual(self.client.get('/review-tools.js').status_code,200)
        if shutil.which('node'):
            for script in (REVIEW_TOOLS,REPORT_TOOLS.removeprefix('<script>').removesuffix('</script>')):
                result=subprocess.run(['node','--check'],input=script,text=True,encoding='utf-8',capture_output=True)
                self.assertEqual(result.returncode,0,result.stderr)

    def test_session_report_filename_is_portable(self):
        self.assertEqual(report_filename('MySession'),'MySession-screening-report.html')
        self.assertEqual(report_filename('../Grid:1\\test?'),'_Grid_1_test_-screening-report.html')
        self.assertEqual(report_filename('...'),'EPU-session-screening-report.html')
        self.assertEqual(report_filename('Gríd α'),'Gríd α-screening-report.html')

    def test_dashboard_legend_swatches(self):
        if not shutil.which('node'):
            self.skipTest('Node unavailable')
        function='function renderAtlasLegend(mode){'+REVIEW_TOOLS.split('function renderAtlasLegend(mode){',1)[1].split('\nlet manualTargets',1)[0]
        script=r'''
const assert=require('node:assert/strict');
class Element {constructor(){this.children=[];this.style={}} append(...nodes){this.children.push(...nodes)} replaceChildren(){this.children=[]} setAttribute(){}}
const document={createElement:()=>new Element()},atlasLegend=new Element();
const ratingColors=['#64748b','#dc2626','#f97316','#ca8a04','#65a30d','#15803d'],categoryColors={2:'#3b82f6'};
'''+function+r'''
function item(label){return atlasLegend.children.find(e=>e.children[1]?.textContent===label).children[0]}
renderAtlasLegend('rating');
assert.equal(item('Rating 1').style.background,'#dc2626');
assert.equal(item('Rating 5').style.background,'#15803d');
assert.equal(item('Suitable outline').style.background,'transparent');
assert.equal(item('Suitable outline').style.border,'2px solid #059669');
assert.equal(item('Manual target').style.border,'2px dashed #0891b2');
assert.equal(item('Active square').style.border,'2px solid #fff');
renderAtlasLegend('status');
assert.equal(item('Suitable').style.background,'#059669');
assert.equal(item('Unsuitable').style.background,'#dc2626');
assert.equal(item('Unmarked').style.background,'#64748b');
renderAtlasLegend('raw');assert.equal(atlasLegend.children.length,0);
'''
        result=subprocess.run(['node'],input=script,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_report_legends_and_hover_metadata(self):
        if not shutil.which('node'):
            self.skipTest('Node unavailable')
        setup=r'''
const assert=require('node:assert/strict');
class Element {
 constructor(){this.children=[];this.style={setProperty(k,v){this[k]=v}};this.textContent='';this.attributes={}}
 append(e){this.children.push(e)}
 after(){}
 replaceChildren(){this.children=[];this.textContent=''}
 setAttribute(k,v){this.attributes[k]=v}
 querySelector(){return new Element()}
}
const nodes={};const $=id=>nodes[id]||(nodes[id]=new Element());
const document={createElement:()=>new Element(),querySelector:()=>new Element()};
const colors={suitable:'#059669',unsuitable:'#dc2626',target:'#0891b2',unmarked:'#64748b'};
const ratings=['#64748b','#dc2626','#f97316','#ca8a04','#65a30d','#15803d'];
const record={label:'GridSquare 12',atlas_key:'12',number:1,status:'suitable',rating:4,comment:'Keep <this> & check ice',screened:true,position:{x:20,y:30}};
const plan={records:[record],category_nodes:[{key:'12',x:.2,y:.3,category:2}],category_colors:{2:'#3b82f6'}};
let current=record;function select(r){current=r}
function markers(){$('markers').replaceChildren();plan.records.filter(r=>r.position).forEach(()=>$('markers').append(new Element()))}
$('atlas-mode').value='annotated';$('report-opacity').value='50';
'''
        checks=r'''
const legendText=()=>$('atlas-card').children[0].children.map(e=>e.textContent).join(' ');
assert.match(legendText(),/Suitable/);assert.doesNotMatch(legendText(),/EPU category/);
assert.match($('markers').children[0].title,/Suitability: Suitable\nRating: 4 \/ 5\nComment: Keep <this> & check ice/);
assert.equal($('markers').style.opacity,.5);
$('atlas-mode').value='rating';markers();assert.match(legendText(),/Rating 1/);assert.match(legendText(),/Rating 5/);assert.doesNotMatch(legendText(),/EPU category/);
$('atlas-mode').value='categories';markers();assert.match(legendText(),/EPU category 2/);assert.doesNotMatch(legendText(),/Rating 5/);
assert.match($('markers').children[0].title,/EPU category 2\nGridSquare 12/);
assert.match($('markers').children[0].title,/Keep <this> & check ice/);
$('markers').children[0].onclick();assert.equal(current,record);
$('atlas-mode').value='raw';markers();assert.equal($('atlas-card').children[0].textContent,'Raw Atlas — no annotation overlays.');
'''
        result=subprocess.run(['node'],input=setup+REPORT_TOOLS.removeprefix('<script>').removesuffix('</script>')+checks,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
