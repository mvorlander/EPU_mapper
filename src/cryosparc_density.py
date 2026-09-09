"""Optional, file-only CryoSPARC density import. No particle pixels are read."""
from collections import defaultdict
import re
import threading
from pathlib import Path

import numpy as np


def exposure_key(path):
    if isinstance(path, bytes):
        path = path.decode('utf-8', errors='replace')
    # Preserve hole, acquisition-area, setting and timestamp; ignore only the
    # downstream EER/alignment/denoising suffix, never match by hole alone.
    match = re.search(r'(FoilHole_\d+_Data_[\d_]+?_20\d{6}_\d{6})(?=[_.]|$)', str(path))
    return match.group(1) if match else None


def load_locations(main_path, passthrough_path=None):
    main = np.load(main_path, allow_pickle=False)
    required = ('location/micrograph_path', 'location/center_x_frac', 'location/center_y_frac')
    if not main.dtype.names or 'uid' not in main.dtype.names:
        raise ValueError('Expected a CryoSPARC structured .cs particle table with uid.')
    if len(np.unique(main['uid'])) != len(main):
        raise ValueError('Duplicate particle UIDs: refusing to count particles twice.')
    other = None
    order = None
    if any(field not in main.dtype.names for field in required):
        if not passthrough_path:
            raise ValueError('Location fields are missing. Supply the matching passthrough .cs file.')
        other = np.load(passthrough_path, allow_pickle=False)
        if not other.dtype.names or 'uid' not in other.dtype.names:
            raise ValueError('Passthrough has no particle UIDs.')
        uids = other['uid']
        if len(np.unique(uids)) != len(uids):
            raise ValueError('Duplicate passthrough UIDs.')
        sorting = np.argsort(uids)
        positions = np.searchsorted(uids[sorting], main['uid'])
        if np.any(positions >= len(uids)) or not np.array_equal(uids[sorting][positions], main['uid']):
            raise ValueError('Passthrough is missing selected particle UIDs; choose the matching output.')
        order = sorting[positions]
    fields = {}
    for field in required:
        if field in main.dtype.names:
            fields[field] = main[field]
        elif other is not None and field in other.dtype.names:
            fields[field] = other[field][order]
        else:
            raise ValueError('Missing required field: '+field)
    return fields, len(main)


class DensityIndex:
    def __init__(self, store):
        self.store = store
        self.lock = threading.RLock()
        self.records = {}
        self.summary = {'loaded': False}
        self.version = 0

    def import_files(self, main, passthrough=None):
        fields, total = load_locations(main, passthrough)
        media = defaultdict(list)
        for row in self.store.execute("SELECT id,grid_id,hole,name FROM media WHERE kind='data'"):
            key = exposure_key(row['name'])
            if key:
                media[key].append(row)
        counts = defaultdict(lambda: np.zeros((16,16), dtype=np.int64))
        rows = {}; matched = invalid = unmatched = ambiguous = 0
        unknown = set()
        for path,x,y in zip(*(fields[k] for k in ('location/micrograph_path','location/center_x_frac','location/center_y_frac'))):
            if not np.isfinite(x) or not np.isfinite(y) or not (0<=x<=1 and 0<=y<=1):
                invalid += 1
                continue
            key = exposure_key(path)
            candidates = media.get(key, [])
            if len(candidates)!=1:
                if candidates: ambiguous += 1
                else: unmatched += 1
                if len(unknown)<10: unknown.add(str(path))
                continue
            row=candidates[0]; rows[row['id']]=row
            # CryoSPARC Y is bottom-up; EPU preview/SVG Y is top-down.
            ix=min(15,int(float(x)*16)); iy=min(15,int((1-float(y))*16))
            counts[row['id']][iy,ix] += 1
            matched += 1
        records = {key:dict(rows[key],histogram=value.tolist(),particles=int(value.sum())) for key,value in counts.items()}
        grid_counts = defaultdict(int)
        for record in records.values():
            grid_counts[record['grid_id']] += record['particles']
        summary=dict(loaded=True, file=Path(main).name, particles=total,matched=matched,
                     grid_counts=dict(grid_counts),
                     invalid=invalid,unmatched=unmatched,ambiguous=ambiguous,
                     represented_exposures=len(records), represented_holes=len({(r['grid_id'],r['hole']) for r in records.values()}),
                     unmatched_examples=sorted(unknown),
                     warning='Subset density, not total particle abundance. Blank = unrepresented or unmapped, not zero. Coordinates assume full-frame micrographs with unchanged orientation; cropped/rotated imports require registration. Alignment shifts are not applied.')
        with self.lock:
            self.records=records;self.summary=summary;self.version+=1
        return summary

    def grid_density(self, gid, transform='identity'):
        with self.lock:
            records = [r for r in self.records.values() if r['grid_id']==gid]
            version = self.version
        areas=self.store.acquisition_areas(gid) if transform in (None,'identity') else self.store.acquisition_areas(gid,transform)
        geometry={a['id']:a for a in areas['areas']}
        from build_collage import parse_grid_info
        g=self.store.grid(gid); m=self.store.media(g['image'])
        info=parse_grid_info(self.store.cache_optional(m['xml']))
        try:
            a,b,c,d=info['ref_matrix']
            physical_area=abs(a*d-b*c)*info['readout_width']*info['readout_height']*1e12
        except (KeyError,TypeError):
            return dict(version=version,cells=[],holes=[],note='GridSquare physical calibration unavailable.')
        cells=[]; holes=defaultdict(lambda:dict(particles=0,area=0,exposures=0)); missing=0
        for record in records:
            region=geometry.get(record['id'])
            if not region:
                missing+=1;continue
            p0,p1,_,p3=np.asarray(region['points'])
            u=p1-p0;v=p3-p0
            area=abs(u[0]*v[1]-u[1]*v[0])*physical_area
            if not np.isfinite(area) or area<=0:continue
            h=holes[record['hole']];h['particles']+=record['particles'];h['area']+=area;h['exposures']+=1
            hist=np.asarray(record['histogram'])
            # 4x4 coarse bins suppress individual dots and avoid false precision.
            bins=hist.reshape(4,4,4,4).sum(axis=(1,3))
            for y in range(4):
                for x in range(4):
                    corners=[(p0+u*xx/4+v*yy/4).tolist() for xx,yy in ((x,y),(x+1,y),(x+1,y+1),(x,y+1))]
                    cells.append(dict(hole=record['hole'],exposure=record['id'],points=corners,density=float(bins[y,x]/(area/16))))
        result=[dict(hole=key,particles=v['particles'],exposures=v['exposures'],density=v['particles']/v['area']) for key,v in holes.items()]
        values=[c['density'] for c in cells]
        return dict(version=version,cells=cells,holes=result,max_density=max(values,default=0),
                    note=f'{len(records)-missing} represented exposures mapped; {missing} missing EPU geometry. Density = selected particles/µm² of represented exposure area. Overlapping exposures are counted independently. Spatial locations use planned EPU footprints.')
