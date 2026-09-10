import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from acquisition_ui import PAGE


class AtlasOverlayTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'),'Node is required')
    def test_active_grid_outline_follows_selection_and_redraw(self):
        code='function drawActiveGridOutline'+PAGE.split('function drawActiveGridOutline',1)[1].split('function renderGrids',1)[0]
        script=r'''
const assert=require('node:assert/strict');
const document={createElementNS:()=>({style:{},setAttribute(k,v){this[k]=v}})};
const svg={children:[],style:{opacity:'0.5'},append(c){this.children.push(c)}};
const views={atlas:{svg,markers:[],mark(ms,click){this.markers=ms;svg.children=[];this.click=click}}};
const config={mode:'acquisition'},atlasFrameNotice={};
const atlas={width:4005,height:4005,nodes:{a:{center:[1000,2000]},b:{center:[3000,1000]}}};
const grids=[{id:'a',name:'GridSquare_a',annotation:{status:'suitable'},exposures:3},{id:'b',name:'GridSquare_b',annotation:{status:'unsuitable'},exposures:2}];
let grid=grids[0];
function selectGrid(id){grid=grids.find(g=>g.id===id);renderAtlas()}
'''+code+r'''
renderAtlas();
assert.equal(svg.children.length,2);
assert.equal(svg.children[1].cx,1000/4005);
assert.equal(svg.children[1].style.stroke,'#ffffff');
assert.equal(svg.children[1].style.fill,'none');
assert.equal(svg.children[1].style.pointerEvents,'none');
assert.equal(svg.children[1]['vector-effect'],'non-scaling-stroke');
assert.equal(views.atlas.markers[0].stroke,'#36c792');
views.atlas.click({id:'b'});
assert.equal(svg.children.length,2);
assert.equal(svg.children[1].cx,3000/4005);
assert.equal(views.atlas.markers[1].stroke,'#ef5963');
renderAtlas();assert.equal(svg.children.length,2);
assert.equal(svg.style.opacity,'0.5');
grid=null;renderAtlas();assert.equal(svg.children.length,0);
grid={id:'unmapped'};renderAtlas();assert.equal(svg.children.length,0);
'''
        result=subprocess.run(['node'],input=script,text=True,encoding='utf-8',capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_screening_mrc_keeps_marker_layer(self):
        source=(ROOT/'src/review_app.py').read_text()
        self.assertIn("markerLayer.style.display=(atlasMode==='screened'||atlasMode==='mrc')?'block':'none'",source)
        self.assertIn('aria-label="Atlas overlay opacity"',(ROOT/'src/dashboard_features.py').read_text())

    @unittest.skipUnless(shutil.which('node'),'Node is required')
    def test_opacity_applies_to_overlay_container_not_individual_markers(self):
        helper='function setAtlasOverlayOpacity'+PAGE.split('function setAtlasOverlayOpacity',1)[1].split('\ntry{setAtlasOverlayOpacity',1)[0]
        script=r'''
const assert=require('node:assert/strict');
const input={},output={},saved={};
const atlasOverlayControl={querySelector:s=>s==='input'?input:output};
const views={atlas:{svg:{style:{},replaceChildren(){}}}};
const localStorage={setItem:(k,v)=>saved[k]=v};
'''+helper+r'''
setAtlasOverlayOpacity(35);
assert.equal(views.atlas.svg.style.opacity,'0.35');
assert.equal(output.textContent,'35%');
assert.equal(saved['epu-atlas-overlay-opacity'],'35');
views.atlas.svg.replaceChildren();
assert.equal(views.atlas.svg.style.opacity,'0.35');
setAtlasOverlayOpacity(0);assert.equal(views.atlas.svg.style.pointerEvents,'none');
setAtlasOverlayOpacity(100);assert.equal(views.atlas.svg.style.pointerEvents,'');
setAtlasOverlayOpacity('invalid');assert.equal(input.value,100);
'''
        result=subprocess.run(['node'],input=script,text=True,encoding='utf-8',capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
