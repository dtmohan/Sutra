"""Locate a named workspace document without opening its contents."""

from contextlib import contextmanager
import os
from pathlib import Path, PurePosixPath
import stat
import time

from context_manager import InvalidUpdate
from repository_tools import link_reason, normalized, read_bytes


MAX_DOCUMENT_BYTES = 512 * 1024
MAX_SCAN_ENTRIES = 5_000
MAX_SCAN_SECONDS = 2


def _target(name):
    normalized(name)
    path = PurePosixPath(name)
    if path.suffix.lower() != '.docx':
        raise InvalidUpdate('Sutra can read DOCX files for document reviews.')
    return path


def _regular(metadata):
    return not link_reason(metadata) and stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1


@contextmanager
def _directory(workspace, parts):
    """Pin POSIX directory components and reject redirected directories."""
    if os.name == 'nt':
        folder = workspace
        for part in parts:
            folder /= part
            metadata = folder.lstat()
            if link_reason(metadata) or not stat.S_ISDIR(metadata.st_mode):
                raise InvalidUpdate('Document directory is not a permitted workspace path.')
        yield folder
        return
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(workspace, flags)
    try:
        for part in parts:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _result(status, sizes=None):
    sizes = sizes or {}
    return {'status': status, 'paths': sorted(sizes), 'sizes': sizes}


def locate_document(workspace, target):
    """Return eligible name matches and byte sizes using only file metadata."""
    workspace = Path(workspace).resolve()
    exact = False
    try:
        absolute = Path(target) if isinstance(target, str) else None
        if absolute is not None and absolute.is_absolute():
            if (target.startswith(('//', '\\\\')) or target not in (str(absolute), absolute.as_posix())
                    or '..' in absolute.parts):
                return _result('blocked')
            # Derive a scope-relative name without resolving the target or following links.
            target = absolute.relative_to(workspace).as_posix()
            exact = True
        name = _target(target)
    except (InvalidUpdate, ValueError):
        return _result('blocked')
    if exact or len(name.parts) > 1:
        try:
            with _directory(workspace, name.parts[:-1]) as folder:
                metadata = ((folder / name.name).lstat() if os.name == 'nt'
                            else os.stat(name.name, dir_fd=folder, follow_symlinks=False))
        except FileNotFoundError:
            return _result('missing')
        except (InvalidUpdate, OSError):
            return _result('blocked')
        return (_result('found', {target: metadata.st_size}) if _regular(metadata)
                else _result('blocked'))

    pending = [()]
    sizes = {}
    blocked = False
    visited = 0
    deadline = time.monotonic() + MAX_SCAN_SECONDS
    while pending:
        if time.monotonic() >= deadline:
            return _result('scan_limit')
        relative = pending.pop()
        try:
            with _directory(workspace, relative) as folder, os.scandir(folder) as entries:
                for entry in entries:
                    visited += 1
                    if visited > MAX_SCAN_ENTRIES or time.monotonic() >= deadline:
                        return _result('scan_limit')
                    logical = PurePosixPath(*relative, entry.name).as_posix()
                    try:
                        normalized(logical)
                    except InvalidUpdate:
                        continue
                    try:
                        metadata = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if entry.name == name.name:
                        if _regular(metadata):
                            sizes[logical] = metadata.st_size
                        else:
                            blocked = True
                    if stat.S_ISDIR(metadata.st_mode) and not link_reason(metadata):
                        pending.append((*relative, entry.name))
        except FileNotFoundError:
            continue
        except (InvalidUpdate, OSError):
            return _result('blocked')
    if not sizes:
        return _result('blocked' if blocked else 'missing')
    return _result('found' if len(sizes) == 1 else 'ambiguous', sizes)


def read_document(workspace, relative_path, *, limit=MAX_DOCUMENT_BYTES):
    """Read the selected DOCX through the existing scoped file reader."""
    _target(relative_path)
    if type(limit) is not int or not 1 <= limit <= MAX_DOCUMENT_BYTES:
        raise InvalidUpdate('Invalid document byte allocation.')
    return read_bytes(workspace, relative_path, limit=limit, binary=True)
