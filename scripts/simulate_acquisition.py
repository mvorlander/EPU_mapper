"""Generate clearly labelled synthetic EPU previews for performance/UI tests."""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw


NS = 'http://schemas.datacontract.org/2004/07/'


def image_xml(width=1024, height=1024, x=0., y=0., pixel=1e-8):
    return f'''<MicroscopeImage xmlns:so="{NS}Fei.SharedObjects"><ReadoutArea><width>{width}</width><height>{height}</height></ReadoutArea><so:microscopeData><so:stage><so:Position><so:X>{x}</so:X><so:Y>{y}</so:Y></so:Position></so:stage></so:microscopeData><so:SpatialScale><so:pixelSize><so:x><so:numericValue>{pixel}</so:numericValue></so:x></so:pixelSize></so:SpatialScale><Defocus>-0.000003</Defocus><ExposureTime>2.5</ExposureTime></MicroscopeImage>'''


def generate(destination, images=10000, squares=20, holes_per_square=100, grid_mrc=True):
    if images < 1 or squares < 1 or not 1 <= holes_per_square <= 100:
        raise ValueError('Images and squares must be positive; holes per square must be 1–100')
    if squares > 20:
        raise ValueError('The synthetic atlas supports at most 20 GridSquares')
    capacity = squares * sum(4 + hole % 5 for hole in range(holes_per_square))
    if images > capacity:
        raise ValueError(f'Requested {images} exposures exceed fixture capacity {capacity}')
    destination=Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    session=destination/'Supervisor_SIMULATED_Acquisition'
    disc=session/'Images-Disc1'
    metadata=session/'Metadata'
    atlas=destination/'Atlas'
    for folder in (disc,metadata,atlas):folder.mkdir(parents=True)
    (destination/'SIMULATED_DATA.json').write_text(json.dumps(dict(simulated=True,requested_images=images,description='Synthetic EPU Mapper validation data; not microscope acquisitions'),indent=2))
    (destination/'README.txt').write_text('SIMULATED EPU acquisition for EPU Mapper tests. NOT experimental data.\nData: JPEG previews + XML only. GridSquare/Atlas MRCs are synthetic optional images.\n')
    (session/'EpuSession.dm').write_text(image_xml())
    rng=np.random.default_rng(41)
    noise=np.clip(rng.normal(130,22,(256,256)),0,255).astype('uint8')
    atlas_image=Image.new('RGB',(1024,1024),'#14202c');ad=ImageDraw.Draw(atlas_image)
    atlas_nodes=[];total=0;physical=0
    start=datetime(2026,9,4,10)
    for square in range(squares):
        if total>=images:break
        gid=str(900000+square)
        gdir=disc/f'GridSquare_{gid}';gdir.mkdir()
        for folder in ('FoilHoles','Data'):(gdir/folder).mkdir()
        ax=110+(square%5)*190;ay=110+(square//5)*190
        ad.rectangle((ax-60,ay-60,ax+60,ay+60),fill='#7c8c9c');ad.text((ax-42,ay-8),f'SIM {gid}',fill='white')
        atlas_nodes.append(f'<KeyValuePairOfintNodeXml><key>{gid}</key><value><Category>1</Category><PositionOnTheAtlas><Center><x>{ax}</x><y>{ay}</y></Center></PositionOnTheAtlas></value></KeyValuePairOfintNodeXml>')
        grid=Image.new('L',(1024,1024),35);draw=ImageDraw.Draw(grid)
        grid_name='GridSquare_'+(start+timedelta(seconds=total*4)).strftime('%Y%m%d_%H%M%S')
        targets=[]
        for hole in range(holes_per_square):
            if total>=images:break
            fid=str(100000000+square*1000+hole)
            x=100+(hole%10)*90;y=100+(hole//10)*90
            draw.ellipse((x-20,y-20,x+20,y+20),fill=190)
            physical+=1
            foil=Image.fromarray(noise.copy());fd=ImageDraw.Draw(foil)
            fd.ellipse((60,60,195,195),outline=230,width=5);fd.text((8,8),f'SIMULATED\nGS {gid}\nFH {fid}',fill=255)
            foil_stamp=(start+timedelta(seconds=total*4)).strftime('%Y%m%d_%H%M%S')
            foil_path=gdir/'FoilHoles'/f'FoilHole_{fid}_{foil_stamp}.jpg'
            foil.save(foil_path,quality=80)
            foil_path.with_suffix('.xml').write_text(image_xml(256,256,(x-512)*1e-8,(512-y)*1e-8))
            targets.append(f'<g:KeyValuePairOfintTargetLocationXmlBpEWF4JT><g:key>{fid}</g:key><g:value><tp:Id>{fid}</tp:Id><p:PixelCenter><a:x>{x}</a:x><a:y>{y}</a:y></p:PixelCenter></g:value></g:KeyValuePairOfintTargetLocationXmlBpEWF4JT>')
            for shot in range(4+(hole%5)):
                if total>=images:break
                total+=1
                stamp=(start+timedelta(seconds=total*4)).strftime('%Y%m%d_%H%M%S')
                name=f'FoilHole_{fid}_Data_70000_{shot}_{stamp}.jpg'
                data=Image.fromarray(np.roll(noise,shot*7,axis=0));dd=ImageDraw.Draw(data)
                dd.text((8,8),f'SIMULATED\nGS {gid}\nFH {fid}\nExposure {shot+1}',fill=255)
                data.save(gdir/'Data'/name,quality=80)
                (gdir/'Data'/Path(name).with_suffix('.xml')).write_text(image_xml(256,256,pixel=1e-10))
                if total%250==0:print(f'Simulated exposures: {total}/{images}',flush=True)
        draw.text((25,25),f'SIMULATED GridSquare {gid}',fill=255)
        grid.save(gdir/(grid_name+'.jpg'),quality=90)
        (gdir/(grid_name+'.xml')).write_text(image_xml())
        if grid_mrc:
            import mrcfile
            with mrcfile.new(gdir/(grid_name+'.mrc')) as m:m.set_data(np.asarray(grid,dtype='float32'))
        (metadata/f'GridSquare_{gid}.dm').write_text(f'<root xmlns:p="{NS}Applications.Epu.Persistence" xmlns:g="{NS}System.Collections.Generic" xmlns:tp="{NS}Fei.Applications.Common.Types" xmlns:a="{NS}System.Drawing">'+''.join(targets)+'</root>')
    atlas_image.save(atlas/'Atlas_SIMULATED.jpg',quality=90)
    (atlas/'Atlas_SIMULATED.xml').write_text(image_xml())
    (atlas/'Atlas.dm').write_text('<root>'+''.join(atlas_nodes)+'</root>')
    if grid_mrc:
        import mrcfile
        with mrcfile.new(atlas/'Atlas_SIMULATED.mrc') as m:m.set_data(np.asarray(atlas_image.convert('L'),dtype='float32'))
    if total!=images:raise ValueError(f'Insufficient configured squares/holes: generated {total} of {images}')
    print(json.dumps(dict(path=str(destination),session=str(session),atlas=str(atlas),images=total,holes=physical)),flush=True)
    return session,atlas


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination',type=Path,help='New directory; never overwrites an existing destination')
    parser.add_argument('--images',type=int,default=10000)
    parser.add_argument('--squares',type=int,default=20)
    args=parser.parse_args()
    generate(args.destination,args.images,args.squares)
