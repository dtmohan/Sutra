"""Observe-only token-probability sensor. Never a source of protocol authority.

Instrument and wait: when enabled, each local call asks Ollama for per-token
log probabilities, reduces them to a small numeric summary, and appends that
summary to a bounded local log. Nothing here admits, withholds, repairs or
re-routes a call, and no kernel input is derived from it. The thresholds are
BOOTSTRAP constants: provisional until a per-model baseline has accumulated.

Enable with SUTRA_LOGIT_SENSOR=1 (numbers only) or SUTRA_LOGIT_SENSOR=tokens
(also keeps the text of the few flagged tokens). Unset means the request body
and every result are byte-identical to an uninstrumented call.

Backend support is capability-dependent. Missing log probabilities are reported
as unavailable; no probabilities or correctness scores are invented. Enabling
the request fields may add backend overhead. Transport errors are not retried
outside the normal governed repair budget.

Each logged row also carries the collector's hardware sample from before and
after the call, so latency and failures can be separated from model
uncertainty. That copy is for joining only; the resource gate is unchanged.
"""
import json
import math
import os
from pathlib import Path
import time

ENV = 'SUTRA_LOGIT_SENSOR'
VERSION = 2
TOP = 3                      # alternatives requested per position (Ollama allows 0..20)
LOW_MARGIN = math.log(2)     # BOOTSTRAP: chosen token under 2x as likely as its runner-up
MAX_TOKENS = 16384           # per-call memory bound
MAX_FLAGS = 8
MAX_CAPTURE_BYTES = 8 * 1024 ** 2
MAX_LOG_BYTES = 1024 ** 2
LOG_NAME = 'logit_sensor.jsonl'


def mode():
    value = os.environ.get(ENV, '')
    return value if value in ('1', 'tokens') else ''


def json_classes(text):
    """Label each character S (structure), K (object key) or V (value)."""
    out = []
    stack = []
    in_string = escaped = is_key = expect_key = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                in_string = False
                out.append('S')
                continue
            out.append('K' if is_key else 'V')
        elif ch == '"':
            in_string, is_key, expect_key = True, expect_key, False
            out.append('S')
        elif ch in '{[':
            stack.append(ch)
            expect_key = ch == '{'
            out.append('S')
        elif ch in '}]':
            if stack:
                stack.pop()
            expect_key = False
            out.append('S')
        elif ch == ',':
            expect_key = bool(stack) and stack[-1] == '{'
            out.append('S')
        elif ch == ':' or ch.isspace():
            if ch == ':':
                expect_key = False
            out.append('S')
        else:
            out.append('V')  # number, true, false, null
    return out


def lexical(token):
    return any(ch.isalnum() for ch in token)


def token_bytes(entry):
    raw=entry.get('bytes')
    if isinstance(raw,list) and raw and all(type(x) is int and 0<=x<=255 for x in raw):return bytes(raw)
    return entry['token'].encode('utf-8')


def decoded(entries):
    return b''.join(token_bytes(e) for e,_ in entries).decode('utf-8','replace')


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def measure(entry):
    """(logprob, margin, overridden, entropy, runner_up) for one position."""
    logprob = float(entry['logprob'])
    token = entry['token']
    alternatives = [(a['token'], float(a['logprob'])) for a in entry.get('top_logprobs') or []
                    if isinstance(a, dict) and isinstance(a.get('token'), str) and number(a.get('logprob'))]
    others = [a for a in alternatives if a[0] != token]
    if not others:
        return logprob, None, False, None, None
    runner = max(others, key=lambda a: a[1])
    # A negative margin shows selection below a returned alternative. It cannot
    # distinguish grammar constraints, sampling or backend numeric behavior.
    margin = logprob - runner[1]
    mass = [math.exp(logprob)] + [math.exp(a[1]) for a in others]
    total = sum(mass)
    entropy = -sum(p / total * math.log(p / total) for p in mass if p > 0) if total > 0 else None
    return logprob, margin, margin < 0, entropy, runner[0]


def stats(rows):
    """rows: measure() tuples for the tokens that carry content."""
    if not rows:
        return {'n': 0}
    logprobs = [r[0] for r in rows]
    margins = sorted(r[1] for r in rows if r[1] is not None)
    entropies = [r[3] for r in rows if r[3] is not None]
    result = {'n': len(rows), 'mean_logprob': round(sum(logprobs) / len(logprobs), 4),
              'min_logprob': round(min(logprobs), 4), 'with_alternatives': len(margins)}
    if margins:
        low = sum(m < LOW_MARGIN for m in margins)
        result.update(mean_margin=round(sum(margins) / len(margins), 4), min_margin=round(margins[0], 4),
                      p10_margin=round(margins[(len(margins) - 1) // 10], 4), low_margin=low,
                      low_margin_ratio=round(low / len(margins), 4),
                      selected_below_alternative=sum(r[2] for r in rows))
    if entropies:
        result['mean_top_k_conditional_entropy'] = round(sum(entropies) / len(entropies), 4)
    return result


def hardware(root, now=None):
    """Compact copy of the collector's latest sample, for joining; no admission decision."""
    now = time.time() if now is None else now
    try:
        sample = json.loads((Path(root) / 'telemetry.json').read_text())
        if not isinstance(sample, dict):
            return {'available': False}
        out = {key: value for key, value in sorted(sample.items())[:32]
               if value is None or number(value) or (isinstance(value, str) and len(value) <= 40)}
        age = now - sample['wall_time'] if number(sample.get('wall_time')) else None
        # Same freshness window as the resource gate; a stale sample is kept but marked.
        out.update(available=True, age_seconds=None if age is None else round(age, 2),
                   stale=age is None or not 0 <= age <= 8)
        from temperature_signals import summarize
        temperatures = summarize(sample.get('temperatures'), now)
        for group in ('cpu', 'gpu'):
            out[group + '_max_celsius'] = temperatures[group]['maximum_celsius']
        return out
    except Exception:
        return {'available': False}


class Sensor:
    def __init__(self, log_dir=None):
        self.mode = mode()
        self.before = hardware(Path(log_dir).parent) if self.mode and log_dir is not None else None
        self.entries = []      # (entry, chunk kind)
        self.seen = 0
        self.malformed = 0
        self.capture_bytes = 0

    def request(self):
        return {'logprobs': True, 'top_logprobs': TOP} if self.mode else {}

    def observe(self, chunk):
        """Collect one stream frame. Malformed sensor data is counted, never raised."""
        if not self.mode:
            return
        try:
            items = chunk.get('logprobs')
            if not isinstance(items, list):
                return
            message = chunk.get('message') or {}
            kind = 'answer' if message.get('content') else 'thinking' if message.get('thinking') else 'unattributed'
            for entry in items:
                self.seen += 1
                if not (isinstance(entry, dict) and isinstance(entry.get('token'), str) and number(entry.get('logprob'))):
                    self.malformed += 1
                elif len(self.entries) < MAX_TOKENS:
                    size=len(json.dumps(entry).encode())
                    if self.capture_bytes+size<=MAX_CAPTURE_BYTES:
                        self.capture_bytes+=size
                        self.entries.append((entry, kind))
        except Exception:
            self.malformed += 1

    def split(self, answer):
        """Index of the first answer token, located by text; None if not found."""
        if not answer:
            return None
        raw = decoded(self.entries)
        start = raw.rfind(answer)
        if start < 0:
            return None
        encoded=raw[:start].encode('utf-8')
        offset=0
        for index,(entry,_) in enumerate(self.entries):
            size=len(token_bytes(entry))
            if offset+size>len(encoded):return index
            offset+=size
        return None

    def summary(self, answer=''):
        base = {'v': VERSION, 'mode': self.mode, 'available': bool(self.entries), 'tokens': self.seen,
                'malformed': self.malformed, 'truncated': self.seen - self.malformed > len(self.entries),
                'meaning':'Token-choice diagnostics only; not claim correctness or a calibrated confidence score',
                'thresholds': {'low_margin_nats': round(LOW_MARGIN, 4), 'top_logprobs': TOP, 'status': 'bootstrap'}}
        if not self.entries:
            return base
        first = self.split(answer)
        if first is None:
            kinds = [kind for _, kind in self.entries]
            attribution = 'chunk'
        else:
            kinds = ['thinking'] * first + ['answer'] * (len(self.entries) - first)
            attribution = 'text'
        answer_index = [i for i, kind in enumerate(kinds) if kind == 'answer']
        text=decoded([self.entries[i] for i in answer_index])
        structured = text.lstrip()[:1] in ('{', '[')
        classes = json_classes(text) if structured else ['V' if ch.isalnum() else 'S' for ch in text]
        byte_classes=''.join(c*len(ch.encode('utf-8')) for ch,c in zip(text,classes))
        value_index = []
        offset = 0
        for i in answer_index:
            width=len(token_bytes(self.entries[i][0]))
            carries='V' in byte_classes[offset:offset+width]
            offset+=width
            if carries:
                value_index.append(i)
        measured = {i: measure(self.entries[i][0]) for i in range(len(self.entries))}
        thinking = [measured[i] for i, kind in enumerate(kinds)
                    if kind == 'thinking' and lexical(self.entries[i][0]['token'])]
        values = stats([measured[i] for i in value_index])
        flagged = sorted((i for i in value_index if measured[i][1] is not None and measured[i][1] < LOW_MARGIN),
                         key=lambda i: measured[i][1])[:MAX_FLAGS]
        flags = []
        for i in flagged:
            flag = {'index': i, 'margin': round(measured[i][1], 4), 'p': round(math.exp(measured[i][0]), 4)}
            if self.mode == 'tokens':
                flag.update(token=self.entries[i][0]['token'][:48], runner_up=(measured[i][4] or '')[:48])
            flags.append(flag)
        base.update(attribution=attribution, structured=structured,
                    counts={'answer': len(answer_index), 'value': len(value_index),
                            'thinking': kinds.count('thinking'), 'unattributed': kinds.count('unattributed')},
                    answer=values, thinking=stats(thinking), flags=flags,
                    tripped=bool(values.get('low_margin')))
        return base

    def finish(self, log_dir, call_id, model, record, identity=None):
        """Attach the summary to the diagnostic record and append it to the bounded log."""
        if not self.mode:
            return None
        try:
            response = record.get('response') if isinstance(record.get('response'), dict) else {}
            answer = (response.get('message') or {}).get('content') or ''
            result = self.summary(answer)
            request=record.get('request') or {}
            from model_activity import ACTIVE
            active=ACTIVE.get()
            from context_manager import sha,canonical
            result['cohort']={'model_identity':identity,'stage':active.identity.get('role') if active else 'unknown',
                'schema_sha256':sha(canonical(request.get('format'))),'decoding':request.get('options'),
                'thinking_requested':request.get('think'),'schema_constrained':bool(request.get('format'))}
            record['logit_sensor'] = result
            append(Path(log_dir).parent / LOG_NAME,
                   dict(result, time=round(time.time(), 3), call_id=str(call_id), model=model,
                        ok=bool(record.get('ok')), output_tokens=response.get('eval_count'),
                        elapsed_s=record.get('elapsed_s'), eval_duration=response.get('eval_duration'),
                        load_duration=response.get('load_duration'),
                        hardware={'before': self.before, 'after': hardware(Path(log_dir).parent)}))
            return result
        except Exception as exc:  # A failed diagnostic must not alter a governed call.
            record['logit_sensor'] = {'v': VERSION, 'available': False, 'error': type(exc).__name__}
            return None


def append(path, row):
    """Bounded advisory log. A busy writer drops diagnostics rather than blocking."""
    line=(json.dumps(row,sort_keys=True,separators=(',',':'))+'\n').encode()
    if len(line)>MAX_LOG_BYTES:return
    fd=os.open(path,os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        import stat
        st=os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink!=1:return
        if os.name=='posix':
            import fcntl
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return
        else:
            import msvcrt
            try:msvcrt.locking(fd,msvcrt.LK_NBLCK,1)
            except OSError:return
        length=os.lseek(fd,0,os.SEEK_END)
        if length+len(line)>MAX_LOG_BYTES:
            os.lseek(fd,max(0,length-MAX_LOG_BYTES),os.SEEK_SET)
            lines=os.read(fd,MAX_LOG_BYTES).splitlines(keepends=True)[1:]
            kept=b''.join(lines)
            while len(kept)+len(line)>MAX_LOG_BYTES and lines:
                lines.pop(0);kept=b''.join(lines)
            os.lseek(fd,0,os.SEEK_SET);os.ftruncate(fd,0);os.write(fd,kept)
        os.write(fd,line)
    finally:os.close(fd)


def read(root, limit=250):
    """Latest summaries for a workspace storage root; missing or damaged lines are skipped."""
    path = Path(root) / LOG_NAME
    rows = []
    if not path.is_file() or path.is_symlink():
        return rows
    with path.open('rb') as handle:
        length=handle.seek(0,2);handle.seek(max(0,length-MAX_LOG_BYTES))
        lines=handle.read(MAX_LOG_BYTES).decode('utf-8',errors='replace').splitlines()
        if length>MAX_LOG_BYTES:lines=lines[1:]
    for line in lines[-limit:]:
        try:
            value=json.loads(line)
            if isinstance(value,dict):rows.append(value)
        except ValueError:
            continue
    return rows


def baseline(rows):
    """Per-model distribution of the wait-phase measurements. Read-only; no decision."""
    models = {}
    for row in rows:
        answer = row.get('answer') or {}
        if not row.get('available') or 'low_margin_ratio' not in answer:
            continue
        cohort=json.dumps(row.get('cohort') or {},sort_keys=True,separators=(',',':'))
        models.setdefault(str(row.get('model'))+' | '+cohort, []).append(row)
    out = {}
    for model, items in models.items():
        ratios = sorted(r['answer']['low_margin_ratio'] for r in items)
        floors = sorted(r['answer']['min_margin'] for r in items)
        quantile = lambda data, q: data[min(len(data) - 1, int(q * len(data)))]
        thermal = {}
        for r in items:
            state = ((r.get('hardware') or {}).get('before') or {}).get('thermal_state')
            thermal.setdefault('unknown' if state is None else str(state), []).append(r['answer']['low_margin_ratio'])
        out[model] = {'calls': len(items), 'tripped': sum(bool(r.get('tripped')) for r in items),
                      # Null test: margins should not move with thermal state.
                      'low_margin_ratio_by_thermal_state': {k: {'calls': len(v), 'mean': round(sum(v) / len(v), 4)} for k, v in sorted(thermal.items())},
                      'failed_calls': sum(not r.get('ok') for r in items),
                      'low_margin_ratio': {'p50': quantile(ratios, .5), 'p90': quantile(ratios, .9), 'max': ratios[-1]},
                      'min_margin': {'p10': quantile(floors, .1), 'p50': quantile(floors, .5)}}
    return out


if __name__ == '__main__':
    import sys
    if len(sys.argv) != 2:
        raise SystemExit('usage: python3 logit_sensor.py <workspace storage directory>')
    print(json.dumps(baseline(read(sys.argv[1], limit=100000)), indent=2, sort_keys=True))
