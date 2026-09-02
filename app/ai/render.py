"""卡片渲染:Jinja2 模板 + 无头 Chromium 截图 → PNG。

**为什么不用文生图。** 卡片是密集的中文排版(表格、金额、截止日)。版式引擎
每次都能排得分毫不差,而且零成本;扩散模型做不到,中文字还经常糊。
所以「AI 生成图片」在这里的含义是:**模型填卡片数据,排版由模板负责**。

画布 1080×1440 CSS px(小红书 3:4),2× 截图。

**卡片上的一切都来自模板和模型填的文字,页面不引任何外部资源。** 这是有意的:
卡片数据是模型产出的,而提示词里会贴进人从平台上抄来的素材 —— 一旦允许「值看起来
像 URL 就去下载」,就等于让一段不可信的文本决定这台机器去请求什么(局域网、云元数据
端点、以及本机这个无鉴权的面板自己)。要加带远程图的模板,那条下载路径得单独设计。

**这条路不走 CloakBrowser,也不走账号的 profile。** 它打开的是本地 `file://`
的 HTML、截个图、关掉,不连任何网站 —— 反检测内核在这儿一点用没有,
而把渲染绑在一个需要自家 launcher 拉起的 pro 构建上,只会让它跟着人家升级
一起挂掉,报的还是「Target page has been closed」这种完全看不出病因的错。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from markupsafe import Markup, escape

from . import catalog
from .draft import Card

CANVAS_W = catalog.CANVAS_W
CANVAS_H = catalog.CANVAS_H
SCALE = catalog.SCALE

# 同时只渲一组卡。每组要起一个 Chromium,并发起五个只会把内存吃光然后一起超时。
_RENDER_LOCK = asyncio.Semaphore(1)


class RenderError(RuntimeError):
    """渲染失败,带一句人能看懂的原因。"""


# ── Jinja ────────────────────────────────────────────────────────────────────
# 卡片文案是模型生成的,和任何不可信字符串一样走自动转义 —— 但作者仍然想给
# 某个词加粗、或强制换行。全部转义,然后只放这几个回来。
_ALLOWED_INLINE = ("em", "b", "s", "u")


def _rich(value: object) -> Markup:
    out = str(escape(str(value)))
    for tag in _ALLOWED_INLINE:
        out = out.replace(f"&lt;{tag}&gt;", f"<{tag}>").replace(f"&lt;/{tag}&gt;", f"</{tag}>")
    out = out.replace("&lt;br&gt;", "<br>").replace("\n", "<br>")
    return Markup(out)


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(catalog.TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "j2"]),
        # StrictUndefined:模板里写错一个字段名要当场报错,不能悄悄渲成空白 ——
        # 那会产出一张缺了一块的卡,而且没有任何日志。
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["rich"] = _rich
    return env


def render_html(card: Card, *, index: int, total: int, brand: dict, theme: str = "") -> str:
    """一张卡的完整 HTML(CSS 内联,不引任何外部资源 —— 离线要能渲)。"""
    if card.template not in catalog.templates():
        raise RenderError(f"没有这个卡片模板:{card.template}")
    template = _env().get_template(f"{card.template}.html.j2")
    return template.render(
        d=card.data,
        brand=brand,
        # base 定结构,theme 定这个号的脸。拼接顺序不能反 ——
        # theme 靠后写才能覆盖前面的 :root。
        base_css=catalog.base_css(),
        theme_css=catalog.theme_css(theme),
        index=index,
        total=total,
        canvas_w=CANVAS_W,
        canvas_h=CANVAS_H,
    )


async def render_cards(
    cards: list[Card],
    out_dir: Path,
    *,
    brand: dict,
    theme: str = "",
    keep_html: bool = False,
) -> list[Path]:
    """把每张卡渲染成 ``out_dir/NN-<模板名>.png``,返回路径列表。"""
    if not cards:
        return []
    # 绝对化:下面要把 HTML 变成 file:// URI,而 as_uri() 对相对路径直接抛异常。
    # 返回的路径还会被发布任务在几小时后读到,那时的工作目录不该是变量。
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    total = len(cards)

    pairs: list[tuple[Path, Path]] = []
    for i, card in enumerate(cards, start=1):
        html = render_html(card, index=i, total=total, brand=brand, theme=theme)
        stem = f"{i:02d}-{card.template}"
        html_path = out_dir / f"{stem}.html"
        html_path.write_text(html, encoding="utf-8")
        pairs.append((html_path, out_dir / f"{stem}.png"))

    async with _RENDER_LOCK:
        png_paths = await _shoot(pairs)

    if not keep_html:
        for html_path, _ in pairs:
            html_path.unlink(missing_ok=True)
    return png_paths


async def _shoot(pairs: list[tuple[Path, Path]]) -> list[Path]:
    try:
        from patchright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover
        raise RenderError("没装 patchright,跑 `pip install -r requirements.txt`") from exc

    out: list[Path] = []
    pw = await async_playwright().start()
    try:
        browser = await _launch(pw)
        try:
            page = await browser.new_page(
                viewport={"width": CANVAS_W, "height": CANVAS_H},
                device_scale_factor=SCALE,
            )
            for html_path, png_path in pairs:
                await page.goto(html_path.as_uri(), wait_until="load")
                # _layout.html.j2 在自动缩字号和字体加载完成后置这个标记。
                try:
                    await page.wait_for_function("window.__cardReady === true", timeout=5_000)
                except Exception:  # noqa: BLE001 - 没继承骨架的模板照样渲染
                    await page.wait_for_timeout(300)
                await page.screenshot(
                    path=str(png_path),
                    clip={"x": 0, "y": 0, "width": CANVAS_W, "height": CANVAS_H},
                )
                out.append(png_path)
        finally:
            await browser.close()
    finally:
        await pw.stop()
    return out


async def _launch(pw):
    """优先系统 Chrome,退回 patchright 自带的 Chromium。

    两个都没有时报的原始错是「Executable doesn't exist at …」加一大段
    ASCII 边框 —— 面板上显示出来只会让人一头雾水,所以在这里换成一句
    能照着做的话。
    """
    last: Exception | None = None
    for kwargs in ({"headless": True, "channel": "chrome"}, {"headless": True}):
        try:
            return await pw.chromium.launch(**kwargs)
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise RenderError(
        "起不来浏览器,渲染不了卡片。先跑一次 `python -m patchright install chromium`"
        f"(或者装一个系统 Chrome)。原始错误:{str(last)[:200]}"
    )


def sample_cards() -> list[Card]:
    """每个模板一张示例卡。给「模板预览」用。"""
    return [Card(template=t.name, data=json.loads(json.dumps(t.sample)))
            for t in catalog.templates().values()]
