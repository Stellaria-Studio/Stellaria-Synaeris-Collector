"""Stage verified GitHub releases without touching active recordings or data."""

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from .version import APP_VERSION, REPOSITORY


MAX_ARCHIVE_BYTES = 1_000_000_000
MAX_EXPANDED_BYTES = 2_000_000_000
MAX_FILES = 20_000
DOWNLOAD_CHUNK_BYTES = 4*1024*1024
EXE_NAME = 'SynaerisCollector.exe'


def version_tuple(value):
    value = str(value).removeprefix('v')
    parts = value.split('.')
    if len(parts) != 3 or any(not part.isascii() or not part.isdecimal()
                             or (len(part) > 1 and part.startswith('0')) for part in parts):
        raise ValueError('Expected a stable major.minor.patch release')
    return tuple(int(part) for part in parts)


def update_root():
    root = Path(os.environ.get('LOCALAPPDATA') or Path.home()/'.local/share')
    return root/'Stellaria Synaeris Collector'/'updates'


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class ReleasePlan:
    version: str
    tag: str
    size: int
    sha256: str
    executable_sha256: str


def select_release(metadata, *, current=APP_VERSION):
    if metadata.get('schema') != 'synaeris-collector-update-v1':
        raise ValueError('Unknown Collector update manifest')
    version = metadata.get('version', '')
    tag = metadata.get('tag', '')
    if tag != 'v'+version:
        raise ValueError('Update version and tag disagree')
    if version_tuple(version) <= version_tuple(current):
        return None
    expected = f'SynaerisCollector-Human-{version}.zip'
    if metadata.get('asset_name') != expected:
        raise ValueError('Update archive name is invalid')
    digest = metadata.get('archive_sha256', '')
    executable_digest = metadata.get('executable_sha256', '')
    for value in (digest, executable_digest):
        if not isinstance(value, str) or len(value) != 64 or any(
                char not in '0123456789abcdefABCDEF' for char in value):
            raise ValueError('Update SHA-256 digest is malformed')
    size = metadata.get('archive_size')
    if not isinstance(size, int) or not 0 < size <= MAX_ARCHIVE_BYTES:
        raise ValueError('Release archive metadata is invalid')
    return ReleasePlan(version, tag, size, digest.lower(), executable_digest.lower())


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        target = urllib.parse.urlparse(newurl)
        if target.scheme != 'https' or not (target.hostname in {'github.com', 'api.github.com'}
                or (target.hostname or '').endswith('.githubusercontent.com')):
            raise ValueError('Update download redirected outside GitHub HTTPS')
        redirect = super().redirect_request(request, fp, code, message, headers, newurl)
        if urllib.parse.urlparse(request.full_url).hostname != target.hostname:
            for location in (redirect.headers, redirect.unredirected_hdrs):
                location.pop('Authorization', None)
                location.pop('authorization', None)
        return redirect


def _opener():
    return urllib.request.build_opener(_SafeRedirect())


def _headers(accept):
    return {'Accept': accept, 'User-Agent': 'Stellaria-Synaeris-Collector',
            'X-GitHub-Api-Version': '2022-11-28'}


def latest_release():
    url = f'https://github.com/{REPOSITORY}/releases/latest/download/collector-update.json'
    request = urllib.request.Request(url,
        headers={**_headers('application/json'), 'Cache-Control': 'no-cache'})
    with _opener().open(request, timeout=20) as response:
        if response.status != 200:
            raise RuntimeError(f'GitHub update check returned HTTP {response.status}')
        if int(response.headers.get('Content-Length') or 0) > 16_384:
            raise ValueError('Update manifest exceeds size limit')
        payload = response.read(16_385)
        if len(payload) > 16_384:
            raise ValueError('Update manifest exceeds size limit')
        return json.loads(payload)


def download_release(plan, destination):
    url = f'https://github.com/{REPOSITORY}/releases/download/{plan.tag}/SynaerisCollector-Human-{plan.version}.zip'
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    count = destination.stat().st_size if destination.exists() else 0
    if count > plan.size or (count == plan.size and sha256(destination) != plan.sha256):
        destination.write_bytes(b'')
        count = 0
    if count == plan.size:
        return
    with destination.open('ab') as stream:
        while count < plan.size:
            end = min(count+DOWNLOAD_CHUNK_BYTES, plan.size)-1
            request = urllib.request.Request(url,
                headers={**_headers('application/octet-stream'),
                         'Range': f'bytes={count}-{end}'})
            with _opener().open(request, timeout=30) as response:
                if (response.status != 206 or response.headers.get('Content-Range')
                        != f'bytes {count}-{end}/{plan.size}'):
                    raise ValueError('GitHub asset returned a different byte range')
                if response.headers.get_content_type() == 'application/json':
                    raise RuntimeError('GitHub returned asset metadata instead of ZIP bytes')
                length = end-count+1
                chunk = bytearray()
                while len(chunk) < length:
                    block = response.read(min(64*1024, length-len(chunk)))
                    if not block:
                        raise RuntimeError('GitHub asset download ended mid-range')
                    chunk.extend(block)
            stream.write(chunk)
            stream.flush()
            count += length
    if sha256(destination) != plan.sha256:
        raise ValueError('Update archive size or SHA-256 mismatch')


def _safe_extract(archive, destination, version):
    expected_root = f'SynaerisCollector-Human-{version}'
    with zipfile.ZipFile(archive) as package:
        entries = package.infolist()
        if len(entries) > MAX_FILES or sum(entry.file_size for entry in entries) > MAX_EXPANDED_BYTES:
            raise ValueError('Update ZIP exceeds expansion limits')
        for entry in entries:
            name = PurePosixPath(entry.filename.replace('\\', '/'))
            if (name.is_absolute() or '..' in name.parts or not name.parts
                    or name.parts[0] != expected_root or ':' in entry.filename):
                raise ValueError('Unsafe path in update ZIP')
            if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('Update ZIP contains a symbolic link')
        package.extractall(destination)
    payload = Path(destination)/expected_root
    release = json.loads((payload/'collector-release.json').read_text(encoding='utf-8-sig'))
    if (release.get('schema') != 'synaeris-collector-release-v1'
            or release.get('version') != version or release.get('repository') != REPOSITORY
            or sha256(payload/EXE_NAME) != release.get('executable_sha256', '').lower()):
        raise ValueError('Update executable identity does not match release manifest')
    return payload


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name+'.partial')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def stage_release(plan, *, root=None, downloader=None):
    root = Path(root or update_root()).resolve()
    versions = root/'versions'
    versions.mkdir(parents=True, exist_ok=True)
    downloads = root/'downloads'
    downloads.mkdir(parents=True, exist_ok=True)
    target = versions/f'{plan.version}-{plan.sha256[:12]}'
    temporary = Path(tempfile.mkdtemp(prefix='staging-', dir=root)).resolve()
    if not temporary.is_relative_to(root):
        raise ValueError('Update staging escaped its root')
    try:
        archive = downloads/f'{plan.version}-{plan.sha256[:12]}.zip.partial'
        if downloader is None:
            download_release(plan, archive)
        else:
            downloader(plan, archive)
            if archive.stat().st_size != plan.size or sha256(archive) != plan.sha256:
                raise ValueError('Update archive size or SHA-256 mismatch')
        payload = _safe_extract(archive, temporary/'unpacked', plan.version)
        exe_digest = sha256(payload/EXE_NAME)
        if exe_digest != plan.executable_sha256:
            raise ValueError('Update executable SHA-256 mismatch')
        if not target.exists():
            os.replace(payload, target)
        elif sha256(target/EXE_NAME) != exe_digest:
            raise ValueError('Existing staged version differs from verified release')
        pointer = {'schema': 'synaeris-collector-update-v1', 'version': plan.version,
                   'tag': plan.tag, 'directory': target.name,
                   'executable_sha256': exe_digest, 'archive_sha256': plan.sha256}
        _atomic_json(root/'current.json', pointer)
        return target/EXE_NAME
    finally:
        if temporary.is_relative_to(root) and temporary.name.startswith('staging-'):
            shutil.rmtree(temporary, ignore_errors=True)


def newer_local_executable(*, current=APP_VERSION, root=None):
    root = Path(root or update_root()).resolve()
    pointer_file = root/'current.json'
    if not pointer_file.exists():
        return None
    try:
        pointer = json.loads(pointer_file.read_text(encoding='utf-8-sig'))
        if pointer.get('schema') != 'synaeris-collector-update-v1':
            return None
        if version_tuple(pointer['version']) <= version_tuple(current):
            return None
        directory = pointer['directory']
        if not isinstance(directory, str) or '/' in directory or '\\' in directory or directory.startswith('.'):
            return None
        executable = (root/'versions'/directory/EXE_NAME).resolve()
        if not executable.is_relative_to((root/'versions').resolve()):
            return None
        if sha256(executable) != pointer['executable_sha256']:
            return None
        return executable
    except (OSError, ValueError, KeyError, TypeError):
        return None


def relaunch_newer(argv, *, current=APP_VERSION, root=None):
    executable = newer_local_executable(current=current, root=root)
    if executable is None:
        return False
    subprocess.Popen([str(executable), *argv], cwd=executable.parent,
                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return True


def check_and_stage(*, current=APP_VERSION, root=None, announce=lambda message: None):
    try:
        plan = select_release(latest_release(), current=current)
        if plan is None:
            return None
        announce(f'发现 Collector {plan.version}，正在后台下载并校验')
        executable = stage_release(plan, root=root)
        announce(f'Collector {plan.version} 已准备好；下次打开 EXE 自动使用新版')
        return executable
    except urllib.error.HTTPError as error:
        announce(f'自动更新暂不可用：GitHub HTTP {error.code}')
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as error:
        announce(f'自动更新暂不可用：{error}')
    return None
