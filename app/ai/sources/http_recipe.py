"""通用素材源:拉一个 HTTP 接口 → 按「取数配方」变成素材文本。

**加一个新项目不用写代码。** 填个 URL,再写一句「这个接口里我要什么」,
让模型看一段返回样本,它产出配方(字段路径 + 排版模板),你过目、存进人设。
第二天起直接复用那份配方,不再调模型。

## 为什么让模型写配方,而不是让它直接把数据念一遍

念一遍最省事,但价格会经过模型的手 —— 而这条链路的产出是直接发到线上账号的,
读者会拿着那些数字去店里。模型把 `$3.49` 复述成 `$3.99` 不会报错、不会有人发现,
直到有人白跑一趟。

让它写配方就没这个问题:它碰的是 `price.salePrice` 这样的**字段路径**,
真正取值的是 `paths.evaluate`。配方还看得见、改得动、存得下来 ——
念一遍是每天赌一次,配方是赌一次然后你检查一次。
"""
from __future__ import annotations

import json

import httpx

from . import Facts, SourceError, SourceSpec
from ..paths import FUNCTION_NAMES
from . import recipe as recipe_mod

DEFAULT_CONFIG = {
    "url": "",
    "headers": {},
    # 「我要什么」——写给模型看的一句话。只在需要重新认接口时用得上。
    "prompt": "",
    "timeout": 30,
    # 给模型看多长的返回样本。整个返回喂进去既贵又没必要 ——
    # 认结构只需要看见头几条记录长什么样。
    "sample_chars": 6000,
    # 下面这些是配方本身。留空 = 让模型认一次然后填上。
    "items_path": "",
    "fields": {},
    "score": "",
    "min_score": 0,
    "pick": 3,
    "pool_shown": 10,
    "boost_keywords": [],
    "boost": 2.0,
    "unique_by": "",
    "line": "",
    "image_field": "image",
    "label": "",
    "intro": "",
    "caveat": "",
}

RECIPE_KEYS = ("items_path", "keep", "fields", "score", "min_score", "pick",
               "pool_shown", "boost_keywords", "boost", "unique_by", "line",
               "image_field", "label", "intro", "caveat")

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

DERIVE_SYSTEM = f"""你在读一个接口的返回样本，任务是写出一份「取数配方」——
一份告诉程序**怎么从这坨数据里取字段**的说明。你不要自己复述任何数值。

只输出 JSON，不要解释，不要 ``` 围栏：

{{
  "items_path": "到条目数组的点路径，比如 products.items。整个返回本身就是数组就留空",
  "keep": {{"字段路径": "必须等于的值"}},   // 可选，用来滤掉非条目的行
  "fields": {{
    "id":    "取一个稳定唯一的 id，比如 sku / id / barcode（必填）",
    "name":  "取名字（必填）",
    "...":   "其余你觉得写文案用得上的字段，名字自己起"
  }},
  "score": "一个能表示『这条有多值得写』的数值字段路径，比如折扣百分比",
  "line": "一行素材长什么样，用 {{字段名}} 占位，字段名要和 fields 对得上",
  "image_field": "fields 里哪个是图片地址；没有就留空",
  "label": "这批数据叫什么，四到八个字",
  "caveat": "这份数据的口径提醒，一句话；比如『全国接口，不分门店；每周三换新』"
}}

表达式语法（只有这些）：

* `a.b.c` 取嵌套字段，`items.0.name` 用下标
* `a.b || c.d` 前面取不到就用后面
* `路径 | 函数` 管道，可以串多个。函数只有：{'、'.join(FUNCTION_NAMES)}
  - `money` 把 7.0 变成 $7.00（补两位小数，不改数值）
  - `resize(1200)` 把图片地址里的 w=200&h=200 换成 w=1200&h=1200
  - `int` / `round` / `text` / `strip` / `first` / `join(、)` / `lower` / `upper` / `number`

规矩：

* **字段路径必须是样本里真实存在的**。拿不准的字段就别放进 fields ——
  编一个路径出来，程序取到的是空值，而那会静悄悄地变成一张缺了一块的卡。
* 钱一律走 `| money`，图片一律走 `| resize(1200)`。
* `line` 里只用 fields 里定义过的名字。
"""


async def _get(url: str, headers: dict, timeout: float):
    if not str(url or "").startswith(("http://", "https://")):
        raise SourceError("素材源没填 url（要以 http:// 或 https:// 开头）")
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as cli:
            r = await cli.get(url, headers={**(headers or {}), "User-Agent": _UA})
    except httpx.HTTPError as exc:
        raise SourceError(f"连不上 {url}：{exc}") from exc
    if r.status_code >= 400:
        raise SourceError(f"{url} 返回 {r.status_code}：{r.text[:200]}")
    try:
        return r.json()
    except ValueError as exc:
        raise SourceError(
            f"返回的不是 JSON（{exc}）。开头是：{r.text[:160]} —— "
            f"有些接口要带特定请求头才吐 JSON，检查一下 headers。") from exc


def _recipe(config: dict) -> dict:
    return {k: config[k] for k in RECIPE_KEYS if k in config}


def _has_recipe(config: dict) -> bool:
    fields = config.get("fields")
    return isinstance(fields, dict) and bool(fields)


async def derive_recipe(payload, config: dict) -> dict:
    """让模型看一段样本,写出配方。返回的是配方 dict,**不含任何数值**。"""
    from ..client import AiError, channel_from_settings, complete_json

    sample = json.dumps(payload, ensure_ascii=False, indent=1)[
        :max(1000, int(config.get("sample_chars") or 6000))]
    want = str(config.get("prompt") or "").strip() or "把这个接口里每条记录的关键字段取出来。"
    user = (f"接口地址：{config.get('url')}\n\n"
            f"我要的是：{want}\n\n"
            f"返回样本（可能被截断）：\n{sample}")
    try:
        got, _usage = await complete_json(channel_from_settings(), DERIVE_SYSTEM, user)
    except AiError as exc:
        raise SourceError(f"让模型认接口失败：{exc}") from exc
    if not isinstance(got.get("fields"), dict) or not got["fields"]:
        raise SourceError("模型没给出 fields，配方不可用。把「我要什么」写具体一点再试")
    return {k: v for k, v in got.items() if k in RECIPE_KEYS}


async def fetch(config: dict, *, exclude: list[str], seed: str | None = None) -> Facts:
    payload = await _get(config.get("url"), config.get("headers") or {},
                         float(config.get("timeout") or 30))
    merged = {**config}
    derived: dict | None = None

    if _has_recipe(merged):
        try:
            text, keys, note = recipe_mod.run(payload, merged, exclude=exclude, seed=seed)
            return Facts(text=text, keys=keys, note=note)
        except recipe_mod.RecipeError as exc:
            # 配方过期(对方改了字段名)是常事。**不要静默换一份新的** ——
            # 说清楚发生了什么,再让模型重认一次,人才知道该去核对配方。
            derived_reason = f"原配方用不了（{exc}），已让模型重新认了一次接口。"
    else:
        derived_reason = "这个素材源还没有配方，已让模型认了一次接口。"

    derived = await derive_recipe(payload, merged)
    merged.update(derived)
    try:
        text, keys, note = recipe_mod.run(payload, merged, exclude=exclude, seed=seed)
    except recipe_mod.RecipeError as exc:
        raise SourceError(f"模型给的配方也取不出数据：{exc}") from exc
    return Facts(
        text=text, keys=keys,
        note=f"{derived_reason} {note}",
        recipe=derived,
    )


SPEC = SourceSpec(
    name="http_recipe",
    label="HTTP 接口（配方 / 可让 AI 认）",
    summary="填个 URL 和一句「我要什么」，让模型看一段返回样本写出取数配方；"
            "之后每天照配方取数，价格由代码搬运，不经模型的手。加新站点不用写代码。",
    default_config=DEFAULT_CONFIG,
    fetch=fetch,
)
