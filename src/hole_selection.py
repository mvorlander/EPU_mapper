"""EPU hole-intensity selection and lossless CryoSPARC particle subsetting."""
from __future__ import annotations

import csv
import json
import shutil
import uuid
import zipfile
from collections import defaultdict
from pathlib import Path

import numpy as np

from acquisition_store import target_groups, xml_root
from cryosparc_density import exposure_key, LocationColumns


def _bool(text):
    return str(text or '').strip().lower() == 'true'


def _number(text):
    try:
        value = float(text)
        return value if np.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _grid_metadata(store, gid):
    grid = store.grid(gid)
    directory = Path(grid['path'])
    for ancestor in list(directory.parents)[:5]:
        source = ancestor/'Metadata'/(directory.name+'.dm')
        cached = store.cache_optional(source)
        if cached:
            return Path(cached)
    return None


def session_filter(store):
    cached = store.meta('hole-selection:session')
    if cached and (cached.get('minimum') is not None or cached.get('maximum') is not None):
        return cached
    result = dict(minimum=None, maximum=None, smart=False, ice_thickness=False,
                  note='EPU session filter settings were not found.')
    grids = store.execute('SELECT path FROM grids ORDER BY stamp LIMIT 1')
    if grids:
        for ancestor in list(Path(grids[0]['path']).parents)[:6]:
            local = store.cache_optional(ancestor/'EpuSession.dm')
            if not local:
                continue
            root = xml_root(local)
            settings = root.find('.//FilterHolesSettings')
            if settings is not None:
                result = dict(
                    minimum=_number(settings.findtext('MinimumIntensity')),
                    maximum=_number(settings.findtext('MaximumIntensity')),
                    smart=_bool(settings.findtext('EnableSmartHoleSelection')),
                    ice_thickness=_bool(settings.findtext('IceThicknessEnabled')),
                    note='Recorded EPU filter settings. The final EPU Selected state is stored separately.',
                )
                break
    store.set_meta('hole-selection:session', result)
    return result


def grid_targets(store, gid, transform='identity'):
    """Return every EPU target with normalized display coordinates and filter metadata."""
    from build_collage import parse_grid_info
    from scripts.plot_foilhole_positions import _TRANSFORM_FUNCS

    metadata = _grid_metadata(store, gid)
    if not metadata:
        return dict(targets=[], note='GridSquare target metadata is unavailable.')
    grid = store.grid(gid)
    image = store.media(grid['image'])
    info = parse_grid_info(store.cache_optional(image['xml'])) if image['xml'] else {}
    width, height = info.get('readout_width'), info.get('readout_height')
    if not width or not height:
        return dict(targets=[], note='GridSquare reference dimensions are unavailable.')
    mapping = _TRANSFORM_FUNCS.get(transform, _TRANSFORM_FUNCS['identity'])
    groups = target_groups(metadata)
    targets = []
    for pair in xml_root(metadata).iter('KeyValuePairOfintTargetLocationXmlBpEWF4JT'):
        value = pair.find('value')
        if value is None:
            continue
        hole = value.findtext('Id') or pair.findtext('key')
        center = value.find('PixelCenter')
        if not hole or center is None:
            continue
        x = _number(center.findtext('x')); y = _number(center.findtext('y'))
        if x is None or y is None:
            continue
        u, v = mapping(x/width, y/height)
        if not (0 <= u <= 1 and 0 <= v <= 1):
            continue
        filt = value.find('FilterProperties')
        primary = groups.get(str(hole))
        targets.append(dict(
            hole=str(hole), x=u, y=v, primary=str(primary) if primary else None,
            intensity=_number(filt.findtext('PixelIntensityMean')) if filt is not None else None,
            stdev=_number(filt.findtext('PixelIntensityStDev')) if filt is not None else None,
            selected=_bool(value.findtext('Selected')),
            near_grid_bar=_bool(value.findtext('IsNearGridBar')),
            quality=value.findtext('Quality') or '',
            smart_ice=_number(value.findtext('SmartIceThicknessResult')),
        ))
    media = store.execute("SELECT hole,COUNT(CASE WHEN kind='data' THEN 1 END) exposures FROM media WHERE grid_id=? GROUP BY hole", (gid,))
    acquired = {str(row['hole']): int(row['exposures']) for row in media}
    for target in targets:
        target['exposures'] = acquired.get(target['hole'], 0)
    known={target['hole'] for target in targets}
    missing=set(acquired)-known
    if missing:
        try:
            markers={str(marker['hole']):marker for marker in store.geometry(gid,transform)['markers']}
        except (OSError,ValueError,KeyError):
            markers={}
        for hole in sorted(missing):
            marker=markers.get(hole)
            if marker:
                targets.append(dict(hole=hole,x=marker['x'],y=marker['y'],primary=marker.get('anchor'),intensity=None,
                    stdev=None,selected=True,near_grid_bar=False,quality='',smart_ice=None,
                    exposures=acquired[hole],metadata_missing=True))
    note=''
    unresolved=len(missing-{target['hole'] for target in targets})
    if unresolved:note=f'{unresolved} acquired holes lack target coordinates and remain included during particle export.'
    elif not targets:note='No target locations were recorded for this GridSquare.'
    return dict(targets=targets, note=note)


def default_state(store):
    settings = session_filter(store)
    return dict(mode='intensity', low=settings.get('minimum'), high=settings.get('maximum'), overrides={})


def selection_state(store, gid):
    saved = store.annotation('hole-selection:settings')
    per_grid = store.annotation('hole-selection:'+gid)
    # Read the early per-grid format once without losing existing brush work.
    state = saved or per_grid or default_state(store)
    clean = default_state(store)
    # Earlier releases exposed EPU's final Selected flag as an active rule.
    # Keep that information in the histogram, but migrate the working rule to
    # the more transparent recorded-intensity range.
    if state.get('mode') in ('all', 'intensity'):
        clean['mode'] = state['mode']
    clean['low'] = _number(state.get('low'))
    clean['high'] = _number(state.get('high'))
    clean['overrides'] = {str(k): bool(v) for k, v in (per_grid.get('overrides') or {}).items()}
    return clean


def resolve_selection(targets, state):
    by_hole = {str(t['hole']): t for t in targets}
    result = {}
    for hole, target in by_hole.items():
        if target.get('metadata_missing'):
            included = True
        elif state['mode'] == 'all':
            included = True
        elif state['mode'] == 'intensity':
            value = target.get('intensity')
            included = value is not None and (state['low'] is None or value >= state['low']) and (state['high'] is None or value <= state['high'])
        else:
            anchor = by_hole.get(str(target.get('primary'))) if target.get('primary') else None
            included = bool((anchor or target).get('selected'))
        if hole in state['overrides']:
            included = state['overrides'][hole]
        result[hole] = included
    return result


def save_selection(store, gid, value, transform='identity'):
    store.grid(gid)
    state = default_state(store)
    mode = value.get('mode', 'epu')
    if mode not in ('all', 'intensity'):
        raise ValueError('Invalid automatic selection mode')
    state.update(mode=mode, low=_number(value.get('low')), high=_number(value.get('high')))
    valid = {t['hole'] for t in grid_targets(store, gid, transform)['targets']}
    state['overrides'] = {str(k): bool(v) for k, v in (value.get('overrides') or {}).items() if str(k) in valid}
    settings={key:state[key] for key in ('mode','low','high')}
    store.execute('INSERT OR REPLACE INTO annotations VALUES (?,?)', ('hole-selection:settings', json.dumps(settings)))
    store.execute('INSERT OR REPLACE INTO annotations VALUES (?,?)', ('hole-selection:'+gid, json.dumps(dict(overrides=state['overrides']))))
    return selection_payload(store, gid, transform)


def selection_payload(store, gid, transform='identity'):
    payload = grid_targets(store, gid, transform)
    state = selection_state(store, gid)
    resolved = resolve_selection(payload['targets'], state)
    density = getattr(store, 'density', None)
    particles = defaultdict(int)
    if density:
        with density.lock:
            for record in density.records.values():
                if record['grid_id'] == gid:
                    particles[str(record['hole'])] += int(record['particles'])
    included = [h for h, keep in resolved.items() if keep]
    payload.update(state=state, resolved=resolved, session=session_filter(store),
                   counts=dict(holes=len(included), acquired_holes=sum(bool(t['exposures']) and resolved.get(t['hole'], False) for t in payload['targets']),
                               exposures=sum(t['exposures'] for t in payload['targets'] if resolved.get(t['hole'], False)),
                               particles=sum(particles[h] for h in included)), particles=dict(particles))
    return payload


def intensity_histogram(store, transform='identity', bins=64):
    """Session-wide recorded EPU intensity distribution for the filter editor."""
    values=[];selected=[]
    for grid in store.grids():
        # Histogramming does not need images, dimensions, acquisition counts or
        # coordinate transforms. Reading only the filter fields avoids dozens
        # of additional network-share lookups on large sessions.
        metadata=_grid_metadata(store,grid['id'])
        if not metadata:
            continue
        records=[]
        for pair in xml_root(metadata).iter('KeyValuePairOfintTargetLocationXmlBpEWF4JT'):
            target=pair.find('value')
            if target is None:
                continue
            filt=target.find('FilterProperties')
            value=_number(filt.findtext('PixelIntensityMean')) if filt is not None else None
            if value is not None:
                hole=target.findtext('Id') or pair.findtext('key') or str(len(records))
                primary=target.findtext('PrimaryId')
                records.append((hole,primary if primary and primary!='0' else None,value,_bool(target.findtext('Selected'))))
        flags={hole:chosen for hole,_,_,chosen in records}
        for _,primary,value,chosen in records:
            values.append(value);selected.append(flags.get(primary,chosen) if primary else chosen)
    if not values:return dict(edges=[],counts=[],selected=[],total=0,note='No recorded EPU hole intensities were found.')
    values=np.asarray(values);selected=np.asarray(selected,dtype=bool)
    low=float(values.min());high=float(values.max())
    if high<=low:high=low+1
    counts,edges=np.histogram(values,bins=bins,range=(low,high))
    chosen,_=np.histogram(values[selected],bins=edges)
    return dict(edges=edges.tolist(),counts=counts.astype(int).tolist(),selected=chosen.astype(int).tolist(),
                total=len(values),epu_selected=int(selected.sum()),minimum=low,maximum=float(values.max()),
                note='Recorded PixelIntensityMean values; relative image intensity, not calibrated ice thickness.')


def _write_subset(path, source, mask, selected, order=None, chunk=None):
    if chunk is None:chunk=max(10_000,min(250_000,(128*1024*1024)//max(1,source.dtype.itemsize)))
    total=int(mask.sum()) if selected else int(len(mask)-mask.sum())
    output=np.lib.format.open_memmap(path,mode='w+',dtype=source.dtype,shape=(total,))
    position=0
    for start in range(0,len(mask),chunk):
        stop=min(len(mask),start+chunk);choose=np.asarray(mask[start:stop])==selected
        if not np.any(choose):continue
        values=source[start:stop] if order is None else source[order[start:stop]]
        values=values[choose];output[position:position+len(values)]=values;position+=len(values)
    output.flush();del output


def selection_document(store, transform='identity'):
    """Portable resolved selection; sufficient for filtering without reopening EPU data."""
    selections = {}
    selection_details = {}
    holes=[]
    for grid in store.grids():
        payload = selection_payload(store, grid['id'], transform)
        selections[grid['id']] = payload['resolved']
        selection_details[grid['id']] = payload['state']
        holes.extend(dict(grid_square=grid['id'],foil_hole=str(hole),selected=bool(chosen))
                     for hole,chosen in sorted(payload['resolved'].items()))
    exposures=[]
    for row in store.execute("SELECT grid_id,hole,name FROM media WHERE kind='data'"):
        key=exposure_key(row['name'])
        if key:
            exposures.append(dict(key=key,grid_square=row['grid_id'],foil_hole=str(row['hole']),
                                  selected=bool(selections.get(row['grid_id'],{}).get(str(row['hole']),True))))
    source=str(getattr(store,'source',''))
    return dict(format='EPU Mapper FoilHole selection',version=1,source=source,
                grids=selection_details,holes=holes,exposures=exposures,
                note='Resolved selection by EPU exposure key. Source images and particle data are not embedded.')


def _load_selection(value):
    if isinstance(value,(str,Path)):
        value=json.loads(Path(value).read_text(encoding='utf-8'))
    if not isinstance(value,dict) or value.get('format')!='EPU Mapper FoilHole selection' or value.get('version')!=1:
        raise ValueError('Choose an EPU Mapper FoilHole selection file.')
    if not isinstance(value.get('exposures'),list):raise ValueError('Selection file has no exposure mapping.')
    return value


def filter_particle_tables(selection, main_path, pass_path=None, keep_unmatched=False,
                           include_excluded=False, output_directory=None, working_root=None):
    """Apply a portable selection to arbitrary UID-bearing CryoSPARC tables."""
    selection=_load_selection(selection);main_path=Path(main_path);pass_path=Path(pass_path) if pass_path else None
    columns=LocationColumns(main_path,pass_path,required=('location/micrograph_path',));main=columns.main;passthrough=columns.other
    media=defaultdict(list)
    for row in selection['exposures']:
        key=row.get('key')
        if key:media[str(key)].append(row)
    if output_directory:
        folder=Path(output_directory)/('EPU_Mapper_particle_filter_'+uuid.uuid4().hex[:8])
    else:
        if working_root is None:raise ValueError('An output directory is required.')
        folder=Path(working_root)/uuid.uuid4().hex
    folder.mkdir(parents=True)
    mask_path=folder/'.selection-mask';keep=np.memmap(mask_path,mode='w+',dtype=np.bool_,shape=(len(main),));keep[:]=False
    matched = ambiguous = unmatched = 0
    hole_counts = defaultdict(lambda: dict(total=0, kept=0))
    for start,stop,(paths,) in columns.chunks():
        unique,inverse=np.unique(paths,return_inverse=True);codes=np.full(len(unique),-1,dtype=np.int64);rows=[]
        for i,path in enumerate(unique):
            candidates=media.get(exposure_key(path),[])
            if len(candidates)==1:codes[i]=len(rows);rows.append(candidates[0])
            elif candidates:codes[i]=-2
        particle_codes=codes[inverse];chunk_keep=np.full(stop-start,bool(keep_unmatched),dtype=bool)
        ambiguous+=int((particle_codes==-2).sum());unmatched+=int((particle_codes==-1).sum());matched+=int((particle_codes>=0).sum())
        for code,row in enumerate(rows):
            hits=particle_codes==code;chosen=bool(row.get('selected',True));chunk_keep[hits]=chosen
            item=hole_counts[(row.get('grid_square',''),str(row.get('foil_hole','')))];n=int(hits.sum());item['total']+=n;item['kept']+=n if chosen else 0
        keep[start:stop]=chunk_keep
    keep.flush();kept_count=int(keep.sum());excluded_count=len(main)-kept_count
    _write_subset(folder/'particles_kept.cs',main,keep,True)
    if include_excluded:_write_subset(folder/'particles_excluded.cs',main,keep,False)
    if passthrough is not None:
        _write_subset(folder/'passthrough_kept.cs',passthrough,keep,True,columns.order)
        if include_excluded:_write_subset(folder/'passthrough_excluded.cs',passthrough,keep,False,columns.order)
    with (folder/'hole_selection.csv').open('w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle); writer.writerow(['grid_square','foil_hole','selected','particles_total','particles_kept'])
        for record in sorted(selection.get('holes',[]),key=lambda v:(v.get('grid_square',''),str(v.get('foil_hole','')))):
            gid,hole=record.get('grid_square',''),str(record.get('foil_hole',''))
            counts = hole_counts[(gid, hole)]
            writer.writerow([gid,hole,bool(record.get('selected')),counts['total'],counts['kept']])
    manifest = dict(format='EPU Mapper CryoSPARC particle filter', version=1,
                    source_files=dict(main=main_path.name,**({'passthrough':pass_path.name} if pass_path else {})),
                    particles=dict(total=len(main), kept=kept_count, excluded=excluded_count, matched=matched, unmatched=unmatched, ambiguous=ambiguous),
                    unmatched_policy='keep' if keep_unmatched else 'exclude', selections=selection.get('grids',{}),
                    excluded_tables_included=bool(include_excluded),
                    note='Rows, fields and particle UIDs are preserved. Passthrough rows are UID-aligned to the main table before chunked subsetting.')
    (folder/'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    del keep;mask_path.unlink()
    if output_directory:
        return dict(key='', path=str(folder), **manifest['particles'])
    archive = folder.with_suffix('.zip')
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_STORED) as output:
        for path in sorted(folder.iterdir()):
            output.write(path, path.name)
    shutil.rmtree(folder)
    return dict(key=archive.stem, path=str(archive), **manifest['particles'])


def filter_particles_from_file(selection_path, main_path, pass_path=None, output_directory=None,
                               keep_unmatched=False, include_excluded=False):
    return filter_particle_tables(selection_path,main_path,pass_path,keep_unmatched,include_excluded,output_directory)


def export_particles(store, density=None, transform='identity', keep_unmatched=False,
                     include_excluded=False, output_directory=None, sources=None):
    """Create filtered .cs tables from an explicit source or the mapped density source."""
    if sources is None:
        if density is None: sources={}
        else:
            with density.lock:sources=dict(density.sources or {})
    if not sources.get('main'):
        raise ValueError('Choose a CryoSPARC particle file for filtering.')
    return filter_particle_tables(selection_document(store,transform),sources['main'],sources.get('passthrough'),
                                  keep_unmatched,include_excluded,output_directory,store.root/'particle-exports')
