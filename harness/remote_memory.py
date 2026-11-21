"""Fresh remote GPU admission through the existing Karma/STP boundary.

Only a loaded, content-bound llama.cpp instance is admitted. A physically
resident weights/KV pool already counts against observed free VRAM. If Windows
evicts it, a separately configured full restoration envelope is reserved before
compute can restore the pool. Margins and reserves remain planning inputs,
never claims about a verified inference peak.
"""
from collections import OrderedDict
from contextlib import contextmanager
import base64
import copy
import math
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import tempfile
import threading
import time

from context_manager import InvalidUpdate, canonical
from karma_adapter import KarmaAdapter
from monitoring import ResourceWait


PROFILE = 'remote-nvidia-v1'
MAX_SAMPLE_AGE = 8
MAX_CAPTURE_SECONDS = 20
TUNNEL_READINESS_SECONDS = 20
MAX_COLD_CAPTURE_SECONDS = 180
MAX_COLD_RPC_SECONDS = 160
MIN_HASH_BYTES_PER_SECOND = 128 * 1024**2
COLD_RPC_FIXED_SECONDS = 5
COLD_METADATA_SECONDS = 20
MAX_INTEGER = 2**53 - 1
GIB = 1024**3
_DIGEST = re.compile('[a-f0-9]{64}')
_GPU_UUID = re.compile(r'GPU-[a-fA-F0-9-]{16,64}')
_verified = OrderedDict()
_verified_lock = threading.Lock()
MAX_VERIFIED_MODELS = 32
_REMOTE_STEPS = frozenset(('listener', 'process', 'executable_hash', 'os_identity',
    'model_stat', 'model_hash', 'gpu_inventory', 'gpu_processes', 'ram',
    'instance_recheck', 'reply'))
_PS_STAGES = r'''
function Observation-Step([string]$name) {
 [Console]::Error.WriteLine('SUTRA_REMOTE_STAGE:'+$name)
 [Console]::Error.Flush()
}
'''


@contextmanager
def _stage(name, seconds=None):
    """Retain bounded host diagnostics without exposing exception arguments."""
    began = time.monotonic()
    try:
        yield
    except Exception as error:
        if not getattr(error, 'remote_observation', None):
            cause = error
            for _ in range(4):
                nested = cause.__cause__ or cause.__context__
                if nested is None or nested is cause:
                    break
                cause = nested
            details = {'stage': name, 'elapsed_seconds': round(max(0, time.monotonic() - began), 6),
                       'timeout_seconds': seconds, 'error_type': type(error).__name__[:80],
                       'cause_type': type(cause).__name__[:80],
                       'failure_kind': ('timeout' if isinstance(cause, (TimeoutError, subprocess.TimeoutExpired))
                                        else 'unavailable' if isinstance(cause, OSError) else 'rejected')}
            stderr = getattr(cause, 'stderr', None)
            if isinstance(stderr, bytes):
                stderr = stderr[-8192:].decode('ascii', errors='ignore')
            if isinstance(stderr, str):
                markers = re.findall(r'^SUTRA_REMOTE_STAGE:([a-z_]{1,32})\r?$', stderr[-8192:], re.M)
                known = [marker for marker in markers if marker in _REMOTE_STEPS]
                if known:
                    details['last_remote_step'] = known[-1]
            error.remote_observation = details
        raise


def _literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def _probe_script(profile, nonce, cached=None, expected_binding=None):
    # Every substituted value comes from the trusted, validated host profile,
    # not the model packet. Never put an API key or raw command line in output.
    return _PS_STAGES + r'''
$ErrorActionPreference='Stop'
$modelPath=MODEL_PATH
$port=REMOTE_PORT
$gpuIndex=GPU_INDEX
Observation-Step 'listener'
$listeners=@(Get-NetTCPConnection -State Listen -LocalPort $port)
if ($listeners.Count -ne 1 -or $listeners[0].LocalAddress -ne '127.0.0.1') { throw 'One loopback listener is required' }
$listener=$listeners[0]
Observation-Step 'process'
$proc=Get-CimInstance Win32_Process -Filter ('ProcessId='+$listener.OwningProcess)
if (!$proc -or !$proc.ExecutablePath) { throw 'Listener process identity unavailable' }
function Read-Argument($names) {
  foreach ($name in $names) {
    $pattern='(?i)(?:^|\s)'+[regex]::Escape($name)+'(?:=|\s+)(?:"([^"]*)"|([^\s"]+))'
    $match=[regex]::Match($proc.CommandLine,$pattern)
    if($match.Success) { if($match.Groups[1].Success) { return $match.Groups[1].Value }; return $match.Groups[2].Value }
  }
  return $null
}
$exe=Get-Item -LiteralPath $proc.ExecutablePath
Observation-Step 'executable_hash'
$exeHash=(Get-FileHash -LiteralPath $exe.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
Observation-Step 'os_identity'
$os=Get-CimInstance Win32_OperatingSystem
Observation-Step 'model_stat'
$before=Get-Item -LiteralPath $modelPath -Force
if ($before.PSIsContainer -or ($before.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Model is not a regular file' }
$stamp=@($before.Length,$before.LastWriteTimeUtc.Ticks,$before.CreationTimeUtc.Ticks)
$binding=@{host_id=$env:COMPUTERNAME;boot_id=$os.LastBootUpTime.ToUniversalTime().ToString('o');pid=[int]$proc.ProcessId;
 started_at=$proc.CreationDate.ToUniversalTime().ToString('o');executable_sha256=$exeHash;model_path=$before.FullName;
 bytes=[long]$before.Length;mtime_ticks=$before.LastWriteTimeUtc.Ticks.ToString();creation_ticks=$before.CreationTimeUtc.Ticks.ToString()}
$expected=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String(EXPECTED_JSON)) | ConvertFrom-Json
if($null -ne $expected) { foreach($key in $binding.Keys) { if($expected.$key -cne $binding[$key]) { throw 'Remote model identity changed after cold-size preflight' } } }
$cached=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String(CACHE_JSON)) | ConvertFrom-Json
$reuse=($null -ne $cached)
if($reuse) { foreach($key in $binding.Keys) { if($cached.binding.$key -cne $binding[$key]) { $reuse=$false } } }
$timer=[Diagnostics.Stopwatch]::StartNew()
Observation-Step 'model_hash'
if($reuse) { $modelHash=$cached.sha256; $digestBasis='verified_hash_reused_with_stat_and_instance' }
else { $modelHash=(Get-FileHash -LiteralPath $modelPath -Algorithm SHA256).Hash.ToLowerInvariant(); $digestBasis='fresh_full_hash' }
$timer.Stop()
$after=Get-Item -LiteralPath $modelPath -Force
if (($stamp -join ':') -ne (@($after.Length,$after.LastWriteTimeUtc.Ticks,$after.CreationTimeUtc.Ticks) -join ':')) { throw 'Model changed during identity observation' }
Observation-Step 'gpu_inventory'
$gpuRaw=@(& nvidia-smi --query-gpu=index,uuid,name,driver_version,memory.total,memory.free,memory.used --format=csv,noheader,nounits)
if ($LASTEXITCODE -ne 0) { throw 'NVIDIA GPU measurement failed' }
$gpuRows=@($gpuRaw | ConvertFrom-Csv -Header index,uuid,name,driver,total_mib,free_mib,used_mib)
$selected=@($gpuRows | Where-Object { [int]$_.index -eq $gpuIndex })
if ($selected.Count -ne 1) { throw 'Configured GPU is not unique' }
$gpu=$selected[0]
Observation-Step 'gpu_processes'
$appRaw=@(& nvidia-smi --query-compute-apps=pid,gpu_uuid --format=csv,noheader,nounits)
if ($LASTEXITCODE -ne 0) { throw 'NVIDIA process measurement failed' }
$apps=@($appRaw | ConvertFrom-Csv -Header pid,uuid)
$gpuPids=@($apps | Where-Object { $_.uuid.Trim() -eq $gpu.uuid.Trim() } | ForEach-Object { [int]$_.pid })
# RAM and device counters are always measured after any cold hash pass.
Observation-Step 'ram'
$os=Get-CimInstance Win32_OperatingSystem
Observation-Step 'instance_recheck'
$again=Get-CimInstance Win32_Process -Filter ('ProcessId='+$listener.OwningProcess)
if (!$again -or $again.CreationDate -ne $proc.CreationDate -or $again.ExecutablePath -ne $proc.ExecutablePath) { throw 'Listener instance changed during measurement' }
Observation-Step 'reply'
@{
 version=1;nonce=NONCE;host_id=$env:COMPUTERNAME;boot_id=$os.LastBootUpTime.ToUniversalTime().ToString('o')
 listener=@{address=$listener.LocalAddress;port=[int]$listener.LocalPort;pid=[int]$listener.OwningProcess}
 process=@{pid=[int]$proc.ProcessId;started_at=$proc.CreationDate.ToUniversalTime().ToString('o');executable=$exe.FullName;executable_sha256=$exeHash;
   model_path=(Read-Argument @('--model','-m'));model=(Read-Argument @('--alias','-a'));context_tokens=(Read-Argument @('--ctx-size','-c'));
   gpu_layers=(Read-Argument @('--n-gpu-layers','--gpu-layers','-ngl'));gpu_device=(Read-Argument @('--device','-dev'))}
 model=@{path=$after.FullName;sha256=$modelHash;bytes=[long]$after.Length;mtime_ticks=$after.LastWriteTimeUtc.Ticks.ToString();creation_ticks=$after.CreationTimeUtc.Ticks.ToString();digest_basis=$digestBasis;hash_ms=$timer.Elapsed.TotalMilliseconds}
 gpu=@{index=[int]$gpu.index;uuid=$gpu.uuid.Trim();name=$gpu.name.Trim();driver=$gpu.driver.Trim();total_bytes=([long]$gpu.total_mib*1048576);
   free_bytes=([long]$gpu.free_mib*1048576);used_bytes=([long]$gpu.used_mib*1048576);process_pids=$gpuPids}
 ram=@{total_bytes=([long]$os.TotalVisibleMemorySize*1024);free_bytes=([long]$os.FreePhysicalMemory*1024)}
} | ConvertTo-Json -Depth 6 -Compress
'''.replace('MODEL_PATH', _literal(profile['model_path'])).replace(
        'REMOTE_PORT', str(profile['remote_port'])).replace('GPU_INDEX', str(profile['gpu_index'])).replace(
        'NONCE', _literal(nonce)).replace('CACHE_JSON', _literal(base64.b64encode(canonical(cached).encode()).decode())).replace(
        'EXPECTED_JSON', _literal(base64.b64encode(canonical(expected_binding).encode()).decode()))



def verification_budget(model_bytes, digest_basis):
    """Bound identity work by bytes; never relax counter freshness or capacity."""
    _integer(model_bytes, 'model bytes', 1)
    if digest_basis == 'verified_hash_reused_with_stat_and_instance':
        return {'kind': 'verified_identity_reuse', 'model_bytes': model_bytes,
                'rpc_seconds': MAX_CAPTURE_SECONDS, 'capture_seconds': MAX_CAPTURE_SECONDS,
                'minimum_hash_bytes_per_second': None, 'ceiling_applied': False}
    if digest_basis != 'fresh_full_hash':
        raise ValueError('Remote model digest evidence is unavailable')
    # Integer arithmetic avoids rounding surprises for large JSON-safe sizes.
    hash_seconds = (model_bytes + MIN_HASH_BYTES_PER_SECOND - 1) // MIN_HASH_BYTES_PER_SECOND
    requested = max(MAX_CAPTURE_SECONDS, COLD_RPC_FIXED_SECONDS + hash_seconds)
    rpc_seconds = min(MAX_COLD_RPC_SECONDS, requested)
    return {'kind': 'cold_identity_from_fresh_file_size', 'model_bytes': model_bytes,
            'rpc_seconds': rpc_seconds,
            'capture_seconds': min(MAX_COLD_CAPTURE_SECONDS, rpc_seconds + COLD_METADATA_SECONDS),
            'minimum_hash_bytes_per_second': MIN_HASH_BYTES_PER_SECOND,
            'ceiling_applied': requested > MAX_COLD_RPC_SECONDS}


def _preflight_script(profile, nonce):
    # File and process metadata only. No weights hash, GPU read or inference.
    return _PS_STAGES + r'''
Observation-Step 'model_stat'
$file=Get-Item -LiteralPath MODEL_PATH -Force
if ($file.PSIsContainer -or ($file.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Model is not a regular file' }
Observation-Step 'listener'
$listeners=@(Get-NetTCPConnection -State Listen -LocalPort PORT)
if ($listeners.Count -ne 1 -or $listeners[0].LocalAddress -ne '127.0.0.1') { throw 'One loopback listener is required' }
Observation-Step 'process'
$proc=Get-CimInstance Win32_Process -Filter ('ProcessId='+$listeners[0].OwningProcess)
if (!$proc -or !$proc.ExecutablePath) { throw 'Listener process identity unavailable' }
Observation-Step 'executable_hash'
$exeHash=(Get-FileHash -LiteralPath $proc.ExecutablePath -Algorithm SHA256).Hash.ToLowerInvariant()
Observation-Step 'os_identity'
$os=Get-CimInstance Win32_OperatingSystem
@{version=1;nonce=NONCE;binding=@{host_id=$env:COMPUTERNAME;boot_id=$os.LastBootUpTime.ToUniversalTime().ToString('o');
 pid=[int]$proc.ProcessId;started_at=$proc.CreationDate.ToUniversalTime().ToString('o');executable_sha256=$exeHash;
 model_path=$file.FullName;bytes=[long]$file.Length;mtime_ticks=$file.LastWriteTimeUtc.Ticks.ToString();creation_ticks=$file.CreationTimeUtc.Ticks.ToString()}} | ConvertTo-Json -Depth 4 -Compress
'''.replace('MODEL_PATH', _literal(profile['model_path'])).replace(
        'PORT', str(profile['remote_port'])).replace('NONCE', _literal(nonce))


def _preflight_binding(profile, response, nonce):
    if (not isinstance(response, dict) or type(response.get('version')) is not int or
            response['version'] != 1 or response.get('nonce') != nonce):
        raise InvalidUpdate('Remote size preflight does not match its fresh observation request')
    binding = response.get('binding')
    if not isinstance(binding, dict) or set(binding) != {
            'host_id', 'boot_id', 'pid', 'started_at', 'executable_sha256',
            'model_path', 'bytes', 'mtime_ticks', 'creation_ticks'}:
        raise InvalidUpdate('Remote size preflight identity is invalid')
    _integer(binding['bytes'], 'model bytes', 1)
    _integer(binding['pid'], 'listener process', 1)
    for key in ('host_id', 'boot_id', 'started_at'):
        _string(binding[key], 'preflight ' + key)
    if _path(binding['model_path']) != _path(profile['model_path']):
        raise InvalidUpdate('Remote size preflight refers to a different model file')
    digest = binding['executable_sha256']
    if (not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None or
            profile.get('executable_sha256') and digest != profile['executable_sha256']):
        raise InvalidUpdate('Remote size preflight executable differs from its committed identity')
    for key in ('mtime_ticks', 'creation_ticks'):
        if not isinstance(binding[key], str) or re.fullmatch(r'[1-9][0-9]{0,18}', binding[key]) is None:
            raise InvalidUpdate('Remote size preflight file timestamp is invalid')
    return copy.deepcopy(binding)


def _remaining(cap, deadline):
    seconds = min(cap, deadline - time.monotonic())
    if seconds <= 0:
        raise InvalidUpdate('Remote identity observation exceeded its bounded verification window')
    return seconds


def _cache_binding(sample):
    process, model = sample['process'], sample['model']
    return {'host_id': sample['host_id'], 'boot_id': sample['boot_id'], 'pid': process['pid'],
            'started_at': process['started_at'], 'executable_sha256': process['executable_sha256'],
            'model_path': model['path'], 'bytes': model['bytes'],
            'mtime_ticks': model['mtime_ticks'], 'creation_ticks': model['creation_ticks']}


def _cached(profile):
    with _verified_lock:
        value = _verified.get(profile['profile_hash'])
        if value is not None:
            _verified.move_to_end(profile['profile_hash'])
        return copy.deepcopy(value)


def _remember(profile, sample):
    with _verified_lock:
        _verified[profile['profile_hash']] = {'binding': _cache_binding(sample),
                                               'sha256': sample['model']['sha256']}
        _verified.move_to_end(profile['profile_hash'])
        while len(_verified) > MAX_VERIFIED_MODELS:
            _verified.popitem(last=False)


def _context(props):
    settings = props.get('default_generation_settings') or {}
    if not isinstance(settings, dict) or not isinstance(settings.get('params', {}), dict):
        raise InvalidUpdate('Remote context metadata is invalid')
    return settings.get('n_ctx', (settings.get('params') or {}).get('n_ctx'))


def _server(profile, deadline=None):
    from remote_control import request_json
    def read(path):
        cap = 5 if deadline is None else _remaining(5, deadline)
        with _stage('server_' + path.strip('/').replace('/', '_'), cap):
            return request_json(profile, path, timeout_seconds=cap)
    props = read('/props')
    models = read('/v1/models')
    slots = read('/slots')
    if not isinstance(props, dict) or not isinstance(models, dict) or not isinstance(slots, list):
        raise InvalidUpdate('Remote provider metadata has an invalid shape')
    rows = models.get('data')
    if not isinstance(rows, list) or len(rows) > 16 or len(slots) > 16:
        raise InvalidUpdate('Remote provider metadata exceeds its observation bound')
    names = []
    for row in rows:
        if not isinstance(row, dict):
            raise InvalidUpdate('Remote model inventory is invalid')
        aliases = row.get('aliases') or []
        if not isinstance(aliases, list) or len(aliases) > 16:
            raise InvalidUpdate('Remote model aliases are invalid')
        names += [row.get('id')] + aliases
    return {'models': names, 'model_path': props.get('model_path'),
            'context_tokens': _context(props), 'total_slots': props.get('total_slots'),
            'is_sleeping': props.get('is_sleeping'),
            'slots': [{'id': row.get('id'), 'is_processing': row.get('is_processing')}
                      if isinstance(row, dict) else {} for row in slots]}


def verify_instance(profile, instance, *, timeout_seconds=5):
    """Confirm the same listener process, without rereading weights or claiming idle."""
    from remote_control import run_ps
    script = r'''
$ErrorActionPreference='Stop'
$rows=@(Get-NetTCPConnection -State Listen -LocalPort PORT)
if($rows.Count -ne 1) { throw 'Remote listener is not unique' }
$p=Get-CimInstance Win32_Process -Filter ('ProcessId='+$rows[0].OwningProcess)
$os=Get-CimInstance Win32_OperatingSystem
$file=Get-Item -LiteralPath MODEL_PATH -Force
@{pid=[int]$rows[0].OwningProcess;address=$rows[0].LocalAddress;started_at=$p.CreationDate.ToUniversalTime().ToString('o');executable=$p.ExecutablePath;
  host_id=$env:COMPUTERNAME;boot_id=$os.LastBootUpTime.ToUniversalTime().ToString('o');
  model_file=@{bytes=[long]$file.Length;mtime_ticks=$file.LastWriteTimeUtc.Ticks.ToString();creation_ticks=$file.CreationTimeUtc.Ticks.ToString()}} | ConvertTo-Json -Depth 3 -Compress
'''.replace('PORT', str(profile['remote_port'])).replace('MODEL_PATH', _literal(profile['model_path']))
    with _stage('instance_confirmation', timeout_seconds):
        current = run_ps(profile, script, timeout_seconds=timeout_seconds, reuse_tunnel=True)
    if (not isinstance(current, dict) or current.get('pid') != instance.get('pid') or
            current.get('started_at') != instance.get('started_at') or
            current.get('address') != '127.0.0.1' or
            _path(current.get('executable')) != _path(instance.get('executable')) or
            current.get('host_id') != instance.get('host_id') or
            current.get('boot_id') != instance.get('boot_id') or
            current.get('model_file') != instance.get('model_file')):
        raise InvalidUpdate('Remote listener instance changed after admission')
    return current


def probe(profile):
    """Separate a bounded cold identity pass from fresh counter admission."""
    from lifecycle import remaining
    from remote_control import ensure_tunnel, run_ps
    cached = _cached(profile)
    recovered_timeout = None
    while True:
        with _stage('ssh_tunnel', TUNNEL_READINESS_SECONDS):
            ensure_tunnel(profile)
        started = time.time()
        began = time.monotonic()
        nonce = secrets.token_hex(16)
        with _stage('identity_preflight', 5):
            preflight = _preflight_binding(profile,
                run_ps(profile, _preflight_script(profile, nonce), timeout_seconds=5, reuse_tunnel=True), nonce)
            reuse = cached is not None and cached['binding'] == preflight
            if recovered_timeout is not None and not reuse:
                raise InvalidUpdate('Remote identity changed before the warm inspection retry')
        basis = 'verified_hash_reused_with_stat_and_instance' if reuse else 'fresh_full_hash'
        budget = verification_budget(preflight['bytes'], basis)
        deadline = began + budget['capture_seconds']
        rpc_began = time.monotonic()
        rpc_limit = _remaining(budget['rpc_seconds'], deadline)
        try:
            with _stage('identity_probe', rpc_limit):
                observed = run_ps(profile, _probe_script(profile, nonce, cached, preflight),
                                  timeout_seconds=rpc_limit, reuse_tunnel=True)
        except (OSError, InvalidUpdate) as error:
            failure = getattr(error, 'remote_observation', {})
            available = remaining()
            # Only a transient warm transport timeout earns a retry. All GPU
            # evidence must come from the new nonce-bound observation.
            if (not reuse or recovered_timeout is not None or failure.get('failure_kind') != 'timeout'
                    or (available is not None and available < TUNNEL_READINESS_SECONDS + MAX_CAPTURE_SECONDS)):
                if recovered_timeout is not None:
                    failure['prior_timeout'] = recovered_timeout
                raise
            recovered_timeout = failure
        else:
            break
    rpc_elapsed = time.monotonic() - rpc_began
    if not isinstance(observed, dict) or observed.get('nonce') != nonce:
        raise InvalidUpdate('Remote hardware response does not match its fresh observation request')
    model = observed.get('model') or {}
    if _cache_binding(observed) != preflight or model.get('digest_basis') != basis:
        raise InvalidUpdate('Remote model identity changed after cold-size preflight')
    hash_ms = model.get('hash_ms')
    if (type(hash_ms) not in (int, float) or not math.isfinite(hash_ms) or
            not 0 <= hash_ms <= rpc_elapsed * 1000):
        raise InvalidUpdate('Remote hash timing exceeds its containing SSH observation')
    # Counters are measured after hashing. This conservative upper bound also
    # includes pre-hash process/SSH overhead; it never turns cold hash time into
    # eight-second-old GPU/RAM evidence or trusts the remote host's wall clock.
    measured_at = time.time()
    counter_age_upper = rpc_elapsed - hash_ms / 1000
    observed['server'] = _server(profile, deadline=deadline)
    binding = dict(observed.get('process') or {}, host_id=observed.get('host_id'),
                   boot_id=observed.get('boot_id'), model_file={key: model.get(key)
                   for key in ('bytes', 'mtime_ticks', 'creation_ticks')})
    verify_instance(profile, binding, timeout_seconds=_remaining(5, deadline))
    observed.update(profile=PROFILE, profile_hash=profile['profile_hash'],
                    wall_time=measured_at - counter_age_upper,
                    capture_started_at=started, capture_seconds=time.monotonic() - began,
                    verification_budget=budget, counter_age_upper_seconds=counter_age_upper)
    observed.pop('recovered_identity_timeout', None)
    if recovered_timeout is not None:
        observed['recovered_identity_timeout'] = recovered_timeout
    if basis == 'verified_hash_reused_with_stat_and_instance':
        if (not cached or cached['sha256'] != model.get('sha256') or
                cached['binding'] != _cache_binding(observed)):
            raise InvalidUpdate('Remote model digest reuse does not match this process verified checkpoint')
    # A real, fully validated hash only: no file/ledger reconstruction or cache
    # injection. Failed, stale, busy and mismatched observations stay untrusted.
    try:
        if _validate(profile, observed, 1, time.time()) is not None:
            _remember(profile, observed)
    except (KeyError, TypeError, ValueError, OverflowError):
        pass
    return observed


def _integer(value, label, minimum=0):
    if type(value) is not int or not minimum <= value <= MAX_INTEGER:
        raise ValueError('Invalid remote ' + label)
    return value


def _string(value, label, maximum=512):
    if not isinstance(value, str) or not 1 <= len(value) <= maximum or not value.isprintable():
        raise ValueError('Invalid remote ' + label)
    return value


def _path(value):
    return value.replace('/', '\\').rstrip('\\').casefold() if isinstance(value, str) else None


def _blocked(reason, *, kind='measurement_unavailable', adapter=None, sample=None):
    adapter = adapter or KarmaAdapter()
    state = dict(adapter.frame(PROFILE, 2 * adapter.boundary.k, 0.), available=False,
                 admitted=False, provider='remote_llama', reason=reason,
                 blocker=kind, sample=sample, observed_at=time.time(),
                 thermal_label='unavailable', peak_memory_verified=False)
    return state


def _validate(profile, sample, requested_context, now):
    if (not isinstance(sample, dict) or sample.get('profile') != PROFILE or
            type(sample.get('version')) is not int or sample['version'] != 1):
        raise ValueError('Remote sample profile is invalid')
    if sample.get('profile_hash') != profile['profile_hash']:
        raise ValueError('Remote profile changed after observation')
    if not isinstance(sample.get('nonce'), str) or re.fullmatch('[a-f0-9]{32}', sample['nonce']) is None:
        raise ValueError('Remote observation request identity is invalid')
    stamps = [sample.get('wall_time'), sample.get('capture_started_at'), sample.get('capture_seconds'), now]
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in stamps):
        raise ValueError('Remote observation time is invalid')
    at, start, elapsed, _ = stamps
    model_evidence = sample.get('model')
    if not isinstance(model_evidence, dict):
        raise ValueError('Remote model identity section is invalid')
    budget = verification_budget(model_evidence.get('bytes'), model_evidence.get('digest_basis'))
    supplied = sample.get('verification_budget')
    if supplied is not None and supplied != budget:
        raise ValueError('Remote verification window differs from its file-size bound')
    # Old display/test records do not acquire a larger window merely because
    # they mention a large file; new probes carry their exact derived policy.
    capture_limit = budget['capture_seconds'] if supplied is not None else MAX_CAPTURE_SECONDS
    counter_age = sample.get('counter_age_upper_seconds')
    if (counter_age is not None and (type(counter_age) not in (int, float) or
            not math.isfinite(counter_age) or not 0 <= counter_age <= MAX_SAMPLE_AGE)):
        raise ValueError('Remote counter observation exceeds its freshness bound')
    if not 0 <= now - at <= MAX_SAMPLE_AGE or not 0 <= elapsed <= capture_limit or not start <= at:
        raise ValueError('Remote observation is stale, future-dated or exceeded its capture window')
    if not 0 <= at - start <= capture_limit:
        raise ValueError('Remote observation capture window is invalid')
    _integer(requested_context, 'requested context', 1)
    if requested_context > profile['context_tokens']:
        raise ValueError('Requested context exceeds the configured remote context pool')
    host = _string(sample.get('host_id'), 'host identity')
    boot = _string(sample.get('boot_id'), 'boot identity')
    listener, process, model = (sample.get(key) for key in ('listener', 'process', 'model'))
    gpu, ram, server = (sample.get(key) for key in ('gpu', 'ram', 'server'))
    if not all(isinstance(value, dict) for value in (listener, process, model, gpu, ram, server)):
        raise ValueError('Remote hardware identity sections are invalid')
    pid = _integer(process.get('pid'), 'listener process', 1)
    if (listener.get('pid') != pid or listener.get('address') != '127.0.0.1' or
            listener.get('port') != profile['remote_port']):
        raise ValueError('Remote listener does not match the configured loopback endpoint')
    started = _string(process.get('started_at'), 'process start')
    executable = _string(process.get('executable'), 'executable path', 4096)
    if _path(executable).rsplit('\\', 1)[-1] != 'llama-server.exe':
        raise ValueError('Remote listener is not the configured llama.cpp executable')
    exe_hash = process.get('executable_sha256')
    if not isinstance(exe_hash, str) or _DIGEST.fullmatch(exe_hash) is None:
        raise ValueError('Remote executable fingerprint is invalid')
    if profile.get('executable_sha256') and exe_hash != profile['executable_sha256']:
        raise ValueError('Remote executable differs from its pinned fingerprint')
    if (model.get('sha256') != profile['model_sha256'] or
            _path(model.get('path')) != _path(profile['model_path']) or
            _path(process.get('model_path')) != _path(profile['model_path']) or
            _path(server.get('model_path')) != _path(profile['model_path'])):
        raise ValueError('Remote resident model differs from the committed GGUF identity')
    _integer(model.get('bytes'), 'model bytes', 1)
    # Ticks are emitted as strings because Windows ticks exceed JS safe integers.
    for key in ('mtime_ticks', 'creation_ticks'):
        if not isinstance(model.get(key), str) or re.fullmatch(r'[1-9][0-9]{0,18}', model[key]) is None:
            raise ValueError('Invalid remote model file identity')
    if model.get('digest_basis') not in ('fresh_full_hash', 'verified_hash_reused_with_stat_and_instance'):
        raise ValueError('Remote model digest evidence is unavailable')
    duration = model.get('hash_ms')
    if type(duration) not in (int, float) or not math.isfinite(duration) or not 0 <= duration <= min(budget['rpc_seconds'], elapsed) * 1000:
        raise ValueError('Remote model digest timing is invalid')
    aliases = _string(process.get('model'), 'process model aliases', 512)
    if profile['model'] not in aliases.split(','):
        raise ValueError('Remote process model alias changed')
    api_models = server.get('models')
    if not isinstance(api_models, list) or not 1 <= len(api_models) <= 32:
        raise ValueError('Remote API model aliases are invalid')
    for name in api_models:
        _string(name, 'API model alias', 512)
    if profile['model'] not in api_models:
        raise ValueError('Remote API model alias changed')
    context = server.get('context_tokens')
    if type(context) is not int or context != profile['context_tokens']:
        raise ValueError('Remote context pool changed; refresh the configured profile before use')
    if str(process.get('context_tokens')) != str(context):
        raise ValueError('Remote process context differs from the API allocation')
    if process.get('gpu_layers') != 'all' or process.get('gpu_device') != 'CUDA' + str(profile['gpu_index']):
        raise ValueError('Remote process does not select the configured CUDA device and full layer offload')
    gpu_uuid = gpu.get('uuid')
    if (type(gpu.get('index')) is not int or gpu['index'] != profile['gpu_index'] or
            not isinstance(gpu_uuid, str) or _GPU_UUID.fullmatch(gpu_uuid) is None or
            profile.get('gpu_uuid') and gpu_uuid != profile['gpu_uuid']):
        raise ValueError('Remote GPU differs from the configured device')
    _string(gpu.get('name'), 'GPU name', 256)
    _string(gpu.get('driver'), 'GPU driver', 64)
    processes = gpu.get('process_pids')
    if not isinstance(processes, list) or not 0 <= len(processes) <= 256:
        raise ValueError('Remote GPU process residency is unavailable')
    for entry in processes:
        _integer(entry, 'GPU process', 1)
    if pid not in processes:
        required = ('cold_gpu_bytes', 'cold_host_bytes', 'cold_context_tokens')
        if any(key not in profile for key in required):
            raise ValueError('The model is not physically resident on the GPU; a full restoration allocation is required')
        for key in required:
            _integer(profile[key], 'restoration ' + key, 1)
        if profile['cold_context_tokens'] < profile['context_tokens']:
            raise ValueError('Restoration allocation does not cover the configured context pool')
        if profile['cold_gpu_bytes'] < model['bytes']:
            raise ValueError('Full GPU restoration allocation is smaller than the committed model file')
    for section in (gpu, ram):
        total = _integer(section.get('total_bytes'), 'physical memory', 1)
        free = _integer(section.get('free_bytes'), 'free memory')
        if free > total:
            raise ValueError('Remote free memory exceeds physical capacity')
    used = _integer(gpu.get('used_bytes'), 'used VRAM')
    # Driver-reserved VRAM is not necessarily included in ``used``. Free RAM
    # already excludes it; missing bytes must not be credited as headroom.
    # Independently rounded MiB counters may exceed total by at most 2 MiB.
    if used > gpu['total_bytes'] or used + gpu['free_bytes'] > gpu['total_bytes'] + 2 * 1024**2:
        raise ValueError('Remote VRAM counters are inconsistent')
    if (type(server.get('total_slots')) is not int or server['total_slots'] != 1 or
            not isinstance(server.get('slots'), list) or len(server['slots']) != 1):
        raise ValueError('Remote server must expose one verifiable inference slot')
    if server.get('is_sleeping') is not False:
        raise ValueError('The remote model is sleeping or its loaded state is unavailable')
    slot = server['slots'][0]
    if not isinstance(slot, dict) or type(slot.get('is_processing')) is not bool:
        raise ValueError('Remote inference activity is unavailable')
    if slot['is_processing']:
        return None
    return dict(host_id=host, boot_id=boot, pid=pid, started_at=started, executable=executable,
                executable_sha256=exe_hash, gpu_uuid=gpu_uuid,
                model_sha256=model['sha256'], context_tokens=context,
                model_file={key: model[key] for key in ('bytes', 'mtime_ticks', 'creation_ticks')})


def _compact(sample):
    """Keep bounded evidence only, excluding incidental server metadata."""
    result = {key: sample[key] for key in ('profile', 'profile_hash', 'version', 'nonce',
              'wall_time', 'capture_started_at', 'capture_seconds', 'host_id', 'boot_id')}
    for key in ('verification_budget', 'counter_age_upper_seconds', 'recovered_identity_timeout'):
        if key in sample:
            result[key] = sample[key]
    fields = {'listener': ('address', 'port', 'pid'),
              'process': ('pid', 'started_at', 'executable', 'executable_sha256', 'model_path',
                          'model', 'context_tokens', 'gpu_layers', 'gpu_device'),
              'model': ('path', 'sha256', 'bytes', 'mtime_ticks', 'creation_ticks', 'digest_basis', 'hash_ms'),
              'gpu': ('index', 'uuid', 'name', 'driver', 'total_bytes', 'free_bytes', 'used_bytes', 'process_pids'),
              'ram': ('total_bytes', 'free_bytes'),
              'server': ('models', 'model_path', 'context_tokens', 'total_slots', 'is_sleeping', 'slots')}
    for section, keys in fields.items():
        result[section] = {key: sample[section][key] for key in keys}
    return result


def evaluate(profile, sample, requested_context, *, now=None, adapter=None):
    """Pure admission; never mutates the task, source pages, grants or spending."""
    adapter = adapter or KarmaAdapter()
    now = time.time() if now is None else now
    try:
        instance = _validate(profile, sample, requested_context, now)
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        state = _blocked(str(error)[:300], adapter=adapter)
        state['observation_failure'] = {'stage': 'hardware_validation',
            'error_type': type(error).__name__, 'failure_kind': 'rejected'}
        return state
    if instance is None:
        return _blocked('The remote inference slot is still processing work; no new model call was started.',
                        kind='runtime_busy', adapter=adapter)
    sample = _compact(sample)
    gpu, ram = sample['gpu'], sample['ram']
    margin = profile['working_set_bytes']
    resident = instance['pid'] in gpu['process_pids']
    gpu_cost = margin if resident else profile['cold_gpu_bytes'] + margin
    host_cost = margin if resident else profile['cold_host_bytes'] + margin
    gpu_gate = adapter.memory_allocation(gpu_cost, gpu['free_bytes'], profile['gpu_reserve_bytes'])
    ram_gate = adapter.memory_allocation(host_cost, ram['free_bytes'], profile['host_reserve_bytes'])
    frame = adapter.frame(PROFILE, max(gpu_gate['omega'], ram_gate['omega']), 0.)
    admitted = frame['decision'] == 'ALLOW'
    reason = (('Remote resident model, preallocated context and fresh GPU/RAM reserves permit inference.'
               if resident else 'Fresh GPU/RAM capacity permits the complete configured model restoration envelope.')
              if admitted else 'Remote GPU or host RAM headroom requires resource recovery before inference.')
    hardware = dict(frame, available=True, admitted=admitted, provider='remote_llama',
                    reason=reason, blocker='model_admission' if not admitted else None,
                    sample=sample, thermal_label='unavailable',
                    gpu_allocation=gpu_gate, host_allocation=ram_gate,
                    peak_memory_verified=False)
    return dict(frame, admitted=admitted, available=True, provider='remote_llama',
                model=profile['name'], context_tokens=requested_context,
                resident=resident, resident_context_tokens=profile['context_tokens'],
                estimated_increment_bytes=gpu_cost, estimated_host_increment_bytes=host_cost,
                available_bytes=gpu['free_bytes'],
                system_reserve_bytes=profile['gpu_reserve_bytes'],
                resident_allocation_included_in_observed_free_memory=resident,
                residency_basis=('Exact model process is reported on the configured NVIDIA device; physical per-process byte attribution is unavailable.'
                    if resident else 'Model process physical GPU residency is unverified; no resident allocation credit is taken.'),
                cold_estimate_bytes=None if resident else profile['cold_gpu_bytes'],
                cold_host_estimate_bytes=None if resident else profile['cold_host_bytes'],
                restoration_envelope_context_tokens=profile.get('cold_context_tokens'),
                restoration_required=not resident, peak_memory_verified=False,
                estimate_scope=('Already loaded weights and fixed KV pool; reserve working margin only. Peak workspace is unverified.'
                    if resident else 'No physical resident credit; reserve full trusted GPU and host restoration envelopes plus working margin. Peak is unverified.'),
                source='Fresh SSH NVIDIA/process/GGUF measurements and bound loopback API',
                instance=instance, hardware=hardware, reason=reason,
                gpu_allocation=gpu_gate, host_allocation=ram_gate)


def _save(root, plan):
    """A bounded display record; admission never reads or trusts this cache."""
    record = {'version': 1, 'provider': 'remote_llama', 'saved_at': time.time(), 'plan': plan}
    sample = (plan.get('hardware') or plan).get('sample')
    if isinstance(sample, dict) and sample.get('profile') == PROFILE:
        record['last_observation'] = sample
    else:
        previous = snapshot(root).get('sample')
        if isinstance(previous, dict) and previous.get('profile') == PROFILE:
            record['last_observation'] = previous
    text = canonical(record)
    if len(text.encode()) > 65536:
        return
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.remote-memory-', dir=root)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(text)
        os.replace(temporary, root / 'remote-memory.json')
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def snapshot(root, *, now=None):
    """Display-only last observation, with no SSH/API or admission authority."""
    import json
    now = time.time() if now is None else now
    missing = {'version': 1, 'profile': PROFILE, 'provider': 'remote_llama',
               'available': False, 'admitted': False, 'decision': None, 'stale': True,
               'reason': 'No fresh remote GPU observation is available.'}
    try:
        descriptor = os.open(Path(root) / 'remote-memory.json', os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 65536 or
                    hasattr(os, 'getuid') and info.st_uid != os.getuid() or
                    os.name != 'nt' and info.st_mode & 0o077):
                return missing
            record = json.loads(os.read(descriptor, 65537))
        finally:
            os.close(descriptor)
        plan = record['plan']
        if type(record.get('version')) is not int or record['version'] != 1 or not isinstance(plan, dict):
            return missing
        hardware = plan.get('hardware', plan)
        if not isinstance(hardware, dict):
            return missing
        sample = hardware.get('sample') or record.get('last_observation') or {}
        if not isinstance(sample, dict):
            return missing
        stamp = sample.get('wall_time', hardware.get('observed_at', record.get('saved_at')))
        if type(stamp) not in (int, float) or not math.isfinite(stamp):
            return missing
        age = now - stamp
        stale = not 0 <= age <= MAX_SAMPLE_AGE
        gpu, ram = sample.get('gpu') or {}, sample.get('ram') or {}
        if not isinstance(gpu, dict) or not isinstance(ram, dict):
            return missing
        display = dict(sample, gpu_uuid=gpu.get('uuid'), vram_total_bytes=gpu.get('total_bytes'),
                       vram_free_bytes=gpu.get('free_bytes'), host_available_bytes=ram.get('free_bytes'))
        summary = {key: plan.get(key) for key in ('model', 'decision', 'admitted', 'reason', 'context_tokens',
                   'estimated_increment_bytes', 'system_reserve_bytes', 'resident')}
        summary['observed_at'] = hardware.get('observed_at', (hardware.get('sample') or {}).get('wall_time', record.get('saved_at')))
        return dict(missing, model=plan.get('model'), available=not stale and bool(hardware.get('available')),
                    admitted=not stale and bool(hardware.get('admitted')),
                    decision=None if stale else hardware.get('decision'), stale=stale,
                    reason=('Remote GPU readings are stale; the next dispatch requests a fresh observation.'
                            if stale else hardware.get('reason', missing['reason'])),
                    observed_at=stamp, age_seconds=round(max(0, age), 2), sample=display,
                    plan=plan, last_admission=summary)
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return missing


def _observe(profile, context):
    try:
        sample = probe(profile)
    except (OSError, ValueError, InvalidUpdate, KeyError, TypeError, AttributeError, OverflowError) as error:
        # InvalidUpdate messages in this path are host-authored and sanitized by
        # remote_control. Preserve the observed cause, never a remote body/key.
        cause = str(error)[:300] if isinstance(error, InvalidUpdate) else type(error).__name__
        state = _blocked('Fresh remote GPU observation unavailable: ' + cause)
        details = getattr(error, 'remote_observation', None)
        state['observation_failure'] = (copy.deepcopy(details) if isinstance(details, dict) else
            {'stage': 'observation_validation', 'error_type': type(error).__name__,
             'failure_kind': 'rejected'})
        return state
    return evaluate(profile, sample, context)


def admission(provider, root, requested_context=None):
    """Read-only readiness for callers that have not committed a model dispatch."""
    from remote_profiles import require_assignment
    profile = provider.profile
    require_assignment(profile)
    if hasattr(provider, 'fresh_context_metadata'):
        provider.fresh_context_metadata()
    context = profile['context_tokens'] if requested_context is None else requested_context
    plan = _observe(profile, context)
    plan.setdefault('model', profile['name'])
    _save(root, plan)
    return plan


def resource_state(root, model_name, *, refresh=True):
    """Providerless fresh admission for routing/recovery; never starts inference."""
    from remote_profiles import load, require_assignment
    profile = load(model_name)
    require_assignment(profile)
    if not refresh:
        return _blocked('A fresh remote GPU observation is required before admission.')
    plan = _observe(profile, profile['context_tokens'])
    plan.setdefault('model', profile['name'])
    _save(root, plan)
    return plan.get('hardware', plan)


def recover(root, model_name, audit=None):
    state = resource_state(root, model_name)
    result = {'profile': PROFILE, 'action': 'observe_remote_provider', 'after': state,
              'admitted': state['admitted'], 'max_release_actions': 0,
              'automatic_inference': False, 'release_kernel': None}
    if audit:
        audit('MEMORY_RECOVERY_OBSERVED', result)
    return result


def require_dispatch_fresh(provider):
    """Check the admitted measurement again immediately before HTTP POST.

    No implicit retry, fresh hash scan, task allocation or reservation refund is
    performed after the invocation has already been committed by the gateway.
    """
    plan = getattr(provider, 'memory_plan', None) or {}
    hardware = plan.get('hardware') or {}
    sample = hardware.get('sample')
    fresh = evaluate(provider.profile, sample, getattr(provider, 'runtime_context_tokens', 0))
    instance = getattr(provider, 'remote_instance', None)
    if fresh.get('admitted') and instance != fresh.get('instance'):
        fresh = _blocked('Remote instance binding changed before HTTP dispatch.', kind='runtime_uncertain')
    if not fresh.get('admitted'):
        state = dict(fresh.get('hardware', fresh))
        state['runtime'] = {'kind': 'model_capacity', 'stage': 'remote_pre_transport',
                            'inference_started': False}
        raise ResourceWait(state)
    return fresh


@contextmanager
def dispatch(provider, root, call_id, system, payload, schema):
    from remote_profiles import require_assignment
    from runtime_memory import dispatch_context, enabled, lease
    if not enabled():
        raise InvalidUpdate('Remote inference requires the shared Sutra runtime directory')
    with lease():
        require_assignment(provider.profile)
        if hasattr(provider, 'fresh_context_metadata'):
            provider.fresh_context_metadata()
        provider.runtime_context_tokens = dispatch_context(provider, system, payload, schema)
        plan = _observe(provider.profile, provider.runtime_context_tokens)
        plan.setdefault('model', provider.model)
        _save(root, plan)
        if not plan.get('admitted'):
            state = dict(plan.get('hardware', plan))
            state['runtime'] = {'kind': 'model_capacity', 'stage': 'remote_dispatch',
                                'inference_started': False, 'plan': plan}
            raise ResourceWait(state)
        previous = getattr(provider, 'remote_instance', None)
        if previous is not None and previous != plan['instance']:
            state = _blocked('Remote model instance changed after context selection; refresh and resume.',
                             kind='runtime_uncertain')
            state['runtime'] = {'kind': 'identity_changed', 'inference_started': False}
            raise ResourceWait(state)
        provider.remote_instance = plan['instance']
        provider.memory_plan = plan
        yield plan
