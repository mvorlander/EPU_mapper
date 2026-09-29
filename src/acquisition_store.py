"""Local, persistent acquisition index. Network reads are queued and bounded.

The index is trusted until explicit refresh (or opt-in live polling). Data MRCs
are indexed by exact preview filename only and read on explicit request.
FoilHole-only scans never enter a Data directory.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import re
import shutil
import sqlite3
import sys
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path


def cache_home():
    if sys.platform == "darwin":
        return Path.home() / "Library/Caches/EPUMapper/acquisitions"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "EPUMapper/acquisitions"
    return Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "EPUMapper/acquisitions"


def stable_id(value):
    return hashlib.sha256(str(value).encode()).hexdigest()[:24]


def timestamp(name):
    match = re.search(r"(20\d{6})_(\d{6})", name)
    return "".join(match.groups()) if match else ""


def xml_root(path):
    """Strip namespaces from a locally cached EPU XML document."""
    try:
        root = ET.parse(path).getroot()
        for node in root.iter():
            node.tag = node.tag.split('}')[-1]
        return root
    except (OSError, ET.ParseError, TypeError):
        return ET.Element('missing')


def target_groups(path):
    groups = {}
    for pair in xml_root(path).iter('KeyValuePairOfintTargetLocationXmlBpEWF4JT'):
        value = pair.find('value')
        if value is None:
            continue
        hole = value.findtext('Id')
        primary = value.findtext('PrimaryId')
        if hole:
            groups[hole] = primary if primary and primary != '0' else None
    return groups


def matrix_vector(matrix, x, y, inverse=False):
    # EPU serializes a System.Windows.Media.Matrix: row-vector convention.
    a,b,c,d = matrix
    if inverse:
        det = a*d-b*c
        if abs(det) < 1e-40:
            raise ValueError('Singular coordinate transformation')
        return ((d*x-c*y)/det, (-b*x+a*y)/det)
    return (a*x+c*y, b*x+d*y)


class AcquisitionStore:
    def __init__(self, source, atlas=None, mode="acquisition", cache_root=None, readers=2):
        self.source = Path(os.path.abspath(os.path.expanduser(str(source))))
        self.atlas = Path(os.path.abspath(os.path.expanduser(str(atlas)))) if atlas else None
        self.mode = mode
        self.ignore_data = mode == "foilhole"
        self.root = (Path(cache_root) if cache_root else cache_home()) / stable_id(self.source)
        self.root.mkdir(parents=True, exist_ok=True)
        self.files = self.root / "files"
        self.files.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "index.sqlite", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA busy_timeout=5000;
            CREATE TABLE IF NOT EXISTS grids(id TEXT PRIMARY KEY, path TEXT, name TEXT, image TEXT, mrc TEXT, stamp TEXT, indexed INTEGER DEFAULT 0, markers TEXT DEFAULT '[]', note TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS media(id TEXT PRIMARY KEY, grid_id TEXT, kind TEXT, hole TEXT, name TEXT, path TEXT, xml TEXT, stamp TEXT, generation TEXT);
            CREATE INDEX IF NOT EXISTS media_group ON media(grid_id,kind,hole,stamp);
            CREATE TABLE IF NOT EXISTS cached(id TEXT PRIMARY KEY,path TEXT,mtime INTEGER,size INTEGER,access REAL);
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS annotations(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS metrics(media_id TEXT PRIMARY KEY,value TEXT);
        ''')
        self.db.commit()
        self.jobs = {}
        self.tasks = queue.PriorityQueue()
        self.serial = 0
        self.closed = False
        self.io = threading.BoundedSemaphore(max(1, min(4, readers)))
        self.scan_lock = threading.Lock()
        self.scan_state = {"status": "cached" if self.meta("indexed:" + mode) else "new", "message": "Cached index; refresh to check source", "grids_done": 0}
        self.threads = []
        self.prefetch_generation = 0
        for index in range(max(1, min(4, readers))):
            thread = threading.Thread(target=self._worker, name=f"epu-cache-{index}", daemon=True)
            thread.start()
            self.threads.append(thread)

    def execute(self, sql, args=()):
        with self.lock:
            cursor = self.db.execute(sql, args)
            rows = [dict(row) for row in cursor.fetchall()] if cursor.description else []
            self.db.commit()
            return rows

    def meta(self, key, default=None):
        rows = self.execute("SELECT value FROM meta WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set_meta(self, key, value):
        self.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value)))

    def submit(self, title, work, priority=0, generation=None):
        with self.lock:
            key = uuid.uuid4().hex
            self.serial += 1
            self.jobs[key] = {"id": key, "status": "queued", "message": title}
            # Retain a bounded history; do not discard queued/running jobs.
            finished = [k for k,v in self.jobs.items() if v['status'] in ('done','error','cancelled')]
            for old in finished[:-100]:
                self.jobs.pop(old, None)
            self.tasks.put((priority, self.serial, key, work, generation))
            return key

    def _worker(self):
        while not self.closed:
            try:
                _, _, key, work, generation = self.tasks.get(timeout=.2)
            except queue.Empty:
                continue
            try:
                if generation is not None and generation != self.prefetch_generation:
                    self.jobs[key].update(status="cancelled", message="Selection changed")
                    continue
                self.jobs[key]["status"] = "running"
                result = work()
                self.jobs[key].update(status="done", result=result, message="Ready")
            except Exception as exc:
                self.jobs[key].update(status="error", message=str(exc))
            finally:
                self.tasks.task_done()

    def list_names(self, directory, directories=False):
        with self.io, os.scandir(directory) as entries:
            # No per-file stat; avoid asking the share about large raw movies.
            return [(entry.name, entry.is_dir(follow_symlinks=False) if directories else False) for entry in entries]

    def add_media(self, path, kind, grid="", hole="", xml="", generation=""):
        key = stable_id(str(path))
        self.execute("INSERT OR REPLACE INTO media VALUES (?,?,?,?,?,?,?,?,?)",
                     (key, grid, kind, str(hole), Path(path).name, str(path), str(xml), timestamp(Path(path).name), generation))
        return key

    def media(self, key):
        rows = self.execute("SELECT * FROM media WHERE id=?", (key,))
        if not rows or (self.ignore_data and rows[0]["kind"] in ('data','data_mrc')):
            raise KeyError("Image not available in this mode")
        return rows[0]

    def local_file(self, key):
        row = self.execute("SELECT path FROM cached WHERE id=?", (key,))
        if row and Path(row[0]["path"]).is_file():
            self.execute("UPDATE cached SET access=? WHERE id=?", (time.time(), key))
            return Path(row[0]["path"])
        return None

    def cache_file(self, key, refresh=False):
        media = self.media(key)
        if media['kind'] == 'data' and Path(media['path']).suffix.lower() not in ('.jpg','.jpeg','.png'):
            raise ValueError('Data MRCs and movies are never loaded')
        local = self.local_file(key)
        if local and not refresh:
            return local
        source = Path(media["path"])
        # Only explicit refresh checks a cached source's signature.
        with self.io:
            before = source.stat()
            if local:
                cached = self.execute("SELECT mtime,size FROM cached WHERE id=?", (key,))[0]
                if (before.st_mtime_ns, before.st_size) == (cached['mtime'], cached['size']):
                    return local
            destination = self.files / (key + source.suffix.lower())
            temporary = destination.with_name(destination.name + '.' + uuid.uuid4().hex + '.part')
            try:
                shutil.copyfile(source, temporary)
                after = source.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise OSError('File is still being written; refresh and retry when acquisition finishes')
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
        self.execute("INSERT OR REPLACE INTO cached VALUES (?,?,?,?,?)", (key, str(destination), after.st_mtime_ns, after.st_size, time.time()))
        return destination

    def cache_optional(self, path, kind="metadata", refresh=False):
        key = stable_id(path)
        self.add_media(path, kind)
        try:
            return self.cache_file(key, refresh=refresh)
        except (FileNotFoundError, NotADirectoryError):
            return None

    def start_scan(self, refresh=False):
        if not self.scan_lock.acquire(blocking=False):
            return False
        self.scan_state.update(status="indexing", message="Discovering GridSquares…", grids_done=0)
        def run():
            try:
                self.scan(refresh)
                self.set_meta("indexed:" + self.mode, time.time())
                self.scan_state.update(status="ready", message="Index ready · cached until Refresh")
            except Exception as exc:
                self.scan_state.update(status="error", message=f"Source unavailable or incomplete: {exc}. Cached images remain usable.")
            finally:
                self.scan_lock.release()
        thread = threading.Thread(target=run, name="epu-index", daemon=True)
        thread.start()
        self.threads.append(thread)
        return True

    def scan(self, refresh=False):
        names = self.list_names(self.source, directories=True)
        discs = [self.source / name for name, directory in names if directory and name.startswith('Images-Disc')]
        if self.source.name.startswith('GridSquare_'):
            grids = [self.source]
        elif discs:
            grids = [disc/name for disc in sorted(discs) for name,directory in self.list_names(disc, directories=True) if directory and name.startswith('GridSquare_')]
        else:
            grids = [self.source/name for name,directory in names if directory and name.startswith('GridSquare_')]
        if not grids:
            raise ValueError('No GridSquare directories found; choose the EPU session or Images-Disc folder')
        if self.atlas:
            self._index_atlas(refresh)
        generation = uuid.uuid4().hex
        grid_entries = {}
        # Grid headers first; the Atlas and square list become usable before Data scans.
        for directory in grids:
            gid = stable_id(directory)
            entries = {name for name,is_dir in self.list_names(directory) if not is_dir}
            grid_entries[str(directory)] = entries
            previews = sorted((n for n in entries if n.startswith('GridSquare_') and Path(n).suffix.lower() in ('.jpg','.jpeg','.png')), key=lambda n:(timestamp(n),n))
            if not previews:
                continue
            image = previews[-1]
            image_id = self.add_media(directory/image, 'grid', gid, xml=directory/Path(image).with_suffix('.xml'), generation=generation)
            matching = next((str(directory/Path(image).with_suffix(ext)) for ext in ('.mrc','.mrcs') if str(Path(image).with_suffix(ext)) in entries), '')
            mrc_id = self.add_media(matching,'grid_mrc',gid,generation=generation) if matching else ''
            self.execute("INSERT INTO grids(id,path,name,image,mrc,stamp) VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET path=excluded.path,name=excluded.name,image=excluded.image,mrc=excluded.mrc,stamp=excluded.stamp",
                         (gid,str(directory),directory.name,image_id,mrc_id,timestamp(image)))
        current = {stable_id(path) for path in grids}
        for stale in self.execute('SELECT id FROM grids'):
            if stale['id'] not in current:
                self.execute('DELETE FROM grids WHERE id=?',(stale['id'],))
                self.execute('DELETE FROM media WHERE grid_id=?',(stale['id'],))
        for index,directory in enumerate(grids):
            if self.closed:
                return
            gid = stable_id(directory)
            self.scan_state.update(message=f"Indexing {directory.name} ({index+1}/{len(grids)})", grids_done=index)
            for folder,kind in [('FoilHoles','foil')] + ([] if self.ignore_data else [('Data','data')]):
                try:
                    entries = {name for name,is_dir in self.list_names(directory/folder) if not is_dir}
                except FileNotFoundError:
                    entries = set()
                by_stem = {}
                for name in sorted(entries):
                    if Path(name).suffix.lower() not in ('.jpg','.jpeg','.png') or not name.startswith('FoilHole_'):
                        continue
                    stem = Path(name).stem
                    if stem not in by_stem or Path(name).suffix.lower() == '.jpg':
                        by_stem[stem] = name
                with self.lock:
                    rows = []
                    for stem,name in by_stem.items():
                        path = directory/folder/name
                        xml = str(path.with_suffix('.xml')) if stem+'.xml' in entries else ''
                        rows.append((stable_id(path),gid,kind,name.split('_')[1],name,str(path),xml,timestamp(name),generation))
                        if kind=='data':
                            # Discover exact exposure counterparts by filename only.
                            # Never substitute fraction stacks or neighboring exposures.
                            match=next((stem+ext for ext in ('.mrc','.mrcs') if stem+ext in entries),None)
                            if match:
                                mrc_path=directory/folder/match
                                rows.append((stable_id(mrc_path),gid,'data_mrc',name.split('_')[1],match,str(mrc_path),xml,timestamp(name),generation))
                    self.db.executemany('INSERT OR REPLACE INTO media VALUES (?,?,?,?,?,?,?,?,?)', rows)
                    self.db.execute("DELETE FROM media WHERE grid_id=? AND kind=? AND generation!=?", (gid,kind,generation))
                    if kind=='data':
                        self.db.execute("DELETE FROM media WHERE grid_id=? AND kind='data_mrc' AND generation!=?",(gid,generation))
                    self.db.commit()
                if kind == 'data':
                    # Track missing JPEGs from image XMLs without treating movie
                    # sidecars as extra exposures when an image XML also exists.
                    missing = []
                    for name in sorted(entries):
                        if not name.endswith('.xml') or not name.startswith('FoilHole_') or '_Data_' not in name:
                            continue
                        stem=Path(name).stem
                        normalized=re.sub(r'_(?:Fractions|fractions|EER)$','',stem)
                        if stem in by_stem or normalized in by_stem or (normalized!=stem and normalized+'.xml' in entries):
                            continue
                        missing.append(dict(hole=name.split('_')[1], name=name))
                    self.set_meta('missing:'+gid,missing)
            entries = grid_entries.get(str(directory), set())
            first = self.execute("SELECT MIN(stamp) AS stamp FROM media WHERE grid_id=? AND kind='foil' AND stamp!=''", (gid,))[0]['stamp']
            preceding = [n for n in entries if n.startswith('GridSquare_') and Path(n).suffix.lower() in ('.jpg','.jpeg','.png') and timestamp(n) and first and timestamp(n)<=first]
            if preceding:
                chosen = max(preceding, key=lambda n:(timestamp(n),n))
                image_id = self.add_media(directory/chosen,'grid',gid,xml=directory/Path(chosen).with_suffix('.xml'))
                matching = next((directory/Path(chosen).with_suffix(ext) for ext in ('.mrc','.mrcs') if str(Path(chosen).with_suffix(ext)) in entries),None)
                mrc_id = self.add_media(matching,'grid_mrc',gid) if matching else ''
                self.execute('UPDATE grids SET image=?,mrc=?,stamp=? WHERE id=?',(image_id,mrc_id,timestamp(chosen),gid))
            self.execute("UPDATE grids SET indexed=1 WHERE id=?", (gid,))
        if refresh:
            # Refresh only previously cached files, never stat every raw movie.
            for row in self.execute("SELECT media.id,media.kind FROM cached JOIN media ON cached.id=media.id"):
                if self.ignore_data and row['kind'] in ('data','data_mrc'):
                    continue
                try:
                    self.cache_file(row['id'],refresh=True)
                except OSError:
                    pass  # keep the known cached version for disconnected shares
            self.execute("UPDATE grids SET markers='[]',note=''")
        self.scan_state['grids_done']=len(grids)
        self.import_screening_reviews()

    def import_screening_reviews(self):
        """Copy legacy square reviews into local storage without overwriting newer edits."""
        if self.meta('user-annotations-cleared',False):
            return
        groups={}
        for grid in self.execute('SELECT id,path,name FROM grids'):
            groups.setdefault(Path(grid['path']).parent,[]).append(grid)
        for parent,grids in groups.items():
            path=parent/'review_responses.json'
            try:
                entries=json.loads(path.read_text(encoding='utf-8'))
            except (OSError,ValueError):
                continue
            if not isinstance(entries,dict):
                continue
            for grid in grids:
                key='grid:'+grid['id'];entry=entries.get(grid['name'])
                if not isinstance(entry,dict) or self.annotation(key):
                    continue
                try:
                    with self.lock:
                        if self.meta('user-annotations-cleared',False):
                            return
                        self.annotate(key,dict(comment=entry.get('comment',''),rating=entry.get('rating',0),
                            status=entry.get('collection_status') or ('suitable' if entry.get('collect') else ''),
                            priority=entry.get('priority','primary')),overwrite=False)
                        self.set_meta('legacy-review:'+grid['id'],entry)
                except (ValueError,TypeError):
                    continue
                # Preserve fields that the unified review form does not yet edit.

    def _index_atlas(self, refresh=False):
        if self.atlas.suffix.lower() in ('.jpg','.jpeg','.png'):
            selected=self.atlas
        else:
            names = self.list_names(self.atlas)
            candidates=sorted(name for name,is_dir in names if not is_dir and name.lower().startswith('atlas') and Path(name).suffix.lower() in ('.jpg','.jpeg','.png'))
            if not candidates:
                raise ValueError('No Atlas JPEG/PNG in selected Atlas directory')
            selected=self.atlas/candidates[-1]
        aid=self.add_media(selected,'atlas')
        local=self.cache_file(aid,refresh=refresh)
        atlas_dir=self.root/'atlas'
        atlas_dir.mkdir(exist_ok=True)
        shutil.copyfile(local,atlas_dir/selected.name)
        metadata = {}
        for path in [selected.parent/'Atlas.dm',selected.with_suffix('.xml')]:
            cached=self.cache_optional(path,refresh=refresh)
            if cached:
                shutil.copyfile(cached,atlas_dir/path.name)
                metadata[path.suffix.lower()] = cached
        from build_collage import _parse_atlas_dm_nodes, parse_grid_info
        # Never reuse staged metadata from a previously selected atlas.
        nodes = _parse_atlas_dm_nodes(metadata['.dm']) if '.dm' in metadata else {}
        dimensions = parse_grid_info(metadata['.xml']) if '.xml' in metadata else {}
        width, height = dimensions.get('readout_width'), dimensions.get('readout_height')
        # DM square centers belong to the assembled atlas, not a camera tile.
        # Read only the matching MRC header, even while displaying its JPEG.
        names = {name for name,_ in self.list_names(selected.parent)}
        mrc_path = next((selected.with_suffix(ext) for ext in ('.mrc','.mrcs') if selected.with_suffix(ext).name in names),None)
        mrc = self.add_media(mrc_path,'atlas_mrc') if mrc_path else ''
        frame_source='xml-readout'
        if mrc_path:
            import mrcfile
            width=height=None
            try:
                with mrcfile.open(mrc_path,header_only=True,permissive=True) as header:
                    w,h=int(header.header.nx),int(header.header.ny)
                if w>0 and h>0:
                    width,height=w,h
                    frame_source='mrc-header'
            except (OSError,ValueError):
                pass  # Incomplete or unavailable MRC: never substitute tile dimensions.
        elif '.xml' in metadata:
            root=ET.parse(metadata['.xml']).getroot()
            for entry in root.iter():
                fields={child.tag.rsplit('}',1)[-1].lower():child.text for child in entry}
                if fields.get('key') in ('NumberOfTilesAcquired','NumberOfTilesPlanned'):
                    try:
                        if float(fields.get('value') or 0)>1:
                            width=height=None
                    except ValueError:
                        pass
            # Out-of-frame centers also rule out the camera readout as a valid frame.
            if width and height and any(n.get('center') and (n['center'][0]>width or n['center'][1]>height) for n in nodes.values()):
                width=height=None
        # Never guess the detector frame from the furthest screened position.
        # Without its reference dimensions, display the atlas without markers.
        if not width or not height:
            nodes = {}
        # Persist mapping locally; all subsequent requests avoid the network.
        self.set_meta('atlas',dict(id=aid,mrc=mrc,name=selected.name,nodes=nodes,width=width,height=height,
            frame_source=frame_source if width and height else None,
            note='' if width and height else 'Atlas overlays unavailable: assembled image dimensions could not be established. Supply the matching Atlas MRC (only its header is read).'))

    def geometry(self, gid, transform='identity'):
        """Read positional metadata only for the selected square, then cache it locally."""
        grid = self.grid(gid)
        geometry_key = [3, self.mode, transform]
        if grid['markers'] != '[]' and self.meta('geometry-transform:'+gid)==geometry_key:
            return dict(markers=json.loads(grid['markers']),note=grid['note'])
        from build_collage import parse_grid_info
        root = str(Path(__file__).resolve().parent.parent)
        if root not in sys.path:
            sys.path.append(root)
        from scripts.plot_foilhole_positions import (
            _load_dm_pixel_centers, _TRANSFORM_FUNCS,
            _epu_stage_payload, _project_marker_epu,
        )
        image = self.media(grid['image'])
        xml = self.cache_optional(image['xml']) if image['xml'] else None
        info = parse_grid_info(xml) if xml else {}
        width,height = info.get('readout_width'),info.get('readout_height')
        directory = Path(grid['path'])
        shadow = self.root/'geometry'/grid['id']
        (shadow/'Metadata').mkdir(parents=True,exist_ok=True)
        local_grid = shadow/directory.name
        local_grid.mkdir(exist_ok=True)
        metadata = None
        for ancestor in list(directory.parents)[:4]:
            metadata = self.cache_optional(ancestor/'Metadata'/(directory.name+'.dm'))
            if metadata:
                shutil.copyfile(metadata,shadow/'Metadata'/(directory.name+'.dm'))
                break
        # Clear the helper's metadata cache when an explicit index refresh
        # invalidated this square. Only local files are passed to the helper.
        from scripts.plot_foilhole_positions import _PIXEL_CENTER_CACHE
        _PIXEL_CENTER_CACHE.pop(shadow/'Metadata'/(directory.name+'.dm'),None)
        positions = _load_dm_pixel_centers(local_grid) if metadata else {}
        kinds = ('foil',) if self.ignore_data else ('foil','data')
        allowed = {r['hole'] for r in self.execute('SELECT DISTINCT hole FROM media WHERE grid_id=? AND kind IN ('+','.join('?' for _ in kinds)+')',(gid,*kinds))}
        markers = []
        mapping = _TRANSFORM_FUNCS.get(transform,_TRANSFORM_FUNCS['identity'])
        if width and height and positions:
            for hole,(x,y) in positions.items():
                if hole not in allowed:
                    continue
                u,v = mapping(x/width,y/height)
                if 0<=u<=1 and 0<=v<=1:
                    markers.append(dict(hole=hole,x=u,y=v))
        # EPU XML stage coordinates remain usable when Metadata/ has not
        # arrived (or contains only some holes). Use the same projection as
        # the established overlay helper, against locally cached XML only.
        # No Data discovery, Data reads, MRC reads, or preview decoding needed.
        covered = {marker['hole'] for marker in markers}
        square_stage = _epu_stage_payload(xml) if xml else {}
        xml_markers = 0
        if width and height and square_stage:
            foils = self.execute("SELECT hole,xml FROM media WHERE grid_id=? AND kind='foil' ORDER BY stamp DESC,name DESC",(gid,))
            for foil in foils:
                if foil['hole'] in covered or not foil['xml']:
                    continue
                try:
                    cached_xml = self.cache_optional(foil['xml'])
                except OSError:
                    continue  # a partially copied/unavailable hole must not hide others
                if not cached_xml:
                    continue
                coords = _project_marker_epu(square_stage,_epu_stage_payload(cached_xml),width,height,1/width,1/height)
                if coords is None:
                    continue
                u,v = mapping(*coords)
                # Never clamp or spread out-of-frame points into plausible
                # positions: keep the mapping faithful to actual metadata.
                if 0<=u<=1 and 0<=v<=1:
                    markers.append(dict(hole=foil['hole'],x=u,y=v))
                    covered.add(foil['hole'])
                    xml_markers += 1
        groups = target_groups(metadata) if metadata else {}
        previews = {r['hole'] for r in self.execute("SELECT DISTINCT hole FROM media WHERE grid_id=? AND kind='foil'",(gid,))}
        for marker in markers:
            anchor = groups.get(marker['hole'])
            if not anchor and marker['hole'] in previews:
                anchor = marker['hole']
            marker.update(anchor=anchor, has_preview=marker['hole'] in previews,
                          role='anchor' if anchor==marker['hole'] else 'shifted' if anchor else 'unknown')
        missing = len(allowed-covered)
        if not allowed:
            note = 'No indexed FoilHole previews yet. Refresh index after more files have copied.'
        elif not width or not height:
            note = 'GridSquare XML reference dimensions are missing. Refresh index after the XML has copied.'
        elif not markers:
            note = 'Hole coordinates unavailable: waiting for FoilHole XML stage positions or Metadata/GridSquare_*.dm pixel positions. Refresh index after copying; Data files are not needed.'
        elif missing:
            note = f'{len(markers)} holes mapped; {missing} still lack usable coordinates. Refresh index as copying progresses.'
        else:
            note = ''
        if markers and transform=='auto':
            note += ' Using metadata coordinates (identity); acquisition mode does not infer rotation.'
        self.execute('UPDATE grids SET markers=?,note=? WHERE id=?',(json.dumps(markers),note,gid))
        self.set_meta('geometry-transform:'+gid,geometry_key)
        return dict(markers=markers,note=note,xml_markers=xml_markers)

    def acquisition_areas(self, gid, transform='identity'):
        """Optional planned camera footprints; never read Data MRCs or pixel data."""
        if self.ignore_data:
            return dict(areas=[], note='Data areas are unavailable in FoilHole-only mode.')
        from build_collage import parse_grid_info
        geometry = self.geometry(gid, 'identity')
        from scripts.plot_foilhole_positions import _TRANSFORM_FUNCS
        markers = {m['hole']:m for m in geometry['markers']}
        grid = self.grid(gid)
        image = self.media(grid['image'])
        info = parse_grid_info(self.cache_optional(image['xml']))
        template = None
        for parent in list(Path(grid['path']).parents)[:4]:
            cached = self.cache_optional(parent/'EpuSession.dm')
            if cached:
                template = xml_root(cached).find('.//TargetAreaTemplate')
                break
        try:
            width,height = info['readout_width'],info['readout_height']
            if width<=0 or height<=0:
                raise ValueError('Invalid GridSquare dimensions')
            grid_matrix = info['ref_matrix']
            physical = tuple(float(template.findtext('PhysicalTransformation/matrix/_'+k)) for k in ('m11','m12','m21','m22'))
            offsets = {}
            for pair in template.iter('KeyValuePairOfintDataAcquisitionAreaXmlBpEWF4JT'):
                value = pair.find('value')
                offsets[value.findtext('Id')] = matrix_vector(physical,float(value.findtext('ShiftInPixels/width')),float(value.findtext('ShiftInPixels/height')))
        except (KeyError, TypeError, ValueError, AttributeError):
            return dict(areas=[],note='Data areas need the EPU session template and GridSquare coordinate transformations.')
        mapping = _TRANSFORM_FUNCS.get(transform,_TRANSFORM_FUNCS['identity'])
        footprints = {}; foil_frames = {}; areas = []; missing = 0; foil_missing = 0
        foils = self.execute("SELECT * FROM media WHERE grid_id=? AND kind='foil' ORDER BY stamp,name",(gid,))
        for row in self.execute("SELECT * FROM media WHERE grid_id=? AND kind='data' ORDER BY stamp,name",(gid,)):
            parts = row['name'].split('_')
            area = parts[3] if len(parts)>3 else ''
            marker = markers.get(row['hole'])
            if not marker or area not in offsets:
                missing += 1
                continue
            # Camera geometry is cached per acquisition area/setting, not per exposure.
            setting = (area,parts[4] if len(parts)>4 else '')
            if setting not in footprints:
                try:
                    data_info = parse_grid_info(self.cache_optional(row['xml']))
                    dw,dh = data_info['readout_width'],data_info['readout_height']
                    footprints[setting] = [matrix_vector(data_info['ref_matrix'],x,y) for x,y in ((-dw/2,-dh/2),(dw/2,-dh/2),(dw/2,dh/2),(-dw/2,dh/2))]
                except (KeyError, TypeError, ValueError, OSError):
                    missing += 1
                    continue
            dx,dy = offsets[area]
            try:
                points = []
                for x,y in footprints[setting]:
                    px,py = matrix_vector(grid_matrix,dx+x,dy+y,inverse=True)
                    points.append(mapping(marker['x']+px/width,marker['y']+py/height))
                if not all(0<=x<=1 and 0<=y<=1 for x,y in points):
                    missing += 1
                    continue
                preceding=[f for f in foils if f['stamp'] and f['stamp']<=row['stamp']]
                foil=preceding[-1] if preceding else foils[0] if len(foils)==1 and not row['stamp'] else None
                foil_points = None
                if foil:
                    if foil['id'] not in foil_frames:
                        try:
                            foil_info=parse_grid_info(self.cache_optional(foil['xml']))
                            fw,fh=foil_info['readout_width'],foil_info['readout_height']
                            if fw<=0 or fh<=0:
                                raise ValueError('Invalid FoilHole dimensions')
                            foil_frames[foil['id']]=(fw,fh,foil_info['ref_matrix'])
                        except (KeyError,TypeError,ValueError,OSError):
                            foil_frames[foil['id']]=None
                    frame=foil_frames[foil['id']]
                    foil_marker=markers.get(str(foil['hole'])) or markers.get(foil['hole'])
                    if frame and foil_marker:
                        fw,fh,foil_matrix=frame
                        target_delta=matrix_vector(grid_matrix,
                            (marker['x']-foil_marker['x'])*width,
                            (marker['y']-foil_marker['y'])*height)
                        candidate=[]
                        for x,y in footprints[setting]:
                            qx,qy=matrix_vector(foil_matrix,target_delta[0]+dx+x,target_delta[1]+dy+y,inverse=True)
                            candidate.append((.5+qx/fw,.5+qy/fh))
                        if all(math.isfinite(v) for point in candidate for v in point):
                            foil_points=candidate
                if foil_points is None:
                    foil_missing += 1
                areas.append(dict(id=row['id'],hole=row['hole'],anchor=marker['anchor'],area=area,
                    points=points,foil=foil['id'] if foil else '',foil_points=foil_points,name=row['name']))
            except ValueError:
                missing += 1
        note = 'Planned Data footprints from EPU template offsets; not measured beam landing positions.'
        if missing:
            note += f' {missing} exposures lack usable geometry.'
        if foil_missing:
            note += f' {foil_missing} areas cannot be placed on a FoilHole preview because its image or coordinate metadata is unavailable.'
        return dict(areas=areas,note=note)

    def grid(self, gid):
        rows=self.execute('SELECT * FROM grids WHERE id=?',(gid,))
        if not rows:
            raise KeyError('Unknown GridSquare')
        return rows[0]

    def grids(self):
        rows=self.execute('SELECT * FROM grids ORDER BY stamp,name')
        for row in rows:
            row['markers']=json.loads(row['markers'])
            allowed = ('foil',) if self.ignore_data else ('foil','data')
            placeholders = ','.join('?' for _ in allowed)
            counts=self.execute(f"SELECT COUNT(DISTINCT hole) AS holes,COUNT(CASE WHEN kind='data' THEN 1 END) AS images FROM media WHERE grid_id=? AND kind IN ({placeholders})",(row['id'],*allowed))[0]
            row.update(holes=counts['holes'],exposures=None if self.ignore_data else counts['images'],annotation=self.annotation('grid:'+row['id']))
            row['missing']=None if self.ignore_data else len(self.meta('missing:'+row['id'],[]))
        return rows

    def holes(self,gid,offset=0,limit=100,query=''):
        allowed=('foil',) if self.ignore_data else ('foil','data')
        where='grid_id=? AND kind IN ('+','.join('?' for _ in allowed)+')'
        args=[gid,*allowed]
        if query:
            where+=' AND hole LIKE ?';args.append('%'+query+'%')
        rows=self.execute(f"SELECT hole,COUNT(CASE WHEN kind='data' THEN 1 END) AS exposures FROM media WHERE {where} GROUP BY hole ORDER BY MIN(stamp),hole LIMIT ? OFFSET ?",(*args,limit,offset))
        total=self.execute(f'SELECT COUNT(DISTINCT hole) AS n FROM media WHERE {where}',args)[0]['n']
        for row in rows:
            row['annotation']=self.annotation('hole:'+gid+':'+row['hole'])
        return dict(rows=rows,total=total,offset=offset)

    def exposures(self,gid,hole):
        foils=self.execute("SELECT * FROM media WHERE grid_id=? AND hole=? AND kind='foil' ORDER BY stamp,name",(gid,hole))
        data=[] if self.ignore_data else self.execute("SELECT * FROM media WHERE grid_id=? AND hole=? AND kind='data' ORDER BY stamp,name",(gid,hole))
        result=[]
        for row in data:
            preceding=[f for f in foils if f['stamp'] and f['stamp']<=row['stamp']]
            foil=preceding[-1] if preceding else foils[0] if len(foils)==1 and not row['stamp'] else None
            result.append(dict(id=row['id'],hole=row['hole'],name=row['name'],stamp=row['stamp'],foil=foil['id'] if foil else '',foil_name=foil['name'] if foil else '',annotation=self.annotation('exposure:'+row['id']),metrics=self.metric(row['id'])))
            candidates=[stable_id(Path(row['path']).with_suffix(ext)) for ext in ('.mrc','.mrcs')]
            matches=self.execute("SELECT id FROM media WHERE kind='data_mrc' AND id IN (?,?)",candidates)
            result[-1]['mrc']=next((key for key in candidates if any(m['id']==key for m in matches)),'')
        return dict(foil=foils[-1]['id'] if foils else '',foil_name=foils[-1]['name'] if foils else '',exposures=result,missing=[] if self.ignore_data else [r for r in self.meta('missing:'+gid,[]) if r['hole']==hole])

    def annotation(self,key):
        rows=self.execute('SELECT value FROM annotations WHERE key=?',(key,))
        return json.loads(rows[0]['value']) if rows else {}

    def annotate(self,key,value,overwrite=True):
        clean=dict(comment=str(value.get('comment',''))[:10000],flag=bool(value.get('flag')),rating=max(0,min(5,int(value.get('rating',0)))),status=value.get('status',''),priority=value.get('priority','primary'))
        if clean['status'] not in ('','suitable','unsuitable') or clean['priority'] not in ('primary','backup','needs_screening'):
            raise ValueError('Invalid annotation')
        action='REPLACE' if overwrite else 'IGNORE'
        self.execute('INSERT OR '+action+' INTO annotations VALUES (?,?)',(key,json.dumps(clean)))
        return clean

    def metric(self,key):
        rows=self.execute('SELECT value FROM metrics WHERE media_id=?',(key,))
        return json.loads(rows[0]['value']) if rows else {}

    def annotation_snapshot(self):
        return {r['key']:json.loads(r['value']) for r in self.execute('SELECT * FROM annotations')}

    def clear_user_annotations(self):
        """Back up and clear only this local session's review annotations."""
        with self.lock:
            snapshot=self.annotation_snapshot()
            folder=self.root/'annotation-backups'
            folder.mkdir(exist_ok=True)
            key=uuid.uuid4().hex
            path=folder/(key+'.json')
            # A failed backup must leave all annotations intact.
            path.write_text(json.dumps(dict(source=str(self.source),annotations=snapshot),indent=2),encoding='utf-8')
            with self.db:
                self.db.execute('DELETE FROM annotations')
                self.db.execute("DELETE FROM meta WHERE key LIKE 'legacy-review:%'")
                self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',('user-annotations-cleared','true'))
            return dict(deleted=len(snapshot),backup=key)

    def cache_status(self):
        rows=self.execute('SELECT COUNT(*) AS files,COALESCE(SUM(size),0) AS bytes FROM cached')[0]
        return dict(**rows,index=str(self.root/'index.sqlite'))

    def close(self):
        self.closed=True
        for thread in self.threads:
            thread.join(timeout=2)
        # A blocked network syscall cannot be forcibly cancelled safely. Daemon
        # workers finish against the still-open connection before process exit.
        if not any(t.is_alive() for t in self.threads):
            self.db.close()
