"""macOS process-footprint observations; no model self-report or process mutation."""
import ctypes
import hashlib
import os
from pathlib import Path
import platform
import subprocess


class UsageV2(ctypes.Structure):
    # sys/resource.h rusage_info_v2, including fields after ri_phys_footprint.
    _fields_ = [('uuid',ctypes.c_ubyte*16)] + [(name,ctypes.c_uint64) for name in (
        'user','system','idle_wakeups','interrupt_wakeups','pageins','wired','resident',
        'footprint','start','exit','child_user','child_system','child_idle','child_interrupt',
        'child_pageins','child_elapsed','disk_read','disk_write')]


def command(args):
    return subprocess.check_output(args,text=True,timeout=5,stderr=subprocess.DEVNULL).strip()


def usage(pid):
    if platform.system() != 'Darwin':raise ValueError('MLX footprint sampling requires macOS')
    lib=ctypes.CDLL('/usr/lib/libproc.dylib',use_errno=True)
    fn=lib.proc_pid_rusage;fn.argtypes=[ctypes.c_int,ctypes.c_int,ctypes.c_void_p];fn.restype=ctypes.c_int
    value=UsageV2()
    if fn(pid,2,ctypes.byref(value)) != 0:raise OSError(ctypes.get_errno(),'Process memory unavailable')
    return {'pid':pid,'footprint_bytes':value.footprint,'rss_bytes':value.resident,'start':value.start}


def executable(pid):
    lib=ctypes.CDLL('/usr/lib/libproc.dylib',use_errno=True)
    fn=lib.proc_pidpath;fn.argtypes=[ctypes.c_int,ctypes.c_void_p,ctypes.c_uint32];fn.restype=ctypes.c_int
    buf=ctypes.create_string_buffer(4096)
    if fn(pid,buf,len(buf)) <= 0:raise OSError('Cannot identify the Ollama executable')
    return Path(buf.value.decode()).resolve()


def server_pid():
    raw=command(['/usr/sbin/lsof','-nP','-iTCP:11434','-sTCP:LISTEN','-Fp'])
    ids={int(line[1:]) for line in raw.splitlines() if line.startswith('p')}
    if len(ids)!=1:raise ValueError('Expected one local Ollama listener')
    pid=ids.pop()
    if executable(pid).name != 'ollama':raise ValueError('Listener is not the local Ollama executable')
    return pid


_hashes={}
def file_hash(path):
    st=path.stat();key=(str(path),st.st_size,st.st_mtime_ns,st.st_ino)
    if key not in _hashes:
        h=hashlib.sha256()
        with path.open('rb') as f:
            for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
        _hashes[key]=h.hexdigest()
    return _hashes[key]


def environment(version):
    pid=server_pid();exe=executable(pid)
    libs=sorted(exe.parent.glob('mlx_metal_v*/*'))
    libs=[p for p in libs if p.is_file() and p.suffix in ('.dylib','.metallib')]
    if not libs:raise ValueError('MLX runtime libraries could not be fingerprinted')
    return {'os_build':command(['/usr/sbin/sysctl','-n','kern.osversion']),
            'hardware_model':command(['/usr/sbin/sysctl','-n','hw.model']),
            'physical_bytes':int(command(['/usr/sbin/sysctl','-n','hw.memsize'])),
            'cpu':command(['/usr/sbin/sysctl','-n','machdep.cpu.brand_string']),
            'machine':platform.machine(),'ollama_version':version,
            'executable_sha256':file_hash(exe),
            'mlx_libraries':{str(p.relative_to(exe.parent)):file_hash(p) for p in libs},
            # A restart can change hidden server options. Invalidate instead of
            # assuming the previous allocator/concurrency configuration persists.
            'server_epoch':{'boot':command(['/usr/sbin/sysctl','-n','kern.boottime']),
                            'pid':pid,'start':usage(pid)['start']}}


def sample_tree(pid):
    raw=command(['/bin/ps','-axo','pid=,ppid='])
    pairs=[tuple(map(int,line.split())) for line in raw.splitlines() if line.strip()]
    ids={pid}
    while True:
        new=ids|{child for child,parent in pairs if parent in ids}
        if ids==new:break
        ids=new
    rows=[]
    for child in sorted(ids):
        try:rows.append(usage(child))
        except OSError:
            if child==pid:raise
            # Child exit is normal at unload; the preceding samples remain.
    return {'processes':rows,'footprint_bytes':sum(r['footprint_bytes'] for r in rows),
            'rss_bytes':sum(r['rss_bytes'] for r in rows)}
