"""Bounded, offline provenance for the workspace selected for review."""
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile

from context_manager import canonical, sha


MAX_METADATA = 1024 * 1024
COMMIT = re.compile(r'(?:[a-f0-9]{40}|[a-f0-9]{64})\Z')
REF = re.compile(r'[A-Za-z0-9._/-]+\Z')


def _metadata(path):
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            return None
        with path.open('rb') as stream:
            raw = stream.read(MAX_METADATA + 1)
        if len(raw) <= MAX_METADATA:
            return raw
    except OSError:
        pass
    return None


def _layout(workspace):
    for root in (workspace, *workspace.parents):
        dotgit = root / '.git'
        if dotgit.is_symlink():
            return root, None, None
        if dotgit.is_dir():
            directory = dotgit
        elif dotgit.exists():
            data = (_metadata(dotgit) or b'').decode('utf-8', 'replace').strip()
            if not data.startswith('gitdir: '):
                return root, None, None
            directory = _resolve(root / data[8:])
            if directory is None:
                return root, None, None
        else:
            continue
        shared = (_metadata(directory / 'commondir') or b'').decode('utf-8', 'replace').strip()
        common = _resolve(directory / shared) if shared else directory
        return (root, directory, common) if common else (root, None, None)
    return None, None, None


def _resolve(path):
    try:
        return path.resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _valid_ref(name):
    return (len(name) <= 255 and REF.fullmatch(name) and all(
        part and not part.startswith('.') and not part.endswith('.') for part in name.split('/')))


def _oid(shared, ref):
    if not _valid_ref(ref) or not ref.startswith('refs/'):
        return None
    raw = (_metadata(shared / ref) or b'').decode('ascii', 'replace').strip()
    if COMMIT.fullmatch(raw):
        return raw
    for line in (_metadata(shared / 'packed-refs') or b'').decode('ascii', 'replace').splitlines():
        value, sep, name = line.partition(' ')
        if sep and ref == name and COMMIT.fullmatch(value):
            return value
    return None


def _git_binary(root):
    # PATH entries in a reviewed checkout are not trusted executable providers.
    directories = (_resolve(Path(p)) for p in os.environ.get('PATH', os.defpath).split(os.pathsep)
                   if os.path.isabs(p))
    path = os.pathsep.join(str(p) for p in directories if p and not p.is_relative_to(root))
    binary = shutil.which('git', path=path)
    candidate = _resolve(Path(binary)) if binary else None
    if candidate and not candidate.is_relative_to(root):
        return str(candidate)
    return None


def _git(binary, cwd, args, additions=None):
    if not binary:
        return None
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_OPTIONAL_LOCKS='0', GIT_ATTR_NOSYSTEM='1',
               GIT_NO_REPLACE_OBJECTS='1', GIT_TERMINAL_PROMPT='0', GIT_NO_LAZY_FETCH='1')
    env.update(additions or {})
    # A timeout bounds work. Capture to a temporary file so a large index
    # cannot force this metadata check to retain unbounded memory.
    try:
        with tempfile.TemporaryFile() as output:
            done = subprocess.run([binary, '--no-pager', '--no-optional-locks', *args],
                                  cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                  stdout=output, stderr=subprocess.DEVNULL, timeout=3)
            if done.returncode == 0:
                output.seek(0)
                raw = output.read(MAX_METADATA + 1)
                if len(raw) <= MAX_METADATA:
                    return raw
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _upstream(binary, root, shared, branch, commit):
    if not branch or not shared:
        return None
    # Use config as inert data and ignore includes. Never read URL values, which
    # can contain credentials. A standard tracking ref is enough for provenance.
    def setting(suffix):
        data = _git(binary, root, ['config', '--file', str(shared / 'config'),
                                  '--no-includes', '--get', 'branch.' + branch + '.' + suffix])
        return (data or b'').decode('ascii', 'replace').strip()
    remote, merge = setting('remote'), setting('merge')
    if not _valid_ref(remote) or '/' in remote or not merge.startswith('refs/heads/'):
        return None
    ref = remote + '/' + merge[len('refs/heads/'):]
    if not _valid_ref(ref):
        return None
    cached = _oid(shared, 'refs/remotes/' + ref)
    return {'ref': ref, 'cached_commit': cached,
            'relation': ('matches_cached_tip' if cached == commit else 'differs_from_cached_tip') if cached else 'unknown',
            'live_verified': False}


def _status(binary, root, workspace, directory, shared, commit):
    if not binary or not (shared / 'objects').is_dir():
        return None
    try:
        with tempfile.TemporaryDirectory(prefix='sutra-git-') as temporary:
            metadata = Path(temporary)
            (metadata / 'refs').mkdir()
            (metadata / 'objects').mkdir()
            (metadata / 'HEAD').write_text(commit + '\n', encoding='ascii')
            version = '[core]\nrepositoryformatversion = 0\n'
            if len(commit) == 64:
                version = '[core]\nrepositoryformatversion = 1\n[extensions]\nobjectformat = sha256\n'
            (metadata / 'config').write_text(version, encoding='ascii')
            exclude = _metadata(shared / 'info' / 'exclude')
            if exclude:
                (metadata / 'info').mkdir()
                (metadata / 'info' / 'exclude').write_bytes(exclude)
            # A clean/process filter from the real repository's config can run
            # even during status. Use a private Git dir with a known config.
            # The original index is opened without optional locks or writes.
            args = ['--git-dir='+temporary, '--work-tree='+str(root),
                    '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath='+os.devnull,
                    '-c', 'core.untrackedCache=false', 'status', '--porcelain=v1', '-z',
                    '--untracked-files=all', '--ignore-submodules=all', '--no-renames']
            if workspace != root:
                args += ['--', ':(top,literal)'+workspace.relative_to(root).as_posix()]
            return _git(binary, root, args, {
                'GIT_INDEX_FILE': str(directory/'index'), 'GIT_OBJECT_DIRECTORY': str(shared/'objects')})
    except OSError:
        return None


def _status_details(root, raw):
    paths = []
    for entry in raw.split(b'\x00'):
        if not entry:
            continue
        name = os.fsdecode(entry[3:])
        logical = PurePosixPath(name)
        if logical.is_absolute() or '..' in logical.parts:
            continue
        source = root / logical
        try:
            parent = _resolve(source.parent)
            if parent is None or not parent.is_relative_to(root):
                continue
            info = source.lstat()
            paths.append([name, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode])
        except (OSError, ValueError):
            paths.append([name, None])
    fingerprint = sha(canonical([hashlib.sha256(raw).hexdigest(), paths]))
    return ('dirty' if raw else 'clean'), fingerprint, len(paths)


def inspect(workspace, *, include_dirty=True):
    """Inspect local checkout metadata without executing repo-supplied config."""
    workspace = Path(workspace).resolve()
    result = {'format': 1, 'kind': 'unlinked', 'workspace_root': str(workspace),
              'observed_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
              'repository_root': None, 'commit': None, 'branch': None,
              'worktree': 'unknown', 'worktree_fingerprint': None, 'changed_path_count': None,
              'upstream': None, 'remote_checked': False}
    root, directory, shared = _layout(workspace)
    if root is None:
        return result
    result.update(kind='git', repository_root=str(root))
    if not directory:
        return result
    head = (_metadata(directory / 'HEAD') or b'').decode('ascii', 'replace').strip()
    if head.startswith('ref: refs/heads/'):
        ref = head[5:]
        if _valid_ref(ref):
            result['branch'] = ref[len('refs/heads/'):]
            result['commit'] = _oid(shared, ref)
    elif COMMIT.fullmatch(head):
        result['commit'] = head
    binary = _git_binary(root)
    result['upstream'] = _upstream(binary, root, shared, result['branch'], result['commit'])
    if include_dirty and result['commit'] is not None:
        raw = _status(binary, root, workspace, directory, shared, result['commit'])
        if raw is not None:
            status, digest, count = _status_details(root, raw)
            result.update(worktree=status, worktree_fingerprint=digest, changed_path_count=count)
    return result


def checkout(p):
    return [p[key] for key in ('kind', 'workspace_root', 'repository_root', 'commit', 'branch')]


def same_snapshot(first, now):
    return (checkout(first) == checkout(now) and
            first['worktree_fingerprint'] == now['worktree_fingerprint'])


def upstream_requested(text):
    # Phrases such as "current architecture" or "latest request" can describe
    # the open tree. An explicit upstream/latest-revision request needs a choice.
    return bool(re.search(r'\b(?:latest (?:commit|branch|release|version|code|source|revision|checkout)|'
                          r'(?:latest|current) (?:upstream|github)|upstream (?:head|commit|tip|version)|'
                          r'(?:on|from|at) github)\b', text, flags=re.IGNORECASE))


def summary(p):
    if p['kind'] == 'unlinked':
        return 'Open folder is an unlinked source snapshot. It has no local Git commit or upstream.'
    branch = p['branch'] or 'detached or unknown branch'
    commit = p['commit'] or 'unresolved commit'
    status = {'clean': 'The working tree is clean.', 'dirty': 'The working tree includes local changes.',
              'unknown': 'Working tree status is unavailable.'}[p['worktree']]
    cache = p['upstream']
    remote = 'Live upstream freshness was not checked.'
    if cache:
        relation = {'matches_cached_tip':'matches', 'differs_from_cached_tip':'differs from',
                    'unknown':'has an unknown relation to'}[cache['relation']]
        remote = f'HEAD {relation} locally cached {cache["ref"]}. ' + remote
    return f'Local Git {branch} at `{commit}`. {status} {remote}'
