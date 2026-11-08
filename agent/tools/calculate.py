"""A safe calculator with single-variable calculus helpers.

Supported operations:
  evaluate  arithmetic + common math functions
  derivative symbolic differentiation of a single-variable expression
  integral   definite integral via composite Simpson
  solve      real roots in an interval via bisection

There is no `eval`, no attribute access, no imports, and no arbitrary code.
"""
from __future__ import annotations

import ast
import math
from typing import Any

from ..errors import ToolError
from ..util import json_dumps
from .base import ToolContext, ToolRegistry, ToolResult, ToolSpec


# ---------------------------------------------------------------------------
# Evaluation


_ALLOWED_NODES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.Call,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.UAdd,
    ast.USub,
)

def _safe_factorial(n):
    n = int(n)
    if n < 0 or n > 170:
        raise ToolError("factorial 参数必须在 0–170 之间")
    return math.factorial(n)


def _safe_comb(n, k):
    n, k = int(n), int(k)
    if n < 0 or k < 0 or k > n or n > 500:
        raise ToolError("comb 参数不合法（n ≤ 500）")
    return math.comb(n, k)


def _safe_perm(n, k):
    n, k = int(n), int(k)
    if n < 0 or k < 0 or k > n or n > 500:
        raise ToolError("perm 参数不合法（n ≤ 500）")
    return math.perm(n, k)


def _safe_pow(a, b):
    if abs(b) > 1000:
        raise ToolError("指数过大")
    result = a**b
    if isinstance(result, complex):
        raise ToolError("结果不是实数")
    return result


_FUNCTIONS: dict[str, Any] = {
    "sqrt": math.sqrt,
    "abs": abs,
    "round": round,
    "floor": math.floor,
    "ceil": math.ceil,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "atan2": math.atan2,
    "sinh": math.sinh,
    "cosh": math.cosh,
    "tanh": math.tanh,
    "asinh": math.asinh,
    "acosh": math.acosh,
    "atanh": math.atanh,
    "exp": math.exp,
    "log": math.log,
    "ln": math.log,
    "log10": math.log10,
    "log2": math.log2,
    "factorial": _safe_factorial,
    "comb": _safe_comb,
    "perm": _safe_perm,
    "gcd": math.gcd,
    "lcm": math.lcm,
    "pow": _safe_pow,
    "min": min,
    "max": max,
    "degrees": math.degrees,
    "radians": math.radians,
}

_CONSTANTS = {"pi": math.pi, "e": math.e, "tau": math.tau}


def _check_expression(expression: str) -> str:
    if not isinstance(expression, str) or not expression.strip():
        raise ToolError("expression 不能为空")
    expression = expression.strip()
    if len(expression) > 600:
        raise ToolError("expression 过长（上限 600 字符）")
    if "__" in expression or ";" in expression:
        raise ToolError("expression 包含不支持的字符")
    # The model often writes x^2; Python's ^ is XOR, so normalize to **.
    expression = expression.replace("^", "**").replace("×", "*").replace("÷", "/")
    return expression


def _parse(expression: str) -> ast.Expression:
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ToolError(f"表达式语法错误: {exc.msg}") from None
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ToolError(f"表达式包含不支持的语法: {type(node).__name__}")
        count += 1
    if count > 500:
        raise ToolError("表达式过于复杂")
    return tree


def _number(value: Any) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolError("只允许数字")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ToolError("结果不是有限数字")
        if abs(value - round(value)) < 1e-12:
            return int(round(value))
        return round(value, 12)
    return value


def _eval_node(node: ast.AST, env: dict[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, env)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ToolError("只允许数字常量")
        return node.value
    if isinstance(node, ast.Name):
        if node.id in env:
            return env[node.id]
        if node.id in _CONSTANTS:
            return _CONSTANTS[node.id]
        raise ToolError(f"未知变量或常量: {node.id}")
    if isinstance(node, ast.UnaryOp):
        value = _eval_node(node.operand, env)
        if isinstance(node.op, ast.UAdd):
            return +value
        if isinstance(node.op, ast.USub):
            return -value
        raise ToolError("不支持的一元运算")
    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left, env)
        right = _eval_node(node.right, env)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, (ast.Div, ast.FloorDiv, ast.Mod)):
            if right == 0:
                raise ToolError("除数不能为 0")
            if isinstance(node.op, ast.Div):
                return left / right
            if isinstance(node.op, ast.FloorDiv):
                return left // right
            return left % right
        if isinstance(node.op, ast.Pow):
            if abs(right) > 1000:
                raise ToolError("指数过大")
            try:
                result = left**right
            except (OverflowError, ValueError) as exc:
                raise ToolError(f"幂运算失败: {exc}") from None
            if isinstance(result, complex):
                raise ToolError("结果不是实数")
            return result
        raise ToolError("不支持的二元运算")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ToolError("只允许调用白名单函数")
        name = node.func.id
        if name not in _FUNCTIONS:
            raise ToolError(f"不支持的函数: {name}")
        args = [_eval_node(arg, env) for arg in node.args]
        if node.keywords:
            raise ToolError("函数不支持关键字参数")
        try:
            return _FUNCTIONS[name](*args)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ToolError(f"函数 {name} 计算失败: {exc}") from None
    raise ToolError(f"不支持的表达式节点: {type(node).__name__}")


def _prepare_env(variables: dict | None, variable: str | None = None, value: float | None = None) -> dict:
    env: dict[str, Any] = {}
    if variables is not None:
        if not isinstance(variables, dict):
            raise ToolError("variables 必须是对象")
        for name, raw in variables.items():
            if not isinstance(name, str) or not name.isidentifier():
                raise ToolError(f"非法变量名: {name!r}")
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise ToolError(f"变量 {name} 必须是数字")
            env[name] = raw
    if variable is not None and value is not None:
        env[variable] = value
    return env


def evaluate(expression: str, variables: dict | None = None) -> tuple[Any, str]:
    expr = _check_expression(expression)
    tree = _parse(expr)
    env = _prepare_env(variables)
    try:
        result = _eval_node(tree, env)
    except ZeroDivisionError:
        raise ToolError("除数不能为 0") from None
    result = _number(result)
    return result, expr


# ---------------------------------------------------------------------------
# Symbolic differentiation


def _node(cls, *args):
    node = cls(*args)
    return node


def _const(value: int | float) -> ast.AST:
    return ast.Constant(value=value)


def _name(name: str) -> ast.AST:
    return ast.Name(id=name, ctx=ast.Load())


def _binop(op, left, right) -> ast.AST:
    return ast.BinOp(left=left, op=op, right=right)


def _call(name: str, *args) -> ast.AST:
    return ast.Call(func=ast.Name(id=name, ctx=ast.Load()), args=list(args), keywords=[])


def _is_zero(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value == 0


def _is_one(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value == 1


def _simplify(node: ast.AST) -> ast.AST:
    if isinstance(node, ast.Expression):
        return _simplify(node.body)
    if isinstance(node, ast.BinOp):
        left = _simplify(node.left)
        right = _simplify(node.right)
        if isinstance(node.op, ast.Add):
            if _is_zero(left):
                return right
            if _is_zero(right):
                return left
            if isinstance(left, ast.Constant) and isinstance(right, ast.Constant):
                return _const(left.value + right.value)
        if isinstance(node.op, ast.Sub):
            if _is_zero(right):
                return left
            if _is_zero(left):
                return _simplify(ast.UnaryOp(op=ast.USub(), operand=right))
            if isinstance(left, ast.Constant) and isinstance(right, ast.Constant):
                return _const(left.value - right.value)
        if isinstance(node.op, ast.Mult):
            if _is_zero(left) or _is_zero(right):
                return _const(0)
            if _is_one(left):
                return right
            if _is_one(right):
                return left
            if isinstance(left, ast.Constant) and isinstance(right, ast.Constant):
                return _const(left.value * right.value)
        if isinstance(node.op, ast.Div):
            if _is_zero(left):
                return _const(0)
            if _is_one(right):
                return left
        if isinstance(node.op, ast.Pow):
            if _is_one(right):
                return left
            if _is_zero(right):
                return _const(1)
        return ast.BinOp(left=left, op=node.op, right=right)
    if isinstance(node, ast.UnaryOp):
        operand = _simplify(node.operand)
        if isinstance(operand, ast.Constant):
            if isinstance(node.op, ast.USub):
                return _const(-operand.value)
            return operand
        return ast.UnaryOp(op=node.op, operand=operand)
    return node


def _d(node: ast.AST, var: str) -> ast.AST:
    if isinstance(node, ast.Expression):
        return _d(node.body, var)
    if isinstance(node, ast.Constant):
        return _const(0)
    if isinstance(node, ast.Name):
        return _const(1 if node.id == var else 0)
    if isinstance(node, ast.UnaryOp):
        inner = _d(node.operand, var)
        return ast.UnaryOp(op=node.op, operand=inner)
    if isinstance(node, ast.BinOp):
        left, right = node.left, node.right
        dl, dr = _d(left, var), _d(right, var)
        if isinstance(node.op, (ast.Add, ast.Sub)):
            return _binop(node.op, dl, dr)
        if isinstance(node.op, ast.Mult):
            return _binop(ast.Add(), _binop(ast.Mult(), dl, right), _binop(ast.Mult(), left, dr))
        if isinstance(node.op, ast.Div):
            return _binop(
                ast.Div(),
                _binop(ast.Sub(), _binop(ast.Mult(), dl, right), _binop(ast.Mult(), left, dr)),
                _binop(ast.Pow(), right, _const(2)),
            )
        if isinstance(node.op, ast.Pow):
            if isinstance(right, ast.Constant) and isinstance(right.value, (int, float)):
                n = right.value
                return _binop(ast.Mult(), _binop(ast.Mult(), _const(n), _binop(ast.Pow(), left, _const(n - 1))), dl)
            # General f(x)^g(x) with ln; only safe for positive bases.
            return _binop(
                ast.Mult(),
                _binop(ast.Pow(), left, right),
                _binop(
                    ast.Add(),
                    _binop(ast.Mult(), dr, _call("ln", left)),
                    _binop(ast.Div(), _binop(ast.Mult(), right, dl), left),
                ),
            )
        raise ToolError("不支持对该运算符求导")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or not node.args:
            raise ToolError("不支持的函数求导")
        name = node.func.id
        u = node.args[0]
        du = _d(u, var)
        if len(node.args) != 1:
            raise ToolError(f"函数 {name} 的求导只支持单参数")
        if name == "sin":
            return _binop(ast.Mult(), _call("cos", u), du)
        if name == "cos":
            return _binop(ast.Mult(), _const(-1), _binop(ast.Mult(), _call("sin", u), du))
        if name == "tan":
            return _binop(ast.Div(), du, _binop(ast.Pow(), _call("cos", u), _const(2)))
        if name == "asin":
            return _binop(ast.Div(), du, _call("sqrt", _binop(ast.Sub(), _const(1), _binop(ast.Pow(), u, _const(2)))))
        if name == "acos":
            return _binop(
                ast.Div(),
                _binop(ast.Mult(), _const(-1), du),
                _call("sqrt", _binop(ast.Sub(), _const(1), _binop(ast.Pow(), u, _const(2)))),
            )
        if name == "atan":
            return _binop(ast.Div(), du, _binop(ast.Add(), _const(1), _binop(ast.Pow(), u, _const(2))))
        if name == "sinh":
            return _binop(ast.Mult(), _call("cosh", u), du)
        if name == "cosh":
            return _binop(ast.Mult(), _call("sinh", u), du)
        if name == "tanh":
            return _binop(ast.Div(), du, _binop(ast.Pow(), _call("cosh", u), _const(2)))
        if name == "exp":
            return _binop(ast.Mult(), _call("exp", u), du)
        if name in {"ln", "log"}:
            return _binop(ast.Div(), du, u)
        if name == "log10":
            return _binop(ast.Div(), du, _binop(ast.Mult(), u, _call("ln", _const(10))))
        if name == "sqrt":
            return _binop(ast.Div(), du, _binop(ast.Mult(), _const(2), _call("sqrt", u)))
        if name == "abs":
            return _binop(ast.Mult(), du, _binop(ast.Div(), u, _call("abs", u)))
        raise ToolError(f"函数 {name} 暂不支持符号求导")
    raise ToolError("不支持的表达式求导")


def derivative(expression: str, variable: str = "x") -> tuple[str, ast.AST]:
    expr = _check_expression(expression)
    if not isinstance(variable, str) or not variable.isidentifier():
        raise ToolError("variable 必须是合法变量名")
    tree = _parse(expr)
    raw = _d(tree, variable)
    simplified = _simplify(raw)
    ast.fix_missing_locations(simplified)
    try:
        text = ast.unparse(simplified)
    except Exception as exc:
        raise ToolError(f"无法生成导数表达式: {exc}") from None
    return text, simplified


def _evaluate_ast(node: ast.AST, variables: dict | None = None, variable: str = "x", value: float | None = None) -> Any:
    env = _prepare_env(variables, variable, value)
    return _number(_eval_node(node, env))


# ---------------------------------------------------------------------------
# Numeric calculus


def integrate(expression: str, lower: float, upper: float, variable: str = "x", variables: dict | None = None) -> float:
    expr = _check_expression(expression)
    if not isinstance(variable, str) or not variable.isidentifier():
        raise ToolError("variable 必须是合法变量名")
    try:
        a = float(lower)
        b = float(upper)
    except (TypeError, ValueError):
        raise ToolError("lower/upper 必须是数字") from None
    if not math.isfinite(a) or not math.isfinite(b):
        raise ToolError("积分上下限必须是有限数字")
    if a == b:
        return 0.0
    sign = 1.0
    if a > b:
        a, b = b, a
        sign = -1.0
    tree = _parse(expr)
    n = 1000
    h = (b - a) / n
    total = 0.0
    try:
        for i in range(n + 1):
            x = a + i * h
            y = _eval_node(tree, _prepare_env(variables, variable, x))
            if not isinstance(y, (int, float)) or not math.isfinite(float(y)):
                raise ToolError("积分过程中出现非有限值")
            weight = 1 if i in (0, n) else (4 if i % 2 else 2)
            total += weight * float(y)
        return sign * total * h / 3.0
    except ZeroDivisionError:
        raise ToolError("积分过程中出现除零") from None


def solve(
    expression: str,
    lower: float,
    upper: float,
    variable: str = "x",
    variables: dict | None = None,
    max_roots: int = 10,
) -> list[float]:
    expr = _check_expression(expression)
    if not isinstance(variable, str) or not variable.isidentifier():
        raise ToolError("variable 必须是合法变量名")
    try:
        a = float(lower)
        b = float(upper)
    except (TypeError, ValueError):
        raise ToolError("lower/upper 必须是数字") from None
    if not math.isfinite(a) or not math.isfinite(b) or a >= b:
        raise ToolError("求根区间要求 lower < upper 且为有限数字")
    tree = _parse(expr)
    env = _prepare_env(variables)
    env[variable] = a

    def f(x: float) -> float:
        env[variable] = x
        y = _eval_node(tree, env)
        if not isinstance(y, (int, float)) or not math.isfinite(float(y)):
            raise ToolError("求根过程中出现非有限值")
        return float(y)

    roots: list[float] = []
    samples = 400
    prev_x = a
    try:
        prev_y = f(a)
        if abs(prev_y) < 1e-12:
            roots.append(a)
        for i in range(1, samples + 1):
            x = a + (b - a) * i / samples
            y = f(x)
            if abs(y) < 1e-12:
                candidate = x
            elif prev_y * y < 0:
                lo, hi = prev_x, x
                flo, fhi = prev_y, y
                for _ in range(80):
                    mid = (lo + hi) / 2
                    fm = f(mid)
                    if abs(fm) < 1e-13:
                        lo = hi = mid
                        break
                    if flo * fm <= 0:
                        hi, fhi = mid, fm
                    else:
                        lo, flo = mid, fm
                candidate = (lo + hi) / 2
            else:
                candidate = None
            if candidate is not None:
                if not roots or abs(candidate - roots[-1]) > 1e-7:
                    roots.append(candidate)
                if len(roots) >= max_roots:
                    break
            prev_x, prev_y = x, y
    except ZeroDivisionError:
        raise ToolError("求根过程中出现除零") from None
    return [round(root, 10) for root in roots]


# ---------------------------------------------------------------------------
# Tool handler


def _get_number(args: dict, key: str, required: bool = True) -> float | None:
    if key not in args or args.get(key) is None:
        if required:
            raise ToolError(f"缺少参数 {key}")
        return None
    value = args.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolError(f"参数 {key} 必须是数字")
    if not math.isfinite(float(value)):
        raise ToolError(f"参数 {key} 必须是有限数字")
    return float(value)


def calculate(ctx: ToolContext, args: dict) -> ToolResult:
    operation = args.get("operation")
    if operation not in {"evaluate", "derivative", "integral", "solve"}:
        raise ToolError("operation 必须是 evaluate/derivative/integral/solve")
    expression = args.get("expression")
    if not isinstance(expression, str) or not expression.strip():
        raise ToolError("expression 必须是非空字符串")
    variable = args.get("variable") or "x"
    if not isinstance(variable, str) or not variable.isidentifier():
        raise ToolError("variable 必须是合法变量名")
    variables = args.get("variables")
    if variables is not None and not isinstance(variables, dict):
        raise ToolError("variables 必须是对象")

    if operation == "evaluate":
        result, expr = evaluate(expression, variables)
        payload = {"ok": True, "operation": operation, "expression": expr, "result": result}
        return ToolResult(
            ok=True,
            content=json_dumps(payload),
            summary=f"计算结果：{result}",
            data=payload,
        )

    if operation == "derivative":
        text, ast_node = derivative(expression, variable)
        point = _get_number(args, "point", required=False)
        payload: dict[str, Any] = {
            "ok": True,
            "operation": operation,
            "expression": expression,
            "variable": variable,
            "derivative": text,
        }
        if point is not None:
            payload["point"] = point
            payload["value"] = _evaluate_ast(ast_node, variables, variable, point)
        return ToolResult(
            ok=True,
            content=json_dumps(payload),
            summary=f"导函数：{text}",
            data=payload,
        )

    lower = _get_number(args, "lower", required=True)
    upper = _get_number(args, "upper", required=True)

    if operation == "integral":
        value = integrate(expression, lower, upper, variable, variables)
        payload = {
            "ok": True,
            "operation": operation,
            "expression": expression,
            "variable": variable,
            "lower": lower,
            "upper": upper,
            "result": round(value, 10),
        }
        return ToolResult(
            ok=True,
            content=json_dumps(payload),
            summary=f"定积分结果：{round(value, 10)}",
            data=payload,
        )

    roots = solve(expression, lower, upper, variable, variables)
    payload = {
        "ok": True,
        "operation": operation,
        "expression": expression,
        "variable": variable,
        "lower": lower,
        "upper": upper,
        "roots": roots,
    }
    summary = "根：" + (", ".join(str(root) for root in roots) if roots else "区间内未找到实根")
    return ToolResult(ok=True, content=json_dumps(payload), summary=summary, data=payload)


def register_calculate_tool(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="calculate",
            description=(
                "安全的高等数学计算。支持 evaluate 求值、derivative 符号求导、"
                "integral 定积分（数值）、solve 区间求根；单变量，支持常见初等函数。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": ["evaluate", "derivative", "integral", "solve"],
                        "description": "计算类型",
                    },
                    "expression": {
                        "type": "string",
                        "description": "数学表达式，例如 x^2+sin(x)，用 ^ 表示幂",
                    },
                    "variable": {
                        "type": "string",
                        "description": "变量名，默认 x",
                    },
                    "point": {
                        "type": "number",
                        "description": "derivative 时可选，在某个点求导数值",
                    },
                    "lower": {
                        "type": "number",
                        "description": "integral/solve 的下限",
                    },
                    "upper": {
                        "type": "number",
                        "description": "integral/solve 的上限",
                    },
                    "variables": {
                        "type": "object",
                        "description": "其它参数的取值，例如 {\"a\": 2}",
                        "additionalProperties": {"type": "number"},
                    },
                },
                "required": ["operation", "expression"],
                "additionalProperties": False,
            },
            handler=calculate,
        )
    )
