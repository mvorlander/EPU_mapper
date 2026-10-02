from pathlib import Path
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from collection_plan import PLAN_HTML


class ReportHoleOverlayTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'),'Node required')
    def test_selection_navigation_labels_and_independent_opacity(self):
        code=PLAN_HTML.split('function updateActiveHole()',1)[1].split("$('search').oninput",1)[0]
        code='function updateActiveHole()'+code
        setup=r'''
const assert=require('node:assert/strict');
class Element{
 constructor(){this.children=[];this.style={setProperty(){}};this.dataset={};this.attributes={};this.textContent='';this.className='';this.classList={toggle:(name,on)=>{const classes=new Set(this.className.split(' '));on?classes.add(name):classes.delete(name);this.className=[...classes].join(' ')}}}
 append(node){this.children.push(node)} replaceChildren(){this.children=[]} setAttribute(k,v){this.attributes[k]=v}
}
const nodes={},$=id=>nodes[id]||(nodes[id]=new Element()),document={createElement:()=>new Element()};
const holeLayer=new Element(),plan={assets:{}},fv={set(){}},dv={set(){}},gv={set(){}};
let current=null,pairIndex=0;function markers(){}function list(){}
const record={label:'Square',source:'test',details:true,priority:'primary',screened:true,status:'',rating:0,comment:'',pairs:[{id:'A',foil_name:'a',data_name:'a1'},{id:'A',foil_name:'a',data_name:'a2'},{id:'B',foil_name:'b',data_name:'b1'}],hole_markers:[{x:20,y:30,foil_name:'a',label:'A'},{x:60,y:70,foil_name:'b',label:'B'}]};
$('markers').style.opacity='0.8';
'''
        checks=r'''
select(record);
const active=()=>holeLayer.children.filter(m=>m.attributes['aria-pressed']==='true').map(m=>m.dataset.hole);
assert.deepEqual(active(),['A']);
assert.ok(holeLayer.children.every(m=>m.textContent===''));
assert.equal(holeLayer.children[0].attributes['aria-label'],'Show FoilHole A');
$('next').onclick();assert.deepEqual(active(),['A']);
$('next').onclick();assert.deepEqual(active(),['B']);
$('previous').onclick();assert.deepEqual(active(),['A']);
holeLayer.children[1].onclick();assert.equal(pairIndex,2);assert.deepEqual(active(),['B']);
setGridOverlayOpacity(35);assert.equal(holeLayer.style.opacity,'0.35');assert.equal($('markers').style.opacity,'0.8');assert.equal($('grid-opacity-value').textContent,'35%');
select(record);assert.equal(holeLayer.style.opacity,'0.35');assert.deepEqual(active(),['A']);
setGridOverlayOpacity(0);assert.equal(holeLayer.style.pointerEvents,'none');
setGridOverlayOpacity(100);assert.equal(holeLayer.style.pointerEvents,'');
select({...record,pairs:[],hole_markers:[]});assert.deepEqual(active(),[]);
'''
        result=subprocess.run(['node'],input=setup+code+checks,text=True,encoding='utf-8',capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
