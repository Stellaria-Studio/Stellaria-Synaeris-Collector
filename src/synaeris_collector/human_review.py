"""Review sealed human streams and candidate coverage without inventing outcomes."""
import bisect
from collections import Counter, defaultdict
import json
from pathlib import Path
import subprocess

import cv2
import numpy as np
from .storage import atomic_json, iter_rows

def percentiles(values):
    return {f'p{p}': float(np.percentile(values, p)) for p in (50, 95, 100)} if values else None

def review(path, output):
    path, output = Path(path).resolve(), Path(output).resolve()
    manifest = json.loads((path/'manifest.json').read_text(encoding='utf-8-sig'))
    if manifest['status'] != 'complete' or manifest.get('synthetic'):
        raise ValueError('Only sealed real recordings can be reviewed')
    output.mkdir(parents=True, exist_ok=True)
    frames = list(iter_rows(path, 'frames'))
    stamps = [row['monotonic_timestamp'] for row in frames]
    if not frames or any(left >= right for left, right in zip(stamps, stamps[1:])):
        raise ValueError('Invalid acquisition mapping')
    if any(row['payload']['frame_index'] != i for i, row in enumerate(frames)):
        raise ValueError('Invalid encoded frame mapping')
    duration = manifest['duration_ns']/1e9
    sources, kinds, keys, chords = Counter(), Counter(), Counter(), Counter()
    held = set()
    offsets = []
    outside = 0
    for row in sorted(iter_rows(path, 'input'), key=lambda row:(row['monotonic_timestamp'],row['sequence'])):
        sources[row['source']+':'+row['actor']] += 1
        kinds[row['kind']] += 1
        stamp = row['monotonic_timestamp']
        outside += not 0 <= stamp <= manifest['duration_ns']
        idx = bisect.bisect_left(stamps, stamp)
        nearest = min((stamps[i] for i in (idx-1,idx) if 0 <= i < len(stamps)), key=lambda value:abs(value-stamp))
        offsets.append(abs(nearest-stamp)/1e6)
        data = row['payload']
        if row['source'] == 'win32_low_level_hook' and row['actor'] == 'HUMAN' and not data.get('injected', True):
            if row['kind'] == 'KEY_DOWN':
                held.add(data['vk'])
                if not data.get('repeat'):
                    keys[str(data['vk'])] += 1
                    if len(held)>1:
                        chords['+'.join(map(str, sorted(held)))] += 1
            elif row['kind'] == 'KEY_UP':
                held.discard(data['vk'])
        elif row['kind'] == 'INPUT_STATE_RESET':
            held.clear()
    scans, queue_delays, observation_ages, texts, rois = [], [], [], [], Counter()
    for row in iter_rows(path, 'ocr'):
        if row['kind'] == 'OCR_SCAN':
            scans.append(row['payload'].get('latency_ms',0))
            if isinstance(row['payload'].get('queue_delay_ms'), (int,float)):
                queue_delays.append(row['payload']['queue_delay_ms'])
            if isinstance(row['payload'].get('observation_age_ms'), (int,float)):
                observation_ages.append(row['payload']['observation_age_ms'])
        elif row['kind'] == 'OCR_TEXT':
            texts.append(row['payload'].get('confidence',0))
            rois[row['payload'].get('roi','unknown')] += 1
    perf = [row['payload'] for row in iter_rows(path,'perf')]
    metrics = {}
    for key in ('collector_rss_bytes','collector_cpu_pct','vram_used_mib','capture_latency_ms','encode_submit_latency_ms'):
        metrics[key] = percentiles([row[key] for row in perf if isinstance(row.get(key),(int,float))])
    index = json.loads((path/'auto_index.json').read_text(encoding='utf-8')) if (path/'auto_index.json').exists() else None
    scene_seconds = defaultdict(float)
    if index:
        for span in index['segments']:
            scene_seconds[span['scene']] += (span['end_ns']-span['start_ns'])/1e9
    saved_qc = json.loads((path/'quality.json').read_text(encoding='utf-8')) if (path/'quality.json').exists() else None
    qc_consistent = bool(saved_qc and saved_qc['episode_id']==manifest['episode_id']
        and saved_qc['counts']==manifest['counts'] and saved_qc['decoded_frames']==len(frames))
    result = {'episode_id': manifest['episode_id'], 'status':manifest['status'],
        'duration_seconds':duration, 'acquisition_frames':len(frames), 'capture_fps':len(frames)/duration,
        'capture_available_fraction':sum(bool(row['payload'].get('capture_available')) for row in frames)/len(frames),
        'game_focused_fraction':sum(bool(row['payload'].get('game_focused')) for row in frames)/len(frames),
        'fresh_frame_fraction':sum(bool(row['payload'].get('fresh')) for row in frames)/len(frames),
        'input_sources':dict(sources), 'input_kinds':dict(kinds), 'human_nonrepeat_key_down':dict(keys),
        'human_chord_down_counts':dict(chords.most_common(16)), 'input_timestamp_outside_episode':outside,
        'nearest_acquisition_offset_ms':percentiles(offsets), 'ocr_latency_ms':percentiles(scans),
        'ocr_queue_delay_ms':percentiles(queue_delays),
        'ocr_observation_age_ms':percentiles(observation_ages),
        'ocr_confidence':percentiles(texts), 'ocr_roi_readings':dict(rois),
        'observation_request_drops':manifest.get('ocr_queue_drops'),
        'observation_requests_coalesced':manifest.get('ocr_requests_coalesced', manifest.get('ocr_queue_drops')),
        'ocr_scheduler':{'latest_frame_wins':manifest.get('ocr_latest_frame_wins'),
            'max_rois_per_frame':manifest.get('ocr_max_rois_per_frame'),
            'requests_submitted':manifest.get('ocr_requests_submitted'),
            'frames_processed':manifest.get('ocr_frames_processed'),
            'regions_scanned':manifest.get('ocr_regions_scanned'),
            'regions_cached':manifest.get('ocr_regions_cached')},
        'auto_scene_seconds':dict(scene_seconds), 'auto_label_counts':index['label_counts'] if index else None,
        'auto_segments':len(index['segments']) if index else None,
        'scene_declaration':manifest['config'].get('scenario'), 'collection_profile':manifest.get('collection_profile',{}).get('id'),
        'semantic_group_declarations':{key:manifest['config'].get(key) for key in ('region','quest','puzzle','poi')},
        'saved_full_decode_qc':saved_qc, 'saved_qc_matches_manifest_and_frames':qc_consistent,
        'perf':metrics, 'manual_marker_bytes':(path/'annotations.jsonl').stat().st_size,
        'game_fps':None, 'training_semantics_verified':False,
        'limitation':'Nearest-frame offset tests timestamp alignment only, not action onset or effect. Raw mouse identity remains UNKNOWN. Scene declarations/candidates do not authenticate outcomes.'}
    atomic_json(output/'review.json', result)
    return result

def sample_sheet(path, output, count=12):
    """One ordered, strict mother-video decode; selected encoded ordinals only."""
    from .capture import find_ffmpeg, hidden_process_options
    path, output = Path(path).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    frames = list(iter_rows(path,'frames'))
    chosen = sorted(set(np.linspace(0,max(0,len(frames)-1),count,dtype=int).tolist()))
    expression = '+'.join(f'eq(n\\,{i})' for i in chosen)
    diagnostics = []
    for backend, args in (('cuda',['-hwaccel','cuda','-hwaccel_output_format','cuda']),('cpu',[])):
        destination = output/backend
        destination.mkdir(exist_ok=True)
        download = ',hwdownload,format=nv12' if backend=='cuda' else ''
        command = [find_ffmpeg(),'-hide_banner','-loglevel','error','-xerror',*args,
            '-i',str(path/'visual.mkv'),'-map','0:v:0','-an','-vf',f'select={expression}{download},scale=640:360',
            '-vsync','0','-y',str(destination/'sample_%03d.png')]
        process = subprocess.run(command,capture_output=True,timeout=max(90,len(frames)/30+60),**hidden_process_options())
        pictures = sorted(destination.glob('sample_*.png'))
        if process.returncode==0 and len(pictures)==len(chosen):
            break
        diagnostics.append({'backend':backend,'error':process.stderr.decode(errors='replace')[-2000:]})
    else:
        raise RuntimeError('Strict video sample decode failed: '+str(diagnostics))
    mapping = [{'file':str(file), 'frame_index':ordinal,
        'acquisition_timestamp_ns':frames[ordinal]['monotonic_timestamp'],
        'video_pts_ns':frames[ordinal]['payload'].get('video_pts_ns')}
        for file,ordinal in zip(pictures,chosen)]
    for page in range((len(pictures)+5)//6):
        sheet=np.full((2*386,3*640,3),18,dtype=np.uint8)
        for i,item in enumerate(mapping[page*6:(page+1)*6]):
            frame=cv2.imdecode(np.fromfile(item['file'],dtype=np.uint8),cv2.IMREAD_COLOR)
            x,y=(i%3)*640,(i//3)*386
            sheet[y:y+360,x:x+640]=frame
            label=f"{item['acquisition_timestamp_ns']/1e9:.2f}s / frame {item['frame_index']}"
            cv2.putText(sheet,label,(x+8,y+379),0,.55,(230,230,230),1)
        cv2.imencode('.jpg',sheet)[1].tofile(output/f'sheet_{page:02d}.jpg')
    atomic_json(output/'mapping.json',{'decode_backend':backend,'diagnostics':diagnostics,
        'strict_decode_reached_end':True,'samples':mapping})
    return mapping
