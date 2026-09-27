"""Cheap post-recording scene/decision candidates. Never edits mother streams or rewards."""
from collections import Counter, defaultdict
from difflib import SequenceMatcher
import html
import json
from pathlib import Path
import re
from .storage import atomic_json, iter_rows

VERSION = 'auto-index-v2'
SECOND = 1_000_000_000
SCENE_NAMES = {'world': '大世界', 'map': '地图', 'dialogue': '对白',
               'menu': '菜单', 'loading': '加载', 'title': '入口',
               'unknown': '未确定', 'unavailable': '画面不可用',
               'map_candidate': '地图候选', 'dialogue_candidate': '对白候选'}
LABEL_NAMES = {'item_receipt_candidate': '拾取提示候选', 'quest_text_candidate': '任务文字候选',
    'subtitle_text_candidate': '字幕文字候选',
    'quest_text_changed_candidate': '任务文字变化候选', 'puzzle_clue_candidate': '机关线索候选',
    'combat_objective_candidate': '战斗目标候选', 'input_quiet_candidate': '输入停顿候选',
    'scene_change_candidate': '场景变化候选', 'focus_lost': '切出游戏', 'focus_gained': '返回游戏',
    'tool_failed': '工具失败', 'tool_cancelled': '工具取消', 'collector_error': '采集异常',
    'capture_deadline_miss': '采集延迟', 'manual_think': '人工思考标记', 'manual_mistake': '人工错误标记',
    'manual_discovery': '人工发现标记', 'manual_subgoal_complete': '人工子目标标记'}
ACTIVITY_NAMES = {'movement_input': '移动键输入', 'skill_key_input': '技能键输入',
                  'mouse_delta_input': '鼠标位移输入', 'subtitle_text': '字幕文字'}

def quest_signature(text):
    compact=re.sub(r'\s+','',text)
    compact=re.sub(r'\[?\d+(?:\.\d+)?m\]?','',compact,flags=re.I)
    return re.sub(r'\[?V\]?点击追踪|\[?V\]?暂停追踪','',compact,flags=re.I)

def same_quest(left,right):
    return (re.findall(r'\d+',left)==re.findall(r'\d+',right) and
            SequenceMatcher(None,left,right).ratio()>=.92)

def native_scene(mode):
    name = str(mode).upper().replace('_', '')
    if name in {'MAIN', 'WORLD', 'WORLDUI'}:
        return 'world'
    if 'MAP' in name:
        return 'map'
    if 'TALK' in name or 'DIALOG' in name:
        return 'dialogue'
    if 'LOAD' in name:
        return 'loading'
    if 'TITLE' in name:
        return 'title'
    if 'MENU' in name:
        return 'menu'
    return None

def organize(episode):
    path = Path(episode).resolve()
    manifest = json.loads((path / 'manifest.json').read_text(encoding='utf-8-sig'))
    if manifest['status'] == 'recording':
        raise ValueError('停止录制并封存后才能自动整理')
    duration = int(manifest.get('duration_ns', 0))
    if duration <= 0:
        raise ValueError('缺少有效时长')
    bins = defaultdict(lambda: {'frames': 0, 'available': 0, 'focused': 0,
                               'texts': [], 'ui': None, 'inputs': 0, 'keys': [], 'signals': [], 'activities': set()})
    invalid = 0
    def bucket(row):
        nonlocal invalid
        stamp = row['monotonic_timestamp']
        if not 0 <= stamp <= duration:
            invalid += 1
            return None
        return bins[min(stamp // SECOND, (duration-1) // SECOND)]
    def signal(row, label, score, detail=None):
        target = bucket(row)
        if target is not None:
            target['signals'].append({'timestamp_ns': row['monotonic_timestamp'],
                'label': label, 'score': score, 'detail': detail,
                'evidence': {'stream': row['_stream'], 'sequence': row['sequence'],
                             'source': row['source'], 'actor': row['actor']}})
    for row in iter_rows(path, 'frames'):
        target = bucket(row)
        if target is None:
            continue
        data = row['payload']
        index = data['frame_index']
        pts = data.get('video_pts_ns')
        target['frames'] += 1
        target['available'] += bool(data.get('capture_available', data.get('game_focused', False)))
        target['focused'] += bool(data.get('game_focused'))
        point = (row['monotonic_timestamp'], index, pts)
        if 'first' not in target or point[0] < target['first'][0]:
            target['first'] = point
        if 'last' not in target or point[0] > target['last'][0]:
            target['last'] = point
    if not any(target['frames'] for target in bins.values()):
        raise ValueError('缺少真实帧时间映射，不能生成片段')
    for row in iter_rows(path, 'ui'):
        target = bucket(row)
        data = row['payload']
        mode = native_scene(data.get('mode'))
        if target is not None and row['source'].startswith('bettergi') and mode and data.get('valid', True):
            point = (row['monotonic_timestamp'], row['sequence'])
            if target['ui'] is None or point > target['ui']['point']:
                target['ui'] = {'point': point, 'scene': mode, 'source': row['source'],
                                'sequence': row['sequence'], 'score': .9}
    for row in iter_rows(path, 'ocr'):
        target = bucket(row)
        data = row['payload']
        if target is not None and row['kind'] == 'OCR_TEXT' and float(data.get('confidence') or 0) >= .75:
            target['texts'].append(row)
    for row in iter_rows(path, 'input'):
        target = bucket(row)
        if target is None:
            continue
        target['inputs'] += 1
        if row['kind']=='MOUSE_DELTA':
            target['activities'].add('mouse_delta_input')
        if row['kind'] in {'KEY_DOWN', 'KEY_UP', 'INPUT_STATE_RESET'}:
            target['keys'].append(row)
    for stream in ('tools', 'events'):
        for row in iter_rows(path, stream):
            row['_stream'] = stream
            kind = row['kind']
            if kind in {'TOOL_FAILED', 'TOOL_CANCELLED', 'COLLECTOR_ERROR', 'CAPTURE_DEADLINE_MISS', 'FOCUS_LOST', 'FOCUS_GAINED'}:
                signal(row, kind.lower(), .9, row['payload'])
            elif kind in {'THINK', 'MISTAKE', 'DISCOVERY', 'SUBGOAL_COMPLETE'}:
                signal(row, 'manual_' + kind.lower(), 1, row['payload'])
    width = (manifest.get('video') or {}).get('width')
    height = (manifest.get('video') or {}).get('height')
    labels, spans, activity_spans = [], [], []
    last_native = None
    last_quest = None
    pending_quest = None
    seen = {}
    held = set()
    quiet_start = None
    quiet_emitted = False
    last_ocr_scene = None
    for number in range((duration-1)//SECOND+1):
        target = bins[number]
        start, end = number*SECOND, min(duration, (number+1)*SECOND)
        activities=target['activities']
        observed_keys={key[2] for key in held} | {row['payload'].get('vk') for row in target['keys'] if row['kind']=='KEY_DOWN'}
        movement=bool(observed_keys & {65,68,83,87})
        if movement:
            activities.add('movement_input')
        if observed_keys & {69,81,82}:
            activities.add('skill_key_input')
        scene, score, source = 'unknown', 0., 'insufficient_evidence'
        if target['ui']:
            last_native = target['ui']
        native_fresh = last_native and end-last_native['point'][0] <= 2*SECOND
        if native_fresh:
            scene, score, source = last_native['scene'], last_native['score'], last_native['source']
        elif last_ocr_scene and end-last_ocr_scene[0] <= 4*SECOND:
            _, scene, score, source = last_ocr_scene
        if movement and scene=='dialogue_candidate':
            scene,score,source='unknown',0.,'insufficient_evidence'
            last_ocr_scene=None
        by_roi_frame = defaultdict(list)
        for row in target['texts']:
            data = row['payload']
            by_roi_frame[(data.get('roi'), data.get('frame_index'))].append(row)
        for (roi, frame), readings in by_roi_frame.items():
            readings.sort(key=lambda row: row['sequence'])
            text = ' '.join(row['payload']['text'] for row in readings).strip()
            compact = re.sub(r'\s+', '', text)
            first = {**readings[0], '_stream': 'ocr'}
            if roi == 'map' and re.search(r'传送锚点|七天神像|探索度|探索进度|原粹树脂|TeleportWaypoint|StatueofTheSeven', compact, re.I):
                if not native_fresh:
                    scene, score, source = 'map_candidate', .65, 'ocr_pattern'
                    last_ocr_scene = (first['monotonic_timestamp'], scene, score, source)
            if roi == 'dialogue' and width and height:
                for row in readings:
                    box = row['payload'].get('bbox') or []
                    if box and len(row['payload']['text'].strip()) >= 12:
                        x = sum(point[0] for point in box)/len(box)/width
                        y = sum(point[1] for point in box)/len(box)/height
                        if .27 <= x <= .87 and .73 <= y <= .94:
                            activities.add('subtitle_text')
                            signal(first,'subtitle_text_candidate',.55,{'text':row['payload']['text'],'frame_index':frame})
                            if not native_fresh and not movement:
                                scene, score, source = 'dialogue_candidate', .55, 'ocr_position_pattern'
                                last_ocr_scene = (row['monotonic_timestamp'], scene, score, source)
            if roi == 'receipt' and '获得' in compact and len(compact.replace('获得', '')) >= 2:
                signal(first, 'item_receipt_candidate', .65, {'text': text, 'frame_index': frame,
                    'inventory_delta_verified': False})
            if roi == 'quest' and compact:
                signature=quest_signature(text)
                if last_quest is None:
                    signal(first, 'quest_text_candidate', .6, {'text': text, 'quest_id': None})
                    last_quest=signature
                elif same_quest(signature,last_quest):
                    pending_quest=None
                elif pending_quest and same_quest(signature,pending_quest['signature']) and 100_000_000<=first['monotonic_timestamp']-pending_quest['row']['monotonic_timestamp']<=4*SECOND:
                    signal(first, 'quest_text_changed_candidate', .55,
                        {'text': text, 'quest_id': None, 'stage': None,
                         'first_observation_timestamp_ns':pending_quest['row']['monotonic_timestamp'],
                         'first_observation_sequence':pending_quest['row']['sequence'],'confirmations':2})
                    last_quest=signature
                    pending_quest=None
                else:
                    pending_quest={'signature':signature,'row':first,'text':text}
                if re.search(r'机关|谜题|解谜|点亮|解开', compact):
                    signal(first, 'puzzle_clue_candidate', .45, {'text': text, 'puzzle_id': None})
                if re.search(r'击败|战胜|消灭|清除敌人', compact):
                    signal(first, 'combat_objective_candidate', .45, {'text': text})
        for row in sorted(target['keys'], key=lambda row: (row['monotonic_timestamp'], row['sequence'])):
            key = (row['source'], row['actor'], row['payload'].get('vk'))
            if row['kind'] == 'KEY_DOWN':
                held.add(key)
            elif row['kind'] == 'KEY_UP':
                held.discard(key)
            else:
                held.clear()
        if target['available'] == 0:
            scene, score, source = 'unavailable', 1., 'frame_availability'
            activities=set()
            held.clear()
            last_ocr_scene = None
            quiet_start, quiet_emitted = None, False
        elif target['inputs'] or held:
            quiet_start, quiet_emitted = None, False
        else:
            quiet_start = start if quiet_start is None else quiet_start
            if end-quiet_start >= 3*SECOND and not quiet_emitted:
                quiet_emitted = True
                target['signals'].append({'timestamp_ns': quiet_start, 'label': 'input_quiet_candidate',
                    'score': .3, 'detail': {'meaning': 'No recorded input or held key; does not identify thinking or intent'},
                    'evidence': {'stream': 'input', 'source': VERSION, 'actor': 'UNKNOWN'}})
        if target['available']:
            if spans and spans[-1]['scene'] != scene:
                target['signals'].append({'timestamp_ns': start, 'label': 'scene_change_candidate',
                    'score': min(score, .65), 'detail': {'previous': spans[-1]['scene'], 'current': scene},
                    'evidence': {'stream': 'ui', 'source': source, 'actor': 'UNKNOWN',
                                 'frame_index': target['first'][1]}})
            for item in target['signals']:
                key = (item['label'], json.dumps(item['detail'], ensure_ascii=False, sort_keys=True))
                # Suppress repeated notification/prompt reads, not distinct input actions.
                dedup_key = (item['label'], (item.get('detail') or {}).get('text')) if isinstance(item.get('detail'), dict) and 'text' in item['detail'] else key
                previous = seen.get(dedup_key, -10*SECOND)
                if item['timestamp_ns']-previous < 3*SECOND and not item['label'].startswith('manual_'):
                    continue
                seen[dedup_key] = item['timestamp_ns']
                labels.append({**item, 'episode_id': manifest['episode_id'], 'source': VERSION,
                    'weak_label': True, 'verified': False, 'reward': None,
                    'context_start_ns': max(0, item['timestamp_ns']-15*SECOND),
                    'context_end_ns': min(duration, item['timestamp_ns']+20*SECOND)})
        else:
            # Explicit errors/focus events remain useful even on unavailable frames.
            for item in target['signals']:
                if item['label'] in {'focus_lost', 'focus_gained', 'collector_error', 'tool_failed', 'tool_cancelled'} or item['label'].startswith('manual_'):
                    labels.append({**item, 'episode_id': manifest['episode_id'], 'source': VERSION,
                        'weak_label': True, 'verified': False, 'reward': None,
                        'context_start_ns': max(0, item['timestamp_ns']-15*SECOND),
                        'context_end_ns': min(duration, item['timestamp_ns']+20*SECOND)})
        record = {'start_ns': start, 'end_ns': end, 'scene': scene, 'score': score, 'evidence_source': source,
            'activities':sorted(activities),
            'activity_seconds':{name:(end-start)/SECOND for name in activities},
            'frame_count': target['frames'], 'available_frames': target['available'],
            'focused_frames': target['focused'], 'input_event_count': target['inputs'],
            'first': target.get('first'), 'last': target.get('last')}
        if activities:
            if activity_spans and activity_spans[-1]['end_ns']==start and activity_spans[-1]['activities']==record['activities'] and end-activity_spans[-1]['start_ns']<=120*SECOND:
                activity_spans[-1]['end_ns']=end
            else:
                activity_spans.append({'start_ns':start,'end_ns':end,'activities':record['activities'],
                    'episode_id':manifest['episode_id'],'evidence_streams':['input','ocr'],
                    'weak_label':True,'verified':False,'reward':None})
        if spans and spans[-1]['scene'] == scene and spans[-1]['evidence_source'] == source and end-spans[-1]['start_ns'] <= 120*SECOND:
            previous = spans[-1]
            previous['end_ns'] = end
            previous['activities']=sorted(set(previous['activities'])|activities)
            for name,seconds in record['activity_seconds'].items():
                previous['activity_seconds'][name]=previous['activity_seconds'].get(name,0)+seconds
            for key in ('frame_count','available_frames','focused_frames','input_event_count'):
                previous[key] += record[key]
            previous['score'] = min(previous['score'], score)
            if previous['first'] is None:
                previous['first'] = record['first']
            if record['last'] is not None:
                previous['last'] = record['last']
        else:
            spans.append(record)
    for index, record in enumerate(spans):
        first, last = record.pop('first'), record.pop('last')
        record.update(segment_id=f"{manifest['episode_id']}_{index:04d}", episode_id=manifest['episode_id'],
            first_frame_index=first[1] if first else None, last_frame_index=last[1] if last else None,
            video_start_pts_ns=first[2] if first else None, video_last_pts_ns=last[2] if last else None,
            weak_label=True, verified=False, reward=None)
    labels.sort(key=lambda label: label['timestamp_ns'])
    result = {'version': VERSION, 'episode_id': manifest['episode_id'], 'duration_ns': duration,
        'boundary_resolution_ns': SECOND, 'score_is_calibrated_probability': False,
        'segments': spans, 'activity_spans':activity_spans, 'labels': labels, 'label_counts': dict(Counter(item['label'] for item in labels)),
        'scene_counts': dict(Counter(item['scene'] for item in spans)), 'invalid_timestamp_rows': invalid,
        'training_eligibility_changed': False, 'synthetic': manifest.get('synthetic', False),
        'activity_semantics':'Overlapping observed input/text candidates; neither UI modality nor verified game effects.',
        'limitation': 'Logical candidate segments only; mother data and manual annotations unchanged. Unknown scenes retained. No thinking, success, inventory, puzzle or combat outcome verification.'}
    atomic_json(path / 'auto_index.json', result)
    write_html(path, result)
    return result

def write_html(path, result):
    escape = html.escape
    rows = []
    for record in result['segments']:
        pts = record['video_start_pts_ns']
        rows.append('<tr><td>{:.1f}–{:.1f}s</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>'.format(
            record['start_ns']/SECOND, record['end_ns']/SECOND,
            escape(SCENE_NAMES.get(record['scene'], record['scene'])),
            escape('、'.join('{} {:.0f}秒'.format(ACTIVITY_NAMES.get(name,name),record['activity_seconds'][name]) for name in record['activities'])),
            '未验证', '{:.1f}s'.format(pts/SECOND) if pts is not None else '无可用帧', record['input_event_count']))
    def description(item):
        detail = item.get('detail') or {}
        if not isinstance(detail, dict):
            return str(detail)
        if 'text' in detail:
            return str(detail['text'])
        if 'previous' in detail:
            return SCENE_NAMES.get(detail['previous'], detail['previous']) + ' → ' + SCENE_NAMES.get(detail['current'], detail['current'])
        if item['label'] == 'input_quiet_candidate':
            return '没有记录到输入或按住的键；可能在等待，不代表人的思考'
        return str(detail.get('message') or '')
    tags = ''.join('<li>{:.1f}s · {} · {}</li>'.format(item['timestamp_ns']/SECOND,
        escape(LABEL_NAMES.get(item['label'], item['label'])), escape(description(item))) for item in result['labels'])
    style = 'body{font:16px system-ui;background:#111722;color:#e1e7f0;margin:32px}a{color:#7ae4cc}table{border-collapse:collapse;width:100%}td,th{padding:10px;border-bottom:1px solid #354153;text-align:left}li{margin:10px 0;overflow-wrap:anywhere}'
    document = '<!doctype html><html lang="zh"><meta charset="utf-8"><title>采集片段索引</title><style>{}</style><h1>采集片段索引</h1><p>{}</p><p>自动候选供集中复核；无需在游玩时打标签。未修改完整录像或人工标记，未验证成功、思考或解谜结果。字幕可以与移动和操作同时出现，表中保留重叠线索。</p><p><a href="visual.mkv">打开完整录像</a> · <a href="auto_index.json">查看完整候选数据</a></p><p>采集时间和视频播放时间分别记录；按“视频位置”在播放器定位。此索引不要求浏览器支持MKV。</p><table><tr><th>采集时间</th><th>场景</th><th>同时出现的线索</th><th>结果</th><th>视频位置</th><th>输入事件</th></tr>{}</table><h2>候选标签</h2><ul>{}</ul></html>'.format(style, escape(result['episode_id']), ''.join(rows), tags)
    temporary = path / 'auto_index.html.tmp'
    temporary.write_text(document, encoding='utf-8')
    temporary.replace(path / 'auto_index.html')
