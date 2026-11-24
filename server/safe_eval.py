"""受限表达式求值器 —— 安全命门。

设计前提：**绝不执行 LLM 生成的任意 Python。**
读解的算据九成是四则运算、幂、对数、百分比换算，用一个白名单 AST 求值器
就能覆盖；需要 numpy 的大矩阵、特征值另走沙箱（见 docs/ARCHITECTURE.md §4）。

安全约束（任一不满足即拒绝，不做"尽量放行"）：
  1. 只解析单个表达式（ast.parse mode="eval"），不接受语句、import、赋值。
  2. 只允许白名单节点与算子；属性访问、下标、推导式、lambda、条件表达式一律拒。
  3. 函数与常量只允许白名单内的名字，变量只能来自调用方传入的 symbols。
  4. 幂运算指数绝对值有上限，防 9**9**9 之类的计算爆炸。
  5. 结果必须是有限实数；复数、inf、nan 一律判失败。
  6. 表达式长度与 AST 节点数有上限。

用法：
    from safe_eval import evaluate
    evaluate("(E/m)**2 + (t/s)**2", {"E": 1.0, "m": 2.0, "t": 3.0, "s": 4.0})
"""

from __future__ import annotations

import ast
import math
from typing import Any

MAX_LEN = 400          # 表达式字符数上限
MAX_NODES = 120        # AST 节点数上限
MAX_POW_EXP = 64       # 幂指数绝对值上限
MAX_ABS_RESULT = 1e300 # 结果量级上限

_ALLOWED_BINOPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.Pow: None,  # 单独处理（要检查指数上限）
    ast.Mod: lambda a, b: math.fmod(a, b),
}

_ALLOWED_UNARYOPS = {
    ast.UAdd: lambda a: +a,
    ast.USub: lambda a: -a,
}

_CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau}

_FUNCS: dict[str, Any] = {
    "abs": abs,
    "min": min,
    "max": max,
    "round": round,
    "sqrt": math.sqrt,
    "log": math.log,
    "log2": math.log2,
    "log10": math.log10,
    "exp": math.exp,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "floor": math.floor,
    "ceil": math.ceil,
}


class UnsafeExpression(ValueError):
    """表达式违反白名单规则。属降级：这一步不可信，但报告继续出。"""
    notice = "calc_untrusted"


class EvalError(ValueError):
    """表达式合法但计算失败（除以零、定义域外等）。属降级，同上。"""
    notice = "calc_untrusted"


def _reject(node: ast.AST) -> None:
    raise UnsafeExpression(f"不允许的语法: {type(node).__name__}")


def _check_tree(tree: ast.AST) -> None:
    n = 0
    for node in ast.walk(tree):
        n += 1
        if n > MAX_NODES:
            raise UnsafeExpression(f"表达式过于复杂（节点数 > {MAX_NODES}）")
        if isinstance(node, ast.Expression):
            continue
        # 算子与上下文节点本身会被 walk 单独访问，其合法性已由父节点分支判定
        if isinstance(node, (ast.operator, ast.unaryop, ast.cmpop, ast.expr_context, ast.boolop)):
            continue
        if isinstance(node, ast.BinOp):
            if type(node.op) not in _ALLOWED_BINOPS:
                _reject(node.op)
            continue
        if isinstance(node, ast.UnaryOp):
            if type(node.op) not in _ALLOWED_UNARYOPS:
                _reject(node.op)
            continue
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise UnsafeExpression("只允许实数常量")
            continue
        if isinstance(node, ast.Name):
            continue
        if isinstance(node, ast.Load):
            continue
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise UnsafeExpression("只允许直接调用白名单函数")
            if node.func.id not in _FUNCS:
                raise UnsafeExpression(f"不在白名单的函数: {node.func.id}")
            if node.keywords:
                raise UnsafeExpression("不允许关键字参数")
            for a in node.args:
                if isinstance(a, ast.Starred):
                    raise UnsafeExpression("不允许星号展开")
            continue
        # 其余全部拒绝：Attribute / Subscript / Lambda / IfExp / Dict / List /
        # Tuple / Comprehension / JoinedStr / NamedExpr / Await / Yield 等
        _reject(node)


def _eval_node(node: ast.AST, env: dict[str, float]) -> float:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, env)
    if isinstance(node, ast.Constant):
        return float(node.value)
    if isinstance(node, ast.Name):
        if node.id in env:
            return env[node.id]
        if node.id in _CONSTS:
            return _CONSTS[node.id]
        raise UnsafeExpression(f"未定义的变量: {node.id}")
    if isinstance(node, ast.UnaryOp):
        return _ALLOWED_UNARYOPS[type(node.op)](_eval_node(node.operand, env))
    if isinstance(node, ast.BinOp):
        a = _eval_node(node.left, env)
        b = _eval_node(node.right, env)
        if isinstance(node.op, ast.Pow):
            if abs(b) > MAX_POW_EXP:
                raise UnsafeExpression(f"指数绝对值超过 {MAX_POW_EXP}")
            try:
                return a ** b
            except OverflowError as exc:
                raise EvalError(f"幂运算溢出: {exc}") from exc
        op = _ALLOWED_BINOPS.get(type(node.op))
        if op is None:
            raise UnsafeExpression(f"不允许的算子: {type(node.op).__name__}")
        try:
            return op(a, b)
        except ZeroDivisionError as exc:
            raise EvalError("除以零") from exc
    if isinstance(node, ast.Call):
        fn = _FUNCS[node.func.id]
        args = [_eval_node(a, env) for a in node.args]
        if node.func.id == "round" and len(args) == 2:
            args[1] = int(args[1])
        try:
            return float(fn(*args))
        except (ValueError, TypeError, OverflowError) as exc:
            raise EvalError(f"调用 {node.func.id} 失败: {exc}") from exc
    raise UnsafeExpression(f"不允许的语法: {type(node).__name__}")


def has_variable(expr: str, symbols: dict | None = None) -> tuple[bool, str]:
    """判定「这个表达式是不是真含变数」。返回 (是否有真变量, 原因)。

    为什么要比「含 Name 节点」更严：实测见过模型把常量塞进 symbols 冒充变量——

        expr = "50000 * 60", symbols = {"50000": 50000, "60": 60}

    形如「至少用到一个变量」的规则全满足，但本质仍是把原文数字换个写法。
    所以变量名必须是语义词；纯数字当变量名的，一律不算。
    """
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return False, "表达式语法错误"
    consts = {"pi", "e", "tau"}
    names = {n.id for n in ast.walk(tree)
             if isinstance(n, ast.Name) and n.id not in consts}
    if not names:
        return False, "表达式全是常量，等于复述原文"

    syms = {str(k): v for k, v in (symbols or {}).items()}
    real = []
    for name in names:
        key = str(name)
        if key.replace(".", "").replace("_", "").isdigit():
            continue          # 变量名就是数值字面量 —— 伪装
        if key in syms:
            real.append(key)
    if not real:
        return False, "变量名就是数值本身（把常量塞进 symbols 冒充变量）"
    return True, ""


def has_operation(expr: str) -> bool:
    """表达式里有没有真正的运算。

    `eda_share` 这种只有单个变量的「算式」，求值结果就是变量本身，
    没有任何推导 —— 它和一个数字没区别，不能算验证。
    """
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return False
    return not isinstance(tree.body, ast.Name)


def evaluate(expr: str, symbols: dict[str, float] | None = None) -> float:
    """求值单个算术表达式。任何越界行为抛 UnsafeExpression / EvalError。"""
    if not isinstance(expr, str):
        raise UnsafeExpression("表达式必须是字符串")
    expr = expr.strip()
    if not expr:
        raise UnsafeExpression("表达式为空")
    if len(expr) > MAX_LEN:
        raise UnsafeExpression(f"表达式过长（> {MAX_LEN} 字符）")

    env: dict[str, float] = {}
    for k, v in (symbols or {}).items():
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            raise UnsafeExpression(f"变量 {k} 必须是实数")
        env[str(k)] = float(v)

    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise UnsafeExpression(f"语法错误: {exc.msg}") from exc

    _check_tree(tree)
    val = _eval_node(tree, env)

    if isinstance(val, complex):
        raise EvalError("结果为复数，拒绝（本求值器只接受实数）")
    val = float(val)
    if not math.isfinite(val):
        raise EvalError("结果非有限数（inf / nan）")
    if abs(val) > MAX_ABS_RESULT:
        raise EvalError(f"结果量级过大（> {MAX_ABS_RESULT:g}）")
    return val
