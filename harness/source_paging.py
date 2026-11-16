"""Plan fresh source work inside remaining execution and task allocations.

This only selects optional source pages. Contracts, pinned memory, permissions
and spent work are never reduced or refunded. Every actual read is re-admitted.
"""
from math import ceil
from pathlib import Path
from context_manager import InvalidUpdate
import orchestration as m


def remaining(s, sid):
    owner = m.ACTIVE.get() or m.task(s, sid)['id']
    frames = m.gates(s, owner, {})
    m.allowed(frames)
    return {k: max(0, min(f['limits'][k] - f['used'][k] for f in frames))
            for k in frames[0]['limits']}


def select(s, sid, workspace, files, purpose, reserve_fraction=0):
    """A stat-only plan; scoped readers still reject symlinks and changed files."""
    left = remaining(s, sid)
    page_cap = s.snapshot(sid)['constraints']['review_page_kib'] * 1024
    # Leave positive headroom: the canonical session FIN boundary applies to
    # every later operation, including synthesis with no additional source cost.
    cap = int(max(0, min(page_cap, left['source_bytes'] - 1,
                         left['source_bytes'] * (1 - reserve_fraction))))
    configs = [p for p in files if Path(p).name == 'tsconfig.json']
    sizes = {p: (Path(workspace) / p).stat().st_size for p in files}
    size = sum(sizes[p] for p in configs)
    chosen = list(configs)
    for name in files:
        if name not in configs and size + sizes[name] <= cap:
            chosen.append(name); size += sizes[name]
    if len(chosen) == len(configs):
        # Report the smallest useful next page to the normal kernel gate.
        code = [p for p in files if p not in configs]
        needed = size + min((sizes[p] for p in code), default=1)
        # The page has to fit both the per-page cap and the fraction of the
        # remaining allowance available to this stage.
        if needed > page_cap:
            raise InvalidUpdate('No complete source file fits the per-page allocation')
        measured = max(needed + 1, ceil(needed / (1 - reserve_fraction)))
        cost = {'operations': 1, 'source_bytes': measured, 'seconds': 20}
        owner = m.ACTIVE.get() or m.task(s, sid)['id']
        m.allowed(m.gates(s, owner, cost))
        raise InvalidUpdate('No complete source file fits the source-page allocation')
    record = {'session': sid, 'owner': m.ACTIVE.get(), 'purpose': purpose,
              'selected': chosen, 'omitted': [p for p in files if p not in chosen],
              'selected_bytes': size, 'requested_bytes': sum(sizes.values()),
              'page_cap': cap, 'remaining': left,
              'fresh_reads_required': True, 'spending_refunded': False}
    with s.db:
        s._audit('SOURCE_PAGE_PLAN', record)
    return chosen, record
