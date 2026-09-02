"""取数配方里那个小小的表达式语言:`price.salePrice | money`。

**为什么要有它。** 素材源本来是一个站点一个 Python 模块 —— 加一个项目就得加一段
代码,代码只会越堆越多。真正跟站点有关的其实只有一件事:*怎么从这坨 JSON 里认出
商品和价格*。把那一件事写成配置,加项目就只剩填配置。

**为什么不用现成的 JSONPath。** 取值只是一半,另一半是格式化:价格要补两位小数、
商品图要把 `w=200` 换成 `w=1200`。JSONPath 做不了后半段,还得再配一套。而这里
需要的取值能力其实很小(点路径 + 下标 + 一个兜底),自己实现反而更短、没有依赖,
而且**函数是白名单**——这些表达式有可能是模型写的,不能让它变成一个求值器。

语法:

    a.b.c                取嵌套字段
    items.0.name         下标
    a.b || c.d           前面取不到(空/缺)就用后面
    price.sale | money   管道:先取值,再依次过函数
    images.big | resize(1200)

函数只有白名单里那几个,拿不准的一律不加 —— 这条链路的产出会被直接发到线上账号。
"""
from __future__ import annotations

import re
from typing import Any, Callable

MAX_EXPR = 400          # 配方可能是模型写的,给个上限,别让它塞一篇文章进来


class PathError(ValueError):
    """表达式写错了。消息要能直接显示给人看。"""


# ── 函数白名单 ───────────────────────────────────────────────────────────────
def _text(v: Any) -> str:
    return " ".join(str(v).split()) if v is not None else ""


def _money(v: Any) -> str:
    """`7.0` → `$7.00`。**这不是改数字**,只是补两位小数和货币符号。

    读不成数字就原样返回 —— 有些站点的价格本来就是 `"$7.00"` 或 `"7,00 €"`,
    强行解析只会把对的东西弄坏。
    """
    if v is None or v == "":
        return ""
    try:
        return f"${float(v):.2f}"
    except (TypeError, ValueError):
        return _text(v)


def _number(v: Any) -> float:
    """挑出第一个数,用来排序/过滤。取不到就是 0 —— 排序里当最差处理。"""
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d+(?:\.\d+)?", str(v or ""))
    return float(m.group(0)) if m else 0.0


def _resize(v: Any, width: str = "1200") -> str:
    """把图片地址里的尺寸换掉。`w=200&h=200` → `w=1200&h=1200`。

    很多站点的图接口默认给缩略图,原样发出去是糊的。
    """
    try:
        n = int(float(width))
    except (TypeError, ValueError) as exc:
        raise PathError(f"resize 的参数要是数字,给的是 {width!r}") from exc
    return re.sub(r"w=\d+&h=\d+", f"w={n}&h={n}", _text(v))


def _first(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return v[0] if v else None
    return v


def _join(v: Any, sep: str = " ") -> str:
    if isinstance(v, (list, tuple)):
        return sep.join(_text(x) for x in v if x not in (None, ""))
    return _text(v)


_FUNCS: dict[str, Callable[..., Any]] = {
    "text": _text,
    "money": _money,
    "number": _number,
    "int": lambda v: int(_number(v)),
    "round": lambda v: round(_number(v)),
    "resize": _resize,
    "first": _first,
    "join": _join,
    "lower": lambda v: _text(v).lower(),
    "upper": lambda v: _text(v).upper(),
    "strip": lambda v: _text(v).strip(),
}

FUNCTION_NAMES = tuple(sorted(_FUNCS))

_CALL_RE = re.compile(r"^([a-z_]+)\s*(?:\(\s*([^()]*?)\s*\))?$")
_PIPE_RE = re.compile(r"(?<!\|)\|(?!\|)")


def _walk(data: Any, path: str) -> Any:
    """点路径取值。取不到返回 None —— 缺字段是常态,不是错误。"""
    cur = data
    for seg in path.split("."):
        seg = seg.strip()
        if not seg:
            raise PathError(f"路径里有空的一节:{path!r}")
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(seg)
        elif isinstance(cur, (list, tuple)):
            if not seg.lstrip("-").isdigit():
                return None
            idx = int(seg)
            cur = cur[idx] if -len(cur) <= idx < len(cur) else None
        else:
            return None
    return cur


def evaluate(data: Any, expr: str) -> Any:
    """按表达式从 `data` 里取一个值。"""
    expr = (expr or "").strip()
    if not expr:
        return None
    if len(expr) > MAX_EXPR:
        raise PathError(f"表达式太长了({len(expr)} 字符,上限 {MAX_EXPR})")

    # 只在**单个** `|` 上切,`||` 是「取不到就用后面」不是管道。
    # 直接 split("|") 会把 `sku || barcode` 切成三段,取值永远是空 ——
    # 而空值不报错,只会静悄悄地少一个字段。
    head, *funcs = [p.strip() for p in _PIPE_RE.split(expr)]
    value = None
    for alt in [a.strip() for a in head.split("||")]:
        if not alt:
            continue
        # 引号括起来的是字面量,给「取不到就用这个默认值」用
        if len(alt) >= 2 and alt[0] == alt[-1] and alt[0] in "'\"":
            value = alt[1:-1]
        else:
            value = _walk(data, alt)
        if value not in (None, "", [], {}):
            break

    for raw in funcs:
        if not raw:
            continue
        m = _CALL_RE.match(raw)
        if not m:
            raise PathError(f"看不懂的管道函数:{raw!r}")
        name, arg = m.group(1), m.group(2)
        fn = _FUNCS.get(name)
        if fn is None:
            raise PathError(f"没有 `{name}` 这个函数。只能用:{'、'.join(FUNCTION_NAMES)}")
        try:
            value = fn(value, arg) if arg not in (None, "") else fn(value)
        except PathError:
            raise
        except TypeError as exc:
            raise PathError(f"`{name}` 的参数不对:{exc}") from exc
    return value


def evaluate_fields(data: Any, fields: dict[str, str]) -> dict[str, Any]:
    """按一组表达式取一整条记录。某个字段写错时**只让那个字段报错**,
    不要把整条记录丢掉 —— 一个可选字段的表达式写歪了,不该让当天没素材。"""
    out: dict[str, Any] = {}
    for key, expr in (fields or {}).items():
        try:
            out[key] = evaluate(data, str(expr))
        except PathError as exc:
            out[key] = ""
            out.setdefault("_errors", {})
            out["_errors"][key] = str(exc)
    return out
