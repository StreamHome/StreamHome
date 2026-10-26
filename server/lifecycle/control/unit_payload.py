"""Read the publisher's fixed unit definitions as data, never as Python code."""
from __future__ import annotations

import ast

from .root_files import _UNIT_PATHS
from .rpc import ControlError


def parse(raw: bytes) -> dict[str, bytes]:
    """Accept only the existing literal/replace/encode unit declaration grammar.

    No imports, attribute lookup, calls or module body are executed. The final
    mapping must contain exactly the protected publisher-owned destinations.
    """
    if not isinstance(raw, bytes) or not 0 < len(raw) <= 32768:
        raise ControlError('control_units_invalid')
    try:
        tree = ast.parse(raw)
        if sum(1 for _ in ast.walk(tree)) > 4096:
            raise ValueError('unit declarations exceed bound')
        values = {}

        def value(node):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return node.value
            if isinstance(node, ast.Name) and node.id in values:
                return values[node.id]
            if isinstance(node, ast.Dict) and len(node.keys) <= len(_UNIT_PATHS):
                pairs = [(value(key), value(item)) for key, item in zip(node.keys, node.values)]
                if len({key for key, _ in pairs}) != len(pairs):
                    raise ValueError('duplicate unit destination')
                return dict(pairs)
            if isinstance(node, ast.Call) and not node.keywords and isinstance(node.func, ast.Attribute):
                source = value(node.func.value)
                if isinstance(source, str):
                    if node.func.attr == 'encode' and not node.args:
                        return source.encode('utf-8')
                    if node.func.attr == 'replace' and len(node.args) == 2:
                        before, after = (value(arg) for arg in node.args)
                        if isinstance(before, str) and before and isinstance(after, str):
                            if len(source) + source.count(before) * (len(after) - len(before)) > 32768:
                                raise ValueError('expanded unit exceeds bound')
                            changed = source.replace(before, after)
                            if len(changed) <= 32768:
                                return changed
            raise ValueError('unsupported unit expression')

        result = None
        for node in tree.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                continue
            if isinstance(node, ast.ImportFrom) and node.module == '__future__':
                continue
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
                if name in values:
                    raise ValueError('duplicate declaration')
                values[name] = value(node.value)
                continue
            if (isinstance(node, ast.FunctionDef) and node.name == 'unit_files'
                    and not node.decorator_list and not node.args.args and not node.args.posonlyargs
                    and not node.args.kwonlyargs and node.args.vararg is None and node.args.kwarg is None
                    and len(node.body) == 1 and isinstance(node.body[0], ast.Return) and result is None):
                result = value(node.body[0].value)
                continue
            raise ValueError('unsupported unit declaration')
        if (not isinstance(result, dict) or set(result) != _UNIT_PATHS
                or any(not isinstance(item, bytes) or not 0 < len(item) <= 32768
                    or b'\x00' in item for item in result.values())):
            raise ValueError('invalid unit inventory')
        return result
    except (SyntaxError, UnicodeError, ValueError, TypeError, RecursionError) as exc:
        raise ControlError('control_units_invalid') from exc


def compatible(previous: dict[str, bytes], candidate: dict[str, bytes]) -> None:
    """Protocol one permits description/comment changes, preserving every directive.

    An execution, ownership, dependency or sandbox change needs a separately
    qualified unit protocol. Line order and duplicate directives remain material.
    """
    def contract(raw):
        lines = raw.decode('utf-8').splitlines()
        if any(line.rstrip().endswith('\\') for line in lines):
            raise ControlError('control_units_incompatible')
        return tuple(line.strip() for line in lines
            if line.strip() and not line.lstrip().startswith(('#', ';', 'Description=')))
    if (set(previous) != _UNIT_PATHS or set(candidate) != _UNIT_PATHS
            or any((contract(previous[name]) != contract(candidate[name])) if name.endswith('.service')
                else previous[name] != candidate[name] for name in _UNIT_PATHS)):
        raise ControlError('control_units_incompatible')
