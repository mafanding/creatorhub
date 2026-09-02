"""AI 创作的后台任务:一行 `AiDraft` 就是一个任务。

为什么状态存库不存内存:生成一篇要几十秒到几分钟(写稿一到三轮 + 起浏览器
渲六张图)。放内存里的话,页面一刷新就没了,服务重启更是连产出都找不回来 ——
而那时候钱已经花掉了。

为什么不塞进 `MonitorEngine`:那个循环管的是**账号级**的操作(发布、评论、
关注),每一步都要过风控闸和账号锁。写稿和渲染不碰任何账号,不该去排那个队。
"""
from __future__ import annotations

import asyncio
import json
import shutil
from datetime import datetime
from pathlib import Path

from sqlmodel import select

from ..db import get_session
from ..models import AiBrand, AiDraft
from . import catalog, prompts, render, sources
from .client import AiError, channel_from_settings
from .draft import Card, Draft, validate_draft
from .writer import write_draft

# 和 main.py 里 UPLOAD_DIR 一个待遇:相对工作目录的持久目录,不是临时目录 ——
# 发布任务引用的是这里的绝对路径,临时目录一清,队列里排着的任务就没图了。
DRAFTS_DIR = Path("./data/ai_drafts")

STATUS_PENDING = "pending"
STATUS_GENERATING = "generating"
STATUS_RENDERING = "rendering"
STATUS_READY = "ready"
STATUS_FAILED = "failed"
RUNNING_STATES = (STATUS_PENDING, STATUS_GENERATING, STATUS_RENDERING)

# 同时只跑一个创作任务。渲染那步要起浏览器,而写稿那步是纯等待 ——
# 放开并发换来的那点吞吐,不值得让一台笔记本同时开三个 Chromium。
_JOB_SEM = asyncio.Semaphore(1)
_RUNNING: dict[int, asyncio.Task] = {}


def draft_dir(draft_id: int) -> Path:
    """**必须是绝对路径。** 两个地方指着它:

    * 渲染要把本地 HTML 变成 `file://` URI,而 `Path.as_uri()` 对相对路径直接
      抛 `ValueError: relative path can't be expressed as a file URI` ——
      表现是整篇「失败」,错误信息完全看不出跟目录有关。
    * 发布任务存的是这里的路径,而它是几分钟甚至几小时以后由另一条协程读的。
      相对路径能不能读到,取决于那时候的工作目录 —— 那不是个该赌的东西。
    """
    return (DRAFTS_DIR / str(draft_id)).resolve()


def brand_dict(brand: AiBrand | None) -> dict:
    """`AiBrand` → 提示词和渲染都认识的那个字典。人设可以没有 —— 那就全是空值,
    模型照样能写,只是写出来的东西没有「这个号」的味道。"""
    if brand is None:
        return {}
    return {
        "name": brand.name,
        "mark": brand.mark,
        "date_label": brand.date_label,
        "footer_note": brand.footer_note,
        "doc": brand.doc,
        "theme": brand.theme,
        "product_name": brand.product_name,
        "product_line": brand.product_line,
        "cta_line": brand.cta_line,
        "tags_strategy": brand.tags_strategy,
        "banned_words": brand.banned_words,
    }


def allowed_templates(brand: AiBrand | None) -> list[str]:
    """这个人设允许用的模板。留空 = 全部。写错了名字就当没写 ——
    一份过期的白名单不该让所有创作都失败。"""
    known = catalog.template_names()
    if brand is None or not (brand.allowed_templates or "").strip():
        return known
    try:
        picked = json.loads(brand.allowed_templates)
    except json.JSONDecodeError:
        return known
    if not isinstance(picked, list):
        return known
    kept = [str(n) for n in picked if str(n) in known]
    return kept or known


def recent_source_keys(brand_id: int | None, limit: int = 6) -> list[str]:
    """同一个人设最近几篇用掉的素材条目 id,**最近的排在前面**。

    素材源按这个顺序做去重和见底兜底(见 sources/woolworths.py)。
    超市特价一周才换一次,不排除的话七天会写出七篇几乎一样的东西。
    """
    if not brand_id:
        return []
    with get_session() as s:
        rows = s.exec(
            select(AiDraft)
            .where(AiDraft.brand_id == brand_id)
            .order_by(AiDraft.id.desc())
            .limit(max(0, limit))
        ).all()
    keys: list[str] = []
    for row in rows:
        try:
            got = json.loads(row.source_keys_json or "[]")
        except json.JSONDecodeError:
            continue
        keys.extend(str(k) for k in got if str(k))
    return list(dict.fromkeys(keys))


async def pull_facts(brand: AiBrand, *, exclude: list[str] | None = None,
                     save_recipe: bool = True) -> sources.Facts:
    """按人设配的素材源拉一次。没配源就抛错 —— 调用方该先判断。

    模型现认了一次接口时(`Facts.recipe` 有值),把配方**存回人设** ——
    否则每天都要为同一个接口再付一次钱,而且那份配方谁也看不见、改不动。
    """
    if not (brand and (brand.facts_source or "").strip()):
        raise AiError("这个人设没有配素材源")
    try:
        config = json.loads(brand.facts_config or "{}")
    except json.JSONDecodeError as exc:
        raise AiError(f"人设里的素材源配置不是合法 JSON:{exc}") from exc
    if not isinstance(config, dict):
        raise AiError("素材源配置要是一个 JSON 对象")
    if exclude is None:
        exclude = recent_source_keys(brand.id, brand.facts_dedup_drafts)
    try:
        facts = await sources.fetch(brand.facts_source, config, exclude=exclude)
    except sources.SourceError as exc:
        raise AiError(str(exc)) from exc

    if facts.recipe and save_recipe and brand.id:
        merged = {**config, **facts.recipe}
        with get_session() as s:
            row = s.get(AiBrand, brand.id)
            if row is not None:
                row.facts_config = json.dumps(merged, ensure_ascii=False)
                row.updated_at = datetime.utcnow()
                s.add(row)
                s.commit()
        brand.facts_config = json.dumps(merged, ensure_ascii=False)
    return facts


def recent_titles(brand_id: int | None, limit: int = 12) -> list[str]:
    """同一个人设最近写过的标题。喂给提示词做去重 —— 一个号连着写同一个题,
    读者比你先看出来。"""
    if not brand_id:
        return []
    with get_session() as s:
        rows = s.exec(
            select(AiDraft)
            .where(AiDraft.brand_id == brand_id, AiDraft.status == STATUS_READY)
            .order_by(AiDraft.id.desc())
            .limit(limit)
        ).all()
    return [r.title for r in rows if r.title]


def sweep_interrupted() -> int:
    """启动时把卡在「进行中」的行打回失败。

    进程一重启,那些任务的协程就没了,而行还停在 `generating` —— 面板会
    一直转圈等一个永远不会来的结果。宁可明说「中断了,重来一次」。
    """
    n = 0
    with get_session() as s:
        rows = s.exec(select(AiDraft).where(AiDraft.status.in_(RUNNING_STATES))).all()
        for row in rows:
            row.status = STATUS_FAILED
            row.error = "服务重启,这次生成中断了。点「重新生成」再来一次。"
            row.updated_at = datetime.utcnow()
            s.add(row)
            n += 1
        if n:
            s.commit()
    return n


def enqueue(draft_id: int) -> bool:
    """把一行排进后台跑。已经在跑就返回 False(不重复起)。"""
    if draft_id in _RUNNING and not _RUNNING[draft_id].done():
        return False
    task = asyncio.create_task(run_draft_job(draft_id))
    _RUNNING[draft_id] = task
    # 收尸:不 pop 掉,这个字典会随着草稿数量一直长,而里面全是已完成的 Task。
    task.add_done_callback(lambda _t, i=draft_id: _RUNNING.pop(i, None))
    return True


def is_running(draft_id: int) -> bool:
    task = _RUNNING.get(draft_id)
    return bool(task and not task.done())


async def run_draft_job(draft_id: int) -> None:
    """跑完一整篇:写稿 → 自检重写 → 渲染 → 再验一遍图。

    **任何一步失败都要把原因写回那一行。** 无人盯着的时候,日志是没人看的,
    面板上那行红字才是现场。
    """
    async with _JOB_SEM:
        try:
            await _run(draft_id)
        except AiError as exc:
            _fail(draft_id, str(exc))
        except Exception as exc:  # noqa: BLE001 - 兜住一切,别让任务静默死掉
            _fail(draft_id, f"{type(exc).__name__}: {exc}")


async def _run(draft_id: int) -> None:
    with get_session() as s:
        row = s.get(AiDraft, draft_id)
        if row is None:
            return
        brand = s.get(AiBrand, row.brand_id) if row.brand_id else None
        # 出了 session 之后 SQLModel 对象就 detach 了,所以先把要用的字段拷出来。
        brand_row = (
            AiBrand(**{k: getattr(brand, k) for k in AiBrand.model_fields})
            if brand is not None else None
        )
        params = {
            "direction": row.direction,
            "facts": row.facts,
            "brand": brand_dict(brand),
            "theme": brand.theme if brand else "",
            "cards_min": brand.cards_min if brand else 4,
            "cards_max": brand.cards_max if brand else 7,
            "templates": allowed_templates(brand),
            "brand_id": row.brand_id,
        }
        row.status = STATUS_GENERATING
        row.error = ""
        row.updated_at = datetime.utcnow()
        s.add(row)
        s.commit()

    # 素材框空着而人设配了源:自动拉一次当天的真实数据。
    # **人手填了就不动它** —— 手填优先于自动,不然人改半天会被无声覆盖。
    if not params["facts"].strip() and brand_row is not None and brand_row.facts_source:
        facts = await pull_facts(brand_row)
        params["facts"] = facts.text
        with get_session() as s:
            row = s.get(AiDraft, draft_id)
            if row is not None:
                row.facts = facts.text
                row.source_keys_json = json.dumps(facts.keys, ensure_ascii=False)
                row.updated_at = datetime.utcnow()
                s.add(row)
                s.commit()

    channel = channel_from_settings()
    steps: list[str] = []
    result = await write_draft(
        channel,
        params["brand"],
        direction=params["direction"],
        facts=params["facts"],
        cards_min=params["cards_min"],
        cards_max=params["cards_max"],
        template_names=params["templates"],
        recent_titles=recent_titles(params["brand_id"]),
        on_step=steps.append,
    )

    with get_session() as s:
        row = s.get(AiDraft, draft_id)
        if row is None:
            return
        _apply_draft(row, result.draft)
        row.attempts = result.attempts
        row.model = channel.label
        row.usage_json = json.dumps(result.usage, ensure_ascii=False)
        row.problems_json = json.dumps(result.problems, ensure_ascii=False)
        row.status = STATUS_RENDERING
        row.updated_at = datetime.utcnow()
        s.add(row)
        s.commit()

    out_dir = draft_dir(draft_id)
    # 重跑时先清干净:上一次留下的 PNG 不清掉,张数变少的那次会把旧图一起
    # 当成这一篇的配图发出去。
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    images = await render.render_cards(
        result.draft.cards, out_dir,
        brand=prompts.brand_for_render(params["brand"]),
        theme=params["theme"],
    )

    # 渲染完再验一次 —— 这一次带上图。空文件、张数对不上都只有这时候看得出来。
    problems = validate_draft(
        result.draft, images,
        known_templates=set(params["templates"]),
        cards_range=(params["cards_min"], params["cards_max"]),
    )

    with get_session() as s:
        row = s.get(AiDraft, draft_id)
        if row is None:
            return
        # 渲染会把远程图地址改写成本地文件名,卡片数据得跟着更新,
        # 否则「重新渲染」会拿着一个已经不存在的 URL 再下一次。
        _apply_draft(row, result.draft)
        row.images_json = json.dumps([str(p) for p in images], ensure_ascii=False)
        row.problems_json = json.dumps(problems, ensure_ascii=False)
        # 预检没过**也算 ready**:草稿和图都在,人改一句就能发。
        # 真正的失败是根本没产出 —— 那条路走的是 _fail。
        row.status = STATUS_READY
        row.error = ""
        row.finished_at = datetime.utcnow()
        row.updated_at = row.finished_at
        s.add(row)
        s.commit()


def _apply_draft(row: AiDraft, draft: Draft) -> None:
    row.title = draft.title
    row.content = draft.content
    row.tags_json = json.dumps(draft.tags, ensure_ascii=False)
    row.cards_json = json.dumps([c.to_dict() for c in draft.cards], ensure_ascii=False)
    row.notes = draft.notes


def _fail(draft_id: int, why: str) -> None:
    with get_session() as s:
        row = s.get(AiDraft, draft_id)
        if row is None:
            return
        row.status = STATUS_FAILED
        row.error = why[:800]
        row.finished_at = datetime.utcnow()
        row.updated_at = row.finished_at
        s.add(row)
        s.commit()


# ── 重新渲染(改完卡片数据之后)────────────────────────────────────────────
async def rerender(draft_id: int) -> list[str]:
    """按库里现在的卡片数据重渲一遍。返回预检问题。

    人在面板上改了一个字段就该能看到新图,不必再花一次生成的钱。
    """
    with get_session() as s:
        row = s.get(AiDraft, draft_id)
        if row is None:
            raise AiError("草稿不存在")
        brand = s.get(AiBrand, row.brand_id) if row.brand_id else None
        cards = [Card.from_dict(c) for c in json.loads(row.cards_json or "[]")]
        draft = Draft(
            title=row.title, content=row.content,
            tags=json.loads(row.tags_json or "[]"), cards=cards, notes=row.notes,
        )
        theme = brand.theme if brand else ""
        rendered_brand = prompts.brand_for_render(brand_dict(brand))
        templates = allowed_templates(brand)
        row.status = STATUS_RENDERING
        row.updated_at = datetime.utcnow()
        s.add(row)
        s.commit()

    out_dir = draft_dir(draft_id)
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    async with _JOB_SEM:
        images = await render.render_cards(
            draft.cards, out_dir, brand=rendered_brand, theme=theme)

    problems = validate_draft(draft, images, known_templates=set(templates))
    with get_session() as s:
        row = s.get(AiDraft, draft_id)
        if row is None:
            raise AiError("草稿不存在")
        row.images_json = json.dumps([str(p) for p in images], ensure_ascii=False)
        row.problems_json = json.dumps(problems, ensure_ascii=False)
        row.status = STATUS_READY
        row.error = ""
        row.updated_at = datetime.utcnow()
        s.add(row)
        s.commit()
    return problems
