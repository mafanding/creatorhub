"""取数配方的执行:一份配置 → 一段素材文本。

**这一层里没有任何跟具体站点有关的东西。** 站点特有的部分全在配方里
(`items_path` / `fields` / `line`),而配方是配置,可以是人写的,也可以是模型
看着一段返回样本写的。加一个新项目 = 加一份配方,不加代码。

三件事:

* `extract` —— 按配方从原始数据里取出一串条目。**取值是代码做的**,
  模型碰的是字段路径,不是价格。
* `select`  —— 挑今天写哪几样:折扣深度加权随机 + 日常品加权 + 同类去重
  + 排除最近写过的。为什么不丢给模型:见下面 `select` 的注释。
* `render`  —— 拼成人和模型都读得懂的素材文本。
"""
from __future__ import annotations

import random
from datetime import date

from ..paths import evaluate, evaluate_fields

# 一条记录必须有的两样:一个稳定的 id(拿来去重)和一个名字。缺哪个都没法用。
REQUIRED_FIELDS = ("id", "name")

DEFAULT_LINE = "{name} —— {now}"
DEFAULT_INTRO = (
    "正文里的每一个数字都只能从下面照抄，**一个都不要自己算**。"
    "名字如果是英文原名，写进正文和卡片时翻成中文说清楚是什么，但不要改规格和数字。"
)


class RecipeError(ValueError):
    """配方本身有问题(路径写错、取不出条目)。消息要能直接给人看。"""


def extract(payload, recipe: dict) -> list[dict]:
    """按配方取出条目。取不到就抛错 —— 静默返回空列表会变成一篇没有数据的稿子。"""
    fields = recipe.get("fields") or {}
    if not isinstance(fields, dict) or not fields:
        raise RecipeError("配方里没有 fields，不知道该取哪些字段")
    missing = [k for k in REQUIRED_FIELDS if k not in fields]
    if missing:
        raise RecipeError(f"配方的 fields 里缺少必填项:{'、'.join(missing)}")

    items_path = str(recipe.get("items_path") or "").strip()
    rows = evaluate(payload, items_path) if items_path else payload
    if isinstance(rows, dict):
        rows = list(rows.values())
    if not isinstance(rows, list):
        raise RecipeError(
            f"items_path `{items_path or '(整个返回)'}` 取到的不是一个列表，"
            f"而是 {type(rows).__name__} —— 配方的 items_path 写错了")

    keep = recipe.get("keep") if isinstance(recipe.get("keep"), dict) else {}
    score_expr = str(recipe.get("score") or "")

    out: list[dict] = []
    for row in rows:
        if keep and any(evaluate(row, k) != v for k, v in keep.items()):
            continue
        got = evaluate_fields(row, fields)
        got.pop("_errors", None)     # 单个字段取不到不致命,整条能用就用
        if not str(got.get("id") or "").strip() or not str(got.get("name") or "").strip():
            continue
        got["id"] = str(got["id"])
        try:
            got["_score"] = float(evaluate(row, score_expr) or 0) if score_expr else 0.0
        except (TypeError, ValueError):
            got["_score"] = 0.0
        out.append(got)

    if not out:
        raise RecipeError(
            "按这份配方一条记录都没取出来。多半是 items_path 或 fields 里的路径"
            "跟这个接口对不上了 —— 让 AI 重新认一次，或者自己看一眼返回内容")
    return out


def _boosted(item: dict, keywords: list[str]) -> bool:
    blob = " ".join(str(v) for k, v in item.items() if not k.startswith("_")).lower()
    return any(k and k in blob for k in keywords)


def select(items: list[dict], recipe: dict, *, exclude: list[str],
           seed: str | None = None) -> tuple[list[dict], list[dict], int, bool, bool]:
    """挑今天写哪几样。返回 (选中的, 候选池, 实际门槛, 是否放宽了, 是否有重复)。

    **为什么选品不丢给模型。** 很多数据源(超市特价是典型)一周才换一次,而帖子
    每天发。把一周不变的池子原样交出去,每天挑出来的都是「最狠的那几个」,
    于是一周七篇长得几乎一样 —— 这是一眼能看出来的机器特征。所以这里做三件事:

    * **按分数加权随机**(权重 `score²`),不是取前 N 名。分高的仍然更容易中,
      但不保证 —— 不确定性正是要的东西。
    * **关键词加权**。深折扣天然扎堆在某几类商品上(超市里就是零食和糖果),
      纯按分数抽经常抽出一整篇「今天全是饼干」。命中 `boost_keywords` 的乘一个
      系数。**这是加权不是过滤** —— 分最高的那样仍然该被写。
    * **同类只取一样**(`unique_by`,比如同一个品牌)。加权随机最容易犯的错就是
      一次抽中同一个牌子的两种口味,读者看到的是同一件事说了两遍。
    """
    pick_n = max(1, int(recipe.get("pick") or 3))
    floor = int(recipe.get("min_score") or 0)
    keywords = [str(k).lower() for k in (recipe.get("boost_keywords") or [])]
    boost = float(recipe.get("boost") or 1.0)
    unique_by = str(recipe.get("unique_by") or "").strip()

    # `exclude` 的约定是**最近的排在前面**。池子常常就那么几样,排除几篇之后
    # 必然见底 —— 见底时不是「放弃去重、退回全池」,而是按「隔得最久的先回来」
    # 补齐:同样是重复,重复三天前写过的那样,总好过重复昨天那样。
    skip = list(dict.fromkeys(str(k) for k in (exclude or [])))
    skip_set = set(skip)
    fresh = [it for it in items if it["id"] not in skip_set]
    reused = False
    if len(fresh) < pick_n:
        reused = True
        by_id = {it["id"]: it for it in items}
        for key in reversed(skip):
            if len(fresh) >= pick_n * 3:
                break
            if (it := by_id.get(key)) is not None:
                fresh.append(it)

    # 门槛不够挑就一档一档往下放,而不是直接取全池 ——
    #「今天没什么好货」本身是该让写手知道的事实(render 会写出来)。
    threshold, pool = floor, []
    for candidate in (floor, floor * 0.75, floor * 0.5, 0):
        candidate = int(candidate)
        if candidate > floor:
            continue
        pool = [it for it in fresh if it["_score"] >= candidate]
        threshold = candidate
        if len(pool) >= pick_n:
            break
    if not pool:
        raise RecipeError("今天一条能用的记录都没有")

    rng = random.Random(seed) if seed else random.Random()
    chosen: list[dict] = []
    rest = list(pool)
    seen: set[str] = set()
    while rest and len(chosen) < pick_n:
        weights = [
            (max(1.0, float(it["_score"])) ** 2) * (boost if _boosted(it, keywords) else 1.0)
            for it in rest
        ]
        picked = rng.choices(rest, weights=weights, k=1)[0]
        chosen.append(picked)
        mark = str(picked.get(unique_by) or "").strip().lower() if unique_by else ""
        if mark:
            seen.add(mark)
        rest = [it for it in rest if it is not picked
                and (not unique_by
                     or str(it.get(unique_by) or "").strip().lower() not in seen)]

    chosen.sort(key=lambda it: -it["_score"])
    return chosen, pool, threshold, threshold < floor, reused


def _line(item: dict, template: str) -> str:
    """一条记录渲成一行。模板里写错字段名不该炸掉整次拉取 ——
    留个 `{字段名?}` 在那儿,人一眼就看得见是哪个字段没取到。"""
    class _Safe(dict):
        def __missing__(self, key):
            return "{" + key + "?}"

    try:
        return template.format_map(_Safe(
            {k: v for k, v in item.items() if not k.startswith("_")}))
    except (ValueError, IndexError) as exc:
        raise RecipeError(f"line 模板写错了({exc}):{template!r}") from exc


def render(chosen: list[dict], pool: list[dict], recipe: dict, *,
           day: date, threshold: int, relaxed: bool, reused: bool) -> str:
    line_tpl = str(recipe.get("line") or DEFAULT_LINE)
    image_field = str(recipe.get("image_field") or "image")
    pool_shown = max(0, int(recipe.get("pool_shown") or 10))
    label = str(recipe.get("label") or "今天的数据")
    caveat = str(recipe.get("caveat") or "")

    lines = [f"数据日期：{day.year} 年 {day.month} 月 {day.day} 日"
             + (f"（{caveat}）" if caveat else ""), ""]
    lines += [str(recipe.get("intro") or DEFAULT_INTRO), ""]
    lines += [f"## {label} · 选中的 {len(chosen)} 样（第一样是封面，标题点名的就是它）", ""]
    for i, it in enumerate(chosen, start=1):
        lines.append(f"{i}. {_line(it, line_tpl)}")
        if (img := str(it.get(image_field) or "")).startswith(("http://", "https://")):
            lines.append(f"   图：{img}")
        lines.append("")

    picked_ids = {it["id"] for it in chosen}
    others = sorted((it for it in pool if it["id"] not in picked_ids),
                    key=lambda it: -it["_score"])
    if others and pool_shown:
        lines += ["## 其余候选（用来判断这批值不值，不是让你另外编例子）", ""]
        lines += [f"- {_line(it, line_tpl)}" for it in others[:pool_shown]]
        if len(others) > pool_shown:
            lines.append(f"- （还有 {len(others) - pool_shown} 样没列出来）")
        lines.append("")

    lines.append(f"候选门槛：分数 {threshold} 以上，共 {len(pool)} 样。")
    if relaxed:
        # 悄悄降门槛等于悄悄换掉选品标准。写出来,让人看见今天这批本来就薄。
        lines.append(f"⚠︎ 今天够格的不够挑，门槛已放宽到 {threshold} —— "
                     f"这批本来就不算好，正文别写得像捡到大便宜。")
    if reused:
        lines.append("⚠︎ 没剩下没写过的了，上面有条目最近发过 —— 换个角度写，"
                     "别把上一篇的说法再说一遍。")
    return "\n".join(lines)


def run(payload, recipe: dict, *, exclude: list[str],
        seed: str | None = None, day: date | None = None) -> tuple[str, list[str], str]:
    """配方 + 原始数据 → (素材文本, 用掉的条目 id, 一句进度)。"""
    items = extract(payload, recipe)
    chosen, pool, threshold, relaxed, reused = select(
        items, recipe, exclude=exclude, seed=seed)
    text = render(chosen, pool, recipe, day=day or date.today(),
                  threshold=threshold, relaxed=relaxed, reused=reused)
    note = (f"{len(items)} 条记录，够格的 {len(pool)} 样，抽中 {len(chosen)} 样"
            + ("（有重复）" if reused else ""))
    return text, [it["id"] for it in chosen], note
