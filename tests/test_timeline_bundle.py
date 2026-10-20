"""Clip-pack manifest mapping, immutable registration and soundtrack routing."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from ComfyUI_H3_Continuum_Join.timeline.bundle import import_bundle
from ComfyUI_H3_Continuum_Join.timeline.model import blank_project, compile_project


class Assets:
    def __init__(self, root):
        self.root = root
        self.calls = []

    def upload_path(self):
        import uuid
        return self.root / uuid.uuid4().hex

    def adopt(self, path, checksum, name, *, cancel):
        cancel()
        assert checksum == hashlib.sha256(path.read_bytes()).hexdigest()
        self.calls.append(name)
        if name.endswith('.m4a'):
            return dict(id=checksum, name=name, kind='audio', duration='14',
                        video=None, audio={'start_time': '0', 'sample_rate': '44100'})
        return dict(id=checksum, name=name, kind='video',
                    duration='7' if name == 'chunk_01.mp4' else '124/25',
                    video={'start_time': '0.08' if name == 'chunk_01.mp4' else '0',
                           'avg_frame_rate': '25/1'}, audio={'sample_rate': '44100'})


def manifest():
    return {'global_prefix': 'Shared identity', 'segments': [
        {'chunk': 1, 'filename': 'chunk_01.mp4', 'start_seconds': 0,
         'end_seconds': 7, 'duration_seconds': 7, 'prompt': 'First action'},
        {'chunk': 2, 'filename': 'chunk_02.mp4', 'start_seconds': 7,
         'end_seconds': 11.96, 'duration_seconds': 4.96, 'prompt': 'Second action'},
    ]}


def pack(tmp_path, value=None, extras=None):
    path = tmp_path / 'pack.zip'
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('scene/prompts.json', json.dumps(value or manifest()))
        z.writestr('scene/chunk_01.mp4', b'picture one')
        z.writestr('scene/chunk_02.mp4', b'picture two')
        z.writestr('scene/original_audio.m4a', b'continuous PCM source')
        z.writestr('scene/REVIEW/notes.txt', b'not imported')
        for name, content in (extras or {}).items():
            z.writestr(name, content)
    return path


def test_pack_preserves_pts_global_prompt_and_one_soundtrack(tmp_path):
    current = blank_project()
    original = copy.deepcopy(current)
    assets = Assets(tmp_path)
    p = import_bundle(pack(tmp_path), assets, current)
    assert current == original
    assert p['project_id'] == current['project_id']
    assert (p['width'], p['height']) == (768, 512)
    assert p['duration_frames'] == 287  # Round absolute 11.96s on the 24fps grid.
    assert p['chunk_frames'] == 168
    assert p['selection'] == [0, 287]
    assert p['operation'] == 'recreate'
    assert len(p['assets']) == 3
    assert assets.calls == ['chunk_01.mp4', 'chunk_02.mp4', 'original_audio.m4a']
    windows = compile_project(p)['jobs'][0]['windows']
    assert [w['prompt'] for w in windows] == [
        'Shared identity\nFirst action', 'Shared identity\nSecond action']
    assert windows[0]['video'][0]['source'] == ['2/25', '177/25']
    assert windows[1]['video'][0]['source'][0] == '0'
    assert all(len(w['guide']) == 1 for w in windows)
    assert windows[1]['guide'][0]['source'][0] == '7'
    assert all(c['audio_role'] == 'neither' for c in p['tracks'][0]['clips'])
    assert p['tracks'][-1]['clips'][0]['audio_role'] == 'both'
    assert p['audio_sample_rate'] == 44100
    assert sorted(x.name for x in tmp_path.iterdir()) == ['pack.zip']


@pytest.mark.parametrize('mutation,match', [
    (lambda v: v['segments'][1].update(start_seconds=6), 'contiguous'),
    (lambda v: v['segments'][0].update(filename='../outside.mp4'), 'relative archive'),
    (lambda v: v['segments'][0].update(duration_seconds=8), 'disagrees'),
    (lambda v: v.update(global_prefix=42), 'must be text'),
    (lambda v: v['segments'][1].update(prompt=None), 'prompt must be text'),
])
def test_invalid_pack_leaves_current_project_untouched(tmp_path, mutation, match):
    value = manifest()
    mutation(value)
    p = blank_project()
    before = copy.deepcopy(p)
    assets = Assets(tmp_path)
    with pytest.raises(ValueError, match=match):
        import_bundle(pack(tmp_path, value), assets, p)
    assert p == before
    assert not assets.calls


def test_ambiguous_soundtrack_requires_manifest_filename(tmp_path):
    assets = Assets(tmp_path)
    path = pack(tmp_path, extras={'scene/other.m4a': b'other'})
    with pytest.raises(ValueError, match='Multiple soundtracks'):
        import_bundle(path, assets, {})
    assert not assets.calls
    value = manifest()
    value['audio_filename'] = 'original_audio.m4a'
    p = import_bundle(pack(tmp_path, value, {'scene/other.m4a': b'other'}), assets, {})
    assert len(p['assets']) == 3


def test_cancelled_pack_cleans_partial_file_and_never_changes_project(tmp_path):
    p = blank_project()
    old = copy.deepcopy(p)
    def cancel():
        raise InterruptedError('cancelled')
    with pytest.raises(InterruptedError):
        import_bundle(pack(tmp_path), Assets(tmp_path), p, cancel=cancel)
    assert p == old
    assert [x.name for x in tmp_path.iterdir()] == ['pack.zip']
