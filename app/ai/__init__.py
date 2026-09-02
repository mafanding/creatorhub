"""AI 创作 —— 用提示词 + API 生成一篇图文笔记(正文 + 配图卡片)。

搬自 xhs-autonote,但**只保留提示词 + API 这一种形态**:那边还有一套
命令行通道(claude / codex / gemini 的 CLI),它们要装对应的工具、要登录态、
要一个能跑几百步工具循环的文件系统 —— 在一个双击就能起的本地面板里,
那是一条永远有人配不通的路。

一次创作的全过程:

```
人设(AiBrand) + 一句方向 + 真实素材
  ↓  prompts.build_user_prompt
一次 API 调用 → 一段 JSON(标题 / 正文 / 标签 / 卡片数据)
  ↓  draft.validate_draft —— 字数、风险词、模板名、张数
不过就把问题原样回喂重写(最多 3 轮)
  ↓  render.render_cards —— Jinja2 模板 + 无头 Chromium → 1080×1440 PNG
  ↓  再验一次(这次带上图:空文件、张数对不上只有这时候看得出来)
AiDraft(ready) —— 摆在面板上,人看过之后一键转成发布任务
```

**图片不是文生图。** 模型只填卡片数据,排版由模板负责 —— 卡片上全是中文
密排的表格和金额,版式引擎每次都能排得分毫不差,扩散模型做不到。
"""
from __future__ import annotations

from .catalog import CANVAS_H, CANVAS_W, template_names, templates, themes
from .client import PROVIDERS, AiChannel, AiError, channel_from_settings
from .draft import Card, Draft, display_width, validate_draft
from .writer import write_draft

__all__ = [
    "CANVAS_H", "CANVAS_W", "PROVIDERS",
    "AiChannel", "AiError", "Card", "Draft",
    "channel_from_settings", "display_width", "template_names", "templates",
    "themes", "validate_draft", "write_draft",
]
