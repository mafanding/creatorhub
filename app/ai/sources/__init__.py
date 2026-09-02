"""素材源 —— 每天自动把「今天的真实数据」拉回来,填进创作的素材框。

**为什么这层必须存在。** 素材框是正文里唯一可信的数字来源;人每天手工去接口
抄一遍价格,抄十天就会烦,烦了就会开始让模型「自己发挥」—— 而那正是这套东西
最不能出的事:读者拿着编出来的价格去店里。

**为什么这里没有「一个站点一个模块」。** 那样加一个项目就得加一段代码,代码只会
越堆越多,而真正跟站点有关的其实只有一件事:*怎么从这坨 JSON 里认出条目和数值*。
那一件事被写成了**配方**(`recipe.py` 执行,`paths.py` 求值),配方是配置 ——
可以人写,也可以让模型看一段返回样本写出来。**加一个新项目 = 一份配方,不是一段代码。**

所以这里只有一个真正的源(`http_recipe`),外加 `presets/` 里几份写好的配方。
预设也只是配置文件,不是代码。

`Facts` 是所有源统一的产出:一段人和模型都读得懂的文本 + 一串去重用的 id ——
下游是语言模型,统一成文本比各搞各的结构化格式有用得多。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Awaitable, Callable

PRESETS_DIR = Path(__file__).resolve().parent / "presets"


@dataclass
class Facts:
    """一次拉取的产出。

    `text` 直接进创作的素材框 —— 所以它得是人也能一眼看懂、能自己改的文本,
    不是一坨 JSON。`keys` 是这次用掉的条目 id,存进草稿,下一篇拉取时排除掉。
    `recipe` 只在模型现认了一次接口时才有值 —— 调用方应该把它存回人设,
    这样第二天起就不必再花那次钱,而且配方摆在那里是人能核对的。
    """

    text: str
    keys: list[str] = field(default_factory=list)
    note: str = ""          # 一句话进度,给面板和日志看
    recipe: dict | None = None


@dataclass(frozen=True)
class SourceSpec:
    name: str
    label: str
    summary: str
    default_config: dict
    fetch: Callable[..., Awaitable[Facts]]


class SourceError(RuntimeError):
    """拉取失败,带一句人能看懂的原因。"""


@lru_cache(maxsize=1)
def _specs() -> dict[str, SourceSpec]:
    from . import http_recipe

    out: dict[str, SourceSpec] = {http_recipe.SPEC.name: http_recipe.SPEC}
    # 预设 = 一份写好的配方。坏掉的预设**跳过就好**,不要让整个面板打不开 ——
    # 一个 JSON 少了个逗号不该变成「AI 创作用不了」。
    for path in sorted(PRESETS_DIR.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            config = raw["config"]
        except (ValueError, KeyError, OSError):
            continue
        out[path.stem] = SourceSpec(
            name=path.stem,
            label=str(raw.get("label") or path.stem),
            summary=str(raw.get("summary") or ""),
            default_config={**http_recipe.DEFAULT_CONFIG, **config},
            fetch=http_recipe.fetch,
        )
    return out


def available() -> dict[str, SourceSpec]:
    return _specs()


def spec(name: str) -> SourceSpec:
    got = _specs().get((name or "").strip())
    if got is None:
        raise SourceError(f"没有这个素材源:{name!r}。可选:{'、'.join(_specs()) or '（无）'}")
    return got


async def fetch(name: str, config: dict | None = None, *,
                exclude: list[str] | None = None, seed: str | None = None) -> Facts:
    """拉一次。

    `exclude` 是最近几篇用过的条目 id,**最近的排在前面**(见底时按「隔得最久的
    先回来」补齐)。`seed` 只给测试用,让抽签可复现。
    """
    s = spec(name)
    merged = {**s.default_config, **(config or {})}
    return await s.fetch(merged, exclude=list(exclude or []), seed=seed)
