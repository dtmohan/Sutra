"""Context allocation from installed Ollama metadata, never model self-report."""
import re
from context_manager import InvalidUpdate
from paging import Budget


def allocation(metadata):
    info = metadata.get('model_info') or {}
    architecture = info.get('general.architecture', '')
    maximum = info.get(architecture + '.context_length')
    if type(maximum) is not int or maximum <= 0:
        values = [v for k, v in info.items() if k.endswith('.context_length') and type(v) is int and v > 0]
        maximum = min(values) if values else None
    configured = re.search(r'^\s*num_ctx\s+(\d+)\s*$', metadata.get('parameters', ''), re.M)
    configured = int(configured[1]) if configured else None
    # Model capability is not a promise of available KV-cache memory. This is
    # the tested local runtime allocation; hardware admission still applies.
    runtime_cap=metadata.get('_sutra_runtime_cap')
    if runtime_cap is not None and (type(runtime_cap) is not int or runtime_cap<4096):
        raise InvalidUpdate('Invalid runtime memory context allocation')
    limit = min(v for v in (maximum, configured, runtime_cap, 65536) if v is not None)
    if maximum is None:
        limit = min(limit, 8192)  # Explicit conservative fallback, not an invented capability.
    if limit < 4096:
        raise InvalidUpdate('Installed model context is too small for the conversation protocol')
    budget = Budget(limit, min(4096, limit // 8), 256)
    return budget, {'model_context_tokens': maximum, 'configured_context_tokens': configured,
                    'runtime_context_cap': 65536, 'memory_context_cap':runtime_cap, 'effective_context_tokens': limit,
                    'output_reserve_tokens': budget.response_reserve,
                    'source': ('sutra_engine_metadata' if metadata.get('_sutra_provider') == 'sutra_engine'
                               else 'strata_metadata' if metadata.get('_sutra_provider') == 'strata'
                               else 'ollama_metadata' if maximum is not None else 'metadata_missing_conservative_fallback')}
