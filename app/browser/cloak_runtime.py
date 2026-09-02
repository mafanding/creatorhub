"""CloakBrowser 内核解析:License 校验、版本落地与 Pro/GitHub 版回退。

CloakBrowser 是把指纹改在 Chromium C++ 源码层的隐身内核,官方 ``cloakbrowser``
wrapper 负责下载、签名校验与 License 校验。CreatorHub 只复用它的这几件事,
浏览器仍旧由 Patchright 的 ``launch_persistent_context`` 拉起,平台代码继续用
同一套 BrowserContext/Page API。

回退规则(用户可见语义):

* 配好 key 且服务端校验通过 → 用 **CloakBrowser Pro** 最新内核;
* 没配 key / key 失效 / Pro 内核暂时拿不到 → 回退到 **CloakBrowser GitHub 版**
  (免费构建,随 wrapper 从 GitHub Releases 落地)。

wrapper 的 key 解析顺序是 参数 > 环境变量 > ``<cache_dir>/license.key``,所以
"强制免费版"必须同时清掉环境变量并使用 CreatorHub 自己的 cache 目录 —— 否则
宿主机上残留的 ``~/.cloakbrowser/license.key`` 会让回退变成空操作。
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional

CLOAK_BROWSER_BACKEND = "cloak_browser"
CLOAK_PRO_RUNTIME_ID = "cloak-pro"
CLOAK_FREE_RUNTIME_ID = "cloak-free"
CLOAK_CUSTOM_RUNTIME_ID = "cloak-custom"

EDITION_RUNTIME_IDS = {
    "pro": CLOAK_PRO_RUNTIME_ID,
    "free": CLOAK_FREE_RUNTIME_ID,
    "custom": CLOAK_CUSTOM_RUNTIME_ID,
}
EDITION_LABELS = {
    "pro": "CloakBrowser Pro · 隐身内核",
    "free": "CloakBrowser · GitHub 版",
    "custom": "CloakBrowser · 自定义内核",
}

# ``chromium-146.0.7680.177.5`` / ``chromium-151.0.7922.108.3-pro``
_BINARY_DIR_RE = re.compile(r"^chromium-(?P<version>\d[\d.]*)(?P<pro>-pro)?$")
# 版本号只允许纯数字与点:它会被拼进可执行文件路径,``../..`` 之类必须挡在门外。
_VERSION_RE = re.compile(r"\d+(?:\.\d+){0,5}")

# CloakBrowser 文档里的种子都是 5 位十进制数,内核按有符号整数解析。为了既保持
# 账号级确定性又不越界,把任意种子映射到 [10000, 2^31-1]。
_SEED_MIN = 10000
_SEED_MAX = 0x7FFFFFFF

# 持久化 Profile 默认会被 BrowserScan 一类检测判成隐身窗口(存储配额被内核归一
# 化)。官方文档建议显式抬高配额,让 profile 看起来像常规安装。
DEFAULT_STORAGE_QUOTA_MB = 5000


# Pro 内核用退出码表达 License 拒绝。上游 wrapper 给的是英文原文,这里换成中文,
# 否则用户在中文界面里只会看到一句 "browser closed" 或英文长句。
LICENSE_DENIAL_MESSAGES = {
    76: ("CloakBrowser Pro:当前套餐的并发会话数已用满。请关闭其它正在运行的会话,"
         "或升级套餐 —— 免费 Key 只有 1 个并发。"),
    77: "CloakBrowser Pro:License Key 无效、已过期或未送达内核,请检查 Key 配置。",
    78: "CloakBrowser Pro:无法校验 License(授权服务器不可达或网络异常)。",
    79: "CloakBrowser Pro:本地配置问题,内核缓存目录不可写。",
}


class CloakBrowserError(RuntimeError):
    """CloakBrowser 内核相关的可预期失败。"""


def cloak_fingerprint_seed(seed: str) -> int:
    """把账号的持久化种子映射成 CloakBrowser 接受的 ``--fingerprint`` 整数。"""
    value = str(seed or "0").strip()
    if value.isdigit():
        numeric = int(value)
        if _SEED_MIN <= numeric <= _SEED_MAX:
            return numeric
    raw = int.from_bytes(
        hashlib.sha256(value.encode("utf-8")).digest()[:8], "big")
    return _SEED_MIN + raw % (_SEED_MAX - _SEED_MIN + 1)


def mask_license_key(key: str) -> str:
    """只回显够用来核对的片段,永远不把完整 key 送回前端。"""
    value = str(key or "").strip()
    if not value:
        return ""
    if len(value) <= 10:
        return value[:3] + "***"
    return f"{value[:6]}…{value[-4:]}"


def version_from_binary_path(path: str | Path) -> tuple[str, bool]:
    """从 wrapper 的缓存目录名反推 (版本, 是否 Pro)。未知时返回 ("", False)。"""
    try:
        parents = Path(path).resolve().parents
    except (OSError, RuntimeError):
        return "", False
    for parent in list(parents)[:4]:
        match = _BINARY_DIR_RE.match(parent.name)
        if match:
            return match.group("version"), bool(match.group("pro"))
    return "", False


def _version_tuple(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in str(value).split("."))
    except (TypeError, ValueError):
        return ()


def version_at_least(value: str, floor: str) -> bool:
    """内核版本是否 >= floor;版本未知(空串)一律按"不满足"处理。"""
    left, right = _version_tuple(value), _version_tuple(floor)
    if not left or not right:
        return False
    return left >= right


@dataclass(frozen=True)
class CloakLicenseState:
    configured: bool = False
    valid: bool = False
    plan: str = ""
    expires: str = ""
    detail: str = ""
    masked_key: str = ""

    @property
    def entitles_pro(self) -> bool:
        return bool(self.configured and self.valid)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "configured": self.configured,
            "valid": self.valid,
            "plan": self.plan,
            "expires": self.expires,
            "detail": self.detail,
            "masked_key": self.masked_key,
        }


@dataclass(frozen=True)
class CloakBinaryState:
    edition: str = "free"          # pro | free | custom
    version: str = ""
    executable_path: str = ""
    installed: bool = False
    detail: str = ""
    license: CloakLicenseState = field(default_factory=CloakLicenseState)

    @property
    def runtime_id(self) -> str:
        return EDITION_RUNTIME_IDS.get(self.edition, CLOAK_FREE_RUNTIME_ID)

    @property
    def label(self) -> str:
        base = EDITION_LABELS.get(self.edition, EDITION_LABELS["free"])
        return f"{base} {self.version}".strip() if self.version else base

    def to_dict(self) -> Dict[str, Any]:
        return {
            "edition": self.edition,
            "runtime_id": self.runtime_id,
            "label": self.label,
            "version": self.version,
            "executable_path": self.executable_path,
            "installed": self.installed,
            "detail": self.detail,
            "license": self.license.to_dict(),
        }


def wrapper_modules() -> Any:
    """按需导入官方 wrapper。缺依赖时抛 :class:`CloakBrowserError`。

    ``cloakbrowser`` 只在函数内部导入 playwright,所以运行期不会真的加载第二个
    浏览器驱动(pip 依赖里仍会装上 playwright 包,只是用不到)。
    """
    try:
        from cloakbrowser import config as cb_config       # noqa: PLC0415
        from cloakbrowser import download as cb_download   # noqa: PLC0415
        from cloakbrowser import license as cb_license     # noqa: PLC0415
    except Exception as exc:  # pragma: no cover - 依赖缺失路径
        raise CloakBrowserError(
            "未安装 cloakbrowser 依赖,请执行 pip install -r requirements.txt"
        ) from exc
    return cb_config, cb_download, cb_license


class CloakBrowserRuntime:
    """解析当前应当启动的 CloakBrowser 内核,并缓存结果。

    所有对官方 wrapper 的调用都是阻塞的(HTTP + 约 200MB 下载),因此:

    * :meth:`inspect` 只读磁盘,启动期与接口查询都用它;
    * :meth:`resolve` 才会联网/下载,必须放进线程池;
    * :meth:`ensure_ready` 是给事件循环用的异步入口,并发调用共用同一次解析。
    """

    def __init__(
        self,
        *,
        license_key: str = "",
        cache_dir: str = "./data/cloakbrowser",
        executable_path: str = "",
        allow_headless: bool = True,
        platform: str = "auto",
        release_channel: str = "stable",
        browser_version: str = "",
        auto_download: bool = True,
        log: Optional[Callable[[str], None]] = None,
    ):
        self._license_key = str(license_key or "").strip()
        self._cache_dir = str(cache_dir or "./data/cloakbrowser").strip()
        self._executable_path = str(executable_path or "").strip()
        self.allow_headless = bool(allow_headless)
        self.platform = _normalize_platform(platform)
        self.release_channel = (
            "preview"
            if str(release_channel or "stable").strip().lower() == "preview"
            else "stable"
        )
        self.browser_version = str(browser_version or "").strip()
        self.auto_download = bool(auto_download)
        self._log = log or (lambda message: print(f"[cloakbrowser] {message}"))
        self._env_lock = threading.RLock()
        self._resolve_lock = threading.RLock()
        self._async_lock: Optional[asyncio.Lock] = None
        self._state: Optional[CloakBinaryState] = None
        self._license_state: Optional[CloakLicenseState] = None
        self._inspect_cache: Optional[tuple[float, CloakBinaryState]] = None

    # ── 配置 ──
    @property
    def license_key(self) -> str:
        return self._license_key

    def set_license_key(self, key: str) -> None:
        """换 key 后作废缓存,下一次解析重新判定 Pro/GitHub 版。"""
        value = str(key or "").strip()
        if value == self._license_key:
            return
        self._license_key = value
        self.invalidate()

    def invalidate(self) -> None:
        # 不加 _resolve_lock:那把锁在下载期间会被持有几分钟,而这里只是清缓存
        # (GIL 下的属性赋值本身就是原子的),不能让"保存 Key"卡在下载后面。
        self._state = None
        self._license_state = None
        self._inspect_cache = None

    @property
    def cache_dir(self) -> Path:
        return Path(self._cache_dir).expanduser().resolve()

    @property
    def custom_executable(self) -> str:
        return self._executable_path

    @property
    def cached_state(self) -> Optional[CloakBinaryState]:
        return self._state

    # ── 环境隔离 ──
    @contextmanager
    def _wrapper_env(self, *, license_key: Optional[str]):
        """让 wrapper 只看见 CreatorHub 给的 key / 缓存目录。

        ``CLOAKBROWSER_DOWNLOAD_URL`` 故意不动:用户显式配了私有镜像就应当继续
        走镜像(代价是官方 wrapper 会因此停用 Pro 通道,状态里会说明)。
        """
        overrides: Dict[str, Optional[str]] = {
            "CLOAKBROWSER_CACHE_DIR": str(self.cache_dir),
            # 始终显式传参,避免宿主机环境变量把"回退免费版"变成空操作。
            "CLOAKBROWSER_LICENSE_KEY": None,
            "CLOAKBROWSER_BINARY_PATH": self._executable_path or None,
            "CLOAKBROWSER_VERSION": self.browser_version or None,
            "CLOAKBROWSER_RELEASE_CHANNEL": self.release_channel,
            # 我们下载完就直接执行这个二进制。自定义下载源本来就绕过了官方的
            # Ed25519 签名校验,再让 SKIP_CHECKSUM 把哈希校验也关掉,等于把任意
            # 代码执行的门完全敞开 —— 这一项永远强制清掉。
            "CLOAKBROWSER_SKIP_CHECKSUM": None,
        }
        if license_key:
            overrides["CLOAKBROWSER_LICENSE_KEY"] = license_key
        with self._env_lock:
            saved = {key: os.environ.get(key) for key in overrides}
            try:
                for key, value in overrides.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                yield
            finally:
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

    def _download_url_override(self) -> str:
        return os.environ.get("CLOAKBROWSER_DOWNLOAD_URL", "").strip()

    # ── License ──
    def license_state(self, *, refresh: bool = False) -> CloakLicenseState:
        """校验 key。结果缓存在进程内;wrapper 自己还有 24 小时文件缓存。"""
        if self._license_state is not None and not refresh:
            return self._license_state
        key = self._license_key
        if not key:
            state = CloakLicenseState(detail="未配置 License Key,使用 GitHub 免费版")
            self._license_state = state
            return state
        masked = mask_license_key(key)
        if self._executable_path:
            state = CloakLicenseState(
                configured=True, valid=False, masked_key=masked,
                detail="已指定自定义内核路径,License 不参与内核选择")
            self._license_state = state
            return state
        if self._download_url_override():
            state = CloakLicenseState(
                configured=True, valid=False, masked_key=masked,
                detail="检测到 CLOAKBROWSER_DOWNLOAD_URL 自定义下载源:"
                       "官方 Pro 通道已停用,且内核不再经过官方签名校验(仅校验镜像自带哈希)")
            self._license_state = state
            return state
        try:
            _, _, cb_license = wrapper_modules()
        except CloakBrowserError as exc:
            state = CloakLicenseState(
                configured=True, masked_key=masked, detail=str(exc))
            self._license_state = state
            return state
        try:
            with self._wrapper_env(license_key=key):
                info = cb_license.validate_license(key)
        except Exception as exc:  # pragma: no cover - 网络异常路径
            state = CloakLicenseState(
                configured=True, masked_key=masked,
                detail=f"License 校验失败,暂用 GitHub 免费版:{exc}")
            self._license_state = state
            return state
        if info is None:
            state = CloakLicenseState(
                configured=True, masked_key=masked,
                detail="License 服务器不可达且无本地缓存,暂用 GitHub 免费版")
        elif not getattr(info, "valid", False):
            state = CloakLicenseState(
                configured=True, masked_key=masked,
                plan=str(getattr(info, "plan", "") or ""),
                expires=str(getattr(info, "expires", "") or ""),
                detail="License Key 无效或已过期,已回退 GitHub 免费版")
        else:
            plan = str(getattr(info, "plan", "") or "")
            state = CloakLicenseState(
                configured=True, valid=True, masked_key=masked, plan=plan,
                expires=str(getattr(info, "expires", "") or ""),
                detail=("免费 Key 仅允许 1 个并发会话" if plan == "free" else ""))
        self._license_state = state
        return state

    # ── 磁盘路径(不读环境变量,不联网)──
    # 这些是官方 wrapper 缓存目录的布局约定。inspect() 必须能在**不进入
    # _wrapper_env** 的情况下回答"内核在不在磁盘上" —— 否则一次 200MB 下载
    # 会连带把 /api/browser-backends、登录流程和写操作门禁全部堵死。
    def _binary_path(self, version: str, *, pro: bool) -> Path:
        base = self.cache_dir / f"chromium-{version}{'-pro' if pro else ''}"
        if sys.platform == "darwin":
            return base / "Chromium.app" / "Contents" / "MacOS" / "Chromium"
        if os.name == "nt":
            return base / "chrome.exe"
        return base / "chrome"

    def _read_marker(self, name: str) -> str:
        """读版本标记文件。内容会被拼进可执行文件路径,必须先当成不可信输入校验。"""
        try:
            value = (self.cache_dir / name).read_text().strip()
        except OSError:
            return ""
        return value if _VERSION_RE.fullmatch(value) else ""

    def _disk_pro_version(self, tag: str) -> str:
        prefix = ("latest_pro_version_preview"
                  if self.release_channel == "preview"
                  else "latest_pro_version")
        version = self._read_marker(f"{prefix}_{tag}")
        if not version:
            return ""
        path = self._binary_path(version, pro=True)
        # 与 wrapper 的 _pro_binary_ready 一致:存在且可执行才算数。
        return version if path.is_file() and os.access(path, os.X_OK) else ""

    def _disk_free_version(self, tag: str, base_version: str) -> str:
        for name in (f"latest_version_{tag}", "latest_version"):
            version = self._read_marker(name)
            if not version or not version_at_least(version, base_version):
                continue
            if self._binary_path(version, pro=False).is_file():
                return version
        return base_version

    # ── 内核解析 ──
    _INSPECT_TTL_SECONDS = 2.0

    def inspect(self) -> CloakBinaryState:
        """只读磁盘的状态查询:不校验 License,不联网,不下载,不加解析锁。

        这条路径会被事件循环线程频繁调用(``/api/browser-backends``、登录流程、
        写操作门禁、environment_snapshot),所以它绝不能和 :meth:`resolve` 抢同一
        把锁 —— 否则一次 200MB 下载会把整个服务堵死。目录布局按官方 wrapper 的
        约定自己算,只 stat 文件。
        """
        cached = self._inspect_cache
        if cached is not None and (
                time.monotonic() - cached[0]) < self._INSPECT_TTL_SECONDS:
            return cached[1]
        state = self._inspect_uncached()
        self._inspect_cache = (time.monotonic(), state)
        return state

    def _inspect_uncached(self) -> CloakBinaryState:
        license_state = self._license_state or CloakLicenseState(
            configured=bool(self._license_key),
            masked_key=mask_license_key(self._license_key),
            detail=("License 尚未校验" if self._license_key
                    else "未配置 License Key,使用 GitHub 免费版"))
        if self._executable_path:
            path = Path(self._executable_path).expanduser()
            version, _ = version_from_binary_path(path)
            return CloakBinaryState(
                edition="custom", version=version, executable_path=str(path),
                installed=path.is_file(),
                detail="" if path.is_file() else "自定义 CloakBrowser 内核路径不存在",
                license=license_state)
        try:
            cb_config, _, _ = wrapper_modules()
        except CloakBrowserError as exc:
            return CloakBinaryState(
                edition="free", detail=str(exc), license=license_state)
        try:
            # 两个都是纯平台判断,不读环境变量。
            tag = cb_config.get_platform_tag()
            base_version = cb_config.get_chromium_version()
        except Exception as exc:  # pragma: no cover - 平台不受支持
            return CloakBinaryState(
                edition="free", detail=str(exc), license=license_state)
        candidates: list[tuple[str, str]] = []
        # 只有"配了 key"才可能落 Pro 内核;这里不做校验,校验交给 resolve()。
        if self._license_key:
            pro_version = (self.browser_version
                           or self._disk_pro_version(tag))
            if pro_version:
                candidates.append(("pro", pro_version))
        free_version = self.browser_version or self._disk_free_version(
            tag, base_version)
        if free_version:
            candidates.append(("free", free_version))
        for edition, version in candidates:
            path = self._binary_path(version, pro=(edition == "pro"))
            if path.is_file():
                return CloakBinaryState(
                    edition=edition, version=version,
                    executable_path=str(path), installed=True,
                    license=license_state)
        edition, version = candidates[0] if candidates else ("free", "")
        return CloakBinaryState(
            edition=edition, version=version,
            executable_path=(str(self._binary_path(
                version, pro=(edition == "pro"))) if version else ""),
            installed=False, detail="CloakBrowser 内核尚未下载",
            license=license_state)

    def resolve(self, *, allow_download: Optional[bool] = None,
                refresh_license: bool = False) -> CloakBinaryState:
        """阻塞式解析:必要时联网校验 License 并下载内核。"""
        download = self.auto_download if allow_download is None else bool(allow_download)
        with self._resolve_lock:
            if self._executable_path:
                # License 已在此前解析过(自定义路径下它只影响文案),刷新一次
                # 保证 state.license 不是"尚未校验"的占位。
                license_state = self.license_state(refresh=refresh_license)
                state = self._inspect_uncached()
                if not state.installed:
                    raise CloakBrowserError(
                        f"自定义 CloakBrowser 内核不存在:{self._executable_path}")
                self._state = state
                self._inspect_cache = None
                return state
            cb_config, cb_download, _ = wrapper_modules()
            license_state = self.license_state(refresh=refresh_license)
            if not download:
                state = self._inspect_uncached()
                if not state.installed:
                    raise CloakBrowserError(
                        "CloakBrowser 内核尚未下载,且已关闭自动下载")
                self._state = state
                self._inspect_cache = None
                return state
            notes: list[str] = []
            path = ""
            edition = "free"
            if license_state.entitles_pro:
                try:
                    with self._wrapper_env(license_key=self._license_key):
                        path = cb_download.ensure_binary(
                            license_key=self._license_key,
                            browser_version=self.browser_version or None,
                            release_channel=self.release_channel)
                    edition = "pro"
                except Exception as exc:
                    # key 有效但 Pro 内核暂时拿不到 —— 用户要的语义是"倒退回
                    # GitHub 版",所以这里不把异常抛给调用方。
                    notes.append(f"Pro 内核获取失败,已回退 GitHub 免费版:{exc}")
                    self._log(f"Pro 内核获取失败,回退免费版: {exc!r}")
                    path = ""
            if not path:
                with self._wrapper_env(license_key=None):
                    path = cb_download.ensure_binary(
                        browser_version=self.browser_version or None,
                        release_channel=self.release_channel)
                edition = "free"
            version, is_pro = version_from_binary_path(path)
            if is_pro:
                edition = "pro"
            if not version:
                with self._wrapper_env(license_key=self._license_key or None):
                    version = str(cb_config.get_effective_version(
                        pro=(edition == "pro"),
                        release_channel=self.release_channel) or "")
            detail = "；".join([note for note in notes if note])
            if not detail and license_state.detail and not license_state.entitles_pro:
                detail = license_state.detail
            state = CloakBinaryState(
                edition=edition, version=version, executable_path=str(path),
                installed=Path(path).is_file(), detail=detail,
                license=license_state)
            self._state = state
            self._inspect_cache = None
            return state

    async def ensure_ready(self, *, allow_download: Optional[bool] = None,
                           refresh_license: bool = False) -> CloakBinaryState:
        """事件循环入口。已解析且内核仍在磁盘上时直接返回缓存。"""
        cached = self._state
        if (cached is not None and cached.installed and not refresh_license
                and Path(cached.executable_path).is_file()):
            return cached
        if self._async_lock is None:
            self._async_lock = asyncio.Lock()
        async with self._async_lock:
            cached = self._state
            if (cached is not None and cached.installed and not refresh_license
                    and Path(cached.executable_path).is_file()):
                return cached
            return await asyncio.to_thread(
                self.resolve, allow_download=allow_download,
                refresh_license=refresh_license)

    # ── 启动辅助 ──
    def launch_env(self, state: CloakBinaryState
                   ) -> tuple[Optional[Dict[str, str]], str]:
        """Pro 内核自身会在进程内校验 License,必须把 key 带进子进程。

        Patchright/Playwright 的 ``env`` 是**替换**而不是合并,所以这里必须基于
        ``os.environ`` 复制一份,否则 Chromium 会丢掉 HOME/PATH 等基础变量。
        返回 ``(env, 拒绝记录文件路径)``;``env`` 为 ``None`` 表示无需覆盖,
        直接继承父进程环境。
        """
        if state.edition != "pro" or not self._license_key:
            return None, ""
        merged = dict(os.environ)
        merged["CLOAKBROWSER_LICENSE_KEY"] = self._license_key
        # 这里只放 key 与拒绝记录路径;不额外注入宿主环境里没有的东西。
        merged.pop("CLOAKBROWSER_SKIP_CHECKSUM", None)
        status_file = self.mint_denial_path()
        if status_file:
            merged["CLOAKBROWSER_LICENSE_STATUS_FILE"] = status_file
        return merged, status_file

    def mint_denial_path(self) -> str:
        """为一次 Pro 内核启动申请"授权拒绝"记录文件。

        并发超限这类拒绝是在 CDP 握手**之后**才判定的:浏览器会自己退出,而
        Playwright 那时已经拿到连接,退出码根本不会作为启动失败冒出来。内核会
        把退出码写进这个文件,后续操作失败时再读回来,才能给出"并发用满"而不是
        "浏览器莫名关闭"。旧内核不认识这个变量,只是不写文件,不影响启动。
        """
        try:
            _, _, cb_license = wrapper_modules()
            mint = getattr(cb_license, "mint_denial_file", None)
            return str(mint() or "") if callable(mint) else ""
        except Exception:
            return ""

    def denial_reason(self, status_file: str) -> str:
        """读取并消费一次授权拒绝记录;没有拒绝时返回空串。"""
        if not status_file:
            return ""
        try:
            _, _, cb_license = wrapper_modules()
            reader = getattr(cb_license, "read_denial_file", None)
            if not callable(reader):
                return ""
            code = reader(status_file)
        except Exception:
            return ""
        if code is None:
            return ""
        return LICENSE_DENIAL_MESSAGES.get(
            int(code), f"CloakBrowser Pro:授权被拒绝(退出码 {code})")

    def describe_launch_failure(self, error_text: str) -> str:
        """把 Pro 内核的 License 退出码翻成中文;非 License 失败返回空串。"""
        try:
            _, _, cb_license = wrapper_modules()
        except CloakBrowserError:
            return ""
        try:
            message = cb_license.license_error_message(error_text)
        except Exception:  # pragma: no cover - wrapper 结构变化兜底
            return ""
        if not message:
            return ""
        # 上游给的是英文原文,按退出码换成中文;认不出来时保留原文而不是丢掉。
        for code, chinese in LICENSE_DENIAL_MESSAGES.items():
            english = _wrapper_license_message(cb_license, code)
            if english and english == message:
                return chinese
        return str(message)

    def status(self) -> Dict[str, Any]:
        """给接口/前端用的完整状态,不含明文 key。"""
        state = self._state or self.inspect()
        payload = state.to_dict()
        payload.update({
            # 与 CloakBrowserBackend.available 用同一口径:内核目录被外部删掉时,
            # 两个接口不能一个说可用、一个说不可用。
            "available": bool(
                state.installed and state.executable_path
                and Path(state.executable_path).is_file()),
            "allow_headless": self.allow_headless,
            "platform": self.platform,
            "release_channel": self.release_channel,
            "auto_download": self.auto_download,
            "cache_dir": str(self.cache_dir),
            "custom_executable": self._executable_path,
            # 私有镜像地址可能带 user:pass@,脱敏后再出接口。
            "download_url_override": _mask_url_credentials(
                self._download_url_override()),
            "wrapper_version": _wrapper_version(),
            "python": sys.executable,
        })
        return payload


def _wrapper_license_message(cb_license: Any, code: int) -> str:
    """取上游对某个退出码的英文原文,用来把它映射成中文。"""
    try:
        return str(cb_license._LICENSE_EXIT_MESSAGES.get(code, ""))
    except Exception:
        return ""


def _mask_url_credentials(url: str) -> str:
    value = str(url or "").strip()
    if not value:
        return ""
    return re.sub(r"://[^/@\s]+@", "://***@", value)


def _wrapper_version() -> str:
    try:
        from cloakbrowser import __version__  # noqa: PLC0415
        return str(__version__)
    except Exception:
        return ""


def _normalize_platform(value: str) -> str:
    requested = str(value or "auto").strip().lower()
    if requested in {"windows", "linux", "macos"}:
        return requested
    return host_platform()


def host_platform() -> str:
    if sys.platform == "darwin":
        return "macos"
    if os.name == "nt":
        return "windows"
    return "linux"
