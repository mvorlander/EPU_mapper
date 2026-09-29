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


class LocationColumns:
    """Memory-mapped CryoSPARC location columns, UID-aligned in bounded slices."""
    required = ('location/micrograph_path', 'location/center_x_frac', 'location/center_y_frac')

    def __init__(self, main_path, passthrough_path=None, required=None):
        self.required=tuple(required or type(self).required)
        self.main = np.load(main_path, allow_pickle=False, mmap_mode='r')
        main = self.main
        if not main.dtype.names or 'uid' not in main.dtype.names:
            raise ValueError('Expected a CryoSPARC structured .cs particle table with uid.')
        main_uids=np.sort(main['uid'])
        if len(main_uids)>1 and np.any(main_uids[1:]==main_uids[:-1]):
            raise ValueError('Duplicate particle UIDs: refusing to count particles twice.')
        del main_uids
        self.other = None; self.order = None
        needs_other = any(field not in main.dtype.names for field in self.required)
        if needs_other and not passthrough_path:
            raise ValueError('Location fields are missing. Supply the matching passthrough .cs file.')
        if passthrough_path:
            self.other = np.load(passthrough_path, allow_pickle=False, mmap_mode='r')
            other = self.other
            if not other.dtype.names or 'uid' not in other.dtype.names:
                raise ValueError('Passthrough has no particle UIDs.')
            uids = other['uid']
            sorting = np.argsort(uids)
            ordered=uids[sorting]
            if len(ordered)>1 and np.any(ordered[1:]==ordered[:-1]):
                raise ValueError('Duplicate passthrough UIDs.')
            positions = np.searchsorted(ordered, main['uid'])
            if np.any(positions >= len(uids)) or not np.array_equal(ordered[positions], main['uid']):
                raise ValueError('Passthrough is missing selected particle UIDs; choose the matching output.')
            self.order = sorting[positions]
        for field in self.required:
            if field not in main.dtype.names and (self.other is None or field not in self.other.dtype.names):
                raise ValueError('Missing required field: '+field)

    def values(self, field, start=0, stop=None):
        stop = len(self.main) if stop is None else stop
        if field in self.main.dtype.names:
            return self.main[field][start:stop]
        return self.other[field][self.order[start:stop]]

    def chunks(self, size=None):
        if size is None:
            paths=self.main['location/micrograph_path'] if 'location/micrograph_path' in self.main.dtype.names else self.other['location/micrograph_path']
            size=max(10_000,min(250_000,(64*1024*1024)//max(1,paths.dtype.itemsize)))
        for start in range(0, len(self.main), size):
            stop = min(len(self.main), start+size)
            yield start, stop, tuple(self.values(field,start,stop) for field in self.required)


def load_locations(main_path, passthrough_path=None):
    columns = LocationColumns(main_path,passthrough_path)
    main = columns.main
    required = ('location/micrograph_path', 'location/center_x_frac', 'location/center_y_frac')
    fields = {field:columns.values(field) for field in required}
    return fields, len(main)


class DensityIndex:
    def __init__(self, store):
        self.store = store
        self.lock = threading.RLock()
        self.records = {}
        self.summary = {'loaded': False}
        self.sources = {}
        self.version = 0

    def import_files(self, main, passthrough=None, source_names=None):
        columns = LocationColumns(main,passthrough); total=len(columns.main)
        media = defaultdict(list); media_rows=[]
        for row in self.store.execute("SELECT id,grid_id,hole,name FROM media WHERE kind='data'"):
            key = exposure_key(row['name'])
            if key:
                media[key].append(len(media_rows));media_rows.append(row)
        counts = np.zeros((len(media_rows),256),dtype=np.int64)
        rows = {}; matched = invalid = unmatched = ambiguous = 0
        unknown = set()
        for _,_,(paths,x,y) in columns.chunks():
            x=np.asarray(x);y=np.asarray(y)
            valid=np.isfinite(x)&np.isfinite(y)&(x>=0)&(x<=1)&(y>=0)&(y<=1);invalid+=int((~valid).sum())
            unique,inverse=np.unique(paths,return_inverse=True)
            codes=np.full(len(unique),-1,dtype=np.int64)
            for i,path in enumerate(unique):
                candidates=media.get(exposure_key(path),[])
                if len(candidates)==1:codes[i]=candidates[0]
                elif candidates:codes[i]=-2
                elif len(unknown)<10:unknown.add(str(path))
            particle_codes=codes[inverse]
            ambiguous+=int((valid&(particle_codes==-2)).sum());unmatched+=int((valid&(particle_codes==-1)).sum())
            good=valid&(particle_codes>=0);matched+=int(good.sum())
            if not np.any(good):continue
            ix=np.minimum(15,(x[good]*16).astype(np.int64));iy=np.minimum(15,((1-y[good])*16).astype(np.int64))
            flat=particle_codes[good]*256+iy*16+ix
            counts += np.bincount(flat,minlength=counts.size).reshape(counts.shape)
        records={}
        for index,value in enumerate(counts):
            if not value.any():continue
            row=media_rows[index];rows[row['id']]=row
            records[row['id']]=dict(row,histogram=value.reshape(16,16).tolist(),particles=int(value.sum()))
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
            self.sources=dict(main=str(main),passthrough=str(passthrough) if passthrough else '',**(source_names or {}))
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
