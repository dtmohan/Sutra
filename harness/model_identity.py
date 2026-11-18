"""Host-observed Ollama identities. Manifest identity is not weight identity.

Weight fingerprints describe content-addressed model/adapter layer references;
they do not independently re-hash multi-gigabyte weights or attest the server.
Unknown metadata never proves separation. No inference or downloads occur here.
"""
import hashlib
import json
import os
from pathlib import Path
import re
from context_manager import InvalidUpdate, canonical, sha

MAX_MANIFEST_BYTES = 4 * 1024 * 1024


def family(architecture):
    value = str(architecture or '').lower()
    # Architecture strings are not a reliable training genealogy. Recognise
    # these declared families conservatively; unfamiliar ones stay unknown.
    for prefix in ('qwen', 'llama', 'gemma', 'mistral', 'deepseek', 'phi'):
        if value.startswith(prefix): return prefix
    return None


def weights(digest, root=None):
    if not isinstance(digest, str) or not re.fullmatch(r'(?:sha256:)?[a-f0-9]{64}', digest): return None
    digest = digest.removeprefix('sha256:')
    root = Path(root or os.environ.get('OLLAMA_MODELS') or Path.home() / '.ollama' / 'models')
    path = root / 'blobs' / ('sha256-' + digest)
    try:
        with path.open('rb') as handle: raw = handle.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES or hashlib.sha256(raw).hexdigest() != digest: return None
        manifest = json.loads(raw)
        layers = manifest.get('layers')
        if not isinstance(layers, list): return None
        selected = []
        for layer in layers:
            kind = layer.get('mediaType', '')
            if kind in ('application/vnd.ollama.image.model', 'application/vnd.ollama.image.tensor',
                        'application/vnd.ollama.image.adapter'):
                if not re.fullmatch('sha256:[a-f0-9]{64}', layer.get('digest', '')): return None
                # Registry provenance (e.g. `from`) and templates are not weights.
                # Tensor names bind the blobs to their roles in the network.
                entry={'mediaType':kind,'digest':layer['digest']}
                if kind.endswith('.tensor'):
                    if not isinstance(layer.get('name'),str) or not layer['name']:return None
                    entry['name']=layer['name']
                selected.append(entry)
        if not selected or not any(x['mediaType'].endswith(('.model', '.tensor')) for x in selected): return None
        if all(x['mediaType'].endswith('.tensor') for x in selected):
            if len({x['name'] for x in selected})!=len(selected):return None
            selected.sort(key=lambda x:x['name'])
        return sha(canonical(selected))
    except (OSError, ValueError, TypeError, AttributeError): return None


def from_metadata(name, tag, metadata, root=None):
    details = metadata.get('details') or {}
    architecture = (metadata.get('model_info') or {}).get('general.architecture') or details.get('family') or (tag.get('details') or {}).get('family')
    digest = tag.get('digest')
    return {'name': name, 'manifest_digest': digest, 'weights_sha256': weights(digest, root),
            'architecture': architecture or None, 'family': family(architecture),
            'backend':details.get('runner') or (tag.get('details') or {}).get('runner'),
            'basis': 'content_addressed_layer_descriptors', 'server_attested': False}


def observe(name, ollama=None):
    from strata_model import MODEL, observe as observe_strata
    if name == MODEL:
        return observe_strata()
    if ollama is None:
        from vscode_bridge import ollama
    tags = ollama('/api/tags').get('models', [])
    tag = next((m for m in tags if m.get('name') == name), None)
    if not tag or 'cloud' in name.lower(): raise InvalidUpdate('Choose an installed local model')
    metadata = ollama('/api/show', {'model': name})
    if metadata.get('remote_model') or metadata.get('remote_host'): raise InvalidUpdate('Remote review models are not allowed')
    return from_metadata(name, tag, metadata)


def unknown(name):
    return {'name': name, 'manifest_digest': None, 'weights_sha256': None,
            'architecture': None, 'family': None, 'basis': 'unavailable', 'server_attested': False}


def compare(mapper, reviewer):
    a, b = mapper.get('weights_sha256'), reviewer.get('weights_sha256')
    status = ('same' if a == b else 'different') if a and b else 'unknown'
    # Equal known manifests also establish equality, but unequal manifests do
    # not establish different weights (templates and parameters change them).
    if status == 'unknown' and mapper.get('manifest_digest') and mapper.get('manifest_digest') == reviewer.get('manifest_digest'):
        status = 'same'
    fa, fb = mapper.get('family'), reviewer.get('family')
    return {'mapper_model': mapper['name'], 'reviewer_model': reviewer['name'],
            'weight_relation': status, 'same_model': {'same': True, 'different': False}.get(status),
            'same_family': fa == fb if fa and fb else None,
            'mapper_identity': mapper, 'reviewer_identity': reviewer,
            'statistical_independence_verified': False}


def enforce(separation, strict):
    if type(strict) is not bool: raise InvalidUpdate('require_distinct_reviewer must be true or false')
    if strict and separation['weight_relation'] != 'different':
        raise InvalidUpdate('Distinct reviewer weights are not established (' + separation['weight_relation'] + ')')


def choose(mapper, requested='auto', models=None):
    if not isinstance(requested, str) or not requested: raise InvalidUpdate('reviewer_model must be a model name or auto')
    if models is None:
        from vscode_bridge import ollama
        from model_catalog import catalog
        models = [m for m in catalog(ollama)['models'] if m.get('eligible')]
        for row in models: row['identity'] = observe(row['name'], ollama)
    by = {m['name']: m for m in models if m.get('eligible', True)}
    if requested != 'auto' and requested not in by: raise InvalidUpdate('Reviewer must be an eligible installed local chat model')
    def separation(m): return compare(mapper, m.get('identity') or unknown(m['name']))
    if requested != 'auto' and separation(by[requested])['weight_relation'] == 'different':
        selected, reason = by[requested], 'requested'
    else:
        different = [m for m in by.values() if separation(m)['weight_relation'] == 'different']
        unknowns = [m for m in by.values() if m['name'] != mapper['name'] and separation(m)['weight_relation'] == 'unknown']
        pool = different or unknowns or list(by.values())
        if not pool: raise InvalidUpdate('No eligible local reviewer is installed')
        # Prefer known separation before lineage and then cost. Lineage remains
        # a heuristic; a small different-family model is not a verified judge.
        selected = min(pool, key=lambda m: (separation(m)['same_family'] is not False,
                         m.get('parameter_count') or m.get('size') or float('inf'), m['name']))
        reason = 'automatic' if requested == 'auto' else 'replaced_unseparated_choice'
    result = separation(selected)
    result.update(selection=reason, requested_model=requested)
    return selected['name'], result


def label(separation):
    relation = separation['weight_relation']
    status = {'same': 'Same model weights', 'different': 'Different model weights', 'unknown': 'Weight separation unknown'}[relation]
    family_note = {True:'same declared family',False:'different declared families',None:'family unknown'}[separation['same_family']]
    return status + ': ' + separation['mapper_model'] + ' → ' + separation['reviewer_model'] + ' (' + family_note + '). Reviewer agreement is an unverified model opinion.'
