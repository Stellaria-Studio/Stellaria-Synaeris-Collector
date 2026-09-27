import json
from pathlib import Path
import shutil
import zipfile

import pytest

from synaeris_collector.updater import (ReleasePlan, newer_local_executable,
    select_release, sha256, stage_release, version_tuple)
from synaeris_collector.version import REPOSITORY


def _archive(path, version, *, bad_path=False):
    root = f'SynaerisCollector-Human-{version}'
    executable = b'fixture executable, never run'
    manifest = {'schema': 'synaeris-collector-release-v1', 'version': version,
        'repository': REPOSITORY, 'executable_sha256': __import__('hashlib').sha256(executable).hexdigest()}
    with zipfile.ZipFile(path, 'w') as package:
        package.writestr(f'{root}/SynaerisCollector.exe', executable)
        package.writestr(f'{root}/collector-release.json', json.dumps(manifest))
        if bad_path:
            package.writestr(f'{root}/../outside.txt', b'bad')
    return ReleasePlan(version, 'v'+version, 123, path.stat().st_size, sha256(path))


def test_release_selection_requires_newer_stable_digest():
    assert version_tuple('v0.6.1') > version_tuple('0.6.0')
    with pytest.raises(ValueError):
        version_tuple('0.6.1rc1')
    asset = {'id': 7, 'name': 'SynaerisCollector-Human-0.6.1.zip',
        'state': 'uploaded', 'size': 42, 'digest': 'sha256:'+'a'*64}
    release = {'tag_name': 'v0.6.1', 'draft': False, 'prerelease': False,
        'assets': [asset]}
    assert select_release(release, current='0.6.0').sha256 == 'a'*64
    assert select_release(release, current='0.6.1') is None
    assert select_release(dict(release, prerelease=True), current='0.6.0') is None
    with pytest.raises(ValueError, match='SHA-256'):
        select_release(dict(release, assets=[dict(asset, digest=None)]), current='0.6.0')


def test_verified_update_stages_outside_recordings_and_activates_on_next_launch(tmp_path):
    archive = tmp_path/'test.zip'
    plan = _archive(archive, '0.6.1')
    root = tmp_path/'updates'
    destination = stage_release(plan, root=root,
        downloader=lambda _, target: shutil.copyfile(archive, target))
    assert destination.is_file()
    assert destination == newer_local_executable(current='0.6.0', root=root)
    assert newer_local_executable(current='0.6.1', root=root) is None
    assert (root/'current.json').is_file()
    assert list(root.glob('staging-*')) == []


def test_update_rejects_path_traversal_and_does_not_publish_pointer(tmp_path):
    archive = tmp_path/'bad.zip'
    plan = _archive(archive, '0.6.1', bad_path=True)
    root = tmp_path/'updates'
    with pytest.raises(ValueError, match='Unsafe path'):
        stage_release(plan, root=root,
            downloader=lambda _, target: shutil.copyfile(archive, target))
    assert not (root/'current.json').exists()


def test_update_rejects_archive_digest_mismatch(tmp_path):
    archive = tmp_path/'test.zip'
    plan = _archive(archive, '0.6.1')
    plan = ReleasePlan(plan.version, plan.tag, plan.asset_id, plan.size, '0'*64)
    with pytest.raises(ValueError, match='SHA-256'):
        stage_release(plan, root=tmp_path/'updates',
            downloader=lambda _, target: shutil.copyfile(archive, target))
