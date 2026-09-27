import hashlib
import json
from pathlib import Path
import pytest
from synaeris_collector.auto_index import organize, SECOND
from synaeris_collector.config import Config
from synaeris_collector.storage import EpisodeWriter, atomic_json

def fixture(tmp_path, seconds=8, unavailable=()):
    writer = EpisodeWriter(Config(output=str(tmp_path), capture='synthetic'))
    for index in range(seconds):
        writer.emit('frames', 'VIDEO_FRAME', {'frame_index': index,
            'video_pts_ns': round(index/30*SECOND), 'game_focused': index not in unavailable,
            'capture_available': index not in unavailable}, timestamp=index*SECOND+1000)
    return writer

def seal(writer, seconds):
    writer.close()
    writer.manifest.update(duration_ns=seconds*SECOND, video={'width': 2560, 'height': 1440})
    atomic_json(writer.path/'manifest.json', writer.manifest)
    return writer.path

def test_scene_boundaries_stale_source_black_frames_and_playback_mapping(tmp_path):
    writer = fixture(tmp_path, unavailable=(4,))
    writer.emit('ui','UI_STATE', {'mode':'Main'}, source='bettergi_native', timestamp=100_000_000)
    writer.emit('ui','UI_STATE', {'mode':'Talk'}, source='bettergi_native', timestamp=2*SECOND+100_000_000)
    path = seal(writer, 8)
    result = organize(path)
    scenes = {span['scene'] for span in result['segments']}
    assert {'world','dialogue','unavailable','unknown'} <= scenes
    talk = next(span for span in result['segments'] if span['scene']=='dialogue')
    assert talk['start_ns']==2*SECOND
    assert talk['first_frame_index']==2
    assert talk['video_start_pts_ns']==round(2/30*SECOND)
    assert sum(span['frame_count'] for span in result['segments'])==8
    assert result['segments'][0]['start_ns']==0 and result['segments'][-1]['end_ns']==8*SECOND
    assert all(left['end_ns']==right['start_ns'] for left,right in zip(result['segments'],result['segments'][1:]))

def test_ocr_candidates_deduplicate_preserve_mother_review_and_escape_html(tmp_path):
    writer = fixture(tmp_path)
    for index in (1,2):
        writer.emit('ocr', 'OCR_TEXT', {'roi':'receipt','text':'获得 <img onerror="alert(1)">',
            'confidence':.99,'frame_index':index}, source='rapidocr_cpu', timestamp=index*SECOND+1000)
    writer.emit('ocr','OCR_TEXT', {'roi':'receipt','text':'获得薄荷','confidence':.1,'frame_index':6}, timestamp=6*SECOND)
    writer.mark('MISTAKE', 'wrong turn', timestamp=7*SECOND)
    path = seal(writer,8)
    atomic_json(path/'review.json', {'training_eligible':False})
    originals = {file.name: hashlib.sha256(file.read_bytes()).hexdigest() for file in path.iterdir()}
    result = organize(path)
    assert result['label_counts']['item_receipt_candidate']==1
    manual = next(item for item in result['labels'] if item['label']=='manual_mistake')
    assert manual['context_start_ns']==0 and manual['context_end_ns']==8*SECOND
    assert all(item['verified'] is False and item['reward'] is None for item in result['labels'])
    assert {file:hashlib.sha256((path/file).read_bytes()).hexdigest() for file in originals}==originals
    markup=(path/'auto_index.html').read_text(encoding='utf-8')
    assert '<img' not in markup and '&lt;img' in markup
    assert result['training_eligibility_changed'] is False

def test_no_bgi_map_dialogue_puzzle_objectives_are_only_candidates(tmp_path):
    writer=fixture(tmp_path)
    writer.emit('ocr','OCR_TEXT', {'roi':'map','text':'传送锚点','confidence':.99,'frame_index':0}, timestamp=1000)
    writer.emit('ocr','OCR_TEXT', {'roi':'quest','text':'调查机关解开谜题','confidence':.95,'frame_index':1}, timestamp=SECOND)
    writer.emit('ocr','OCR_TEXT', {'roi':'dialogue','text':'我们应该先到附近寻找相关线索。','confidence':.99,
        'frame_index':2,'bbox':[[700,1100],[1600,1100],[1600,1200],[700,1200]]}, timestamp=2*SECOND)
    result=organize(seal(writer,8))
    assert {'map_candidate','dialogue_candidate'} <= set(result['scene_counts'])
    assert result['label_counts']['puzzle_clue_candidate']==1
    assert 'puzzle_complete' not in result['label_counts']
    assert not any(item['label']=='THINK' for item in result['labels'])

def test_held_key_is_not_input_quiet_or_inferred_thinking(tmp_path):
    writer=fixture(tmp_path,seconds=10)
    writer.emit('input','KEY_DOWN', {'vk':87}, source='win32_low_level_hook', actor='HUMAN', timestamp=0)
    writer.emit('input','KEY_UP', {'vk':87}, source='win32_low_level_hook', actor='HUMAN', timestamp=6*SECOND)
    result=organize(seal(writer,10))
    quiet=[item for item in result['labels'] if item['label']=='input_quiet_candidate']
    assert len(quiet)==1 and quiet[0]['timestamp_ns']>=6*SECOND
    assert all(item['verified'] is False for item in quiet)

def test_active_episode_refused_and_long_unknown_segments_cover_every_frame(tmp_path):
    writer=fixture(tmp_path,seconds=245)
    with pytest.raises(ValueError,match='封存'):
        organize(writer.path)
    assert not (writer.path/'auto_index.json').exists()
    result=organize(seal(writer,245))
    assert sum(span['frame_count'] for span in result['segments'])==245
    assert all(span['end_ns']-span['start_ns'] <= 120*SECOND for span in result['segments'])
    assert len(result['segments'])==3

def test_movement_and_subtitle_overlap_does_not_assert_modal_dialogue(tmp_path):
    writer=fixture(tmp_path,seconds=6)
    writer.emit('input','KEY_DOWN',{'vk':87},source='win32_low_level_hook',actor='HUMAN',timestamp=0)
    writer.emit('input','MOUSE_DELTA',{'dx':10,'dy':2},source='win32_raw_input',actor='UNKNOWN',timestamp=SECOND)
    writer.emit('ocr','OCR_TEXT',{'roi':'dialogue','confidence':.95,'text':'一边移动一边出现的任务字幕内容',
        'frame_index':1,'bbox':[[700,1100],[1700,1100],[1700,1200],[700,1200]]},timestamp=SECOND)
    result=organize(seal(writer,6))
    overlap=next(span for span in result['segments'] if 'subtitle_text' in span['activities'])
    assert {'movement_input','subtitle_text','mouse_delta_input'}<=set(overlap['activities'])
    assert overlap['scene']=='unknown'
    assert 'dialogue_candidate' not in result['scene_counts']
    assert result['label_counts']['subtitle_text_candidate']==1
    precise=next(span for span in result['activity_spans'] if 'subtitle_text' in span['activities'])
    assert precise['start_ns']==SECOND and precise['end_ns']==2*SECOND
    assert not precise['verified'] and precise['reward'] is None

def test_quest_distance_and_ocr_jitter_do_not_emit_changes_but_stable_counter_does(tmp_path):
    writer=fixture(tmp_path,seconds=8)
    texts=['寻找北部营地地下通道附近的三处机关并调查入口[100m][V]点击追踪',
        '寻找北部营地地下通道附近的三处机关并调查入口[98m][V]点击追踪',
        '寻找北部营地地下通道附近的三处机失并调查入口[97m][V]点击追踪',
        '击败守卫(1/4)','击败守卫(1/4)','任意错字','击败守卫(2/4)','击败守卫(2/4)']
    for t,text in enumerate(texts):
        writer.emit('ocr','OCR_TEXT',{'roi':'quest','confidence':.95,'text':text,'frame_index':t},timestamp=t*SECOND)
    result=organize(seal(writer,8))
    changes=[item for item in result['labels'] if item['label']=='quest_text_changed_candidate']
    assert len(changes)==2
    assert [item['timestamp_ns']//SECOND for item in changes]==[4,7]
    assert all(item['detail']['confirmations']==2 for item in changes)
    assert [item['detail']['text'] for item in changes]==['击败守卫(1/4)','击败守卫(2/4)']
