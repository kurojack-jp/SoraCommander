"""DEV-only drift guard: active Owner command body vs candidate formatter.

Static inspection only: never opens a GUI or issues approval.
"""
import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "src" / "sora_commander_next"


def _body_node(path, *, assignment=None, function=None):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    if function is not None:
        match = [node for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and node.name == function]
        if len(match) != 1:
            raise AssertionError("candidate formatter missing or ambiguous")
        returns = [node.value for node in ast.walk(match[0])
                   if isinstance(node, ast.Return)]
        if len(returns) != 1:
            raise AssertionError("candidate formatter return ambiguous")
        return returns[0]
    match = [node.value for node in ast.walk(tree)
             if isinstance(node, ast.Assign)
             and any(isinstance(target, ast.Name) and target.id == assignment
                     for target in node.targets)]
    if len(match) != 1:
        raise AssertionError("active Owner command body missing or ambiguous")
    return match[0]


class ActiveCommandBodyDriftTests(unittest.TestCase):
    def test_candidate_matches_exact_active_command_body_expression(self):
        active = _body_node(PKG / "owner_approval_dialog.py", assignment="data")
        candidate = _body_node(
            PKG / "owner_review_request_binding_candidate.py",
            function="_active_command_body",
        )
        # Compare Python expression trees, including every literal, field,
        # format flag, and ordering. Ignore only source positions.
        self.assertEqual(
            ast.dump(active, include_attributes=False),
            ast.dump(candidate, include_attributes=False),
            "Owner command text drift: audit both formatters before UI activation",
        )

    def test_active_body_remains_a_pure_format_expression(self):
        active = _body_node(PKG / "owner_approval_dialog.py", assignment="data")
        self.assertIsInstance(active, ast.JoinedStr)
        self.assertFalse(any(isinstance(node, ast.Call) for node in ast.walk(active)))


if __name__ == "__main__":
    unittest.main()
