"""Version-scoped decoder compatibility; the original host schema stays strict."""
from copy import deepcopy

TESTED_MLX_VERSIONS = frozenset({'0.40.16'})


def profile(metadata, runtime_version):
    runner = (metadata.get('details') or {}).get('runner')
    enabled = runner == 'mlx' and runtime_version in TESTED_MLX_VERSIONS
    return {'policy': 'mlx-string-bounds-v1' if enabled else 'native-schema-v1',
            'runner': runner, 'runtime_version': runtime_version}


def for_decoder(host_schema, compatibility):
    schema = deepcopy(host_schema)
    if (not compatibility or compatibility.get('policy') != 'mlx-string-bounds-v1'
            or compatibility.get('runner') != 'mlx'
            or compatibility.get('runtime_version') not in TESTED_MLX_VERSIONS):
        return schema

    def visit(node):
        if isinstance(node, dict):
            if node.get('type') == 'string':
                node.pop('minLength', None)
                node.pop('maxLength', None)
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(schema)
    return schema
