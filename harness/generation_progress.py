"""Bounded literal repetition observations during one generation.

Sentence/paragraph boundaries and fixed-size unpunctuated pieces are fingerprinted
after whitespace normalization. This is not a semantic loop, correctness or
completion detector. An indicator never cancels a call or changes admission.
Only the bounded pending piece retains text; history contains digests and flags.
"""
from collections import deque
import hashlib
import math


PROFILE = 'literal-generation-progress-v1'
LIMITS = {'pending_chars': 4096, 'digest_units': 128, 'recent_units': 32,
          'min_unit_chars': 40, 'min_long_units': 32, 'min_observed_chars': 2048,
          'feed_chars': 16384, 'duplicate_ratio': .8}
_ENDS = frozenset('.!?。！？\n\r')
_COUNTS = frozenset(('received_chars', 'observed_chars', 'omitted_chars', 'invalid_chars',
                     'invalid_feeds', 'long_units', 'duplicate_units', 'novel_units',
                     'recent_units', 'pending_chars'))
_FLAGS = frozenset(('repetition_indicator', 'current_repetition', 'alert', 'alert_emitted',
                    'analysis_complete', 'finished'))
_RATIOS = frozenset(('recent_duplicate_fraction', 'recent_novelty_fraction'))
_KEYS = _COUNTS | _FLAGS | _RATIOS | {'profile', 'limits', 'semantic_assessment'}


def _current(units, observed, recent, fraction):
    return (units >= LIMITS['min_long_units'] and observed >= LIMITS['min_observed_chars']
            and recent >= LIMITS['recent_units'] and fraction >= LIMITS['duplicate_ratio'])


def validate_snapshot(value):
    """Validate plain observer metadata, before a host attaches its kernel frame.

    No incoming kernel decision or extra text is accepted as observation data.
    The copied return value cannot mutate the producer's limits or metrics.
    """
    if (not isinstance(value, dict) or set(value) != _KEYS
            or value.get('profile') != PROFILE or value.get('semantic_assessment') != 'unassessed'
            or not isinstance(value.get('limits'), dict) or value['limits'] != LIMITS):
        raise ValueError('Invalid generation progress profile')
    # bool is a subclass of int; neither count nor numeric-limit checks accept it.
    for key in _COUNTS:
        if type(value[key]) is not int or not 0 <= value[key] <= 2**53 - 1:
            raise ValueError('Invalid generation progress count')
    for key in _FLAGS:
        if type(value[key]) is not bool: raise ValueError('Invalid generation progress flag')
    for key in _RATIOS:
        if type(value[key]) not in (int, float) or not 0 <= value[key] <= 1 or not math.isfinite(value[key]):
            raise ValueError('Invalid generation progress fraction')
    if (any(type(value['limits'][key]) is not type(expected) for key, expected in LIMITS.items())
            or value['received_chars'] != value['observed_chars'] + value['omitted_chars'] + value['invalid_chars']
            or value['long_units'] != value['duplicate_units'] + value['novel_units']
            or value['long_units'] > 0 and value['novel_units'] < 1
            or value['long_units'] * LIMITS['min_unit_chars'] > value['observed_chars']
            or value['recent_units'] > min(value['long_units'], LIMITS['recent_units'])
            or value['pending_chars'] > min(value['observed_chars'], LIMITS['pending_chars'])
            or value['finished'] and value['pending_chars'] != 0):
        raise ValueError('Inconsistent generation progress counts')
    duplicate, novel, recent = (value['recent_duplicate_fraction'], value['recent_novelty_fraction'], value['recent_units'])
    if ((recent == 0 and (duplicate != 0 or novel != 0))
            or recent > 0 and abs(duplicate + novel - 1) > .000002):
        raise ValueError('Inconsistent generation progress fractions')
    if recent:
        repeated = round(duplicate * recent)
        if (abs(duplicate - repeated / recent) > .000001
                or repeated > value['duplicate_units']
                or recent - repeated > value['novel_units']):
            raise ValueError('Generation progress fractions do not represent counted units')
    current = _current(value['long_units'], value['observed_chars'], recent, duplicate)
    complete = not (value['omitted_chars'] or value['invalid_chars'] or value['invalid_feeds'])
    if (value['current_repetition'] != current or value['analysis_complete'] != complete
            or value['repetition_indicator'] != value['alert_emitted']
            or current and not value['repetition_indicator']
            or value['alert'] and not value['alert_emitted']
            or value['repetition_indicator'] and (
                value['long_units'] < LIMITS['min_long_units']
                or value['observed_chars'] < LIMITS['min_observed_chars']
                or value['duplicate_units'] < math.ceil(LIMITS['recent_units'] * LIMITS['duplicate_ratio']))):
        raise ValueError('Inconsistent generation progress indicator')
    result = dict(value); result['limits'] = dict(value['limits'])
    return result


class Progress:
    """One observer per actual generation; feed boundaries have no unit meaning."""
    __slots__ = ('_pending', '_digests', '_counts', '_recent', '_received', '_observed',
                 '_omitted', '_invalid', '_invalid_feeds', '_units', '_duplicates',
                 '_indicator', '_finished', '_new_segment')

    def __init__(self):
        self._pending = []
        self._digests = deque(); self._counts = {}; self._recent = deque(maxlen=LIMITS['recent_units'])
        self._received = self._observed = self._omitted = self._invalid = self._invalid_feeds = 0
        self._units = self._duplicates = 0
        self._indicator = self._finished = self._new_segment = False

    def _flush(self):
        if len(self._pending) < LIMITS['min_unit_chars']:
            self._pending.clear(); return False
        unit = ' '.join(''.join(self._pending).split())
        self._pending.clear()
        if len(unit) < LIMITS['min_unit_chars']: return False
        digest = hashlib.sha256(unit.encode('utf-8')).digest()
        repeated = bool(self._counts.get(digest, 0))
        if len(self._digests) == LIMITS['digest_units']:
            previous = self._digests.popleft(); self._counts[previous] -= 1
            if not self._counts[previous]: del self._counts[previous]
        self._digests.append(digest); self._counts[digest] = self._counts.get(digest, 0) + 1
        self._units += 1; self._duplicates += int(repeated); self._recent.append(not repeated)
        return self._consider_indicator()

    def _consider_indicator(self):
        if not self._recent: return False
        fraction = (len(self._recent) - sum(self._recent)) / len(self._recent)
        if not self._indicator and _current(self._units, self._observed, len(self._recent), fraction):
            self._indicator = True; return True
        return False

    def feed(self, text):
        if not isinstance(text, str):
            self._invalid_feeds += 1; self._pending.clear(); self._recent.clear()
            return self.snapshot()
        self._received += len(text)
        if self._finished:
            self._omitted += len(text)
            return self.snapshot()
        if text and self._new_segment:
            self._recent.clear(); self._new_segment = False
        alert = False
        # The slice itself is bounded, even if the transport supplies a huge chunk.
        for char in text[:LIMITS['feed_chars']]:
            if 0xD800 <= ord(char) <= 0xDFFF:
                self._invalid += 1; self._pending.clear(); self._recent.clear()
                continue
            self._observed += 1; self._pending.append(char)
            if char in _ENDS or len(self._pending) == LIMITS['pending_chars']:
                alert = self._flush() or alert
            # The character floor can be crossed inside an unfinished unit,
            # after a repetitive full unit-window already exists. Latch at that
            # crossing as well as at unit completion, keeping every emitted
            # snapshot internally consistent without rescanning every character.
            if self._observed == LIMITS['min_observed_chars']:
                alert = self._consider_indicator() or alert
        omitted = max(0, len(text) - LIMITS['feed_chars'])
        if omitted:
            self._omitted += omitted; self._pending.clear(); self._new_segment = True
        result = self.snapshot(); result['alert'] = alert
        return result

    def snapshot(self):
        recent = len(self._recent)
        novel = sum(self._recent)
        duplicate = (recent - novel) / recent if recent else 0.
        return {'profile': PROFILE, 'received_chars': self._received, 'observed_chars': self._observed,
                'omitted_chars': self._omitted, 'invalid_chars': self._invalid,
                'invalid_feeds': self._invalid_feeds, 'long_units': self._units,
                'duplicate_units': self._duplicates, 'novel_units': self._units - self._duplicates,
                'recent_units': recent, 'recent_duplicate_fraction': round(duplicate, 6),
                'recent_novelty_fraction': round(novel / recent, 6) if recent else 0.,
                'repetition_indicator': self._indicator,
                'current_repetition': _current(self._units, self._observed, recent, duplicate),
                'alert': False, 'alert_emitted': self._indicator,
                'analysis_complete': not (self._omitted or self._invalid or self._invalid_feeds),
                'pending_chars': len(self._pending), 'finished': self._finished,
                'semantic_assessment': 'unassessed', 'limits': dict(LIMITS)}

    def finish(self):
        alert = False
        if not self._finished:
            alert = self._flush(); self._finished = True
        result = self.snapshot(); result['alert'] = alert
        return result
