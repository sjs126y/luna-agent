"""Safe math expression evaluator."""

import ast
import math
import operator

from personal_agent.tools.entry import ToolEntry
from personal_agent.tools.registry import tool_registry

_SAFE_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub,
    ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.Pow: operator.pow, ast.Mod: operator.mod,
    ast.FloorDiv: operator.floordiv,
    ast.USub: operator.neg, ast.UAdd: operator.pos,
}

_SAFE_NAMES = {
    "pi": math.pi, "e": math.e, "tau": math.tau,
    "sqrt": math.sqrt, "sin": math.sin, "cos": math.cos,
    "tan": math.tan, "log": math.log, "log10": math.log10,
    "log2": math.log2, "ceil": math.ceil, "floor": math.floor,
    "abs": abs, "round": round, "int": int, "float": float,
}


def _safe_eval(expr: str) -> float:
    def _eval_node(node):
        if isinstance(node, ast.Expression):
            return _eval_node(node.body)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.BinOp):
            return _SAFE_OPS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
        if isinstance(node, ast.UnaryOp):
            return _SAFE_OPS[type(node.op)](_eval_node(node.operand))
        if isinstance(node, ast.Name):
            if node.id in _SAFE_NAMES:
                return _SAFE_NAMES[node.id]
            raise ValueError(f"Unknown name: {node.id}")
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in _SAFE_NAMES:
                args = [_eval_node(a) for a in node.args]
                return _SAFE_NAMES[node.func.id](*args)
        raise ValueError(f"Unsupported expression: {ast.dump(node)}")
    return _eval_node(ast.parse(expr, mode="eval"))


async def _calculator(expression: str) -> str:
    try:
        result = _safe_eval(expression)
        if isinstance(result, float) and result == int(result):
            result = int(result)
        return str(result)
    except Exception as e:
        return f"Error: {e}"


tool_registry.register(ToolEntry(
    name="calculator",
    description="Safely evaluate a mathematical expression. Supports +, -, *, /, **, %, //, (), and math functions (sqrt, sin, cos, log, etc.).",
    schema={
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "The mathematical expression to evaluate, e.g. '2 + 3 * 4' or 'sqrt(144)'",
            }
        },
        "required": ["expression"],
    },
    handler=_calculator,
    toolset="builtin",
))
