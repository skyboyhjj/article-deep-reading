"""safe_eval 的回归夹具：正确用例 + 攻击用例。

运行：python3 test_safe_eval.py
交付前必须全绿——这是服务端唯一执行外部文本的地方。
"""

import math
import sys

from safe_eval import EvalError, UnsafeExpression, evaluate

ok_cases = [
    ("1+1", {}, 2.0),
    ("(E/m)**2 + (t/s)**2", {"E": 1.0, "m": 2.0, "t": 3.0, "s": 4.0}, 0.25 + 0.5625),
    ("2**10", {}, 1024.0),
    ("-3+5", {}, 2.0),
    ("abs(-7)", {}, 7.0),
    ("max(1,2,3)", {}, 3.0),
    ("log(e)", {}, 1.0),
    ("sqrt(2)**2", {}, 2.0),
    ("round(3.14159, 3)", {}, 3.142),
    ("10/4", {}, 2.5),
    ("(1.2e3)*(2e-2)", {}, 24.0),
    ("pi", {}, math.pi),
]

attack_cases = [
    "__import__('os').system('id')",
    "().__class__.__bases__[0].__subclasses__()",
    "[x for x in range(10)]",
    "lambda: 1",
    "1 if 1 else 0",
    "open('/etc/passwd')",
    "eval('1+1')",
    "exec('import os')",
    "9**9**9",
    "a.b",
    "f'{1}'",
    "(x := 5)",
    "int.__mro__",
    "'a'*100",          # 字符串常量：非实数
    "True+1",           # 布尔常量：非实数
    "globals()",
    "print(1)",
    "x" * 401,          # 超长
]

fail_cases = [
    ("1/0", EvalError),
    ("log(-1)", EvalError),
    ("sqrt(-1)", EvalError),
    ("2**2000", UnsafeExpression),  # 指数超上限
    ("unknown_var + 1", UnsafeExpression),
]


def main() -> int:
    bad = 0

    for expr, syms, want in ok_cases:
        try:
            got = evaluate(expr, syms)
            if abs(got - want) > 1e-12:
                print(f"[FAIL] {expr!r} 期望 {want} 实得 {got}")
                bad += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {expr!r} 应通过却抛 {type(exc).__name__}: {exc}")
            bad += 1

    for expr in attack_cases:
        try:
            got = evaluate(expr)
            print(f"[FAIL] 攻击用例未被拦截: {expr!r} -> {got!r}")
            bad += 1
        except (UnsafeExpression, EvalError):
            pass
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] 攻击用例抛出非预期异常 {expr!r}: {type(exc).__name__}: {exc}")
            bad += 1

    for expr, want_exc in fail_cases:
        try:
            got = evaluate(expr)
            print(f"[FAIL] {expr!r} 应抛 {want_exc.__name__} 却得 {got!r}")
            bad += 1
        except want_exc:
            pass
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {expr!r} 异常类型不符: {type(exc).__name__}: {exc}")
            bad += 1

    total = len(ok_cases) + len(attack_cases) + len(fail_cases)
    print(f"\n{'全部通过' if bad == 0 else f'{bad} 项失败'} / 共 {total} 项")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
