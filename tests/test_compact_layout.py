"""Regression checks for the shared acquisition/FoilHole layout."""
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from acquisition_ui import PAGE


class CompactLayoutTests(unittest.TestCase):
    def test_review_bar_and_responsive_tools(self):
        self.assertIn('reviewBar.parentElement.insertBefore(reviewBar', PAGE)
        self.assertIn("['scope','rating','suitability','comment']", PAGE)
        self.assertIn('grid-column:2;grid-row:1;position:static', PAGE)
        self.assertIn('@media(max-width:1150px)', PAGE)
        self.assertIn('overlayMenu.append(groupLegend)', PAGE)
        self.assertNotIn("focusHole.hidden=config.mode==='foilhole'", PAGE)

    @unittest.skipUnless(shutil.which('node'), 'Node is required')
    def test_zoom_centers_selected_hole_for_numeric_or_string_ids(self):
        code = PAGE.split("const focusHole=button('Zoom to selected hole',()=>{", 1)[1].split('\n});', 1)[0]
        script = r'''
const assert=require('node:assert/strict');
let fitted=0;
const views={grid:{img:{naturalWidth:1000,naturalHeight:800},
 viewport:{getBoundingClientRect:()=>({width:500,height:400})},fit:()=>fitted++}};
let grid={markers:[{hole:42,x:.2,y:.7}]},hole='42';
function focus(){
''' + code + r'''
}
focus();
assert.equal(views.grid.zoom,6);
assert.equal(views.grid.x,900);
assert.ok(Math.abs(views.grid.y+480)<1e-8);
assert.equal(fitted,1);
hole='missing';focus();assert.equal(fitted,1);
views.grid.img.naturalWidth=0;hole='42';focus();assert.equal(fitted,1);
'''
        result = subprocess.run(['node'], input=script, text=True, encoding='utf-8', capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
