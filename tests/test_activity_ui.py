import subprocess
import shutil
import unittest
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from acquisition_ui import PAGE


@unittest.skipUnless(shutil.which('node'),'Node is required')
class ActivityTests(unittest.TestCase):
    def test_concurrent_activity_and_job_completion(self):
        helpers=PAGE.split('const activityStyle=',1)[1].split('function button(',1)[0]
        script=r'''
const assert=require('node:assert/strict');
class Element {
 constructor(){this.children=[];this.hidden=false;this.attributes={}}
 setAttribute(k,v){this.attributes[k]=v}
 append(c){this.children.push(c)}
 after(){}
 querySelector(){return this.children[0]||null}
}
const viewport=new Element();viewport.parentElement={id:'grid'};
const document={head:new Element(),createElement:()=>new Element(),querySelector:()=>new Element(),querySelectorAll:()=>[viewport]};
const setInterval=()=>{};
let reply={status:'done',result:42};
const fetch=async()=>({ok:true,json:async()=>reply});
const activityStyle='''+helpers+r'''
(async()=>{
 const first=beginActivity('First','grid');
 const second=beginActivity('Second','grid');
 assert.equal(viewport.attributes['aria-busy'],'true');
 first.finish();assert.match(activitySummary.textContent,/Second/);
 assert.doesNotMatch(activitySummary.textContent,/First/);
 second.finish();assert.equal(activitySummary.hidden,true);
 assert.equal(viewport.attributes['aria-busy'],'false');
 const old=beginActivity('Stale selection','grid',()=>false);
 assert.equal(activitySummary.hidden,true);old.finish();
 jobActivities.set('j',['Mapping particle density','grid']);
 assert.equal(await waitJob('j'),42);
 assert.equal(activities.size,0);
 reply={status:'error',message:'Metadata unavailable'};
 await assert.rejects(waitJob('bad'),/Metadata unavailable/);
 assert.equal(activities.size,0);
 assert.equal(await waitJob('cancelled',()=>false),null);
 assert.equal(activities.size,0);
 assert.equal(activitySummary.hidden,true);
 console.log('Activity cleanup, concurrent jobs, cancellation and errors passed');
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        result=subprocess.run(['node'],input=script,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
