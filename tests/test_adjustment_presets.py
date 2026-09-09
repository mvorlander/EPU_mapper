import shutil
import subprocess
import unittest
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from acquisition_ui import PAGE


@unittest.skipUnless(shutil.which('node'),'Node is required')
class AdjustmentPresetTests(unittest.TestCase):
    def test_preset_values_and_reset(self):
        helper='function adjustmentPreset'+PAGE.split('function adjustmentPreset',1)[1].split('const adjustmentStyle=',1)[0]
        script=r'''
const assert=require('node:assert/strict');
const fields=Object.fromEntries(['low','high','gamma','sigma','routine','gamma-value','sigma-value','adjustment-badge','adjustment-status'].map(k=>[k,{value:''}]));
fields.sigma.value=2;
const buttons=['robust','strong','full','equalize','reset'].map(preset=>({dataset:{preset}}));
const panel={classList:{add(){}},querySelector:s=>fields[s.slice(1)],querySelectorAll:s=>s==='input'?['low','high','gamma','sigma'].map(k=>fields[k]):buttons};
let calls=[];
const view={key:'image',name:'preview',mrcKey:'',isMrc:false,img:{hidden:false},card:{querySelector:()=>panel},load:async(...args)=>{calls.push(args)}};
'''+helper+r'''
setupImageAdjustments(view);
buttons[0].onclick();assert.equal(fields.low.value,1);assert.equal(fields.high.value,99);assert.equal(fields.sigma.value,2);
buttons[1].onclick();assert.equal(fields.low.value,2);assert.equal(fields.high.value,98);
buttons[2].onclick();assert.equal(fields.low.value,0);assert.equal(fields.high.value,100);
buttons[3].onclick();assert.equal(fields.routine.value,'equalize');assert.equal(calls.at(-1)[4],true);
buttons[4].onclick();assert.equal(view.adjustmentEnabled,false);assert.equal(fields.sigma.value,0);assert.equal(fields.gamma.value,1);assert.equal(calls.at(-1)[4],false);
assert.equal(fields['adjustment-badge'].textContent,'Original preview');
'''
        result=subprocess.run(['node'],input=script,text=True,encoding='utf-8',capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('Object.values(views).forEach(setupImageAdjustments)',PAGE)
