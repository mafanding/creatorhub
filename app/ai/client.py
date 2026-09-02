"""和大模型说话的通道 —— **只有提示词 + API 两种形态,没有 CLI**。

两条路:

* `openai` —— 任何讲 `/chat/completions` 的端点(OpenAI、DeepSeek、通义、
  月之暗面、本地 Ollama/vLLM,以及各种网关)。用 httpx 直接发,
  项目本来就依赖 httpx,不为一个可选通道再引一个 SDK。
* `anthropic` —— 官方 SDK 直连。**不要拿 openai 通道去连 Claude**:
  兼容层会丢掉思考和缓存语义,而且 Claude 4.6 以后 `temperature` 是被
  拒绝的参数(400),兼容层会照发不误。

这一层只管「把提示词送出去、把 JSON 拿回来」。**判断产出好不好不归它管** ——
那是 `draft.validate_draft` 的事:模型说「我写好了」不算数。

自动评论那条老路(`engine/compose.generate`)不走这里,它有自己的一套设置,
两边互不影响 —— 这样你可以让评论用便宜模型、让写稿用贵的。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import httpx

from ..settings import get_setting

PROVIDER_OPENAI = "openai"
PROVIDER_ANTHROPIC = "anthropic"
PROVIDERS = (PROVIDER_OPENAI, PROVIDER_ANTHROPIC)

ANTHROPIC_DEFAULT_BASE = "https://api.anthropic.com"
# 写稿是「一次想清楚」的活,不是聊天 —— 默认给能力最强的那一档。
# 钉别名而不是带日期的快照名:后者会过期,而过期时报的错读起来像「你没权限」。
ANTHROPIC_DEFAULT_MODEL = "claude-opus-5"


class AiError(RuntimeError):
    """通道层面的失败:没配置、连不上、返回读不了。带一句人能看懂的原因。"""


@dataclass
class AiChannel:
    """一次调用要用的全部东西。"""

    provider: str = PROVIDER_OPENAI
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    max_tokens: int = 16000
    timeout: float = 300.0
    temperature: float = 0.8      # 仅 openai 通道生效,见 complete_json
    extra_headers: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model or '(未填模型)'}"

    def check(self) -> str | None:
        """能用吗。None = 能用,否则返回一句能直接显示给人的原因。"""
        if self.provider not in PROVIDERS:
            return f"未知的通道类型 {self.provider!r},只能是 openai 或 anthropic"
        if not self.model:
            return "没填模型名"
        if self.provider == PROVIDER_ANTHROPIC:
            return None if self.api_key else "没填 API Key"
        if not self.base_url:
            return "没填接口地址,比如 https://api.deepseek.com/v1"
        # 本地端点(ollama / vLLM)通常不需要 key,所以只在远程时强制要
        if not self.api_key and not any(
                h in self.base_url for h in ("localhost", "127.0.0.1")):
            return "没填 API Key"
        return None


def channel_from_settings(overrides: dict | None = None) -> AiChannel:
    """按「内容创作」那组设置装一个通道出来。

    每一项都可以留空回落到自动评论那组设置 —— 大多数人只有一个网关,
    让他为了写稿再填一遍同样的 base_url 和 key 是没道理的。
    但**分成两组是有意的**:写稿值得用贵模型,评论不值得。
    """
    o = overrides or {}

    def pick(key: str, *fallbacks: str, default: str = "") -> str:
        if o.get(key) not in (None, ""):
            return str(o[key]).strip()
        for k in (key, *fallbacks):
            v = get_setting(k, "").strip()
            if v:
                return v
        return default

    provider = pick("ai_content_provider", default=PROVIDER_OPENAI)
    if provider not in PROVIDERS:
        provider = PROVIDER_OPENAI

    base_default = ANTHROPIC_DEFAULT_BASE if provider == PROVIDER_ANTHROPIC else ""
    model_default = ANTHROPIC_DEFAULT_MODEL if provider == PROVIDER_ANTHROPIC else ""

    # key 特殊:overrides 里留空表示「用已存的」,不是「清空」——
    # 前端那个密码框永远回显不出已保存的 key,留空提交是常态。
    key = str(o.get("ai_content_api_key") or "").strip()
    if not key:
        key = get_setting("ai_content_api_key", "").strip() or get_setting("ai_api_key", "").strip()

    def as_int(raw: str, default: int) -> int:
        try:
            return max(1, int(float(raw)))
        except (TypeError, ValueError):
            return default

    def as_float(raw: str, default: float) -> float:
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default

    return AiChannel(
        provider=provider,
        base_url=pick("ai_content_base_url", "ai_base_url", default=base_default).rstrip("/"),
        api_key=key,
        model=pick("ai_content_model", "ai_model", default=model_default),
        max_tokens=as_int(pick("ai_content_max_tokens"), 16000),
        timeout=as_float(pick("ai_content_timeout"), 300.0),
        temperature=as_float(pick("ai_content_temperature", "ai_temperature"), 0.8),
    )


# ── 从模型那段话里把 JSON 抠出来 ────────────────────────────────────────────
_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)


def extract_json(text: str) -> dict:
    """模型很少只回一个干净的 JSON。按宽松到严格的顺序试。

    **不能只 `json.loads(text)` 完事**:网关五花八门,有的会包 ```json 围栏、
    有的会在前面加一句「好的,这是你要的草稿:」。一次生成要花几十秒和真金白银,
    为了一对反引号整轮作废是最没必要的失败。
    """
    text = (text or "").strip()
    if not text:
        raise AiError("模型什么都没返回")

    candidates: list[str] = [text]
    m = _FENCE_RE.search(text)
    if m:
        candidates.insert(0, m.group(1))
    # 最外层那对花括号。贪婪匹配到最后一个 } —— 嵌套对象要整个拿走。
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])

    for c in candidates:
        try:
            value = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise AiError(f"模型返回的不是 JSON,读不了。开头是:{text[:200]}")


# ── 调用 ─────────────────────────────────────────────────────────────────────
async def complete_json(
    channel: AiChannel,
    system: str,
    user: str,
    *,
    history: list[dict] | None = None,
) -> tuple[dict, dict]:
    """要一段 JSON 回来。返回 (解析出来的 dict, 用量统计)。

    `history` 是之前几轮的原始消息(assistant 的回答 + 我们给的预检问题),
    自检重跑时把它带上 —— 让模型看见自己上一版写了什么,比让它从头再写一遍
    更容易改对,也省钱。
    """
    if (why := channel.check()) is not None:
        raise AiError(why)
    if channel.provider == PROVIDER_ANTHROPIC:
        text, usage = await _anthropic_complete(channel, system, user, history or [])
    else:
        text, usage = await _openai_complete(channel, system, user, history or [])
    return extract_json(text), usage


async def complete_text(channel: AiChannel, system: str, user: str) -> tuple[str, dict]:
    """要一段纯文本。给「测试连通性」用。"""
    if (why := channel.check()) is not None:
        raise AiError(why)
    if channel.provider == PROVIDER_ANTHROPIC:
        return await _anthropic_complete(channel, system, user, [])
    return await _openai_complete(channel, system, user, [])


async def _openai_complete(
    channel: AiChannel, system: str, user: str, history: list[dict],
) -> tuple[str, dict]:
    messages = [{"role": "system", "content": system}, *history,
                {"role": "user", "content": user}]
    body = {
        "model": channel.model,
        "messages": messages,
        "max_tokens": channel.max_tokens,
        "temperature": channel.temperature,
    }
    headers = {"Content-Type": "application/json", **channel.extra_headers}
    if channel.api_key:
        headers["Authorization"] = f"Bearer {channel.api_key}"

    url = f"{channel.base_url}/chat/completions"
    async with httpx.AsyncClient(timeout=httpx.Timeout(channel.timeout, connect=20.0)) as cli:
        # response_format 能显著提高「返回的确实是 JSON」的概率,但**不是所有
        # 兼容端点都认识它**(本地 vLLM、部分网关会直接 400)。所以试一次,
        # 被拒了就退回不带它再发 —— 反正 extract_json 本来就兜得住。
        r = await _post(cli, url, headers, {**body, "response_format": {"type": "json_object"}})
        if r.status_code == 400:
            r = await _post(cli, url, headers, body)

    if r.status_code >= 400:
        raise AiError(f"HTTP {r.status_code}:{r.text[:300]}")
    try:
        payload = r.json()
        choice = payload["choices"][0]["message"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise AiError(f"返回读不了({exc}):{r.text[:300]}") from exc

    u = payload.get("usage") or {}
    usage = {"input": u.get("prompt_tokens", 0) or 0,
             "output": u.get("completion_tokens", 0) or 0}
    content = choice.get("content")
    if isinstance(content, list):   # 少数端点回的是内容块数组
        content = "".join(str(b.get("text", "")) for b in content if isinstance(b, dict))
    text = (content or "").strip()
    finish = (payload.get("choices") or [{}])[0].get("finish_reason") or "?"
    # **撞上限要单独报。** 截断的 JSON 到了 extract_json 那里只会变成
    # 「模型返回的不是 JSON」—— 那句话把人引向「模型不听话」,而真正要做的
    # 只是把「最大输出长度」调大。一篇笔记加六张卡的 JSON 有 6-12k。
    if finish == "length":
        raise AiError(f"输出被截断(finish_reason=length,已出 {usage['output']} tokens)。"
                      f"去「设置 → AI 内容创作」把「最大输出长度」调大")
    if not text:
        raise AiError(f"模型返回空内容(finish_reason={finish})")
    return text, usage


async def _post(cli: httpx.AsyncClient, url: str, headers: dict, body: dict):
    try:
        return await cli.post(url, headers=headers, json=body)
    except httpx.HTTPError as exc:
        raise AiError(f"连不上 {url}:{exc}") from exc


async def _anthropic_complete(
    channel: AiChannel, system: str, user: str, history: list[dict],
) -> tuple[str, dict]:
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - 取决于装没装
        raise AiError("没装 anthropic 库,跑 `pip install anthropic`,"
                      "或者把通道切回 OpenAI 兼容接口") from exc

    kwargs: dict = {"api_key": channel.api_key, "timeout": channel.timeout}
    if channel.base_url and channel.base_url != ANTHROPIC_DEFAULT_BASE:
        kwargs["base_url"] = channel.base_url
    client = anthropic.AsyncAnthropic(**kwargs)

    # **不要传 temperature。** Claude 4.6 之后这个参数被移除了,传了直接 400,
    # 而错误信息读起来像是模型名不对 —— 排查会很久。
    try:
        msg = await client.messages.create(
            model=channel.model,
            max_tokens=channel.max_tokens,
            system=system,
            messages=[*history, {"role": "user", "content": user}],
        )
    except Exception as exc:  # noqa: BLE001 - 任何失败都要变成一句人话
        raise AiError(f"{type(exc).__name__}: {str(exc)[:300]}") from exc
    finally:
        await client.close()

    stop = getattr(msg, "stop_reason", "") or "?"
    if stop == "refusal":
        raise AiError("模型拒绝了这个请求(stop_reason=refusal),换个方向或改一下人设描述")

    # 开着思考时第一块可能是 thinking,只取 text 块。
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
    u = getattr(msg, "usage", None)
    usage = {"input": getattr(u, "input_tokens", 0) or 0,
             "output": getattr(u, "output_tokens", 0) or 0}
    # 撞上限要单独报,理由同 openai 那条。这条通道尤其容易撞:Claude 默认
    # 开着自适应思考,而**思考的 token 也算进 max_tokens** —— 6-12k 的 JSON
    # 加上思考,很容易在默认的 16000 上被切断,而切断的 JSON 只会表现成
    # 「模型返回的不是 JSON」。
    if stop == "max_tokens":
        raise AiError(f"输出被截断(stop_reason=max_tokens,已出 {usage['output']} tokens)。"
                      f"去「设置 → AI 内容创作」把「最大输出长度」调大 —— "
                      f"Claude 的思考 token 也算在这个额度里")
    if not text:
        raise AiError(f"模型返回空内容(stop_reason={stop})")
    return text, usage
