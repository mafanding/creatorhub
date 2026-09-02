"""写稿:提示词 → JSON 草稿 → 预检 → 不过就把问题回喂给模型重写。

**这里没有工具循环。** xhs-autonote 那边的 API 通道要跑两三百步(读文件、
跑命令、看图),因为它伺候的是一个装在文件系统上的命令行 Agent。这边不需要:
模型只产出一段 JSON,渲染、校验、落库都由我们的代码来做 —— 确定性的事
交给代码,只把「写什么」留给模型。

自检重跑的上限故意很小(默认 3 次)。预检问题分两类:字数超了这种,模型
一次就能改对;而「命中风险词」往往是它对这个词的判断和我们不一样,再跑五次
也是同样的错 —— 那时候该让人看一眼,不该继续烧钱。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

from . import catalog, prompts
from .client import AiChannel, complete_json
from .draft import Draft, coerce_draft, validate_draft

DEFAULT_MAX_ATTEMPTS = 3


@dataclass
class WriteResult:
    draft: Draft
    problems: list[str]              # 空 = 预检全过
    attempts: int = 0
    usage: dict = field(default_factory=lambda: {"input": 0, "output": 0})
    log: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


async def write_draft(
    channel: AiChannel,
    brand: dict,
    *,
    direction: str = "",
    facts: str = "",
    cards_min: int = 4,
    cards_max: int = 7,
    template_names: list[str] | None = None,
    recent_titles: list[str] | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    on_step: Callable[[str], None] | None = None,
) -> WriteResult:
    """要一篇草稿回来。预检不过时带着问题重写,最多 `max_attempts` 轮。

    **预检没过也返回**,不抛异常:那份草稿本身通常已经八九不离十,人在面板上
    改一句就能用。整轮丢掉才是浪费。调用方看 `result.ok` 决定怎么标状态。
    """
    say = on_step or (lambda _s: None)
    names = [n for n in (template_names or catalog.template_names())
             if n in catalog.templates()] or catalog.template_names()
    # 封面是每篇的第一张,它不在可选清单里的话模型只能违规。
    if catalog.COVER_TEMPLATE in catalog.templates() and catalog.COVER_TEMPLATE not in names:
        names.insert(0, catalog.COVER_TEMPLATE)

    lo, hi = _sane_range(cards_min, cards_max)
    user = prompts.build_user_prompt(
        brand,
        direction=direction,
        facts=facts,
        cards_min=lo,
        cards_max=hi,
        template_names=names,
        recent_titles=recent_titles,
    )

    banned = _banned_extra(brand)
    history: list[dict] = []
    result = WriteResult(draft=Draft(), problems=["还没开始"])

    for attempt in range(1, max(1, max_attempts) + 1):
        say(f"第 {attempt} 轮:{channel.label}")
        payload, usage = await complete_json(channel, prompts.SYSTEM, user, history=history)
        result.attempts = attempt
        result.usage["input"] += usage.get("input", 0)
        result.usage["output"] += usage.get("output", 0)

        draft = coerce_draft(payload)
        problems = validate_draft(
            draft,
            known_templates=set(names),
            cards_range=(lo, hi),
            banned_extra=banned,
        )
        result.draft = draft
        result.problems = problems

        if not problems:
            say(f"预检通过,{len(draft.cards)} 张卡")
            return result

        say(f"预检 {len(problems)} 个问题:" + "；".join(problems[:3]))
        if attempt >= max_attempts:
            break
        # 把上一版原样带回去 —— 让它在自己写的东西上改,比从头再写一遍
        # 更容易改对,也省钱。
        history = [
            {"role": "user", "content": user},
            {"role": "assistant", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        user = prompts.build_retry_prompt(problems)

    return result


def _sane_range(lo: int, hi: int) -> tuple[int, int]:
    """张数范围兜底。给反了、给成 0、给了 30 都不该让整轮失败。"""
    lo = max(1, int(lo or 1))
    hi = max(lo, int(hi or lo))
    return min(lo, 18), min(hi, 18)


def _banned_extra(brand: dict) -> tuple[str, ...]:
    """人设自己那串不能公开的字串。换行或逗号分隔都收。"""
    raw = str(brand.get("banned_words") or "")
    out: list[str] = []
    for line in raw.replace("，", ",").replace("、", ",").splitlines():
        for piece in line.split(","):
            piece = piece.strip()
            if piece:
                out.append(piece)
    return tuple(out)
