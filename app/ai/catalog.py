"""卡片模板与视觉风格的清单。

模板的字段说明**只有一处来源**:每个模板旁边那份 `<name>.sample.json` 的
`_schema`。它同时喂给三个地方 —— 写进提示词、渲染预览、前端展示 ——
所以任何一处都不许再抄一份字段说明:抄了就会和模板本身漂移,
而漂移的表现是模型按旧说明填字段、渲染出一张缺块的卡,不报错。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

AI_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = AI_DIR / "templates"
THEMES_DIR = AI_DIR / "themes"

CANVAS_W = 1080
CANVAS_H = 1440
SCALE = 2

# 每篇的第一张。封面是读者唯一必看的一张,不留给模型决定。
COVER_TEMPLATE = "cover"


@dataclass(frozen=True)
class CardTemplate:
    name: str
    summary: str          # `_schema._card` —— 这张卡是干什么的
    schema: dict          # 完整字段说明,原样来自 sample.json
    sample: dict          # 一份能渲染的示例数据(预览用)

    def as_prompt_block(self) -> str:
        """写进提示词的那一段。字段说明原样给,不做压缩 ——
        它们写的是「几个字」「最多几条」「超了会怎样」,砍掉哪一句
        模型就会在那一处出错。"""
        lines = [f"### `{self.name}`", "", self.summary, "", "字段:"]
        for key, desc in self.schema.items():
            if key.startswith("_"):
                continue
            lines.append(f"- `{key}` —— {desc}")
        return "\n".join(lines)


@dataclass(frozen=True)
class Theme:
    name: str
    label: str
    one_liner: str
    good_for: str


def _read_sample(path: Path) -> tuple[dict, dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    schema = raw.get("_schema") if isinstance(raw.get("_schema"), dict) else {}
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    return schema, data


@lru_cache(maxsize=1)
def templates() -> dict[str, CardTemplate]:
    """所有可用卡片模板。下划线开头的是骨架(`_layout`)不是卡片。

    没有 sample.json 的模板**不进清单** —— 没有字段说明就没法写进提示词,
    模型只能瞎填。宁可它不存在。
    """
    out: dict[str, CardTemplate] = {}
    for path in sorted(TEMPLATES_DIR.glob("*.html.j2")):
        if path.name.startswith("_"):
            continue
        name = path.name.removesuffix(".html.j2")
        sample_path = TEMPLATES_DIR / f"{name}.sample.json"
        if not sample_path.exists():
            continue
        schema, data = _read_sample(sample_path)
        out[name] = CardTemplate(
            name=name,
            summary=str(schema.get("_card", "")).strip(),
            schema=schema,
            sample=data,
        )
    return out


def template_names() -> list[str]:
    return sorted(templates())


@lru_cache(maxsize=1)
def themes() -> dict[str, Theme]:
    index = THEMES_DIR / "index.yaml"
    if not index.exists():
        return {}
    raw = yaml.safe_load(index.read_text(encoding="utf-8")) or {}
    out: dict[str, Theme] = {}
    for d in raw.get("themes") or []:
        if not isinstance(d, dict) or not d.get("name"):
            continue
        name = str(d["name"]).strip()
        if not (THEMES_DIR / f"{name}.css").exists():
            continue
        out[name] = Theme(
            name=name,
            label=str(d.get("label", name)).strip(),
            one_liner=str(d.get("one_liner", "")).strip(),
            good_for=str(d.get("good_for", "")).strip(),
        )
    return out


def theme_css(name: str) -> str:
    """风格的 CSS。选了不存在的名字返回空串 —— 那只是回落到 base 的默认取向,
    不该让整次渲染失败。"""
    name = (name or "").strip()
    if not name:
        return ""
    path = THEMES_DIR / f"{name}.css"
    if not path.exists() or path.parent != THEMES_DIR:
        return ""
    return path.read_text(encoding="utf-8")


def base_css() -> str:
    return (TEMPLATES_DIR / "_base.css").read_text(encoding="utf-8")
