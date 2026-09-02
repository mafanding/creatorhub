"""AI 草稿的数据结构 + 发布前预检。

这一层的全部意义：**发出去之前就把能拦的都拦掉。**

小红书发一篇图文要几分钟(正文逐字输入 + 图片上传)，发完才被平台拒是最坏的
失败模式；而更贵的是发出去了才发现正文里有个网址 —— 那是限流甚至删号。
所以预检比平台自己更严格一点：我们接受的，平台不该再拒。

移植自 xhs-autonote 的 `models.py`，但砍掉了那边特有的东西
(选题库 topic_id / 数据快照新鲜度 / 项目级 never_publish)——
CreatorHub 里没有对应的输入，留着就是永远为空的死代码。
"""
from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import regex

# ── 平台硬上限 ──────────────────────────────────────────────────────────────
# 小红书:标题 20 字符 / 40 显示单位,正文 1000 字,图集 18 张。
# 其余平台的标题上限各不相同,但 main.py 的 add_publish 对所有平台都做
# `title[:20]`,所以 20 就是这套流水线实际能发出去的口径 —— 按它来预检,
# 免得生成一个 26 字的标题、预检放行、入库时被悄悄截断成半句话。
XHS_TITLE_MAX_WIDTH = 40   # 显示单位:中日韩全角 = 2
XHS_TITLE_MAX_CHARS = 20
XHS_CONTENT_MAX_CHARS = 1000
XHS_MAX_IMAGES = 18

# ── 我们自己那份更紧的预算 ──────────────────────────────────────────────────
# 留出余量是因为「显示宽度」各家算法有细微出入(emoji 的分组、变体选择符),
# 卡在硬上限上时一个字的偏差就是一次失败发布。
SAFE_TITLE_MAX_WIDTH = 38
SAFE_CONTENT_MAX_CHARS = 950
MIN_IMAGES = 1

_GRAPHEME_RE = regex.compile(r"\X")
_EXTENDED_PICTOGRAPHIC_RE = regex.compile(r"\p{Extended_Pictographic}")
_DEFAULT_IGNORABLE_RE = regex.compile(r"^\p{Default_Ignorable_Code_Point}$")

# 风险词分两档,因为**适用范围不同**。
#
# HARD —— 站外导流和违规业务。**卡片上的文字也查**:平台会 OCR 配图,
#         图里的网址和正文里的网址是同一个问题。这一档宁可误伤。
HARD_PATTERNS: list[tuple[str, str]] = [
    (r"https?://", "不能出现网址,平台会限流甚至删帖"),
    (r"www\.", "不能出现网址"),
    (r"\.(com|cn|net|org|io|dev|workers\.dev)\b", "不能出现域名"),
    # 中间那个 (我|你|个|一?下)* 是必须的:真人写的是「加我微信」「留个微信」,
    # 不是「加微信」。少了它,最典型的那句引流话术直接穿过这道闸。
    # 动词里**故意不放「发」**:「发微信」「发微信朋友圈」是正常中文,不是引流。
    (r"(加|私|扣|留)\s*(我|你|个|一?下)*\s*(微信|weixin|WeChat|wechat|vx|VX|v信|威信|QQ|qq)",
     "不能出现加微信/QQ 等引流话术"),
    (r"(微信|VX|vx|v信)\s*[:：]", "不能出现联系方式"),
    # 「微信号 kaquan2026」—— 没有动词也没有冒号,但后面跟一串像账号的东西。
    #
    # 这条的每个限制都是**防误伤**,不是防漏:
    # * 后面那串必须含数字或 _ -。不然「微信 Woolworths 的卡」会被判成微信号。
    # * 空格只允许 0-2 个半角/全角,**不跨行**。`\s*` 会跨换行,于是
    #   「……用微信\nWoolworths 那张」也中招。
    # * 长度 5 位起,放过「微信支付」「微信立减金 20」。
    (r"(微信|weixin|wechat|WeChat|v信|威信)[ \t　]{0,2}(号|ID|id)?[ \t　]{0,2}[:：]?[ \t　]{0,2}"
     r"(?=[A-Za-z0-9_-]{5,})[A-Za-z]*[0-9_-][A-Za-z0-9_-]*",
     "疑似微信号,不能出现在公开笔记里"),
    (r"扫码|二维码", "不能引导扫码"),
    (r"(点击|戳)\s*(链接|下方链接|下面链接)", "不能引导点外链"),
    (r"稳赚|保本|无风险|包过|必下卡", "金融类违规承诺"),
    (r"代还|套现|养卡|提额技术|大额下卡", "信用卡违规业务词,必封"),
    # 素材可能来自你自己的账号截图,个人信息会顺着提示词流进文案和卡片。
    (r"\*{2,}\s*\d{3,4}", "疑似银行卡尾号,不能出现在公开笔记里"),
    (r"\d{4}\s*\*{4,}|\d{6,}\*{2,}", "疑似卡号,不能出现在公开笔记里"),
]

# SOFT —— 广告法极限词和营销腔。只查标题/正文/标签这些**成段的自然语言**,
#         不查卡片数据:卡片里全是「满 300 减 60」这种短标签,
#         在那儿做语气判断只会误伤。
#
#         「第一」故意写成 (全网|全国|业内|行业)第一 而不是裸的「第一」——
#         「第一步」「第一次」是正常中文。
SOFT_PATTERNS: list[tuple[str, str]] = [
    (r"免费领取|限时免费|无套路", "疑似营销话术,容易被判广告"),
    (r"(全网|全国|业内|行业)第一|第一名|排名第一|最好用|最强|绝对|100%|百分百",
     "极限词/绝对化用语,广告法风险"),
]

BANNED_PATTERNS: list[tuple[str, str]] = [*HARD_PATTERNS, *SOFT_PATTERNS]


def display_width(text: str) -> int:
    """按平台口径算显示宽度:中日韩/全角 = 2,emoji = 2,其余 = 1。

    组合字符、变体选择符、零宽连接符都不计宽 —— 一个带肤色的 emoji 是
    「一个字」不是四个,按码位数去数会把一个合法标题判成超长。
    """
    width = 0
    for cluster in _GRAPHEME_RE.findall(text):
        if not cluster:
            continue
        if _EXTENDED_PICTOGRAPHIC_RE.search(cluster) and not cluster.isascii():
            width += 2
            continue
        ch = cluster[0]
        if _DEFAULT_IGNORABLE_RE.match(ch) or unicodedata.combining(ch):
            continue
        cat = unicodedata.category(ch)
        if cat in {"Cc", "Cf", "Mn", "Me"}:
            continue
        width += 2 if unicodedata.east_asian_width(ch) in {"W", "F"} else 1
    return width


@dataclass
class Card:
    """一张配图 = 模板名 + 它的数据。字段 schema 见同名 `.sample.json` 的 `_schema`。"""

    template: str
    data: dict

    @staticmethod
    def from_dict(d: dict) -> "Card":
        data = d.get("data")
        return Card(template=str(d.get("template", "")).strip(),
                    data=data if isinstance(data, dict) else {})

    def to_dict(self) -> dict:
        return {"template": self.template, "data": self.data}


@dataclass
class Draft:
    """一篇待发的图文:正文 + 一组卡片。"""

    title: str = ""
    content: str = ""
    tags: list[str] = field(default_factory=list)
    cards: list[Card] = field(default_factory=list)
    notes: str = ""          # 模型自己记的一句:为什么这么写、风险在哪

    @staticmethod
    def from_dict(d: dict) -> "Draft":
        tags = d.get("tags")
        if isinstance(tags, str):                      # 模型偶尔给成 "a,b,c"
            tags = [t for t in regex.split(r"[,，、\s]+", tags) if t]
        cards = d.get("cards")
        return Draft(
            title=str(d.get("title", "") or "").strip(),
            content=str(d.get("content", "") or ""),
            tags=[str(t).strip().lstrip("#") for t in (tags or []) if str(t).strip()],
            cards=[Card.from_dict(c) for c in (cards or []) if isinstance(c, dict)],
            notes=str(d.get("notes", "") or "").strip(),
        )

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "content": self.content,
            "tags": self.tags,
            "cards": [c.to_dict() for c in self.cards],
            "notes": self.notes,
        }

    @staticmethod
    def load(path: Path) -> "Draft":
        return Draft.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def dump(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")


def validate_draft(
    draft: Draft,
    image_paths: list[Path] | None = None,
    *,
    known_templates: set[str] | frozenset[str] | None = None,
    cards_range: tuple[int, int] | None = None,
    banned_extra: tuple[str, ...] = (),
) -> list[str]:
    """返回一串人能看懂的问题。空列表 = 可以发。

    这份清单会**原样回喂给模型**让它自己改,所以每条都要写清「错在哪、
    该改成什么」—— 只说「标题过长」而不给上限,模型改一次还是超。

    * `known_templates` —— 认识的卡片模板名。模型编一个不存在的模板名时,
      渲染那一步才会炸,而那时已经烧掉一次生成的钱了。
    * `banned_extra` —— 这个人设自己那串不能公开的字串(真实店名、单号)。
      通用风险词拦的是有模式的东西,这些没有模式,只能按人设列。
    """
    problems: list[str] = []

    # ── 标题 ─────────────────────────────────────────────────────────────
    title = draft.title.strip()
    if not title:
        problems.append("标题为空")
    else:
        w = display_width(title)
        if w > SAFE_TITLE_MAX_WIDTH:
            problems.append(
                f"标题过长:{w} 显示单位(安全上限 {SAFE_TITLE_MAX_WIDTH},"
                f"平台硬上限 {XHS_TITLE_MAX_WIDTH})—— 中文 1 字算 2 单位,"
                f"也就是纯中文最多 19 个字"
            )
        if len(title) > XHS_TITLE_MAX_CHARS:
            problems.append(f"标题字符数 {len(title)} 超过 {XHS_TITLE_MAX_CHARS}(入库时会被截断)")
        if "\n" in title:
            problems.append("标题里不能有换行")

    # ── 正文 ─────────────────────────────────────────────────────────────
    content = draft.content
    if not content.strip():
        problems.append("正文为空")
    if len(content) > SAFE_CONTENT_MAX_CHARS:
        problems.append(
            f"正文 {len(content)} 字,超过安全上限 {SAFE_CONTENT_MAX_CHARS}"
            f"(硬上限 {XHS_CONTENT_MAX_CHARS})"
        )

    text = f"{title}\n{content}\n{' '.join(draft.tags)}"
    for pattern, why in BANNED_PATTERNS:
        m = regex.search(pattern, text)
        if m:
            problems.append(f"正文/标题命中风险词 “{m.group(0)}”:{why}")

    everything = f"{text}\n" + "\n".join(
        json.dumps(c.data, ensure_ascii=False) for c in draft.cards)
    for secret in banned_extra:
        if secret and secret in everything:
            problems.append(f"出现了人设里声明「不能公开」的字串 “{secret}”")

    # 卡片上的文字也要查 —— 平台会 OCR 配图。只用 HARD 档:卡片数据是
    # 「满 300 减 60」这类短标签,对它做语气判断纯属误伤。
    # **不给任何键开豁免。** 上游那套流水线放过了 image / photo,因为那两个是
    # 渲染器的输入(远程图地址)而不是卡上的字;这里的模板一个都不吃远程图,
    # 留着豁免只会变成一个「把网址藏在这个键名下就能穿过预检」的洞。
    for i, card in enumerate(draft.cards, start=1):
        blob = json.dumps(card.data, ensure_ascii=False)
        for pattern, why in HARD_PATTERNS:
            m = regex.search(pattern, blob)
            if m:
                problems.append(f"第 {i} 张卡({card.template})命中风险词 “{m.group(0)}”:{why}")

    # ── 标签 ─────────────────────────────────────────────────────────────
    if not draft.tags:
        problems.append("没有话题标签,自然流量会差很多")
    if len(draft.tags) > 10:
        problems.append(f"标签 {len(draft.tags)} 个,超过 10 个观感像营销号")
    for t in draft.tags:
        if t.startswith("#"):
            problems.append(f"标签 “{t}” 不要带 # 前缀,发布时会自己加")

    # ── 配图 ─────────────────────────────────────────────────────────────
    if not draft.cards:
        problems.append("没有配图卡片")
    if len(draft.cards) > XHS_MAX_IMAGES:
        problems.append(f"卡片 {len(draft.cards)} 张,超过 {XHS_MAX_IMAGES} 张上限")

    if known_templates is not None:
        for i, card in enumerate(draft.cards, start=1):
            if card.template not in known_templates:
                problems.append(
                    f"第 {i} 张卡用了不存在的模板 “{card.template}”,"
                    f"只能从这些里选:{'、'.join(sorted(known_templates))}"
                )

    # 张数是**范围**不是定值:允许为了内容多一张少一张,但挡住「永远 4 张」
    # 这种一眼可辨的机器特征,也挡住只出 1 张的敷衍产出。
    if cards_range is not None:
        lo, hi = cards_range
        if not lo <= len(draft.cards) <= hi:
            problems.append(f"卡片 {len(draft.cards)} 张,超出这次要求的 {lo}-{hi} 张")

    if image_paths is not None:
        if len(image_paths) < MIN_IMAGES:
            problems.append("渲染出来的图片数量为 0")
        if len(image_paths) != len(draft.cards):
            problems.append(f"卡片 {len(draft.cards)} 张但渲染出 {len(image_paths)} 张图,对不上")
        for p in image_paths:
            if not p.exists():
                problems.append(f"图片不存在:{p}")
            elif p.stat().st_size == 0:
                problems.append(f"图片是空文件:{p}")

    return problems


def coerce_draft(payload: Any) -> Draft:
    """把模型返回的东西变成 Draft。不是 dict 就抛 ValueError。"""
    if not isinstance(payload, dict):
        raise ValueError(f"模型返回的不是一个 JSON 对象,而是 {type(payload).__name__}")
    return Draft.from_dict(payload)
