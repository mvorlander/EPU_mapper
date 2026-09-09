"""Validate a finished synthetic fixture without opening every network image."""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import xml.etree.ElementTree as ET
from PIL import Image


def validate(destination):
    root = Path(destination)
    manifest = json.loads((root / 'SIMULATED_DATA.json').read_text())
    if manifest.get('simulated') is not True:
        raise ValueError('Not a marked simulated dataset')
    session = root / 'Supervisor_SIMULATED_Acquisition'
    ET.parse(session / 'EpuSession.dm')
    with os.scandir(session / 'Images-Disc1') as entries:
        grids = sorted(entry.name for entry in entries if entry.name.startswith('GridSquare_'))
    exposures, holes, sampled = 0, 0, 0
    mrc_files = []
    distribution = Counter()
    for name in grids:
        grid = session / 'Images-Disc1' / name
        with os.scandir(grid / 'Data') as entries:
            data = {entry.name for entry in entries}
        with os.scandir(grid / 'FoilHoles') as entries:
            foil = {entry.name for entry in entries}
        jpgs = sorted(n for n in data if n.endswith('.jpg'))
        foils = sorted(n for n in foil if n.endswith('.jpg'))
        assert len(jpgs) == sum(n.endswith('.xml') for n in data), f'Incomplete Data pairs: {name}'
        assert all(Path(n).with_suffix('.xml').name in data for n in jpgs), name
        assert all(Path(n).with_suffix('.xml').name in foil for n in foils), name
        assert not any(n.lower().endswith(('.mrc', '.mrcs', '.eer')) for n in data), name
        foil_ids = {n.split('_')[1] for n in foils}
        counts = Counter(n.split('_')[1] for n in jpgs)
        assert set(counts) <= foil_ids, f'Data without FoilHole: {name}'
        distribution.update(counts.values())
        exposures += len(jpgs)
        holes += len(foil_ids)
        targets = ET.parse(session / 'Metadata' / (name + '.dm'))
        target_ids = {e.text for e in targets.iter() if e.tag.endswith('}Id')}
        assert target_ids == foil_ids, f'FoilHole metadata mismatch: {name}'
        with os.scandir(grid) as entries:
            grid_files = [grid / e.name for e in entries]
        grid_images = [path for path in grid_files if path.suffix == '.jpg']
        mrc_files.extend(path for path in grid_files if path.suffix == '.mrc')
        assert len(grid_images) == 1, f'Missing/ambiguous GridSquare image: {name}'
        samples = grid_images + [grid / 'Data' / jpgs[0], grid / 'Data' / jpgs[-1], grid / 'FoilHoles' / foils[0]]
        for path in samples:
            with Image.open(path) as image:
                image.load()
            ET.parse(path.with_suffix('.xml'))
            sampled += 1
    assert exposures == manifest['requested_images'], (exposures, manifest['requested_images'])
    with Image.open(root / 'Atlas/Atlas_SIMULATED.jpg') as image:
        image.load()
    atlas = ET.parse(root / 'Atlas/Atlas.dm')
    assert len(atlas.getroot()) == len(grids), 'Atlas/GridSquare count mismatch'
    atlas_mrc = root / 'Atlas/Atlas_SIMULATED.mrc'
    if atlas_mrc.is_file():
        mrc_files.append(atlas_mrc)
    if mrc_files:
        import mrcfile
        for path in mrc_files:
            with mrcfile.mmap(path, mode='r', permissive=False) as image:
                assert image.data.shape == (1024, 1024), f'Invalid synthetic MRC dimensions: {path}'
    result = dict(simulated=True, grids=len(grids), holes=holes, data_images=exposures,
                  data_mrc_files=0, sampled_images_verified=sampled + 1,
                  overview_mrc_headers_verified=len(mrc_files),
                  exposures_per_hole=dict(sorted(distribution.items())))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    print(json.dumps(validate(args.destination), indent=2))
