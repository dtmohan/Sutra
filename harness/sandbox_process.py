"""Trusted parser process limits and shared macOS Seatbelt declarations."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

from context_manager import InvalidUpdate, canonical


def literal(path):
    text = str(Path(path).resolve())
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise InvalidUpdate('Control characters in sandbox path')
    return json.dumps(text, ensure_ascii=False)


def node_runtime_roots(executable):
    executable = Path(executable).resolve()
    # An Electron helper lives inside the signed application bundle, which also
    # contains the linked frameworks. Standalone Node gets no inferred prefix.
    bundles = [parent for parent in executable.parents if parent.suffix == '.app'
               and (parent / 'Contents/Info.plist').is_file()]
    if bundles:
        return (bundles[-1],)
    for cellar in (Path('/opt/homebrew/Cellar'), Path('/usr/local/Cellar')):
        if cellar in executable.parents:
            # Homebrew's linked libraries resolve into the same package cellar.
            return (cellar,)
    return ()


def profile(executable, read_roots=(), read_files=(), write_roots=()):
    def rule(op, paths, predicate):
        paths = sorted(set(paths), key=str)
        return '(' + op + ' ' + ' '.join('(' + predicate + ' ' + literal(p) + ')' for p in paths) + ')' if paths else ''

    roots = tuple(Path(p).resolve() for p in ('/System','/usr','/Library/Apple',*read_roots))
    files = tuple(Path(p).resolve() for p in ('/','/dev/null','/dev/urandom',executable,*read_files))
    scratch = tuple(Path(p).resolve() for p in write_roots)
    ancestors = {parent for p in (*roots,*files,*scratch) for parent in p.parents}
    return ('(version 1)(deny default)(allow sysctl-read)'
            + rule('allow process-exec', [Path(executable).resolve()], 'literal')
            + rule('allow file-read-metadata', ancestors, 'literal')
            + rule('allow file-read*', files, 'literal')
            + rule('allow file-read*', (*roots,*scratch), 'subpath')
            + rule('allow file-write*', scratch, 'subpath'))


def parser_output(node, script, compiler, packet, *, seconds, output_limit):
    """Run the fixed parser with bounded streams and no inherited tool settings."""
    node, script, compiler = (Path(p).resolve() for p in (node, script, compiler))
    # Use the same compiler path in Node resolution and the Seatbelt rules.
    body = canonical(dict(packet, typescript=str(compiler)))
    env = {'ELECTRON_RUN_AS_NODE': '1', 'LANG': 'en_US.UTF-8'}
    if os.name == 'nt':
        env.update({key: os.environ[key] for key in ('SystemRoot','WINDIR') if key in os.environ})
    command = [str(node), str(script)]
    if sys.platform == 'darwin':
        # Node checks the bundled package marker to decide how to load .js.
        seatbelt = profile(node, (*node_runtime_roots(node), compiler.parent),
                           (script, script.parent.parent/'package.json'))
        command = ['/usr/bin/sandbox-exec', '-p', seatbelt, *command]
    with tempfile.TemporaryFile() as request:
        request.write(body.encode('utf-8'));request.seek(0)
        with subprocess.Popen(command, stdin=request, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=env, cwd=script.parent) as proc:
            exceeded = threading.Event()
            output, error = bytearray(), bytearray()

            def collect(stream, destination, limit):
                while True:
                    chunk = stream.read(min(65536, limit - len(destination) + 1))
                    if not chunk:
                        return
                    if len(destination) + len(chunk) > limit:
                        exceeded.set()
                        proc.kill()
                        return
                    destination.extend(chunk)

            readers = [threading.Thread(target=collect, args=(stream, destination, limit))
                       for stream, destination, limit in ((proc.stdout,output,output_limit),(proc.stderr,error,8192))]
            for reader in readers:reader.start()
            try:
                proc.wait(timeout=seconds)
            except BaseException as exc:
                proc.kill();proc.wait()
                if isinstance(exc,subprocess.TimeoutExpired):
                    raise InvalidUpdate('Parser process exceeded its time allocation') from None
                raise
            finally:
                for reader in readers:reader.join()
            if exceeded.is_set():raise InvalidUpdate('Parser process exceeded its output byte allocation')
            return proc.returncode, output.decode('utf-8'), error.decode('utf-8','replace')
