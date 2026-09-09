import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from acquisition_ui import PAGE


@unittest.skipUnless(shutil.which('node'), 'Node is needed for UI regression checks')
class FoilOverlayTests(unittest.TestCase):
    def test_beam_shift_message_distinguishes_unknown_relationship(self):
        helper='function showFoilForExposure'+PAGE.split('function showFoilForExposure',1)[1].split('async function showShot',1)[0]
        script="const assert=require('node:assert/strict');let hole='2',grid={markers:[{hole:'2',role:'shifted',anchor:'1'}]},message='',loaded='';const views={foil:{clear(m){message=m},load(id){loaded=id}}};\n"+helper+"""
showFoilForExposure({foil:''});
assert.match(message,/Beam-shift collection/);
assert.match(message,/centering hole 1/);
assert.match(message,/Data images are available/);
grid.markers=[];
showFoilForExposure({foil:''});
assert.match(message,/not confirmed/);
assert.doesNotMatch(message,/No separate FoilHole image is expected/);
showFoilForExposure({foil:'real-preview'});
assert.equal(loaded,'real-preview');
"""
        result=subprocess.run(['node'],input=script,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_outline_radius_and_selection(self):
        helpers = PAGE.split('function normalizeOverlay', 1)[1].split('class Viewer{', 1)[0]
        script = "const assert=require('node:assert/strict');\nfunction normalizeOverlay" + helpers
        script += """
const circle={style:{},setAttribute(k,v){this[k]=v}};
styleFoilCircle(circle,false);
assert.equal(circle.r,.012);
assert.equal(circle.style.fill,'none');
assert.equal(circle.style.pointerEvents,'all');
styleFoilCircle(circle,true);
assert.equal(circle.style.fill,'none');
assert.equal(circle.style.stroke,'#f2c450');
foilOverlay=normalizeOverlay({style:'filled',radius:2.5});
styleFoilCircle(circle,false);
assert.equal(circle.r,.025);
assert.equal(circle.style.fill,'#248f84');
styleFoilCircle(circle,true);
assert.equal(circle.style.fill,'#f2c450');
assert.equal(normalizeOverlay({radius:-2}).radius,.2);
assert.equal(normalizeOverlay({radius:99}).radius,4);
assert.equal(normalizeOverlay({radius:'bad'}).radius,1.2);
styleFoilCircle(circle,false,{anchor:'123',role:'anchor'});
const anchorColor=circle.style.stroke;
assert.equal(circle.style.strokeDasharray,'');
styleFoilCircle(circle,true,{anchor:'123',role:'shifted'});
assert.equal(circle.style.stroke,anchorColor);
assert.equal(circle.style.strokeDasharray,'.006 .004');
"""
        result = subprocess.run(['node'], input=script, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_page_syntax_and_control_wiring(self):
        script = PAGE.split('<script>')[1].split('</script>')[0].replace('__CONFIG__', '{}')
        result = subprocess.run(['node', '--check'], input=script, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("if(this.id==='grid')styleFoilCircle(c,m.selected,m)", script)
        self.assertIn("$('foilRadiusSlider').oninput", script)
        self.assertIn("views.grid.card.append(overlayMenu)", script)
        self.assertIn("if(this.id==='grid'&&config.mode==='foilhole')c.onpointerenter=",script)
        self.assertIn("p.setAttribute('vector-effect','non-scaling-stroke')",script)
        self.assertIn("Zoom to selected hole",script)
        self.assertIn("No Data areas could be placed.",script)
        self.assertIn("m?.role==='shifted'",script)
        self.assertIn("No separate FoilHole image is expected",script)
        self.assertIn("centering relationship is not confirmed",script)


if __name__ == '__main__':
    unittest.main()
