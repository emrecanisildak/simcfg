"""Referans ifade yorumlayıcısı. Motor implementasyonları bu semantiği birebir uygulamalıdır.

Semantik (conformance/expressions.json ile test edilir)
-------------------------------------------------------
* Bilinmeyen/eksik değişken, parametre veya alan -> null
* Doğruluk (truthiness): null->false, boolean->kendisi, sayı->(!=0), metin/dizi->(boş değil)
* eq/ne: değer eşitliği (1 == 1.0). null == null -> true
* lt/le/gt/ge/between: argümanlardan biri sayı değilse -> false
* add/sub/mul/div/mod/neg/abs/min/max: herhangi bir argüman sayı değilse -> null; sıfıra bölme -> null
* in(x, liste): liste değilse -> false
* exists(x): x != null
* if(c, a, b): tembel (sadece seçilen dal değerlendirilir)
* all/any: kısa devre; boş liste all->true, any->false
* cond: parametreler mevcut bağlamda değerlendirilir, sonra koşul handler'ı (native) veya gövdesi (expression) çağrılır
"""
from __future__ import annotations

from typing import Any, Callable, Protocol

EXPR_KEYS = {"var", "param", "op", "cond", "all", "any", "not"}


def is_expr(v: Any) -> bool:
    return isinstance(v, dict) and bool(v) and bool(set(v) & EXPR_KEYS) and set(v) <= EXPR_KEYS | {"args", "params"}


def truthy(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, (str, list, dict)):
        return len(v) > 0
    return bool(v)


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


class EvalContext(Protocol):
    def lookup(self, path: str) -> Any: ...
    def param(self, name: str) -> Any: ...
    def condition(self, cond_id: str, params: dict) -> Any: ...


def _arith(op: str, xs: list) -> Any:
    if not xs or not all(_num(x) for x in xs):
        return None
    if op == "add":
        return sum(xs)
    if op == "mul":
        r = 1
        for x in xs:
            r *= x
        return r
    if op == "min":
        return min(xs)
    if op == "max":
        return max(xs)
    if op == "sub":
        return xs[0] - xs[1]
    if op == "div":
        return None if xs[1] == 0 else xs[0] / xs[1]
    if op == "mod":
        return None if xs[1] == 0 else xs[0] % xs[1]
    if op == "neg":
        return -xs[0]
    if op == "abs":
        return abs(xs[0])
    raise ValueError(op)


_CMP: dict[str, Callable[[Any, Any], bool]] = {
    "lt": lambda a, b: a < b, "le": lambda a, b: a <= b, "gt": lambda a, b: a > b, "ge": lambda a, b: a >= b,
}


def evaluate(node: Any, ctx: EvalContext) -> Any:
    if not isinstance(node, dict):
        if isinstance(node, list):
            return [evaluate(x, ctx) for x in node]
        return node
    if not is_expr(node):
        return {k: evaluate(v, ctx) for k, v in node.items()}  # literal nesne (ör. geoPoint) içinde ifade olabilir
    if "var" in node:
        return ctx.lookup(node["var"])
    if "param" in node:
        return ctx.param(node["param"])
    if "all" in node:
        return all(truthy(evaluate(x, ctx)) for x in node["all"])
    if "any" in node:
        return any(truthy(evaluate(x, ctx)) for x in node["any"])
    if "not" in node:
        return not truthy(evaluate(node["not"], ctx))
    if "cond" in node:
        params = {k: evaluate(v, ctx) for k, v in (node.get("params") or {}).items()}
        return truthy(ctx.condition(node["cond"], params))
    op = node["op"]
    args = node.get("args") or []
    if op == "if":
        return evaluate(args[1], ctx) if truthy(evaluate(args[0], ctx)) else evaluate(args[2], ctx)
    xs = [evaluate(a, ctx) for a in args]
    if op == "eq":
        return xs[0] == xs[1]
    if op == "ne":
        return xs[0] != xs[1]
    if op in _CMP:
        return _num(xs[0]) and _num(xs[1]) and _CMP[op](xs[0], xs[1])
    if op == "between":
        return all(_num(x) for x in xs) and xs[1] <= xs[0] <= xs[2]
    if op == "in":
        return isinstance(xs[1], list) and xs[0] in xs[1]
    if op == "exists":
        return xs[0] is not None
    return _arith(op, xs)


class DictContext:
    """Test/uyumluluk için düz bağlam: değişkenler tam yol anahtarlarıyla verilir."""

    def __init__(self, vars: dict | None = None, params: dict | None = None, conds: dict | None = None) -> None:
        self.vars = vars or {}
        self.params = params or {}
        self.conds = conds or {}
        self.cond_calls: list[tuple[str, dict]] = []

    def lookup(self, path: str) -> Any:
        return self.vars.get(path)

    def param(self, name: str) -> Any:
        return self.params.get(name)

    def condition(self, cond_id: str, params: dict) -> Any:
        self.cond_calls.append((cond_id, params))
        return self.conds.get(cond_id)
