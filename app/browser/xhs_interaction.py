"""Central, deterministic-testable semantics for visible XHS page actions."""
from __future__ import annotations

import asyncio
import inspect
import random
import sys
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable


class XhsVisibleActionGate:
    """Permit one active, user-visible Xiaohongshu action on the machine."""

    def __init__(self):
        self._semaphore = asyncio.Semaphore(1)
        self.active_account: Any = None
        self._owner_task: asyncio.Task | None = None
        self._depth = 0

    @property
    def owned_by_current_task(self) -> bool:
        return self._owner_task is asyncio.current_task()

    @asynccontextmanager
    async def acquire(self, account_key: Any):
        current = asyncio.current_task()
        if self._owner_task is current:
            if self.active_account != account_key:
                raise RuntimeError("同一可见操作不可嵌套切换小红书账号")
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        acquired = False
        try:
            await self._semaphore.acquire()
            acquired = True
            self.active_account = account_key
            self._owner_task = current
            self._depth = 1
            yield
        finally:
            if acquired:
                self._depth = 0
                self._owner_task = None
                self.active_account = None
                self._semaphore.release()


# 零宽字符不是空白,`str.split()` 不会去掉它们 —— 而编辑器会拿它们做光标锚点。
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"))


def _skeleton(text: str) -> str:
    """只留字符,去掉全部空白和零宽标记。

    `str.split()` 按 Unicode 空白切,所以不换行空格 `\u00a0` 和全角空格
    `\u3000` 也一并处理掉了 —— 编辑器很爱拿它们替换连续空格。
    """
    return "".join(str(text).translate(_ZERO_WIDTH).split())


class XhsInteractionPolicy:
    """Page-native clicks, input and finite scrolling with bounded cadence."""

    def __init__(
            self, *, rng: random.Random | None = None,
            sleep: Callable[[float], Awaitable[None]] | None = None):
        self.rng = rng or random.SystemRandom()
        self.sleep = sleep or asyncio.sleep
        self._action_count = 0
        self._next_rest_at = int(self.rng.randint(14, 24))

    async def _pause(self, low: float, high: float) -> None:
        await self.sleep(float(self.rng.uniform(low, high)))

    async def pause(self, low: float, high: float) -> None:
        """Public bounded pause for condition-polling loops."""
        if low < 0 or high < low:
            raise ValueError("交互停顿范围无效")
        await self._pause(low, high)

    async def _after_action(self) -> None:
        """Occasionally insert a longer bounded break between action bursts."""
        self._action_count += 1
        if self._action_count < self._next_rest_at:
            return
        self._action_count = 0
        self._next_rest_at = int(self.rng.randint(14, 24))
        await self._pause(1.4, 3.8)

    async def reading_pause(self, *, content_length: int = 0) -> float:
        """Pause after navigation using a bounded content-aware dwell time."""
        length = max(0, min(4000, int(content_length or 0)))
        base = 0.55 + min(2.25, length / 1250)
        delay = float(self.rng.uniform(base * 0.72, base * 1.28))
        await self.sleep(delay)
        await self._after_action()
        return delay

    async def click_visible(
            self, locator: Any, *, timeout: int = 10_000) -> None:
        await locator.wait_for(state="visible", timeout=timeout)
        await locator.scroll_into_view_if_needed(timeout=timeout)
        enabled = getattr(locator, "is_enabled", None)
        if callable(enabled):
            for _ in range(20):
                if await enabled():
                    break
                await self._pause(0.05, 0.12)
            else:
                raise RuntimeError("小红书页面控件当前不可用")
        await locator.hover()
        await self._pause(0.08, 0.22)
        await locator.click()
        await self._after_action()

    async def _prepare_input(self, locator: Any) -> None:
        await locator.wait_for(state="visible", timeout=10_000)
        await locator.scroll_into_view_if_needed(timeout=10_000)
        await locator.focus()
        await self._pause(0.06, 0.16)
        modifier = "Meta" if sys.platform == "darwin" else "Control"
        await locator.press(f"{modifier}+A")
        await locator.press("Backspace")

    async def type_short(self, locator: Any, text: str) -> None:
        await self._prepare_input(locator)
        for character in str(text):
            await locator.press_sequentially(character, delay=0)
            await self._pause(0.035, 0.095)
        await self._verify_text(locator, str(text))
        await self._after_action()

    async def insert_long(
            self, locator: Any, text: str, *, page: Any | None = None) -> None:
        await self._prepare_input(locator)
        target_page = page or getattr(locator, "page", None)
        if target_page is None or not hasattr(target_page, "keyboard"):
            raise RuntimeError("长文本输入缺少活动页面")
        await target_page.keyboard.insert_text(str(text))
        await self._pause(0.08, 0.18)
        await self._verify_text(locator, str(text))
        await self._after_action()

    async def _verify_text(
            self, locator: Any, expected: str, *, attempts: int = 8) -> None:
        """确认写进去的确实是我们要写的那段字。

        **比的是去掉全部空白之后的字符流,不是原字符串。** 正文编辑器是
        contenteditable(TipTap / ProseMirror),`input_value()` 在它上面会抛,
        于是读回来的是 `innerText` —— 而 `innerText` 是按**渲染结果**拼的:
        一个 `<p>` 边界算两个换行,TipTap 用 `<p><br></p>` 表示的空行能读成五个。
        于是正文里只要有一个空行(段落之间空一行,这是给手机上读的人写的),
        插进去的 `\n\n` 读回来就是三到五个 `\n`,逐字比必然不等 ——
        **每一篇有分段的笔记都会在这里失败。**

        空白是渲染产物,字符不是。所以空白全部忽略,字符一个都不能少 ——
        截断、没写进去、写错控件仍然拦得住,而且分别报不同的话:
        「发布页面异常」这种笼统的错什么线索都不给。

        允许重试:`insert_text` 是一次 CDP 调用,但 ProseMirror 的事务和 React
        的提交可能晚一拍,80 毫秒读不到不代表没写进去。

        代价说清楚:这样比**看不出「段落被合并成了空格」** —— 内容对,排版丢了。
        真要管排版,该改的是 `insert_long`(按段落插、中间按 Enter),不是这里 ——
        在验证里较真只会把能发的笔记拦下来。
        """
        want = _skeleton(expected)
        actual = ""
        for index in range(max(1, attempts)):
            try:
                actual = str(await locator.input_value())
            except Exception:
                actual = str(await locator.evaluate(
                    "el => el.value ?? el.innerText ?? el.textContent ?? ''"))
            # 快路径:真正的 <input>/<textarea>(标题)本来就该逐字相等
            if actual == expected or _skeleton(actual) == want:
                return
            if index + 1 < attempts:
                await self._pause(0.08, 0.2)

        got = _skeleton(actual)
        if not got:
            raise RuntimeError("小红书文本没写进输入框(可能失焦,或者选错了控件)")
        if want.startswith(got):
            raise RuntimeError(
                f"小红书文本被截断:期望 {len(want)} 字,实际只进去 {len(got)} 字")
        at = next((i for i, (a, b) in enumerate(zip(want, got)) if a != b),
                  min(len(want), len(got)))
        # 只摘一小段:错误信息会进日志和面板,正文整段贴出去没必要
        raise RuntimeError(
            f"小红书文本输入结果与预期不一致:期望 {len(want)} 字、实际 {len(got)} 字,"
            f"第 {at + 1} 字起对不上「{got[at:at + 20]}」")

    async def scroll_step(self, page: Any, *, direction: int = 1) -> int:
        # Triangular sampling avoids a flat machine-like distribution while
        # remaining bounded enough for deterministic collection progress.
        amount = int(self.rng.triangular(350, 900, 610))
        signed = amount if direction >= 0 else -amount
        await page.mouse.wheel(0, signed)
        await self._pause(0.18, 0.52)
        await self._after_action()
        return signed

    async def scroll_until(
            self, page: Any,
            predicate: Callable[[], bool | Awaitable[bool]],
            *, max_steps: int, direction: int = 1) -> bool:
        if await self._resolve(predicate()):
            return True
        for _ in range(max(0, int(max_steps))):
            await self.scroll_step(page, direction=direction)
            if await self._resolve(predicate()):
                return True
        return False

    @staticmethod
    async def _resolve(value: bool | Awaitable[bool]) -> bool:
        if inspect.isawaitable(value):
            value = await value
        return bool(value)
