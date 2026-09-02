"""Woolworths NZ 本周特价。

网页版 `/shop/specials` 背后就是这个接口,公开、不需要登录。少了
`x-requested-with` 那个头会被当成网页版请求,拿不到 JSON。

`size=48` 是**故意压小的**:接口一共几千条特价,全拉回来只会把上下文塞满,
而写一篇笔记用不到四样商品。

## 选品

在「折扣够深」的候选里**按折扣深度加权随机**抽,不是取前 N 名。

理由是时间尺度对不上:特价每周三才换一次,而帖子每天发。取前 N 名的话,
同一批商品会连着上七天,标题都差不多 —— 这是一眼能认出来的机器特征。
加权随机让深折扣仍然更容易被选中(它们本来就更值得写),但不保证,
于是同一周里每天的组合都不一样。

再叠一层:排除最近几篇已经写过的 SKU。这两件事一起,才真的换得动。

## 数字一律照抄

现价、原价、折扣百分比、省多少,四个数接口全都直接给了。**一个都不自己算。**
读者会拿着这些数字去店里,算错一次就是让人白跑一趟或者多花钱。
折扣百分比尤其不要自己拿原价现价去除 —— 接口给的那个才是货架上贴的那个。
"""
from __future__ import annotations

import random
import re
from datetime import date

import httpx

from . import Facts, SourceError, SourceSpec

DEFAULT_CONFIG = {
    "url": "https://www.woolworths.co.nz/api/v1/products?target=specials&size=48",
    "headers": {
        "x-requested-with": "OnlineShopping.WebApp",
        "Accept": "application/json",
    },
    # 折扣低于这个数的不进候选。够不够 pick 那么多样时会自动往下放宽,
    # 并在产出里写明放宽到了几 % —— 悄悄降低门槛等于悄悄换掉选品标准。
    "min_percent": 33,
    "pick": 3,               # 挑几样写进正文
    "pool_shown": 12,        # 另外列几样给模型做判断(不是让它编,是让它有得比)
    "image_width": 1200,     # 接口给的是 200px,发出去是糊的
    "timeout": 30,
    # 「日常真会买的东西」的关键词。命中的商品权重乘以 staple_boost。
    #
    # 这是**加权,不是过滤** —— 半价的巧克力仍然该被写,只是不该每天三样全是糖。
    # 纯按折扣深度抽的话,超市的深折扣天然集中在零食和糖果上(实测某天 33% 以上
    # 八样里七样是零食),于是抽出来的经常是一篇「今天全是些饼干」——
    # 而这个号的读者是来看买菜省钱的。
    #
    # 关键词匹配英文商品名,故意做得粗:它只负责把秤压一点,判断仍然归写手。
    # 这份清单是给人改的,不是标准答案。
    "staple_keywords": [
        "chicken", "beef", "pork", "lamb", "mince", "sausage", "bacon", "fish", "salmon",
        "egg", "milk", "cheese", "butter", "yoghurt", "cream",
        "bread", "rice", "pasta", "flour", "oil", "sauce", "noodle",
        "vegetable", "potato", "onion", "tomato", "fruit", "banana", "apple",
        "toilet paper", "tissue", "towel", "nappies", "detergent", "washing",
        "coffee", "tea", "frozen",
    ],
    "staple_boost": 2.5,
}

# 卡片和正文里的价格都从这里来,所以取值路径写死在一处,不散在各处。
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")


def _money(value) -> str:
    """接口给的是浮点数,写成钱的样子。**这不是改数字**,只是补两位小数。"""
    try:
        return f"${float(value):.2f}"
    except (TypeError, ValueError):
        return ""


def _item(raw: dict, image_width: int) -> dict | None:
    """一条商品。缺价格的直接丢掉 —— 没价格的特价没有意义。"""
    price = raw.get("price") or {}
    sale, original = price.get("salePrice"), price.get("originalPrice")
    if sale in (None, "") or original in (None, ""):
        return None
    try:
        pct = float(price.get("savePercentage") or 0)
        save = float(price.get("savePrice") or 0)
    except (TypeError, ValueError):
        return None
    size = (raw.get("size") or {}).get("volumeSize") or raw.get("unit") or ""
    image = str((raw.get("images") or {}).get("big") or "")
    if image:
        image = re.sub(r"w=\d+&h=\d+", f"w={image_width}&h={image_width}", image)
    name = " ".join(str(raw.get("name") or "").split())
    brand = " ".join(str(raw.get("brand") or "").split())
    return {
        "sku": str(raw.get("sku") or raw.get("barcode") or name),
        "name": name,
        "brand": brand,
        "size": str(size),
        "now": _money(sale),
        "was": _money(original),
        "pct": int(round(pct)),
        "save": _money(save),
        "save_num": save,
        "image": image,
    }


def _is_staple(item: dict, keywords: list[str]) -> bool:
    blob = f"{item['name']} {item['brand']}".lower()
    return any(k and k in blob for k in keywords)


def _pick(pool: list[dict], n: int, rng: random.Random,
          keywords: list[str], boost: float) -> list[dict]:
    """按折扣深度加权,不放回地抽 n 样。

    权重是 `pct²`,日常品再乘 `boost`。

    * **平方**:线性权重下 25% 和 50% 只差一倍,抽出来的组合太平均,
      经常是一篇「今天全是些减一块钱的东西」。平方之后深折扣明显更容易冒头,
      但仍然不是排序 —— 不确定性正是我们要的(特价一周才换,排序会让七篇雷同)。
    * **日常品加权**:见 DEFAULT_CONFIG 里 staple_keywords 那段。
    * **同品牌只取一样**:一次抽中同一个牌子的两种软糖,读者看到的是同一件事
      说了两遍,而那恰恰是加权随机最容易犯的错(同品牌的商品折扣往往一样深)。
    """
    chosen: list[dict] = []
    rest = list(pool)
    seen_brands: set[str] = set()
    while rest and len(chosen) < n:
        weights = [
            (max(1.0, float(it["pct"])) ** 2)
            * (boost if _is_staple(it, keywords) else 1.0)
            for it in rest
        ]
        picked = rng.choices(rest, weights=weights, k=1)[0]
        chosen.append(picked)
        brand = (picked["brand"] or "").strip().lower()
        if brand:
            seen_brands.add(brand)
        rest = [it for it in rest
                if it is not picked
                and (it["brand"] or "").strip().lower() not in seen_brands]
    # 写进正文时按折扣从深到浅排:第一样就是封面那样,标题点的也是它。
    chosen.sort(key=lambda it: (-it["pct"], -it["save_num"]))
    return chosen


def _render(picked: list[dict], pool: list[dict], *, day: date,
            threshold: int, relaxed: bool, pool_shown: int) -> str:
    lines = [
        f"数据日期：{day.year} 年 {day.month} 月 {day.day} 日"
        f"（Woolworths 全国特价接口，不分门店；特价每周三换新）",
        "",
        "正文里的每一个价格都只能从下面照抄。接口已经把现价、原价、折扣百分比和"
        "省多少四个数都给了 —— **一个都不要自己算**，折扣百分比尤其不要拿原价现价去除。",
        "商品名是接口给的英文原名，写进正文和卡片时翻成中文说清楚是什么，"
        "别照抄英文；但**不要改规格和价格**。",
        "",
        f"## 今天选中的 {len(picked)} 样（第一样是封面，标题点名的就是它）",
        "",
    ]
    for i, it in enumerate(picked, start=1):
        title = it["name"]
        lines += [
            f"{i}. {title}" + (f"（{it['size']}）" if it["size"] else ""),
            f"   现价 {it['now']}｜原价 {it['was']}｜{it['pct']}% OFF｜省 {it['save']}",
        ]
        if it["image"]:
            lines.append(f"   图：{it['image']}")
        lines.append("")

    others = [it for it in pool if it["sku"] not in {p["sku"] for p in picked}]
    others.sort(key=lambda it: (-it["pct"], -it["save_num"]))
    if others:
        lines += [
            "## 今天特价池里其余的（用来判断这批折扣值不值，不是让你另外编例子）",
            "",
        ]
        for it in others[:pool_shown]:
            lines.append(f"- {it['name']}"
                         + (f"（{it['size']}）" if it["size"] else "")
                         + f" {it['pct']}% OFF，{it['was']} → {it['now']}，省 {it['save']}")
        if len(others) > pool_shown:
            lines.append(f"- （还有 {len(others) - pool_shown} 样没列出来）")
        lines.append("")

    lines.append(f"候选门槛：{threshold}% OFF 以上，共 {len(pool)} 样。")
    if relaxed:
        # 悄悄降门槛等于悄悄换掉选品标准。写出来,让人看见今天这批本来就薄。
        lines.append(
            f"⚠︎ 今天 {DEFAULT_CONFIG['min_percent']}% 以上的不够挑，"
            f"门槛已放宽到 {threshold}% —— 这批折扣本来就不深，正文别写得像捡到大便宜。")
    return "\n".join(lines)


async def fetch(config: dict, *, exclude: list[str], seed: str | None = None) -> Facts:
    """拉一次。`exclude` 是最近几篇用过的 SKU,**最近的排在前面**(见下面见底时的兜底)。"""
    url = str(config.get("url") or DEFAULT_CONFIG["url"])
    headers = {**(config.get("headers") or {}), "User-Agent": _UA}
    timeout = float(config.get("timeout") or 30)
    pick_n = max(1, int(config.get("pick") or 3))
    pool_shown = max(0, int(config.get("pool_shown") or 12))
    image_width = int(config.get("image_width") or 1200)
    floor = int(config.get("min_percent") or 33)

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as cli:
            r = await cli.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise SourceError(f"连不上 Woolworths 特价接口：{exc}") from exc
    if r.status_code >= 400:
        raise SourceError(f"Woolworths 特价接口返回 {r.status_code}：{r.text[:200]}")
    try:
        raw = r.json()
        rows = (raw.get("products") or {}).get("items") or []
    except (ValueError, AttributeError) as exc:
        raise SourceError(
            f"返回的不是预期的 JSON（{exc}）。这个接口少了 x-requested-with 头就会"
            f"吐网页版内容 —— 先确认请求头还在。") from exc

    items = [it for it in (
        _item(row, image_width) for row in rows
        if isinstance(row, dict) and row.get("type") == "Product") if it]
    if not items:
        raise SourceError("接口通了但一条特价都没解析出来，先手动看一眼返回内容")

    # `exclude` 的约定是**最近的排在前面**。特价一周才换一次,而池子里够深的
    # 常常就七八样 —— 排除几篇之后必然见底。见底时不是「放弃去重、退回全池」,
    # 而是按「隔得最久的先回来」补齐:同样是重复,重复三天前写过的那样,
    # 总好过重复昨天那样。
    skip = list(dict.fromkeys(str(k) for k in (exclude or [])))
    skip_set = set(skip)
    fresh = [it for it in items if it["sku"] not in skip_set]
    reused = False
    if len(fresh) < pick_n:
        reused = True
        by_sku = {it["sku"]: it for it in items}
        for sku in reversed(skip):          # 最久没写的排在最前
            if len(fresh) >= pick_n * 3:    # 补到够抽就停,别把整周的都放回来
                break
            if (it := by_sku.get(sku)) is not None:
                fresh.append(it)

    # 门槛够不够挑。不够就一档一档往下放,而不是直接取全池 ——
    # 「今天没什么好折扣」本身是该让写手知道的事实。
    threshold, pool = floor, []
    for candidate in (floor, 25, 20, 15, 0):
        if candidate > floor:
            continue
        pool = [it for it in fresh if it["pct"] >= candidate]
        threshold = candidate
        if len(pool) >= pick_n:
            break

    rng = random.Random(seed) if seed else random.Random()
    picked = _pick(pool, pick_n, rng,
                   [str(k).lower() for k in (config.get("staple_keywords") or [])],
                   float(config.get("staple_boost") or 1.0))
    if not picked:
        raise SourceError("今天没有任何带折扣的商品，跳过这一篇")

    text = _render(picked, pool, day=date.today(), threshold=threshold,
                   relaxed=threshold < floor, pool_shown=pool_shown)
    if reused:
        text += ("\n⚠︎ 这一周的特价里没剩下没写过的了，上面有商品最近发过 —— "
                 "换个角度写，别把上一篇的说法再说一遍。")
    note = (f"{len(items)} 条特价，{threshold}% 以上 {len(pool)} 样，"
            f"抽中 {len(picked)} 样" + ("（有重复）" if reused else ""))
    return Facts(text=text, keys=[it["sku"] for it in picked], note=note)


SPEC = SourceSpec(
    name="woolworths_nz_specials",
    label="Woolworths NZ 本周特价",
    summary="拉 Woolworths 新西兰的公开特价接口，按折扣深度加权随机挑几样，"
            "自动避开最近几篇写过的商品。价格和商品图都照抄接口。",
    default_config=DEFAULT_CONFIG,
    fetch=fetch,
)
