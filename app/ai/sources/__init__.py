"""素材源 —— 每天自动把「今天的真实数据」拉回来,填进创作的素材框。

**为什么这层必须存在。** 素材框是正文里唯一可信的数字来源;人每天手工去接口
抄一遍价格,抄十天就会烦,烦了就会开始让模型「自己发挥」—— 而那正是这套东西
最不能出的事:读者拿着编出来的价格去店里。

**为什么选品在这一层做,不丢给模型。** 特价每周三才换,而帖子是每天发的。
把一周不变的池子原样喂给模型,它每天都会挑同一批「折扣最狠的」,于是一周七篇
长得几乎一样 —— 这是一眼能看出来的机器特征。所以选品带两件事:
按折扣深度**加权随机**(深的更容易中,但不保证),以及排除最近几篇已经写过的条目。

加一个新站点 = 在这个包里加一个模块 + 在 `_SOURCES` 里登记。产出必须是同一种
`Facts`(一段人和模型都读得懂的文本 + 一串去重用的 id)—— 下游是语言模型,
统一成文本比各搞各的结构化格式有用得多。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable


@dataclass
class Facts:
    """一次拉取的产出。

    `text` 直接进创作的素材框 —— 所以它得是人也能一眼看懂、能自己改的文本,
    不是一坨 JSON。`keys` 是这次用掉的条目 id(商品 SKU 之类),存进草稿,
    下一篇拉取时排除掉。
    """

    text: str
    keys: list[str] = field(default_factory=list)
    note: str = ""          # 一句话进度,给面板和日志看


@dataclass(frozen=True)
class SourceSpec:
    name: str
    label: str
    summary: str
    default_config: dict
    fetch: Callable[..., Awaitable[Facts]]


class SourceError(RuntimeError):
    """拉取失败,带一句人能看懂的原因。"""


def _specs() -> dict[str, SourceSpec]:
    from . import woolworths

    return {s.name: s for s in (woolworths.SPEC,)}


def available() -> dict[str, SourceSpec]:
    return _specs()


def spec(name: str) -> SourceSpec:
    got = _specs().get((name or "").strip())
    if got is None:
        raise SourceError(f"没有这个素材源:{name!r}。可选:{'、'.join(_specs()) or '（无）'}")
    return got


async def fetch(name: str, config: dict | None = None, *,
                exclude: list[str] | None = None, seed: str | None = None) -> Facts:
    """拉一次。`exclude` 是最近几篇用过的条目 id,`seed` 只给测试用(让抽签可复现)。"""
    s = spec(name)
    merged = {**s.default_config, **(config or {})}
    return await s.fetch(merged, exclude=list(exclude or []), seed=seed)
