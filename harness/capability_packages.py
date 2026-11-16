"""Bounded, digest-pinned downloads and extraction. No package-manager scripts."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import uuid
import zipfile
from context_manager import InvalidUpdate, canonical, sha
import capability_registry as registry


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise InvalidUpdate('Tool package redirects are not permitted')


def download(spec):
    from lifecycle import timeout
    if not spec['url'].startswith(('https://files.pythonhosted.org/packages/', 'https://registry.npmjs.org/pyright/-/')):
        raise InvalidUpdate('Package endpoint is not approved')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    end = time.monotonic() + timeout(110)
    with opener.open(spec['url'], timeout=15) as response:
        data = bytearray()
        while True:
            if time.monotonic() > end:
                raise InvalidUpdate('Tool download deadline exceeded')
            block = response.read(min(65536, spec['max_download_bytes'] + 1 - len(data)))
            if not block:
                break
            data.extend(block)
            if len(data) > spec['max_download_bytes']:
                raise InvalidUpdate('Tool download exceeds approved size')
    return bytes(data)


def check_archive(spec, raw):
    if len(raw) > spec['max_download_bytes']:
        raise InvalidUpdate('Tool download exceeds approved size')
    algorithm, expected = spec['integrity'].split('-', 1)
    if algorithm not in ('sha256', 'sha512'):
        raise InvalidUpdate('Unsupported package digest')
    actual = base64.b64encode(hashlib.new(algorithm, raw).digest()).decode()
    if actual != expected:
        raise InvalidUpdate('Tool package integrity mismatch')


def unpack(raw, spec, target):
    check_archive(spec, raw)
    total = 0
    seen = set()
    def write(name, size, mode, stream):
        nonlocal total
        p = PurePosixPath(name)
        if not name or p.is_absolute() or '..' in p.parts or '\\' in name or ':' in name or any(ord(c)<32 for c in name):
            raise InvalidUpdate('Unsafe package member')
        # Casefold also rejects collisions on case-insensitive filesystems.
        if str(p).casefold() in seen or len(seen) >= 10000:
            raise InvalidUpdate('Duplicate package member or member limit')
        seen.add(str(p).casefold()); total += size
        if size < 0 or total > spec['max_expanded_bytes']:
            raise InvalidUpdate('Tool package expansion exceeds allocation')
        dest = target.joinpath(*p.parts)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open('xb') as out:
            remaining = size
            while remaining:
                chunk = stream.read(min(65536, remaining))
                if not chunk:
                    raise InvalidUpdate('Truncated package member')
                out.write(chunk); remaining -= len(chunk)
        dest.chmod(0o755 if mode & 0o111 else 0o644)
    if spec['format'] == 'zip':
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            for item in archive.infolist():
                if item.is_dir(): continue
                mode = item.external_attr >> 16
                if stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                    raise InvalidUpdate('Package links and special files are forbidden')
                with archive.open(item) as stream: write(item.filename, item.file_size, mode, stream)
    else:
        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
            for item in archive:
                if item.isdir(): continue
                if not item.isfile(): raise InvalidUpdate('Package links and special files are forbidden')
                with archive.extractfile(item) as stream: write(item.name, item.size, item.mode, stream)


def folder(root, name):
    spec = registry.specification(name)
    return Path(root) / 'tools' / (name + '-' + spec['version'] + '-' + registry.identity()[:12])


def hashes(directory):
    result = {}
    if directory.is_symlink(): raise InvalidUpdate('Tool directory is a symlink')
    for p in sorted(directory.rglob('*')):
        if p.is_symlink(): raise InvalidUpdate('Installed tool has a symlink')
        if p.is_file():
            if p.stat().st_nlink != 1: raise InvalidUpdate('Installed tool has a hard link')
            result[p.relative_to(directory).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return result


def install(root, name):
    spec = registry.artifact(name)
    target = folder(root, name)
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise InvalidUpdate('Tool installation target is not a regular directory')
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.install-', dir=target.parent))
    try:
        unpack(download(spec), spec, stage)
        if not (stage / spec['entry']).is_file(): raise InvalidUpdate('Tool entry point is absent')
        manifest = hashes(stage)
        (stage / spec['entry']).chmod(0o755)
        replaced = None
        if target.exists():
            # An interrupted install may have renamed its files before it could
            # write a receipt. A newly approved install quarantines that copy;
            # it never trusts or executes it and never refunds the earlier work.
            replaced = target.with_name(target.name + '.quarantine-' + uuid.uuid4().hex)
            os.rename(target, replaced)
        os.rename(stage, target)
        return {'status': 'installed', 'files': manifest, 'package_integrity': spec['integrity'],
                'quarantined_previous': replaced.name if replaced else None}
    finally:
        if stage.exists(): shutil.rmtree(stage)


def run(root, name, files):
    """Read-only sandbox; project config/plugins and arbitrary command flags excluded."""
    from lifecycle import timeout
    from monitoring import require_resources
    if sys.platform != 'darwin': raise InvalidUpdate('Tool execution needs the macOS sandbox; installation is portable')
    spec = registry.artifact(name); tool = folder(root, name).resolve()
    with tempfile.TemporaryDirectory(prefix='tool-check-', dir=root) as temp:
        base = Path(temp).resolve(); snapshot=base/'source'; scratch=base/'scratch'
        snapshot.mkdir(); scratch.mkdir()
        for path, content in files.items():
            dest=snapshot/path;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(content)
        env={'HOME':str(scratch),'TMPDIR':str(scratch),'LANG':'en_US.UTF-8','PATH':'/usr/bin:/bin'}
        entry=tool/spec['entry']
        if name == 'ruff':
            command=[str(entry),'check','--isolated','--no-cache','--output-format=json',*files]
            executable=entry
        else:
            executable=Path(os.environ.get('KARMA_NODE','')).resolve()
            if not executable.is_file(): raise InvalidUpdate('Pyright requires the host Node runtime')
            (scratch/'stubs').mkdir();(scratch/'venvs').mkdir()
            config=base/'pyrightconfig.json'
            config.write_text(canonical({'include':['source/'+p for p in files], 'extraPaths':[],
                'stubPath':str(scratch/'stubs'),'venvPath':str(scratch/'venvs'),'pythonVersion':'3.9',
                'typeCheckingMode':'basic','useLibraryCodeForTypes':False}))
            env['ELECTRON_RUN_AS_NODE']='1'
            command=[str(executable),str(entry),'--project',str(config),'--outputjson',
                     '--pythonpath',str(scratch/'no-interpreter')]
        def lit(p): return json.dumps(str(p))
        profile='(version 1)(deny default)(allow file-read-metadata)(allow sysctl-read)'+\
            '(allow process-exec (literal '+lit(executable)+'))'+\
            '(allow file-read* (literal "/") (literal "/dev/null") (literal "/dev/urandom") '+\
            ' '.join('(subpath '+lit(p)+')' for p in ('/System','/usr','/Library/Apple',tool,base,executable.parent.parent))+')'+\
            '(allow file-write* (subpath '+lit(scratch)+'))'
        sandbox=base/'tool.sb';sandbox.write_text(profile)
        proc=subprocess.Popen(['/usr/bin/sandbox-exec','-f',str(sandbox),*command],cwd=snapshot,
            env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,start_new_session=True)
        output=bytearray();status='finished';end=time.monotonic()+timeout(30)
        selector=selectors.DefaultSelector();selector.register(proc.stdout,selectors.EVENT_READ)
        try:
            while selector.get_map():
                if time.monotonic()>end: status='timeout';break
                require_resources(root)
                for key,_ in selector.select(.1):
                    block=os.read(key.fileobj.fileno(),8192)
                    if not block: selector.unregister(key.fileobj);continue
                    output.extend(block)
                if len(output)>65536: status='output_limit';break
            if status=='finished': proc.wait(timeout=max(.1,end-time.monotonic()))
        finally:
            if proc.poll() is None: os.killpg(proc.pid,signal.SIGKILL);proc.wait(timeout=3)
            selector.close();proc.stdout.close()
        if status=='finished' and proc.returncode not in (0,1): status='process_failed'
        text=bytes(output[:65536]).decode('utf8','replace').replace(str(snapshot)+'/','')
        return {'status':status,'returncode':proc.returncode,'output':text,'output_sha256':sha(text),
                'evidence_kind':'static_tool_diagnostics','correctness_verified':False,
                'network_allowed':False,'project_writes_allowed':False}
