"""Stage a committed Python project in a private Git worktree and observe its tests."""
from dataclasses import dataclass
import ast
import difflib
import hashlib
import os
from pathlib import Path, PurePosixPath
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import zlib

from context_manager import InvalidUpdate, canonical, sha
import git_provenance
from repository_tools import normalized, read_source, source_directory, source_stream
import sandbox_process


VERSION = 1
TEST_TARGET = 'tests'
MAX_FILES = 256
MAX_DIRS = 256
MAX_BYTES = 16 * 1024 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_EDITS = 8
MAX_EDIT_CHARS = 2400
MAX_EDIT_BYTES = 64000
MAX_OUTPUT = 65536
MAX_SCRATCH_BYTES = 8 * 1024 * 1024
MAX_SCRATCH_ENTRIES = 512
MAX_METADATA = 2 * 1024 * 1024
MAX_TEST_MS = 120000
STAGING_SECONDS = 30

_RUNNER = """import resource, sys, unittest
resource.setrlimit(resource.RLIMIT_FSIZE, (1048576, 1048576))
sys.path.insert(0, sys.argv[1])
unittest.main(module=None, argv=['unittest', 'discover', '-s', 'tests'])
"""


@dataclass(frozen=True)
class Snapshot:
    git: str
    commit: str
    commit_data: bytes
    trees: dict
    files: dict
    content: dict

    @property
    def size(self):
        return sum(size for _, _, size in self.files.values())

    @property
    def content_sha256(self):
        return sha(canonical([(name, mode, oid, size) for name, (mode, oid, size) in
                              sorted(self.files.items())]))


def _object(kind, data, oid):
    body = f'{kind} {len(data)}\0'.encode('ascii') + data
    algorithm = 'sha256' if len(oid) == 64 else 'sha1'
    if hashlib.new(algorithm, body).hexdigest() != oid:
        raise InvalidUpdate('Project content differs from its committed Git object')
    return body


def _stop(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait(timeout=3)


def _scratch_fits(path):
    entries, size = 0, 0
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    def scan(fd):
        nonlocal entries, size
        with os.scandir(fd) as children:
            for child in children:
                entries += 1
                if entries > MAX_SCRATCH_ENTRIES:
                    return False
                try:
                    info = child.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        subdir = os.open(child.name, flags, dir_fd=fd)
                        try:
                            if not scan(subdir):
                                return False
                        finally:
                            os.close(subdir)
                    elif stat.S_ISREG(info.st_mode):
                        size += info.st_size
                        if size > MAX_SCRATCH_BYTES:
                            return False
                    else:
                        return False
                except FileNotFoundError:
                    # Tests can remove temporary files while a check is running.
                    continue
        return True

    try:
        fd = os.open(path, flags)
        try:
            return scan(fd)
        finally:
            os.close(fd)
    except OSError:
        return False


def _collect(command, cwd, env, limit, seconds, *, body=None, resource_root=None, scratch=None):
    """Collect bounded combined output and settle the whole process group."""
    output = bytearray()
    with tempfile.TemporaryFile() as stdin:
        if body is not None:
            stdin.write(body)
            stdin.seek(0)
        with subprocess.Popen(command, cwd=cwd, env=env, stdin=stdin, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, start_new_session=True) as proc, selectors.DefaultSelector() as selector:
            started = time.monotonic()
            deadline = started + seconds
            next_check = started + 1
            next_scratch_check = started
            reason = None
            selector.register(proc.stdout, selectors.EVENT_READ)
            try:
                while selector.get_map() or proc.poll() is None:
                    now = time.monotonic()
                    if now >= deadline:
                        reason = 'timeout'
                        break
                    if scratch is not None and now >= next_scratch_check:
                        if not _scratch_fits(scratch):
                            reason = 'scratch_limit'
                            break
                        next_scratch_check = now + .1
                    if resource_root is not None and now >= next_check:
                        from monitoring import resource_state
                        health = resource_state(resource_root, refresh=True, max_wait=min(1, deadline - now))
                        if not health['admitted']:
                            reason = 'resource_wait'
                            break
                        next_check = time.monotonic() + 1
                    delay = max(0, min(.1, deadline - time.monotonic()))
                    for ready, _ in selector.select(delay):
                        chunk = os.read(ready.fd, min(8192, limit + 1 - len(output)))
                        if not chunk:
                            selector.unregister(ready.fileobj)
                            continue
                        output.extend(chunk)
                        if len(output) > limit:
                            reason = 'output_limit'
                            break
                    if reason:
                        break
                # Reap descendants before an approved project can keep working
                # after its receipt, even if its leader closed stdout first.
                if reason or proc.poll() is None:
                    _stop(proc)
                else:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if not reason and scratch is not None and not _scratch_fits(scratch):
                    reason = 'scratch_limit'
            finally:
                if proc.poll() is None:
                    _stop(proc)
            return proc.returncode, bytes(output[:limit]), reason, round(time.monotonic() - started, 3)


def _stage_time(deadline):
    remain = deadline-time.monotonic()
    if remain <= 0:
        raise InvalidUpdate('Isolated project staging exceeded its time allocation')
    return remain


def _git(binary, directory, args, *, objects=None, data=None, deadline):
    env = dict(PATH=os.defpath, LANG='C', LC_ALL='C', HOME=str(directory),
               GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS='0',
               GIT_ATTR_NOSYSTEM='1', GIT_NO_REPLACE_OBJECTS='1', GIT_TERMINAL_PROMPT='0',
               GIT_NO_LAZY_FETCH='1', GIT_ALLOW_PROTOCOL='')
    if objects is not None:
        env['GIT_OBJECT_DIRECTORY'] = str(objects)
    command = [binary, '--no-pager', '--no-optional-locks', '-c', 'core.hooksPath='+os.devnull,
               '-c', 'core.fsmonitor=false', *args]
    try:
        status, out, reason, _ = _collect(command, directory, env, MAX_METADATA,
                                         min(5, _stage_time(deadline)), body=data)
    except OSError:
        raise InvalidUpdate('The isolated Git operation could not start') from None
    if status or reason:
        raise InvalidUpdate('The isolated Git operation failed or exceeded its allocation')
    return out


def _metadata(directory, commit):
    directory.mkdir(mode=0o700)
    (directory / 'objects').mkdir()
    (directory / 'refs').mkdir()
    (directory / 'HEAD').write_text(commit+'\n', encoding='ascii')
    settings = '[core]\nrepositoryformatversion = 0\nbare = true\n'
    if len(commit) == 64:
        settings = '[core]\nrepositoryformatversion = 1\nbare = true\n[extensions]\nobjectformat = sha256\n'
    (directory / 'config').write_text(settings, encoding='ascii')


def _path(raw):
    try:
        name = raw.decode('utf-8')
    except UnicodeDecodeError:
        raise InvalidUpdate('The project contains a non-UTF-8 tracked path') from None
    parts = name.split('/')
    if (not name or len(name) > 240 or any(not part or part in ('.', '..')
            or part.casefold() in ('.git', 'git~1') or part.endswith((' ', '.')) for part in parts)
            or '\\' in name or ':' in name or any(ord(char) < 32 or ord(char) == 127 for char in name)):
        raise InvalidUpdate('The project contains an unsafe tracked path')
    return name


def _tree_index(data, commit):
    directories, files, seen = {}, {}, set()
    for row in data.split(b'\0'):
        if not row:
            continue
        try:
            head, raw = row.split(b'\t', 1)
            mode, kind, oid, length = head.decode('ascii').split()
        except (ValueError, UnicodeDecodeError):
            raise InvalidUpdate('Unexpected Git tree metadata') from None
        name = _path(raw)
        key = unicodedata.normalize('NFD', name.casefold())
        if key in seen or len(oid) != len(commit) or not git_provenance.COMMIT.fullmatch(oid):
            raise InvalidUpdate('The project contains conflicting or invalid Git paths')
        seen.add(key)
        if mode == '040000' and kind == 'tree' and length == '-':
            directories[name] = oid
            continue
        if mode not in ('100644', '100755') or kind != 'blob' or not length.isdecimal():
            raise InvalidUpdate('The project requires tracked regular files without symlinks or submodules')
        size = int(length)
        if size > MAX_FILE_BYTES:
            raise InvalidUpdate('A tracked file exceeds the isolated worktree file limit')
        files[name] = (mode, oid, size)
    if (not files or len(files) > MAX_FILES or len(directories) > MAX_DIRS or
            sum(size for _, _, size in files.values()) > MAX_BYTES):
        raise InvalidUpdate('The tracked project exceeds the isolated worktree allocation')
    return directories, files


def _git_objects(git, metadata, objects, requests, deadline):
    command = ['--git-dir='+str(metadata), 'cat-file', '--batch']
    response = _git(git, metadata.parent, command, objects=objects,
                    data=(''.join(oid+'\n' for oid, _ in requests)).encode('ascii'), deadline=deadline)
    result = {}
    offset = 0
    for oid, kind in requests:
        split = response.find(b'\n', offset)
        header = response[offset:split].split()
        if (split < 0 or len(header) != 3 or header[:2] != [oid.encode(), kind.encode()]
                or not header[2].isdigit()):
            raise InvalidUpdate('Pinned Git objects are unavailable')
        size = int(header[2])
        end = split + 1 + size
        data = response[split+1:end]
        if response[end:end+1] != b'\n':
            raise InvalidUpdate('Incomplete Git object data')
        _object(kind, data, oid)
        result[oid] = data
        offset = end+1
    if offset != len(response):
        raise InvalidUpdate('Unexpected Git object data')
    return result


def _scan_checkout(workspace, directories, files, deadline):
    def scan(fd, prefix):
        with os.scandir(fd) as entries:
            for entry in entries:
                _stage_time(deadline)
                if not prefix and entry.name == '.git':
                    continue
                name = prefix+entry.name
                mode = entry.stat(follow_symlinks=False).st_mode
                if stat.S_ISDIR(mode) and name in directories:
                    child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        scan(child, name+'/')
                    finally:
                        os.close(child)
                elif not stat.S_ISREG(mode) or name not in files:
                    raise InvalidUpdate('Use a clean checkout without extra or ignored files for isolated project tests')

    with source_directory(workspace, ()) as root:
        scan(root, '')


def _checkout_content(workspace, files, paths, deadline):
    checked = {}
    for name in paths:
        _stage_time(deadline)
        if name not in files:
            raise InvalidUpdate('Project edits require existing tracked Python files')
        mode, oid, size = files[name]
        path = PurePosixPath(name)
        with source_directory(workspace, path.parts[:-1]) as parent, source_stream(parent, path.name) as stream:
            data, info = read_source(stream, name, size)
        if len(data) != size or bool(info.st_mode & stat.S_IXUSR) != (mode == '100755'):
            raise InvalidUpdate('Tracked project content or mode differs from the pinned commit')
        _object('blob', data, oid)
        checked[name] = data
    return checked


def _head(directory, common):
    head = (git_provenance._metadata(directory / 'HEAD') or b'').decode('ascii', 'replace').strip()
    if head.startswith('ref: refs/heads/'):
        return git_provenance._oid(common, head[5:])
    if git_provenance.COMMIT.fullmatch(head):
        return head
    return None


def _snapshot(workspace, test_target, deadline, *, edit_targets=None):
    if test_target != TEST_TARGET:
        raise InvalidUpdate('This project test profile requires the tracked tests directory')
    workspace = Path(workspace).resolve()
    root, directory, common = git_provenance._layout(workspace)
    git = git_provenance._git_binary(workspace)
    commit = _head(directory, common) if directory is not None else None
    if root != workspace or not git or not commit:
        raise InvalidUpdate('Select the root of a committed Git project for isolated project tests')
    objects = common / 'objects'
    if (objects.is_symlink() or not objects.is_dir() or (objects / 'info').is_symlink() or
            (objects / 'pack').is_symlink() or (objects / 'info' / 'alternates').exists()
            or (objects / 'info' / 'alternates').is_symlink()):
        raise InvalidUpdate('Isolated project tests require a local Git object store without alternates')
    if git_provenance._status(git, root, workspace, directory, common, commit) != b'':
        raise InvalidUpdate('Use a clean checkout with an unchanged index for isolated project tests')
    try:
        with tempfile.TemporaryDirectory(prefix='sutra-project-source-') as temp:
            metadata = Path(temp) / 'git'
            _metadata(metadata, commit)
            commit_data = _git_objects(git, metadata, objects, [(commit, 'commit')], deadline)[commit]
            first = commit_data.partition(b'\n')[0]
            if not first.startswith(b'tree '):
                raise InvalidUpdate('Pinned Git commit has no valid source tree')
            root_tree = first[5:].decode('ascii')
            if len(root_tree) != len(commit) or not git_provenance.COMMIT.fullmatch(root_tree):
                raise InvalidUpdate('Pinned Git commit has no valid source tree')
            listing = _git(git, metadata.parent, ['--git-dir='+str(metadata), 'ls-tree',
                                                  '-r', '-t', '-l', '-z', '--full-tree', commit],
                           objects=objects, deadline=deadline)
            directories, files = _tree_index(listing, commit)
            if (test_target not in directories or not any(
                    name.startswith(test_target+'/') and PurePosixPath(name).match('test*.py') for name in files)):
                raise InvalidUpdate('The commit needs at least one tracked unittest file in tests')
            trees = {}
            if edit_targets is None:
                requests = [(tree, 'tree') for tree in dict.fromkeys([root_tree, *directories.values()])]
                trees = _git_objects(git, metadata, objects, requests, deadline)
        _scan_checkout(workspace, directories, files, deadline)
        checked = _checkout_content(workspace, files, files if edit_targets is None else edit_targets, deadline)
    except (OSError, UnicodeDecodeError):
        raise InvalidUpdate('The pinned Git checkout changed or could not be read safely') from None
    if _head(directory, common) != commit:
        raise InvalidUpdate('The source checkout moved while binding the project snapshot')
    return Snapshot(git, commit, commit_data, trees, files, checked)


def _change(snapshot, target, after):
    normalized(target)
    if target not in snapshot.files or not target.endswith('.py'):
        raise InvalidUpdate('Project edits require existing tracked Python files')
    original = snapshot.content[target]
    if len(original) > MAX_EDIT_BYTES or len(after.encode('utf-8')) > MAX_EDIT_BYTES:
        raise InvalidUpdate('A proposed Python edit exceeds the worktree edit file limit')
    try:
        before = original.decode('utf-8')
        ast.parse(before)
        ast.parse(after)
    except (UnicodeDecodeError, SyntaxError, ValueError):
        raise InvalidUpdate('A proposed worktree edit is not valid UTF-8 Python') from None
    diff = ''.join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                       fromfile='a/'+target, tofile='b/'+target))
    return {'target':target, 'before':before, 'after':after, 'diff':diff,
            'before_hash':sha(before), 'after_hash':sha(after)}


def _placement(snapshot, changes):
    return {'version':VERSION, 'base_commit':snapshot.commit, 'project_sha256':snapshot.content_sha256,
            'files':len(snapshot.files), 'source_bytes':snapshot.size, 'test_target':TEST_TARGET,
            'changes':changes}


def plan(workspace, edits, *, test_target=TEST_TARGET):
    """Bind an exact replacement set and track the committed project input."""
    if type(edits) is not list or not 1 <= len(edits) <= MAX_EDITS:
        raise InvalidUpdate('A worktree patch needs one to eight replacements')
    proposed = {}
    for edit in edits:
        if (not isinstance(edit, dict) or set(edit) != {'target', 'old', 'new'} or
                any(not isinstance(edit[key], str) for key in edit)):
            raise InvalidUpdate('Invalid worktree replacement')
        target, old, new = (edit[name] for name in ('target', 'old', 'new'))
        if (target in proposed or not old or old == new or len(old) > MAX_EDIT_CHARS
                or len(new) > MAX_EDIT_CHARS):
            raise InvalidUpdate('Project edits need distinct files with bounded exact replacements')
        normalized(target)
        if not target.endswith('.py'):
            raise InvalidUpdate('Project edits require existing tracked Python files')
        proposed[target] = (old, new)
    snapshot = _snapshot(workspace, test_target, time.monotonic()+STAGING_SECONDS, edit_targets=proposed)
    changes = []
    for target, (old, new) in sorted(proposed.items()):
        try:
            before = snapshot.content[target].decode('utf-8')
            new.encode('utf-8')
        except UnicodeError:
            raise InvalidUpdate('A proposed worktree edit is not UTF-8 Python') from None
        if before.count(old) != 1:
            raise InvalidUpdate('A worktree replacement preimage must occur exactly once')
        if '\r\n' in before and '\n' not in before.replace('\r\n',''):
            new = new.replace('\r\n','\n').replace('\n','\r\n')
        if new == old:
            raise InvalidUpdate('A worktree replacement must change the committed text')
        changes.append(_change(snapshot, target, before.replace(old, new, 1)))
    return _placement(snapshot, changes)


def _write_object(metadata, oid, kind, body):
    path = metadata / 'objects' / oid[:2] / oid[2:]
    path.parent.mkdir(exist_ok=True)
    if not path.exists():
        path.write_bytes(zlib.compress(_object(kind, body, oid)))


def _stage(directory, snapshot, changes, deadline):
    metadata, tree = directory / 'git', directory / 'worktree'
    _metadata(metadata, snapshot.commit)
    (metadata / 'shallow').write_text(snapshot.commit+'\n', encoding='ascii')
    _write_object(metadata, snapshot.commit, 'commit', snapshot.commit_data)
    for oid, body in snapshot.trees.items():
        _stage_time(deadline)
        _write_object(metadata, oid, 'tree', body)
    for name, (_, oid, _) in snapshot.files.items():
        _stage_time(deadline)
        _write_object(metadata, oid, 'blob', snapshot.content[name])
    _git(snapshot.git, directory, ['--git-dir='+str(metadata), 'worktree', 'add',
                                   '--detach', '--no-checkout', str(tree), snapshot.commit], deadline=deadline)
    _git(snapshot.git, tree, ['read-tree', snapshot.commit], deadline=deadline)
    patch = {change['target']:change['after'].encode('utf-8') for change in changes}
    for name, (mode, _, _) in sorted(snapshot.files.items()):
        _stage_time(deadline)
        path = tree.joinpath(*PurePosixPath(name).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        # A complete replacement set is materialized before the test process.
        # The source checkout and its Git metadata are never modified.
        with path.open('xb') as stream:
            stream.write(patch.get(name, snapshot.content[name]))
        path.chmod(0o755 if mode == '100755' else 0o644)
    return tree


def run(workspace, directory, committed_plan, timeout_ms, *, resource_root=None):
    """Verify the approved snapshot, stage the patch and run a fixed test profile."""
    if sys.platform != 'darwin' or not Path('/usr/bin/sandbox-exec').is_file():
        raise InvalidUpdate('Isolated project tests currently require the macOS sandbox')
    if type(timeout_ms) is not int or not 1 <= timeout_ms <= MAX_TEST_MS:
        raise InvalidUpdate('Invalid isolated project test time allocation')
    if not isinstance(committed_plan, dict) or committed_plan.get('test_target') != TEST_TARGET:
        raise InvalidUpdate('Invalid isolated project plan')
    started = time.monotonic()
    deadline = started + STAGING_SECONDS
    snapshot = _snapshot(workspace, TEST_TARGET, deadline)
    changes = committed_plan.get('changes')
    if (not isinstance(changes, list) or not 1 <= len(changes) <= MAX_EDITS or
            any(not isinstance(change, dict) or not isinstance(change.get('target'), str)
                or not isinstance(change.get('after'), str) for change in changes)):
        raise InvalidUpdate('Invalid isolated project patch')
    expected = [_change(snapshot, change['target'], change['after']) for change in changes]
    targets = [change['target'] for change in expected]
    if (committed_plan != _placement(snapshot, expected) or sorted(set(targets)) != targets or
            any(change['before'] == change['after'] for change in expected)):
        raise InvalidUpdate('The isolated project plan changed after approval')

    directory = Path(directory)
    workspace = Path(workspace).resolve()
    if (directory.is_symlink() or not directory.is_dir() or directory.resolve().is_relative_to(workspace) or
            any((directory / name).exists() or (directory / name).is_symlink()
                for name in ('git', 'worktree', 'scratch', 'test-output.txt'))):
        raise InvalidUpdate('Isolated project worktree requires a fresh private action directory')
    directory = directory.resolve()
    tree = _stage(directory, snapshot, expected, deadline)
    scratch = directory / 'scratch'
    scratch.mkdir(mode=0o700)
    from host_actions import test_runtime
    interpreter = test_runtime().resolve()
    profile = sandbox_process.profile(interpreter, ('/Library/Developer/CommandLineTools', sys.base_prefix, tree),
                                      (), (scratch,))
    profile += '(deny process-fork)(deny file-read* (literal '+sandbox_process.literal(tree/'.git')+'))'
    env = {'PATH':str(interpreter.parent), 'HOME':str(scratch), 'TMPDIR':str(scratch), 'LANG':'en_US.UTF-8'}
    args = ['-I', '-B', '-S', '-u', '-c', _RUNNER, str(tree)]
    command = ['/usr/bin/sandbox-exec', '-p', profile, str(interpreter), *args]
    identity = hashlib.sha256(interpreter.read_bytes()).hexdigest()
    _stage_time(deadline)
    staged = time.monotonic()-started
    returncode, raw, reason, elapsed = _collect(command, tree, env, MAX_OUTPUT,
                                                timeout_ms/1000, resource_root=resource_root, scratch=scratch)
    output = raw.decode('utf-8', 'replace')
    (directory / 'test-output.txt').write_bytes(raw)
    total = round(time.monotonic()-started, 3)
    return {'status':reason or ('process_exited_zero' if returncode == 0 else 'process_failed'),
            'returncode':returncode, 'elapsed_seconds':total, 'assertions_verified':False,
            'staging_elapsed_seconds':round(staged, 3), 'test_elapsed_seconds':elapsed,
            'evidence_kind':'untrusted_test_process_output', 'output_tail':output[-3000:],
            'output_sha256':hashlib.sha256(raw).hexdigest(), 'sandbox_profile_sha256':sha(profile),
            'runner_sha256':sha(_RUNNER), 'interpreter':str(interpreter), 'interpreter_sha256':identity,
            'test_runner':'Python unittest discovery', 'test_target':TEST_TARGET,
            'network_allowed':False, 'project_writes_allowed':False,
            'worktree':str(tree), 'base_commit':snapshot.commit, 'project_sha256':snapshot.content_sha256,
            'patch_sha256':sha(canonical([(change['target'], change['before_hash'], change['after_hash'])
                                          for change in expected]))}
