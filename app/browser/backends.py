"""Pluggable browser runtime launch plans.

The account/profile lifecycle remains owned by :mod:`app.browser.manager`.
Backends in this module only describe which Chromium executable to launch and
which engine-level identity arguments it needs.  This keeps platform code on
the existing Playwright-compatible BrowserContext/Page API.
"""
from __future__ import annotations

import hashlib
import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Protocol

from .cloak_runtime import (CLOAK_BROWSER_BACKEND, DEFAULT_STORAGE_QUOTA_MB,
                            CloakBrowserRuntime, cloak_fingerprint_seed,
                            version_at_least)
from .identity import Identity


DEFAULT_BACKEND = "default"
LOCAL_BACKEND = "local"
FINGERPRINT_CHROMIUM_BACKEND = "fingerprint_chromium"
ACCOUNT_BROWSER_BACKENDS = {
    DEFAULT_BACKEND,
    LOCAL_BACKEND,
    FINGERPRINT_CHROMIUM_BACKEND,
    CLOAK_BROWSER_BACKEND,
}
# 内核自己拥有 Canvas/WebGL/Audio/navigator 画像的后端。这些后端下 CreatorHub
# 必须跳过历史的 JS 指纹注入与 UA/视口覆盖,否则两层画像会互相打架。
ENGINE_IDENTITY_BACKENDS = {
    FINGERPRINT_CHROMIUM_BACKEND,
    CLOAK_BROWSER_BACKEND,
}
# 全局默认后端的合法取值(账号级还可以取 DEFAULT_BACKEND 表示跟随全局)。
GLOBAL_BROWSER_BACKENDS = {
    LOCAL_BACKEND,
    FINGERPRINT_CHROMIUM_BACKEND,
    CLOAK_BROWSER_BACKEND,
}

# 标准 Chromium 的 WebRTC 出口策略开关(CloakBrowser 基于原版 Chromium,认这两个)。
_WEBRTC_CONCEAL_ARGS = (
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
)

# 这些开关由 CreatorHub 的账号环境统一决定,不允许账号在"附加启动参数"里手填:
# 要么会顶掉账号画像,要么能整个绕开一号一代理的网络隔离。
_BLOCKED_EXTRA_ARG_PREFIXES = {
    "--user-data-dir", "--remote-debugging-address",
    "--remote-debugging-port", "--remote-debugging-pipe",
    "--proxy-server", "--proxy-pac-url", "--no-proxy-server",
    "--disable-spoofing", "--timezone", "--lang", "--accept-lang",
    "--window-size", "--no-sandbox", "--disable-web-security",
    "--ignore-certificate-errors", "--load-extension",
    "--disable-extensions-except",
    # 这几个能让请求整个绕开账号代理(Chromium 后出现的同名开关覆盖先出现的,
    # 用户参数追加在 Patchright 自己的 --proxy-server 之后),等于让一号一代理
    # 静默失效,而界面上代理仍显示已生效。
    "--proxy-bypass-list", "--proxy-auto-detect",
    "--host-resolver-rules", "--host-rules",
    # 与 --no-sandbox 等价的沙箱关闭开关。
    "--disable-setuid-sandbox", "--disable-gpu-sandbox", "--no-zygote",
    # Pro 授权调用的路由方式由账号环境决定:走错代理会把能用的环境弄坏。
    "--license-through-proxy",
}
# 所有 ``--fingerprint*`` 都属于内核画像,由账号资料统一生成。用前缀而不是逐个
# 枚举:CloakBrowser 每加一个新开关,枚举式黑名单就会多出一个缺口。
_BLOCKED_EXTRA_ARG_NAME_PREFIXES = ("--fingerprint",)


class BrowserBackendError(RuntimeError):
    """Base error raised by a configured browser runtime."""


class BrowserBackendUnavailableError(BrowserBackendError):
    """The selected runtime cannot be launched on this machine."""


@dataclass(frozen=True)
class BrowserLaunchPlan:
    name: str
    label: str
    executable_path: str
    args: tuple[str, ...]
    headless: bool
    engine_controlled_identity: bool = False
    runtime_id: str = ""
    version: str = ""
    # 无头模式下必须显式给视口的内核(旧构建的无头窗口尺寸不自洽)。None 表示
    # 沿用 no_viewport,让页面跟随真实窗口。
    headless_viewport: Optional[tuple[int, int]] = None
    # 有头模式下的 --window-size。None 表示由调用方按账号视口决定。
    window_size: Optional[tuple[int, int]] = None
    # 需要替换子进程环境时携带(CloakBrowser Pro 内核自身要读 License Key)。
    env: Optional[Dict[str, str]] = None
    # Pro 内核记录"授权被拒绝"退出码的文件;并发超限这类拒绝发生在握手之后,
    # 只能靠它事后读回原因。
    license_status_file: str = ""
    # 内核默认忽略的 Patchright/Playwright 自带参数。
    ignore_default_args: tuple[str, ...] = (
        "--disable-blink-features=AutomationControlled",
    )


class BrowserRuntimeBackend(Protocol):
    name: str
    label: str

    @property
    def available(self) -> bool: ...

    @property
    def unavailable_reason(self) -> str: ...

    def launch_plan(
        self, identity: Identity, *, requested_headless: bool,
    ) -> BrowserLaunchPlan: ...


def fingerprint_seed_u32(seed: str) -> int:
    """Map an arbitrary persistent account seed to Chromium's uint32 seed."""
    value = str(seed or "0").strip()
    if value.isdigit():
        numeric = int(value)
        if 0 <= numeric <= 0xFFFFFFFF:
            return numeric
    raw = value.encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest()[:4], "big")


def _host_fingerprint_platform() -> str:
    if sys.platform == "darwin":
        return "macos"
    if os.name == "nt":
        return "windows"
    return "linux"


def _accept_language(locale: str) -> str:
    value = str(locale or "zh-CN").strip() or "zh-CN"
    base = value.split("-", 1)[0]
    return value if base == value else f"{value},{base}"


def parse_extra_launch_args(value: str) -> tuple[str, ...]:
    """Parse user-supplied Chromium switches while protecting owned settings."""
    raw = str(value or "").strip()
    if not raw:
        return ()
    if len(raw) > 1200 or any(
            ord(char) < 32 and char not in "\t\r\n" for char in raw):
        raise ValueError("浏览器启动参数格式无效")
    # The UI documents newline/comma separation while shlex keeps quoted
    # values containing spaces together on every host platform.
    normalized = raw.replace("\r", " ").replace("\n", " ").replace(",", " ")
    try:
        tokens = shlex.split(normalized, posix=True)
    except ValueError as exc:
        raise ValueError("浏览器启动参数引号不完整") from exc
    result = []
    for token in tokens:
        if not token.startswith("--") or len(token) > 240:
            raise ValueError("启动参数必须使用 --参数 或 --参数=值 格式")
        name = token.split("=", 1)[0].lower()
        if name in _BLOCKED_EXTRA_ARG_PREFIXES or name.startswith(
                _BLOCKED_EXTRA_ARG_NAME_PREFIXES):
            raise ValueError(f"启动参数与账号环境冲突: {name}")
        result.append(token)
    return tuple(dict.fromkeys(result))


class FingerprintChromiumBackend:
    """Engine-level fingerprint Chromium launched by Patchright.

    The browser owns Canvas/WebGL/Audio/navigator identity.  CreatorHub must
    therefore skip its legacy JavaScript fingerprint injection for this plan.
    """

    name = FINGERPRINT_CHROMIUM_BACKEND
    label = "Fingerprint Chromium · 开源内核"

    def __init__(
        self,
        executable_path: str = "",
        *,
        allow_headless: bool = False,
        platform: str = "auto",
        runtime_id: str = "",
        version: str = "",
        label: str = "",
    ):
        self._raw_path = str(executable_path or "").strip()
        self.allow_headless = bool(allow_headless)
        self.runtime_id = str(runtime_id or "").strip()
        self.version = str(version or "").strip()
        self.label = str(label or "").strip() or self.__class__.label
        requested_platform = str(platform or "auto").strip().lower()
        self.platform = (
            _host_fingerprint_platform()
            if requested_platform == "auto"
            else requested_platform
        )
        if self.platform not in {"windows", "linux", "macos"}:
            self.platform = _host_fingerprint_platform()

    @property
    def executable_path(self) -> Path | None:
        if not self._raw_path:
            return None
        return Path(self._raw_path).expanduser().resolve()

    @property
    def available(self) -> bool:
        path = self.executable_path
        return bool(path and path.is_file())

    @property
    def unavailable_reason(self) -> str:
        if not self._raw_path:
            return "未配置 engine.fingerprint_chromium_path"
        if not self.available:
            return "fingerprint_chromium_path 指向的浏览器不存在"
        return ""

    def launch_plan(
        self, identity: Identity, *, requested_headless: bool,
    ) -> BrowserLaunchPlan:
        path = self.executable_path
        if path is None or not path.is_file():
            raise BrowserBackendUnavailableError(self.unavailable_reason)

        locale = str(identity.locale or "zh-CN").strip() or "zh-CN"
        timezone = (
            str(identity.timezone_id or "Asia/Shanghai").strip()
            or "Asia/Shanghai"
        )
        platform = str(identity.fp_platform or self.platform).strip().lower()
        if platform not in {"windows", "linux", "macos"}:
            platform = self.platform
        brand = str(identity.fp_brand or "Chrome").strip() or "Chrome"
        accepted = (str(identity.fp_accept_languages or "").strip()
                    or _accept_language(locale))
        args = [
            f"--fingerprint={fingerprint_seed_u32(identity.fp_seed)}",
            f"--fingerprint-platform={platform}",
            f"--fingerprint-brand={brand}",
            f"--timezone={timezone}",
            f"--lang={locale}",
            f"--accept-lang={accepted}",
        ]
        if str(identity.fp_webrtc_mode or "conceal") != "allow":
            args.append("--disable-non-proxied-udp")
        if identity.fp_platform_version:
            args.append(
                f"--fingerprint-platform-version={identity.fp_platform_version}")
        if identity.fp_brand_version:
            args.append(
                f"--fingerprint-brand-version={identity.fp_brand_version}")
        if int(identity.fp_hardware_concurrency or 0) > 0:
            args.append(
                "--fingerprint-hardware-concurrency="
                + str(int(identity.fp_hardware_concurrency)))
        try:
            runtime_major = int(self.version.split(".", 1)[0])
        except (TypeError, ValueError):
            runtime_major = 0
        # Upstream exposed explicit WebGL vendor/renderer only in 139-143;
        # Chrome 144+ removed both flags and derives GPU from the seed.
        if 139 <= runtime_major < 144:
            if identity.fp_gpu_vendor:
                args.append(
                    f"--fingerprint-gpu-vendor={identity.fp_gpu_vendor}")
            if identity.fp_gpu_renderer:
                args.append(
                    f"--fingerprint-gpu-renderer={identity.fp_gpu_renderer}")
        disabled = [
            value.strip().lower()
            for value in str(identity.fp_disable_spoofing or "").split(",")
            if value.strip().lower()
            in {"font", "audio", "canvas", "clientrects", "gpu"}
        ]
        if disabled:
            args.append("--disable-spoofing=" + ",".join(dict.fromkeys(disabled)))
        args.extend(parse_extra_launch_args(identity.fp_extra_args))
        return BrowserLaunchPlan(
            name=self.name,
            label=self.label,
            executable_path=str(path),
            args=tuple(args),
            # The upstream runtime documents that headless only normalizes the
            # UA and still leaks other headless traits.  Keep it opt-in.
            headless=bool(requested_headless and self.allow_headless),
            engine_controlled_identity=True,
            runtime_id=self.runtime_id,
            version=self.version,
        )


class CloakBrowserBackend:
    """CloakBrowser 隐身内核(C++ 源码层改指纹)的启动计划。

    与 :class:`FingerprintChromiumBackend` 的区别在参数命名与画像口径:

    * 时区/语言走 ``--fingerprint-timezone`` / ``--fingerprint-locale``,不是
      Chromium 的 ``--timezone`` / ``--lang``(``--lang`` 仍会同步一份,和官方
      wrapper 的默认行为保持一致);
    * 没有 ``--disable-spoofing``,对应能力是 ``--fingerprint-noise=false``;
    * 持久化 Profile 需要显式抬高存储配额,否则会被判成隐身窗口。

    具体用 Pro 还是 GitHub 免费版由 :class:`CloakBrowserRuntime` 决定,这里只
    读取它已经解析好的结果。
    """

    name = CLOAK_BROWSER_BACKEND
    label = "CloakBrowser · 隐身内核"

    # 官方 wrapper 里"无头也能自洽上报窗口尺寸"的最低内核版本。低于它的构建
    # 无头时必须给固定视口,否则 outerWidth/innerWidth 不自洽,本身就是机器人特征。
    HEADLESS_NO_VIEWPORT_MIN_VERSION = "148.0.7778.215.4"

    # 内核按 seed + 平台自动生成 screen 尺寸(官方文档:Win/Linux 1920x1080、
    # macOS 1440x900)。窗口必须落在它之内 —— innerWidth 比 screen.width 还大
    # 是不可能出现的真实窗口,本身就是机器人特征。上游 wrapper 的固定
    # 1920x947 是按 Windows 画的,在 macOS 画像上会直接越界。
    PLATFORM_SCREEN = {
        "windows": (1920, 1080),
        "linux": (1920, 1080),
        "macos": (1440, 900),
    }
    # 实测:无头下 outer = viewport + (2, 126)(窗口边框 + 标签页/地址栏/书签栏)。
    _HEADLESS_CHROME_W = 2
    _HEADLESS_CHROME_H = 126
    _MIN_WINDOW = 360

    @classmethod
    def _fit_window(cls, platform: str, width: int, height: int
                    ) -> tuple[int, int]:
        """把账号视口收进内核伪造的 screen 之内。"""
        screen_w, screen_h = cls.PLATFORM_SCREEN.get(
            platform, cls.PLATFORM_SCREEN["windows"])
        return (
            min(max(cls._MIN_WINDOW, int(width or 0)), screen_w),
            min(max(cls._MIN_WINDOW, int(height or 0)), screen_h),
        )

    @classmethod
    def _fit_headless_viewport(cls, platform: str, width: int, height: int
                               ) -> tuple[int, int]:
        screen_w, screen_h = cls.PLATFORM_SCREEN.get(
            platform, cls.PLATFORM_SCREEN["windows"])
        window_w, window_h = cls._fit_window(platform, width, height)
        return (
            min(window_w, screen_w - cls._HEADLESS_CHROME_W),
            min(window_h, max(cls._MIN_WINDOW, screen_h - cls._HEADLESS_CHROME_H)),
        )

    def __init__(self, runtime: CloakBrowserRuntime):
        self.runtime = runtime

    # ── 状态 ──
    @property
    def state(self):
        return self.runtime.cached_state or self.runtime.inspect()

    @property
    def allow_headless(self) -> bool:
        return self.runtime.allow_headless

    @property
    def platform(self) -> str:
        return self.runtime.platform

    @property
    def edition(self) -> str:
        return self.state.edition

    @property
    def version(self) -> str:
        return self.state.version

    @property
    def runtime_id(self) -> str:
        return self.state.runtime_id

    @property
    def executable_path(self) -> Path | None:
        raw = self.state.executable_path
        return Path(raw) if raw else None

    @property
    def available(self) -> bool:
        state = self.state
        return bool(state.installed and Path(state.executable_path).is_file())

    @property
    def unavailable_reason(self) -> str:
        state = self.state
        if self.available:
            return ""
        return state.detail or "CloakBrowser 内核尚未就绪"

    def describe(self) -> Dict[str, Any]:
        return self.runtime.status()

    # ── 启动计划 ──
    def launch_plan(
        self, identity: Identity, *, requested_headless: bool,
    ) -> BrowserLaunchPlan:
        state = self.state
        path = Path(state.executable_path) if state.executable_path else None
        if path is None or not path.is_file():
            raise BrowserBackendUnavailableError(self.unavailable_reason)

        locale = str(identity.locale or "zh-CN").strip() or "zh-CN"
        timezone = (
            str(identity.timezone_id or "Asia/Shanghai").strip()
            or "Asia/Shanghai"
        )
        platform = str(identity.fp_platform or self.platform).strip().lower()
        if platform not in {"windows", "linux", "macos"}:
            platform = self.platform
        headless = bool(requested_headless and self.allow_headless)
        args = [
            f"--fingerprint={cloak_fingerprint_seed(identity.fp_seed)}",
            f"--fingerprint-platform={platform}",
            f"--fingerprint-timezone={timezone}",
            f"--fingerprint-locale={locale}",
            # 官方 wrapper 设置 locale 时同时写 --lang,保持 UI 语言与画像一致。
            f"--lang={locale}",
            # 常驻 Profile 必须抬高存储配额,否则 BrowserScan 一类检测按默认
            # 配额把它判成隐身窗口。
            f"--fingerprint-storage-quota={DEFAULT_STORAGE_QUOTA_MB}",
        ]
        accepted = str(identity.fp_accept_languages or "").strip()
        if accepted:
            args.append(f"--accept-lang={accepted}")
        if identity.fp_brand:
            args.append(f"--fingerprint-brand={identity.fp_brand}")
        if identity.fp_brand_version:
            args.append(f"--fingerprint-brand-version={identity.fp_brand_version}")
        if identity.fp_platform_version:
            args.append(
                f"--fingerprint-platform-version={identity.fp_platform_version}")
        if int(identity.fp_hardware_concurrency or 0) > 0:
            args.append(
                "--fingerprint-hardware-concurrency="
                + str(int(identity.fp_hardware_concurrency)))
        if identity.fp_gpu_vendor:
            args.append(f"--fingerprint-gpu-vendor={identity.fp_gpu_vendor}")
        if identity.fp_gpu_renderer:
            args.append(f"--fingerprint-gpu-renderer={identity.fp_gpu_renderer}")
        # CloakBrowser 没有逐项 --disable-spoofing;账号关掉任一噪声类项目时,
        # 统一退化成"保留确定性种子、关闭噪声注入"。
        disabled = {
            value.strip().lower()
            for value in str(identity.fp_disable_spoofing or "").split(",")
            if value.strip()
        }
        if disabled & {"canvas", "audio", "clientrects", "font"}:
            args.append("--fingerprint-noise=false")
        # 有头模式(及 Windows 全模式)需要绕过 GPU 黑名单,否则软件渲染会给出
        # 真实用户浏览器不会出现的 renderer 字符串。与官方 wrapper 一致。
        if not headless or os.name == "nt":
            args.append("--ignore-gpu-blocklist")
        # WebRTC 不受 HTTP 层代理约束,STUN 会直接把宿主真实 IP 暴露出去,一号
        # 一代理的防关联就白做了。与 fingerprint_chromium 后端保持同一口径:
        # 只要账号没显式选择放行,就无条件禁掉非代理 UDP(不只在配了代理时)。
        if str(identity.fp_webrtc_mode or "conceal") != "allow":
            args.extend(_WEBRTC_CONCEAL_ARGS)
        args.extend(parse_extra_launch_args(identity.fp_extra_args))

        launch_env, status_file = self.runtime.launch_env(state)
        headless_viewport = None
        if headless and not version_at_least(
                state.version, self.HEADLESS_NO_VIEWPORT_MIN_VERSION):
            # 旧内核无头时窗口尺寸不自洽,必须显式给视口;并且要收进伪造的
            # screen 之内,否则 inner > screen。新内核自己就能自洽,走 no_viewport。
            headless_viewport = self._fit_headless_viewport(
                platform, identity.viewport_w, identity.viewport_h)
        return BrowserLaunchPlan(
            name=self.name,
            label=state.label,
            executable_path=str(path),
            args=tuple(dict.fromkeys(args)),
            headless=headless,
            engine_controlled_identity=True,
            runtime_id=state.runtime_id,
            version=state.version,
            headless_viewport=headless_viewport,
            window_size=self._fit_window(
                platform, identity.viewport_w, identity.viewport_h),
            env=launch_env,
            license_status_file=status_file,
            # CloakBrowser 自带隐藏自动化痕迹的源码补丁,Patchright/Playwright
            # 默认追加的这几个开关反而会造出真实浏览器没有的特征。
            ignore_default_args=(
                "--disable-blink-features=AutomationControlled",
                "--enable-automation",
                "--enable-unsafe-swiftshader",
            ),
        )
