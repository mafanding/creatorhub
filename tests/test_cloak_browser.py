"""CloakBrowser 内核支持:Pro/GitHub 版判定、环境隔离、启动计划与接口层。

这些用例**绝不联网**:所有对官方 ``cloakbrowser`` wrapper 的调用都通过 patch
``app.browser.cloak_runtime.wrapper_modules`` 拦截,假的 ``ensure_binary`` 只在
临时目录里造一个占位文件,不会真的拉 200MB 内核。
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app.db as db
import app.main as main
from app.browser.backends import (
    ACCOUNT_BROWSER_BACKENDS,
    ENGINE_IDENTITY_BACKENDS,
    GLOBAL_BROWSER_BACKENDS,
    BrowserBackendUnavailableError,
    CloakBrowserBackend,
    parse_extra_launch_args,
)
from app.browser.cloak_runtime import (
    CLOAK_BROWSER_BACKEND,
    CLOAK_FREE_RUNTIME_ID,
    CLOAK_PRO_RUNTIME_ID,
    DEFAULT_STORAGE_QUOTA_MB,
    CloakBinaryState,
    CloakBrowserError,
    CloakBrowserRuntime,
    LICENSE_DENIAL_MESSAGES,
    CloakLicenseState,
    cloak_fingerprint_seed,
    mask_license_key,
    version_at_least,
    version_from_binary_path,
)
from app.browser.identity import Identity
from app.browser.manager import BrowserManager
from app.config import Config
from app.models import AppSetting

FREE_VERSION = "146.0.7680.177.5"
PRO_VERSION = "151.0.7922.108.3"
LICENSE_KEY = "cb_live_9f2a7c31d84b5e60"

# 官方 wrapper 会读的所有环境变量:测试机上可能已经装过 CloakBrowser,必须先
# 清干净,否则"回退免费版""自定义下载源"这类判定会被宿主机残留污染。
_CLOAK_ENV_NAMES = (
    "CLOAKBROWSER_LICENSE_KEY",
    "CREATORHUB_CLOAKBROWSER_LICENSE_KEY",
    "CLOAKBROWSER_CACHE_DIR",
    "CLOAKBROWSER_BINARY_PATH",
    "CLOAKBROWSER_VERSION",
    "CLOAKBROWSER_RELEASE_CHANNEL",
    "CLOAKBROWSER_DOWNLOAD_URL",
)


PLATFORM_TAG = "fixture-x64"


def binary_fixture_path(cache: Path, version: str, *, pro: bool = False) -> Path:
    """按官方 wrapper 的缓存布局拼出内核路径(与宿主平台一致)。

    macOS 的 ``Chromium.app/Contents/MacOS/Chromium`` 正好是版本目录下 4 层,
    也是 ``version_from_binary_path`` 回溯深度的上限,顺带把它一起压住。
    """
    base = Path(cache) / f"chromium-{version}{'-pro' if pro else ''}"
    if sys.platform == "darwin":
        return base / "Chromium.app" / "Contents" / "MacOS" / "Chromium"
    if os.name == "nt":
        return base / "chrome.exe"
    return base / "chrome"


class _FakeConfig:
    """``cloakbrowser.config`` 的替身:只回答版本、平台标签与内核落地路径。"""

    def __init__(self, cache_dir: Path, *, free_version: str = FREE_VERSION,
                 pro_version: str = PRO_VERSION):
        self.cache_dir = Path(cache_dir)
        self.free_version = free_version
        self.pro_version = pro_version

    def get_platform_tag(self) -> str:
        return PLATFORM_TAG

    def get_chromium_version(self) -> str:
        return self.free_version

    def get_effective_version(self, pro: bool = False,
                              release_channel=None) -> str:
        return self.pro_version if pro else self.free_version

    def get_binary_path(self, version=None, pro: bool = False) -> Path:
        version = version or self.get_effective_version(pro=pro)
        return binary_fixture_path(self.cache_dir, version, pro=pro)


class _FakeDownload:
    """``cloakbrowser.download`` 的替身:记录调用并在磁盘上造一个占位内核。"""

    def __init__(self, config: _FakeConfig, *, pro_error: Exception = None):
        self.config = config
        self.pro_error = pro_error
        self.calls = []      # 每次调用实际收到的 kwargs
        self.env_seen = []   # 调用瞬间 wrapper 能看到的环境变量

    def ensure_binary(self, **kwargs):
        self.calls.append(dict(kwargs))
        self.env_seen.append({
            name: os.environ.get(name) for name in _CLOAK_ENV_NAMES})
        pro = "license_key" in kwargs
        if pro and self.pro_error is not None:
            raise self.pro_error
        version = kwargs.get("browser_version") or (
            self.config.pro_version if pro else self.config.free_version)
        path = self.config.get_binary_path(version, pro=pro)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"cloak-fixture")
        return str(path)


# 上游 wrapper 对退出码 76 的英文原文(cloakbrowser/license.py `_LICENSE_EXIT_MESSAGES`)。
# 用真实原文当 fixture,才能证明中文映射是我们自己做的,而不是把上游想象成中文。
UPSTREAM_SEAT_LIMIT_MESSAGE = (
    "CloakBrowser Pro: session limit reached for your plan. Close another "
    "running session or upgrade your plan.")


class _FakeLicense:
    """``cloakbrowser.license`` 的替身。``info=None`` 表示服务器不可达。"""

    _LICENSE_EXIT_MESSAGES = {76: UPSTREAM_SEAT_LIMIT_MESSAGE}

    def __init__(self, info=None, *, error_message: str = "",
                 denial_code=None):
        self.info = info
        self.error_message = error_message
        self.denial_code = denial_code
        self.validate_calls = []
        self.error_calls = []
        self.denial_reads = []
        self.minted = []

    def mint_denial_file(self):
        path = f"/tmp/cloak-denial-{len(self.minted)}"
        self.minted.append(path)
        return path

    def read_denial_file(self, path):
        self.denial_reads.append(path)
        code, self.denial_code = self.denial_code, None
        return code

    def validate_license(self, license_key):
        self.validate_calls.append(license_key)
        return self.info

    def license_error_message(self, error_text):
        self.error_calls.append(error_text)
        return self.error_message or None


def _license_info(valid=True, plan="pro", expires="2027-01-01"):
    return SimpleNamespace(valid=valid, plan=plan, expires=expires)


class _CloakEnvTestCase(unittest.TestCase):
    """公共脚手架:临时目录 + 干净的 CLOAKBROWSER_* 环境。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cache = self.root / "cloakbrowser"
        env_patch = patch.dict(os.environ, {}, clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for name in _CLOAK_ENV_NAMES:
            os.environ.pop(name, None)
        self.addCleanup(self.tmp.cleanup)

    def make_wrapper(self, *, info=_license_info(), pro_error=None,
                     error_message="", free_version=FREE_VERSION,
                     pro_version=PRO_VERSION, denial_code=None):
        config = _FakeConfig(self.cache, free_version=free_version,
                             pro_version=pro_version)
        download = _FakeDownload(config, pro_error=pro_error)
        license_stub = _FakeLicense(info, error_message=error_message,
                                    denial_code=denial_code)
        return config, download, license_stub

    def patch_wrapper(self, bundle):
        """把 wrapper 注入点换成假模块,直到本条用例结束。"""
        patcher = patch("app.browser.cloak_runtime.wrapper_modules",
                        return_value=bundle)
        patcher.start()
        self.addCleanup(patcher.stop)
        return patcher

    def make_runtime(self, **kwargs) -> CloakBrowserRuntime:
        kwargs.setdefault("cache_dir", str(self.cache))
        kwargs.setdefault("log", lambda message: None)
        return CloakBrowserRuntime(**kwargs)

    def install_binary(self, version: str, *, pro: bool = False) -> Path:
        """在假的 wrapper 缓存目录里落一个可执行的占位内核。"""
        path = binary_fixture_path(self.cache, version, pro=pro)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"cloak-fixture")
        path.chmod(0o755)
        return path

    def write_marker(self, name: str, value: str) -> None:
        self.cache.mkdir(parents=True, exist_ok=True)
        (self.cache / name).write_text(value, encoding="utf-8")

    def installed_state(self, *, edition="free", version=PRO_VERSION,
                        installed=True, license_state=None) -> CloakBinaryState:
        """造一个"内核已经在磁盘上"的解析结果,免去跑 resolve()。"""
        pro = edition == "pro"
        path = binary_fixture_path(self.cache, version, pro=pro)
        if installed:
            self.install_binary(version, pro=pro)
        return CloakBinaryState(
            edition=edition, version=version, executable_path=str(path),
            installed=installed,
            detail="" if installed else "CloakBrowser 内核尚未下载",
            license=license_state or CloakLicenseState(
                configured=pro, valid=pro,
                masked_key=mask_license_key(LICENSE_KEY) if pro else ""))


class CloakRuntimeHelperTests(unittest.TestCase):
    """种子映射、脱敏与版本号解析的边界。"""

    def test_seed_is_deterministic_and_inside_signed_int_range(self):
        first = cloak_fingerprint_seed("account-fixture")
        second = cloak_fingerprint_seed("account-fixture")

        self.assertEqual(first, second)
        self.assertGreaterEqual(first, 10000)
        self.assertLessEqual(first, 2 ** 31 - 1)
        self.assertNotEqual(first, cloak_fingerprint_seed("account-other"))
        # 已经落在合法区间的纯数字种子原样保留;越界/过小的要重新映射。
        self.assertEqual(cloak_fingerprint_seed("123456"), 123456)
        self.assertGreaterEqual(cloak_fingerprint_seed("2023"), 10000)
        self.assertLessEqual(cloak_fingerprint_seed(""), 2 ** 31 - 1)
        self.assertGreaterEqual(cloak_fingerprint_seed(""), 10000)

    def test_mask_license_key_never_returns_the_full_key(self):
        self.assertEqual(mask_license_key(""), "")
        self.assertEqual(mask_license_key("   "), "")
        self.assertEqual(mask_license_key("cb_1234"), "cb_***")
        masked = mask_license_key(LICENSE_KEY)
        self.assertEqual(masked, "cb_liv…5e60")
        self.assertNotIn(LICENSE_KEY, masked)
        self.assertLess(len(masked), len(LICENSE_KEY))

    def test_version_from_binary_path_reads_wrapper_cache_layout(self):
        base = Path("/tmp/cloak-cache")
        pro = (base / f"chromium-{PRO_VERSION}-pro" / "Chromium.app"
               / "Contents" / "MacOS" / "Chromium")
        free = base / f"chromium-{FREE_VERSION}" / "chrome-linux" / "chrome"

        self.assertEqual(version_from_binary_path(pro), (PRO_VERSION, True))
        self.assertEqual(version_from_binary_path(free), (FREE_VERSION, False))
        # 四段版本号(GitHub 版偶尔如此)同样要能解析。
        self.assertEqual(
            version_from_binary_path(base / "chromium-148.0.7778.215" / "chrome"),
            ("148.0.7778.215", False))
        self.assertEqual(
            version_from_binary_path(base / "unknown-layout" / "chrome"),
            ("", False))
        self.assertEqual(version_from_binary_path(""), ("", False))

    def test_version_at_least_treats_unknown_version_as_too_old(self):
        floor = "148.0.7778.215.4"

        self.assertTrue(version_at_least(floor, floor))
        self.assertTrue(version_at_least("151.0.7922.108.3", floor))
        self.assertFalse(version_at_least("148.0.7778.215.3", floor))
        # 段数更少的版本按元组比较天然更小,不能误判成"已支持"。
        self.assertFalse(version_at_least("148.0.7778.215", floor))
        self.assertFalse(version_at_least("", floor))
        self.assertFalse(version_at_least(floor, ""))
        self.assertFalse(version_at_least("preview", floor))


class CloakRuntimeResolveTests(_CloakEnvTestCase):
    """Pro / GitHub 免费版的判定与回退,以及 wrapper 的环境隔离。"""

    def test_valid_license_resolves_pro_runtime(self):
        _, download, license_stub = bundle = self.make_wrapper(
            info=_license_info(valid=True, plan="pro"))
        self.patch_wrapper(bundle)
        runtime = self.make_runtime(license_key=LICENSE_KEY)

        state = runtime.resolve()

        self.assertEqual(license_stub.validate_calls, [LICENSE_KEY])
        self.assertEqual(len(download.calls), 1)
        self.assertEqual(download.calls[0]["license_key"], LICENSE_KEY)
        self.assertEqual(download.calls[0]["release_channel"], "stable")
        self.assertEqual(state.edition, "pro")
        self.assertEqual(state.runtime_id, CLOAK_PRO_RUNTIME_ID)
        self.assertEqual(state.version, PRO_VERSION)
        self.assertTrue(state.installed)
        self.assertTrue(state.license.valid)
        self.assertTrue(state.license.entitles_pro)
        self.assertEqual(state.license.masked_key, mask_license_key(LICENSE_KEY))
        self.assertIn("Pro", state.label)

    def test_invalid_license_falls_back_to_free_without_passing_key(self):
        _, download, license_stub = bundle = self.make_wrapper(
            info=_license_info(valid=False, plan="", expires=""))
        self.patch_wrapper(bundle)
        runtime = self.make_runtime(license_key=LICENSE_KEY)

        state = runtime.resolve()

        self.assertEqual(license_stub.validate_calls, [LICENSE_KEY])
        self.assertEqual(len(download.calls), 1)
        # 免费通道必须**完全不传** license_key,而不是传一个空值。
        self.assertNotIn("license_key", download.calls[0])
        self.assertEqual(state.edition, "free")
        self.assertEqual(state.runtime_id, CLOAK_FREE_RUNTIME_ID)
        self.assertEqual(state.version, FREE_VERSION)
        self.assertTrue(state.installed)
        self.assertTrue(state.license.configured)
        self.assertFalse(state.license.valid)
        self.assertFalse(state.license.entitles_pro)
        self.assertIn("回退 GitHub 免费版", state.detail)

    def test_unreachable_license_server_falls_back_to_free(self):
        _, download, _ = bundle = self.make_wrapper(info=None)
        self.patch_wrapper(bundle)
        runtime = self.make_runtime(license_key=LICENSE_KEY)

        state = runtime.resolve()

        self.assertEqual(state.edition, "free")
        self.assertEqual(state.runtime_id, CLOAK_FREE_RUNTIME_ID)
        self.assertNotIn("license_key", download.calls[0])
        self.assertIn("License 服务器不可达", state.detail)

    def test_missing_license_key_goes_straight_to_free(self):
        _, download, license_stub = bundle = self.make_wrapper()
        self.patch_wrapper(bundle)
        runtime = self.make_runtime()

        state = runtime.resolve()

        self.assertEqual(license_stub.validate_calls, [])
        self.assertEqual(len(download.calls), 1)
        self.assertNotIn("license_key", download.calls[0])
        self.assertEqual(state.edition, "free")
        self.assertEqual(state.runtime_id, CLOAK_FREE_RUNTIME_ID)
        self.assertFalse(state.license.configured)
        self.assertEqual(state.license.masked_key, "")

    def test_pro_download_failure_falls_back_to_free_instead_of_raising(self):
        _, download, _ = bundle = self.make_wrapper(
            info=_license_info(valid=True, plan="pro"),
            pro_error=RuntimeError("pro artifact 404"))
        self.patch_wrapper(bundle)
        logged = []
        runtime = self.make_runtime(license_key=LICENSE_KEY,
                                    log=logged.append)

        state = runtime.resolve()

        self.assertEqual(len(download.calls), 2)
        self.assertEqual(download.calls[0]["license_key"], LICENSE_KEY)
        self.assertNotIn("license_key", download.calls[1])
        self.assertEqual(state.edition, "free")
        self.assertEqual(state.runtime_id, CLOAK_FREE_RUNTIME_ID)
        self.assertEqual(state.version, FREE_VERSION)
        self.assertTrue(state.installed)
        self.assertIn("Pro 内核获取失败", state.detail)
        self.assertIn("pro artifact 404", state.detail)
        # key 本身仍然有效,只是这次拿不到 Pro 内核。
        self.assertTrue(state.license.valid)
        self.assertTrue(logged)

    def test_unexpected_pro_error_also_falls_back_to_free(self):
        class WeirdError(Exception):
            pass

        _, download, _ = bundle = self.make_wrapper(
            info=_license_info(valid=True, plan="team"),
            pro_error=WeirdError("签名校验失败"))
        self.patch_wrapper(bundle)
        runtime = self.make_runtime(license_key=LICENSE_KEY)

        state = runtime.resolve()

        self.assertEqual(state.edition, "free")
        self.assertEqual(len(download.calls), 2)
        self.assertIn("签名校验失败", state.detail)

    def test_wrapper_never_sees_host_license_key_on_the_free_path(self):
        _, download, _ = bundle = self.make_wrapper()
        self.patch_wrapper(bundle)
        os.environ["CLOAKBROWSER_LICENSE_KEY"] = "host-machine-key"
        os.environ["CLOAKBROWSER_CACHE_DIR"] = str(self.root / "host-cache")
        runtime = self.make_runtime()

        runtime.resolve()

        seen = download.env_seen[0]
        # 宿主机残留的 key 会让"回退免费版"变成空操作,必须被挡在 wrapper 之外。
        self.assertIsNone(seen["CLOAKBROWSER_LICENSE_KEY"])
        self.assertEqual(seen["CLOAKBROWSER_CACHE_DIR"], str(self.cache.resolve()))
        self.assertEqual(seen["CLOAKBROWSER_RELEASE_CHANNEL"], "stable")
        self.assertIsNone(seen["CLOAKBROWSER_BINARY_PATH"])
        # 调用结束后原环境完整还原(包含"调用前不存在"的那几个)。
        self.assertEqual(os.environ["CLOAKBROWSER_LICENSE_KEY"], "host-machine-key")
        self.assertEqual(os.environ["CLOAKBROWSER_CACHE_DIR"],
                         str(self.root / "host-cache"))
        self.assertNotIn("CLOAKBROWSER_RELEASE_CHANNEL", os.environ)
        self.assertNotIn("CLOAKBROWSER_VERSION", os.environ)

    def test_pro_path_passes_the_key_explicitly_not_via_host_env(self):
        _, download, _ = bundle = self.make_wrapper(
            info=_license_info(valid=True, plan="pro"))
        self.patch_wrapper(bundle)
        os.environ["CLOAKBROWSER_LICENSE_KEY"] = "host-machine-key"
        runtime = self.make_runtime(license_key=LICENSE_KEY)

        runtime.resolve()

        self.assertEqual(download.calls[0]["license_key"], LICENSE_KEY)
        self.assertEqual(download.env_seen[0]["CLOAKBROWSER_LICENSE_KEY"],
                         LICENSE_KEY)
        self.assertEqual(os.environ["CLOAKBROWSER_LICENSE_KEY"],
                         "host-machine-key")

    def test_disabled_auto_download_without_binary_raises(self):
        bundle = self.make_wrapper()
        self.patch_wrapper(bundle)
        runtime = self.make_runtime(auto_download=False)

        with self.assertRaisesRegex(CloakBrowserError, "关闭自动下载"):
            runtime.resolve()

    def test_set_license_key_invalidates_cached_resolution(self):
        _, download, license_stub = bundle = self.make_wrapper(
            info=_license_info(valid=True, plan="pro"))
        self.patch_wrapper(bundle)
        runtime = self.make_runtime()

        first = runtime.resolve()
        self.assertEqual(first.edition, "free")

        runtime.set_license_key(LICENSE_KEY)
        self.assertIsNone(runtime.cached_state)
        second = runtime.resolve()

        self.assertEqual(second.edition, "pro")
        self.assertEqual(license_stub.validate_calls, [LICENSE_KEY])
        self.assertEqual(len(download.calls), 2)

    def test_ensure_ready_reuses_cached_state_without_touching_wrapper(self):
        _, download, _ = bundle = self.make_wrapper()
        self.patch_wrapper(bundle)
        runtime = self.make_runtime()

        first = asyncio.run(runtime.ensure_ready())
        second = asyncio.run(runtime.ensure_ready())

        self.assertIs(first, second)
        self.assertEqual(len(download.calls), 1)

    def test_inspect_reads_disk_only_and_never_validates_the_license(self):
        _, download, license_stub = bundle = self.make_wrapper()
        self.patch_wrapper(bundle)
        self.install_binary(PRO_VERSION, pro=True)
        self.write_marker(f"latest_pro_version_{PLATFORM_TAG}", PRO_VERSION)
        runtime = self.make_runtime(license_key=LICENSE_KEY)

        state = runtime.inspect()

        self.assertEqual(state.edition, "pro")
        self.assertEqual(state.version, PRO_VERSION)
        self.assertTrue(state.installed)
        self.assertEqual(
            Path(state.executable_path).resolve(),
            binary_fixture_path(self.cache, PRO_VERSION, pro=True).resolve())
        # 只读磁盘:既不校验 License,也不下载内核。
        self.assertEqual(license_stub.validate_calls, [])
        self.assertEqual(download.calls, [])
        self.assertEqual(state.license.masked_key,
                         mask_license_key(LICENSE_KEY))

    def test_inspect_reports_free_edition_when_no_pro_binary_is_present(self):
        bundle = self.make_wrapper()
        self.patch_wrapper(bundle)
        self.install_binary(FREE_VERSION)
        runtime = self.make_runtime(license_key=LICENSE_KEY)

        state = runtime.inspect()

        self.assertEqual(state.edition, "free")
        self.assertEqual(state.runtime_id, CLOAK_FREE_RUNTIME_ID)
        self.assertEqual(state.version, FREE_VERSION)
        self.assertTrue(state.installed)

    def test_status_reports_edition_without_leaking_the_key(self):
        bundle = self.make_wrapper(info=_license_info(valid=True, plan="pro"))
        self.patch_wrapper(bundle)
        runtime = self.make_runtime(license_key=LICENSE_KEY)
        runtime.resolve()

        status = runtime.status()

        self.assertEqual(status["edition"], "pro")
        self.assertEqual(status["runtime_id"], CLOAK_PRO_RUNTIME_ID)
        self.assertEqual(status["version"], PRO_VERSION)
        self.assertTrue(status["available"])
        self.assertTrue(status["installed"])
        self.assertEqual(status["release_channel"], "stable")
        self.assertTrue(status["auto_download"])
        self.assertEqual(status["cache_dir"], str(self.cache.resolve()))
        self.assertEqual(status["license"]["masked_key"],
                         mask_license_key(LICENSE_KEY))
        self.assertNotIn(LICENSE_KEY, json.dumps(status, ensure_ascii=False))

    def test_custom_executable_path_reports_custom_edition(self):
        bundle = self.make_wrapper()
        self.patch_wrapper(bundle)
        custom = self.root / "chromium-149.0.1.2.3" / "chrome"
        custom.parent.mkdir(parents=True)
        custom.write_bytes(b"custom")
        runtime = self.make_runtime(license_key=LICENSE_KEY,
                                    executable_path=str(custom))

        state = runtime.resolve()

        self.assertEqual(state.edition, "custom")
        self.assertEqual(state.runtime_id, "cloak-custom")
        self.assertEqual(state.version, "149.0.1.2.3")
        self.assertTrue(state.installed)
        # 自定义内核不走 Pro 授权:既不覆盖子进程环境,也不申请拒绝记录文件。
        self.assertEqual(runtime.launch_env(state), (None, ""))

    def test_describe_launch_failure_translates_license_exit_code(self):
        _, _, license_stub = bundle = self.make_wrapper(
            error_message=UPSTREAM_SEAT_LIMIT_MESSAGE)
        self.patch_wrapper(bundle)
        runtime = self.make_runtime(license_key=LICENSE_KEY)

        text = "process did exit: exitCode=76, signal=null"
        reason = runtime.describe_launch_failure(text)

        # 上游给的是英文,界面是中文,所以必须由我们翻。
        self.assertEqual(reason, LICENSE_DENIAL_MESSAGES[76])
        self.assertNotIn("session limit", reason)
        self.assertEqual(license_stub.error_calls, [text])

    def test_unknown_license_message_is_passed_through_untouched(self):
        bundle = self.make_wrapper(error_message="Some future denial text")
        self.patch_wrapper(bundle)
        runtime = self.make_runtime(license_key=LICENSE_KEY)

        reason = runtime.describe_launch_failure(
            "process did exit: exitCode=76, signal=null")

        # 认不出来的原文要原样保留,不能因为翻不了就把原因丢掉。
        self.assertEqual(reason, "Some future denial text")

    def test_post_handshake_seat_denial_is_read_from_the_status_file(self):
        _, _, license_stub = bundle = self.make_wrapper(denial_code=76)
        self.patch_wrapper(bundle)
        runtime = self.make_runtime(license_key=LICENSE_KEY)
        state = self.installed_state(edition="pro")

        env, status_file = runtime.launch_env(state)

        self.assertTrue(status_file)
        self.assertEqual(env["CLOAKBROWSER_LICENSE_STATUS_FILE"], status_file)
        self.assertEqual(runtime.denial_reason(status_file),
                         LICENSE_DENIAL_MESSAGES[76])
        # 读取是消费性的:没有新的拒绝记录时不能再报一次。
        self.assertEqual(runtime.denial_reason(status_file), "")
        self.assertEqual(license_stub.denial_reads, [status_file, status_file])

    def test_describe_launch_failure_stays_quiet_for_real_crashes(self):
        bundle = self.make_wrapper(error_message="")
        self.patch_wrapper(bundle)
        runtime = self.make_runtime(license_key=LICENSE_KEY)

        self.assertEqual(runtime.describe_launch_failure("segfault"), "")


class CloakRuntimeConcurrencyTests(_CloakEnvTestCase):
    """下载期间事件循环线程必须仍然可用(回归:曾经会把整个服务堵死)。"""

    def test_status_queries_do_not_block_on_an_in_flight_download(self):
        started = threading.Event()
        release = threading.Event()
        config, download, license_stub = self.make_wrapper()
        original = download.ensure_binary

        def slow_ensure_binary(**kwargs):
            started.set()
            # 真实场景里这里是一次约 200MB 的下载,可能几分钟。
            release.wait(10)
            return original(**kwargs)

        download.ensure_binary = slow_ensure_binary
        self.patch_wrapper((config, download, license_stub))
        runtime = self.make_runtime(license_key=LICENSE_KEY)

        async def scenario():
            task = asyncio.create_task(runtime.ensure_ready())
            await asyncio.to_thread(started.wait, 5)
            await asyncio.sleep(0.05)
            begin = time.monotonic()
            # inspect/status 只读磁盘;set_license_key 只清缓存。三者都不该
            # 去抢 resolve() 在下载期间持有的锁。
            runtime.inspect()
            runtime.status()
            runtime.set_license_key(LICENSE_KEY)
            elapsed = time.monotonic() - begin
            release.set()
            await task
            return elapsed

        elapsed = asyncio.run(scenario())

        self.assertLess(elapsed, 1.0)

    def test_download_does_not_freeze_other_accounts_browser_acquisition(self):
        """回归:内核下载曾经被整个握在 manager 级的 _cv_lock 里。

        那样一来第一个账号触发的下载会让**所有**账号的 context_for 一起停摆
        几分钟,表现得像整个服务卡死。
        """
        started = threading.Event()
        release = threading.Event()
        config, download, license_stub = self.make_wrapper()
        original = download.ensure_binary

        def slow_ensure_binary(**kwargs):
            started.set()
            release.wait(10)
            return original(**kwargs)

        download.ensure_binary = slow_ensure_binary
        self.patch_wrapper((config, download, license_stub))
        # 不预置 _state:这条用例要的正是"内核还没落地,必须先下载"。
        manager = BrowserManager(
            "Mozilla/5.0 Chrome/130.0.0.0 Safari/537.36",
            str(self.root / "profiles"),
            cloak_browser_license_key=LICENSE_KEY,
            cloak_browser_cache_dir=str(self.cache))
        manager._pw = SimpleNamespace(chromium=_ChromiumStub())
        downloading = Identity(
            account_id=61, profile_dir=str(self.root / "account-61"),
            identity_mode="native", browser_backend=CLOAK_BROWSER_BACKEND)
        unrelated = Identity(
            account_id=62, profile_dir=str(self.root / "account-62"),
            identity_mode="native", browser_backend="local")

        async def scenario():
            task = asyncio.create_task(manager.context_for(downloading))
            await asyncio.to_thread(started.wait, 5)
            await asyncio.sleep(0.05)
            begin = time.monotonic()
            await manager.context_for(unrelated)
            elapsed = time.monotonic() - begin
            release.set()
            await task
            return elapsed

        self.assertLess(asyncio.run(scenario()), 1.0)

    def test_inspect_reads_the_cache_directory_without_touching_the_environment(self):
        self.patch_wrapper(self.make_wrapper())
        os.environ["CLOAKBROWSER_LICENSE_KEY"] = "cb_host_machine_key"
        runtime = self.make_runtime()

        runtime.inspect()

        # inspect() 不进入 _wrapper_env,所以宿主机的环境变量原样保留。
        self.assertEqual(os.environ["CLOAKBROWSER_LICENSE_KEY"],
                         "cb_host_machine_key")
        self.assertNotIn("CLOAKBROWSER_CACHE_DIR", os.environ)


class CloakBrowserBackendLaunchPlanTests(_CloakEnvTestCase):
    """启动计划:CloakBrowser 原生参数、无头视口与 Pro 的 License 环境。"""

    def make_backend(self, *, edition="free", version=PRO_VERSION,
                     installed=True, license_key="", allow_headless=True,
                     platform="windows") -> CloakBrowserBackend:
        runtime = self.make_runtime(license_key=license_key,
                                    allow_headless=allow_headless,
                                    platform=platform)
        runtime._state = self.installed_state(
            edition=edition, version=version, installed=installed)
        return CloakBrowserBackend(runtime)

    @staticmethod
    def identity_for(root: Path, **kwargs) -> Identity:
        base = dict(
            account_id=21,
            profile_dir=str(root / "account-21"),
            identity_mode="native",
            browser_backend=CLOAK_BROWSER_BACKEND,
            fp_seed="cloak-fixture",
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        base.update(kwargs)
        return Identity(**base)

    def test_launch_plan_uses_cloak_native_switches(self):
        backend = self.make_backend(edition="free", version=FREE_VERSION)
        identity = self.identity_for(self.root)

        plan = backend.launch_plan(identity, requested_headless=False)
        args = plan.args

        self.assertEqual(plan.name, CLOAK_BROWSER_BACKEND)
        self.assertTrue(plan.engine_controlled_identity)
        self.assertEqual(plan.runtime_id, CLOAK_FREE_RUNTIME_ID)
        self.assertEqual(plan.version, FREE_VERSION)
        self.assertIn("--fingerprint-timezone=Asia/Shanghai", args)
        self.assertIn("--fingerprint-locale=zh-CN", args)
        self.assertIn("--lang=zh-CN", args)
        self.assertIn(
            f"--fingerprint-storage-quota={DEFAULT_STORAGE_QUOTA_MB}", args)
        self.assertIn("--fingerprint-storage-quota=5000", args)
        self.assertIn("--fingerprint-platform=windows", args)
        # Fingerprint Chromium 的参数名在 CloakBrowser 上是无效开关,不能混用。
        self.assertNotIn("--timezone=Asia/Shanghai", args)
        self.assertFalse([a for a in args if a.startswith("--timezone=")])
        self.assertFalse([a for a in args if a.startswith("--disable-spoofing")])
        self.assertEqual(len(set(args)), len(args))

    def test_launch_plan_seed_is_deterministic_and_in_range(self):
        backend = self.make_backend()
        identity = self.identity_for(self.root, fp_seed="seed-a")

        first = backend.launch_plan(identity, requested_headless=False).args
        second = backend.launch_plan(identity, requested_headless=False).args
        seed_args = [a for a in first if a.startswith("--fingerprint=")]

        self.assertEqual(first, second)
        self.assertEqual(len(seed_args), 1)
        seed = int(seed_args[0].split("=", 1)[1])
        self.assertEqual(seed, cloak_fingerprint_seed("seed-a"))
        self.assertGreaterEqual(seed, 10000)
        self.assertLessEqual(seed, 2 ** 31 - 1)

    def test_launch_plan_applies_optional_account_overrides(self):
        backend = self.make_backend()
        identity = self.identity_for(
            self.root,
            fp_platform="macos",
            fp_platform_version="15.2.0",
            fp_brand="Edge",
            fp_brand_version="151.0.0.0",
            fp_hardware_concurrency=12,
            fp_gpu_vendor="Apple Inc.",
            fp_gpu_renderer="Apple M3",
            fp_accept_languages="zh-CN,zh;q=0.9",
        )

        args = backend.launch_plan(identity, requested_headless=False).args

        self.assertIn("--fingerprint-platform=macos", args)
        self.assertIn("--fingerprint-platform-version=15.2.0", args)
        self.assertIn("--fingerprint-brand=Edge", args)
        self.assertIn("--fingerprint-brand-version=151.0.0.0", args)
        self.assertIn("--fingerprint-hardware-concurrency=12", args)
        self.assertIn("--fingerprint-gpu-vendor=Apple Inc.", args)
        self.assertIn("--fingerprint-gpu-renderer=Apple M3", args)
        self.assertIn("--accept-lang=zh-CN,zh;q=0.9", args)
        self.assertIn("--ignore-gpu-blocklist", args)

    def test_disable_spoofing_degrades_to_fingerprint_noise_switch(self):
        backend = self.make_backend()

        noisy = backend.launch_plan(
            self.identity_for(self.root, fp_disable_spoofing="canvas,audio"),
            requested_headless=False).args
        gpu_only = backend.launch_plan(
            self.identity_for(self.root, fp_disable_spoofing="gpu"),
            requested_headless=False).args

        self.assertIn("--fingerprint-noise=false", noisy)
        self.assertNotIn("--fingerprint-noise=false", gpu_only)

    def test_old_runtime_gets_an_explicit_headless_viewport(self):
        backend = self.make_backend(version="148.0.7778.215.3",
                                    allow_headless=True, platform="windows")
        identity = self.identity_for(self.root, viewport_w=1920,
                                     viewport_h=1080, fp_platform="windows")

        plan = backend.launch_plan(identity, requested_headless=True)

        self.assertTrue(plan.headless)
        self.assertIsNotNone(plan.headless_viewport)
        width, height = plan.headless_viewport
        # 视口必须落在内核按画像伪造的 screen(Windows 1920x1080)之内。
        self.assertLess(width, 1920)
        self.assertLess(height, 1080)
        self.assertEqual(plan.headless_viewport, (1918, 954))

    def test_new_runtime_keeps_no_viewport_in_headless(self):
        backend = self.make_backend(version="148.0.7778.215.4",
                                    allow_headless=True)
        identity = self.identity_for(self.root)

        plan = backend.launch_plan(identity, requested_headless=True)

        self.assertTrue(plan.headless)
        self.assertIsNone(plan.headless_viewport)

    def test_unknown_runtime_version_is_treated_as_old(self):
        backend = self.make_backend(version="", allow_headless=True)
        identity = self.identity_for(self.root)

        plan = backend.launch_plan(identity, requested_headless=True)

        self.assertIsNotNone(plan.headless_viewport)

    def test_allow_headless_false_forces_a_headed_launch(self):
        backend = self.make_backend(version="148.0.7778.215.3",
                                    allow_headless=False)
        identity = self.identity_for(self.root)

        plan = backend.launch_plan(identity, requested_headless=True)

        self.assertFalse(plan.headless)
        self.assertIsNone(plan.headless_viewport)
        self.assertIn("--ignore-gpu-blocklist", plan.args)

    def test_missing_binary_fails_closed(self):
        backend = self.make_backend(installed=False)
        identity = self.identity_for(self.root)

        self.assertFalse(backend.available)
        self.assertIn("CloakBrowser", backend.unavailable_reason)
        with self.assertRaises(BrowserBackendUnavailableError):
            backend.launch_plan(identity, requested_headless=False)

    def test_pro_plan_carries_license_key_and_preserves_process_env(self):
        backend = self.make_backend(edition="pro", license_key=LICENSE_KEY)
        os.environ["CREATORHUB_FIXTURE_ENV"] = "kept"
        self.addCleanup(os.environ.pop, "CREATORHUB_FIXTURE_ENV", None)
        identity = self.identity_for(self.root)

        plan = backend.launch_plan(identity, requested_headless=False)

        self.assertIsNotNone(plan.env)
        self.assertEqual(plan.env["CLOAKBROWSER_LICENSE_KEY"], LICENSE_KEY)
        # env 是替换语义,必须把父进程的其它变量原样带上。
        self.assertEqual(plan.env["CREATORHUB_FIXTURE_ENV"], "kept")
        self.assertIn("PATH", plan.env)
        self.assertEqual(plan.runtime_id, CLOAK_PRO_RUNTIME_ID)

    def test_free_and_custom_plans_do_not_override_process_env(self):
        free = self.make_backend(edition="free", license_key=LICENSE_KEY)
        free_plan = free.launch_plan(
            self.identity_for(self.root), requested_headless=False)
        self.assertIsNone(free_plan.env)

        custom = self.make_backend(edition="custom", license_key=LICENSE_KEY)
        custom_plan = custom.launch_plan(
            self.identity_for(self.root), requested_headless=False)
        self.assertIsNone(custom_plan.env)

    def test_plan_ignores_patchright_automation_switches(self):
        backend = self.make_backend()
        identity = self.identity_for(self.root)

        plan = backend.launch_plan(identity, requested_headless=False)

        self.assertIn("--enable-automation", plan.ignore_default_args)
        self.assertIn("--enable-unsafe-swiftshader", plan.ignore_default_args)
        self.assertIn("--disable-blink-features=AutomationControlled",
                      plan.ignore_default_args)

    def test_cloak_owned_switches_cannot_be_supplied_by_the_account(self):
        for token in ("--fingerprint-timezone=Asia/Tokyo",
                      "--fingerprint-locale=en-US",
                      "--fingerprint-storage-quota=1",
                      "--fingerprint-noise=false",
                      "--license-through-proxy"):
            with self.subTest(token=token):
                with self.assertRaisesRegex(ValueError, "账号环境冲突"):
                    parse_extra_launch_args(token)

    def test_backend_registry_lists_cloak_browser(self):
        self.assertIn(CLOAK_BROWSER_BACKEND, ACCOUNT_BROWSER_BACKENDS)
        self.assertIn(CLOAK_BROWSER_BACKEND, GLOBAL_BROWSER_BACKENDS)
        self.assertIn(CLOAK_BROWSER_BACKEND, ENGINE_IDENTITY_BACKENDS)


class _PageStub:
    def __init__(self, ua="Mozilla/5.0 Chrome/151.0.0.0 Safari/537.36"):
        self.ua = ua

    async def evaluate(self, _expression):
        return self.ua

    async def close(self):
        return None


class _ContextStub:
    def __init__(self):
        self.pages = []
        self.script_calls = []

    async def new_page(self):
        return _PageStub()

    async def add_init_script(self, value):
        self.script_calls.append(value)

    async def cookies(self):
        return []

    def on(self, *_args):
        return None


class _ChromiumStub:
    def __init__(self):
        self.kwargs = None
        self.context = _ContextStub()

    async def launch_persistent_context(self, **kwargs):
        self.kwargs = kwargs
        return self.context


class CloakBrowserManagerTests(_CloakEnvTestCase):
    """BrowserManager 集成:默认后端、Profile 隔离、启动参数与写操作门禁。"""

    def make_manager(self, *, license_key="", state_edition="pro",
                     state_version=PRO_VERSION, installed=True,
                     **kwargs) -> BrowserManager:
        manager = BrowserManager(
            "Mozilla/5.0 Chrome/130.0.0.0 Safari/537.36",
            str(self.root / "profiles"),
            cloak_browser_license_key=license_key,
            cloak_browser_cache_dir=str(self.cache),
            cloak_browser_platform="windows",
            **kwargs)
        manager.cloak_runtime._state = self.installed_state(
            edition=state_edition, version=state_version, installed=installed)
        return manager

    def test_cloak_browser_is_the_global_default_backend(self):
        manager = self.make_manager()
        identity = Identity(
            account_id=41, profile_dir=str(self.root / "account-41"),
            browser_backend="default")

        self.assertEqual(manager.default_browser_backend, CLOAK_BROWSER_BACKEND)
        self.assertEqual(manager.effective_browser_backend(identity),
                         CLOAK_BROWSER_BACKEND)
        self.assertEqual(manager.effective_runtime_id(identity),
                         CLOAK_PRO_RUNTIME_ID)
        self.assertEqual(manager.effective_cloak_runtime_id(identity),
                         CLOAK_PRO_RUNTIME_ID)

    def test_xhs_account_still_pinned_to_system_chrome(self):
        manager = self.make_manager()
        identity = Identity(
            account_id=42, profile_dir=str(self.root / "account-42"),
            platform="xhs", browser_backend="default")

        self.assertEqual(manager.effective_browser_backend(identity), "local")
        self.assertEqual(manager.effective_runtime_id(identity), "")

    def test_runtime_id_follows_the_license_instead_of_the_account(self):
        manager = self.make_manager()
        identity = Identity(
            account_id=43, profile_dir=str(self.root / "account-43"),
            browser_backend=CLOAK_BROWSER_BACKEND,
            browser_runtime_id="cloak-pro")

        manager.cloak_runtime._state = self.installed_state(
            edition="free", version=FREE_VERSION)

        # key 过期后账号不该被钉死在 cloak-pro 上,自动跟随当前解析结果。
        self.assertEqual(manager.effective_runtime_id(identity),
                         CLOAK_FREE_RUNTIME_ID)

    def test_launch_persistent_uses_the_cloak_plan(self):
        manager = self.make_manager(license_key=LICENSE_KEY,
                                    state_version="148.0.7778.215.3",
                                    cloak_browser_allow_headless=True)
        manager._pw = type("PW", (), {"chromium": _ChromiumStub()})()
        manager._browser_channel = "chrome"
        identity = Identity(
            account_id=44, profile_dir=str(self.root / "account-44"),
            identity_mode="legacy", browser_backend="default",
            fp_seed="cloak-44", fp_platform="windows",
            viewport_w=1920, viewport_h=1080)

        context = asyncio.run(manager._launch_persistent(identity, headless=True))
        kwargs = manager._pw.chromium.kwargs
        self.addCleanup(manager._release_profile_lock, identity.key)

        expected = self.cache / "chromium-148.0.7778.215.3-pro"
        self.assertTrue(kwargs["executable_path"].startswith(str(expected)))
        self.assertEqual(
            Path(kwargs["user_data_dir"]),
            Path(identity.profile_dir) / "runtimes" / CLOAK_PRO_RUNTIME_ID)
        # 内核自带反自动化补丁,Patchright 的默认开关反而是特征。
        self.assertIn("--enable-automation", kwargs["ignore_default_args"])
        self.assertIn("--enable-unsafe-swiftshader",
                      kwargs["ignore_default_args"])
        # 旧内核无头必须显式给视口,而不是跟随窗口。
        self.assertEqual(kwargs["viewport"], {"width": 1918, "height": 954})
        self.assertNotIn("no_viewport", kwargs)
        self.assertFalse([a for a in kwargs["args"]
                          if a.startswith("--window-size")])
        # 内核级画像:不覆盖 UA/时区/语言,也不注入历史 JS 指纹脚本。
        self.assertNotIn("user_agent", kwargs)
        self.assertNotIn("timezone_id", kwargs)
        self.assertNotIn("locale", kwargs)
        self.assertNotIn("channel", kwargs)
        self.assertEqual(context.script_calls, [])
        # Pro 内核在子进程里自己校验 License。
        self.assertEqual(kwargs["env"]["CLOAKBROWSER_LICENSE_KEY"], LICENSE_KEY)
        self.assertIn("PATH", kwargs["env"])
        self.assertIn("--fingerprint-timezone=Asia/Shanghai", kwargs["args"])
        self.assertIn("--fingerprint-storage-quota=5000", kwargs["args"])

    def test_headed_launch_sizes_the_window_and_skips_viewport(self):
        manager = self.make_manager(state_edition="free",
                                    state_version=FREE_VERSION)
        manager._pw = type("PW", (), {"chromium": _ChromiumStub()})()
        identity = Identity(
            account_id=45, profile_dir=str(self.root / "account-45"),
            identity_mode="native", browser_backend=CLOAK_BROWSER_BACKEND,
            fp_platform="windows", viewport_w=1280, viewport_h=800)

        asyncio.run(manager._launch_persistent(identity, headless=False))
        kwargs = manager._pw.chromium.kwargs
        self.addCleanup(manager._release_profile_lock, identity.key)

        self.assertFalse(kwargs["headless"])
        self.assertTrue(kwargs["no_viewport"])
        self.assertNotIn("viewport", kwargs)
        self.assertIn("--window-size=1280,800", kwargs["args"])
        self.assertIsNone(kwargs.get("env"))
        self.assertEqual(
            Path(kwargs["user_data_dir"]),
            Path(identity.profile_dir) / "runtimes" / CLOAK_FREE_RUNTIME_ID)

    def test_launch_failure_is_translated_into_a_license_reason(self):
        bundle = self.make_wrapper(
            error_message=UPSTREAM_SEAT_LIMIT_MESSAGE)
        self.patch_wrapper(bundle)
        manager = self.make_manager(license_key=LICENSE_KEY)

        class FailingChromium:
            async def launch_persistent_context(self, **_kwargs):
                raise RuntimeError(
                    "process did exit: exitCode=76, signal=null")

        manager._pw = type("PW", (), {"chromium": FailingChromium()})()
        identity = Identity(
            account_id=46, profile_dir=str(self.root / "account-46"),
            identity_mode="native", browser_backend=CLOAK_BROWSER_BACKEND)

        with self.assertRaisesRegex(BrowserBackendUnavailableError,
                                    "并发会话数已用满"):
            asyncio.run(manager._launch_persistent(identity, headless=False))

    def test_backend_status_reports_edition_and_runtime_id(self):
        manager = self.make_manager()

        status = manager.backend_status(CLOAK_BROWSER_BACKEND)
        by_default = manager.backend_status("default")

        self.assertEqual(status["name"], CLOAK_BROWSER_BACKEND)
        self.assertEqual(status["runtime_id"], CLOAK_PRO_RUNTIME_ID)
        self.assertEqual(status["edition"], "pro")
        self.assertEqual(status["version"], PRO_VERSION)
        self.assertIn("Pro", status["label"])
        self.assertTrue(status["available"])
        self.assertEqual(status["detail"], "")
        self.assertEqual(by_default, status)

    def test_backend_catalog_puts_cloak_browser_first(self):
        manager = self.make_manager()

        catalog = manager.backend_catalog()
        names = [row["name"] for row in catalog["backends"]]

        self.assertEqual(catalog["default"], CLOAK_BROWSER_BACKEND)
        self.assertEqual(names[0], CLOAK_BROWSER_BACKEND)
        self.assertEqual(catalog["backends"][0]["edition"], "pro")
        self.assertEqual(catalog["backends"][0]["version"], PRO_VERSION)
        self.assertTrue(catalog["backends"][0]["available"])
        self.assertIn("local", names)
        self.assertIn("fingerprint_chromium", names)
        self.assertEqual(catalog["cloak"]["runtime_id"], CLOAK_PRO_RUNTIME_ID)
        self.assertIn("license", catalog["cloak"])
        self.assertIn("masked_key", catalog["cloak"]["license"])

    def test_native_write_gate_accepts_cloak_runtime_without_system_chrome(self):
        manager = self.make_manager(native_write_require_system_chrome=True,
                                    native_write_require_verified_proxy=True)
        manager._pw = object()
        manager._browser_channel = None
        account = SimpleNamespace(
            identity_mode="native", browser_backend="default",
            browser_runtime_id="", proxy="", platform="douyin")

        self.assertEqual(manager.native_write_gate_error(account), "")

    def test_native_write_gate_blocks_when_cloak_runtime_is_missing(self):
        manager = self.make_manager(installed=False,
                                    native_write_require_system_chrome=True)
        manager._pw = object()
        manager._browser_channel = None
        account = SimpleNamespace(
            identity_mode="native", browser_backend="default",
            browser_runtime_id="", proxy="", platform="douyin")

        reason = manager.native_write_gate_error(account)

        self.assertTrue(reason.startswith("write_env_blocked:"))
        self.assertIn("CloakBrowser 内核不可用", reason)
        self.assertNotIn("未检测到系统稳定版 Chrome", reason)

    def test_environment_snapshot_exposes_the_cloak_edition(self):
        manager = self.make_manager()
        identity = Identity(
            account_id=47, profile_dir=str(self.root / "account-47"),
            identity_mode="native", browser_backend=CLOAK_BROWSER_BACKEND)

        snapshot = manager.environment_snapshot(identity, headless=False)

        self.assertEqual(snapshot["backend"], CLOAK_BROWSER_BACKEND)
        self.assertEqual(snapshot["browser"], "cloakbrowser")
        self.assertEqual(snapshot["cloak_edition"], "pro")
        self.assertEqual(snapshot["runtime_id"], CLOAK_PRO_RUNTIME_ID)
        self.assertEqual(snapshot["runtime_version"], PRO_VERSION)
        self.assertIn("Pro", snapshot["backend_label"])
        self.assertTrue(snapshot["profile_dir"].endswith(
            str(Path("runtimes") / CLOAK_PRO_RUNTIME_ID)))

    def test_prepare_cloak_runtime_reports_failure_instead_of_raising(self):
        bundle = self.make_wrapper()
        self.patch_wrapper(bundle)
        manager = self.make_manager(installed=False,
                                    cloak_browser_auto_download=False)
        manager.cloak_runtime.invalidate()

        status = asyncio.run(manager.prepare_cloak_runtime())

        self.assertFalse(status["available"])
        self.assertIn("关闭自动下载", status["detail"])

    def test_prepare_cloak_runtime_downloads_and_reports_the_edition(self):
        _, download, _ = bundle = self.make_wrapper(
            info=_license_info(valid=True, plan="pro"))
        self.patch_wrapper(bundle)
        manager = self.make_manager(license_key=LICENSE_KEY)
        manager.cloak_runtime.invalidate()

        status = asyncio.run(
            manager.prepare_cloak_runtime(refresh_license=True))

        self.assertTrue(status["available"])
        self.assertEqual(status["edition"], "pro")
        self.assertEqual(status["runtime_id"], CLOAK_PRO_RUNTIME_ID)
        self.assertEqual(download.calls[0]["license_key"], LICENSE_KEY)


class CloakBrowserApiTests(_CloakEnvTestCase):
    """接口层:License 保存/清除的落库、脱敏与来源优先级。"""

    def setUp(self):
        super().setUp()
        self.previous_engine = db._engine
        self.previous_browser = main.browser
        self.previous_cfg = main.cfg
        db.init_db(str(self.root / "cloak.db"))
        main.cfg = Config()
        main.cfg.engine.profiles_dir = str(self.root / "profiles")
        main.cfg.engine.cloak_browser_license_key = ""
        self.addCleanup(self._restore)

    def _restore(self):
        main.browser = self.previous_browser
        main.cfg = self.previous_cfg
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.previous_engine

    def make_manager(self, **kwargs) -> BrowserManager:
        manager = BrowserManager(
            "UA", str(self.root / "profiles"),
            cloak_browser_cache_dir=str(self.cache), **kwargs)
        main.browser = manager
        return manager

    @staticmethod
    def spy_on_prepare(manager) -> list:
        calls = []
        original = manager.prepare_cloak_runtime

        async def spy(**kwargs):
            calls.append(kwargs)
            return await original(**kwargs)

        manager.prepare_cloak_runtime = spy
        return calls

    def test_set_license_saves_setting_and_returns_masked_status(self):
        bundle = self.make_wrapper(info=_license_info(valid=True, plan="pro"))
        self.patch_wrapper(bundle)
        manager = self.make_manager()
        calls = self.spy_on_prepare(manager)

        result = asyncio.run(main.set_cloak_browser_license(
            main.CloakLicenseIn(license_key=LICENSE_KEY)))

        with db.get_session() as session:
            saved = session.get(AppSetting, main.CLOAK_LICENSE_SETTING)
            self.assertEqual(saved.value, LICENSE_KEY)
        self.assertEqual(calls, [{"refresh_license": True}])
        self.assertEqual(manager.cloak_runtime.license_key, LICENSE_KEY)
        self.assertEqual(result["edition"], "pro")
        self.assertEqual(result["runtime_id"], CLOAK_PRO_RUNTIME_ID)
        self.assertTrue(result["available"])
        self.assertTrue(result["license"]["valid"])
        self.assertEqual(result["license"]["masked_key"],
                         mask_license_key(LICENSE_KEY))
        self.assertEqual(result["license_source"], "settings")
        self.assertTrue(result["is_default"])
        # 返回体里绝不能出现明文 key。
        self.assertNotIn(LICENSE_KEY, json.dumps(result, ensure_ascii=False))

    def test_invalid_license_response_falls_back_to_the_free_edition(self):
        _, download, _ = bundle = self.make_wrapper(
            info=_license_info(valid=False, plan="", expires=""))
        self.patch_wrapper(bundle)
        self.make_manager()

        result = asyncio.run(main.set_cloak_browser_license(
            main.CloakLicenseIn(license_key=LICENSE_KEY)))

        self.assertEqual(result["edition"], "free")
        self.assertEqual(result["runtime_id"], CLOAK_FREE_RUNTIME_ID)
        self.assertTrue(result["available"])
        self.assertTrue(result["license"]["configured"])
        self.assertFalse(result["license"]["valid"])
        self.assertNotIn("license_key", download.calls[0])
        self.assertNotIn(LICENSE_KEY, json.dumps(result, ensure_ascii=False))

    def test_clearing_the_saved_key_falls_back_to_the_config_value(self):
        bundle = self.make_wrapper(info=_license_info(valid=True, plan="pro"))
        self.patch_wrapper(bundle)
        manager = self.make_manager()
        config_key = "cb_config_1122334455667788"
        main.cfg.engine.cloak_browser_license_key = config_key
        main.set_setting(main.CLOAK_LICENSE_SETTING, LICENSE_KEY)
        manager.cloak_runtime.set_license_key(LICENSE_KEY)

        result = asyncio.run(main.clear_cloak_browser_license())

        self.assertEqual(main.get_setting(main.CLOAK_LICENSE_SETTING, ""), "")
        self.assertEqual(manager.cloak_runtime.license_key, config_key)
        self.assertEqual(result["license_source"], "config")
        self.assertEqual(result["license"]["masked_key"],
                         mask_license_key(config_key))
        dumped = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(LICENSE_KEY, dumped)
        self.assertNotIn(config_key, dumped)

    def test_license_key_input_is_validated(self):
        bundle = self.make_wrapper()
        self.patch_wrapper(bundle)
        self.make_manager()

        with self.assertRaises(main.HTTPException) as spaced:
            asyncio.run(main.set_cloak_browser_license(
                main.CloakLicenseIn(license_key="cb live key")))
        with self.assertRaises(main.HTTPException) as oversized:
            asyncio.run(main.set_cloak_browser_license(
                main.CloakLicenseIn(license_key="c" * 201)))

        self.assertEqual(spaced.exception.status_code, 400)
        self.assertEqual(oversized.exception.status_code, 400)
        self.assertEqual(main.get_setting(main.CLOAK_LICENSE_SETTING, ""), "")

    def test_get_and_prepare_endpoints_return_the_same_payload_shape(self):
        bundle = self.make_wrapper(info=_license_info(valid=True, plan="pro"))
        self.patch_wrapper(bundle)
        self.make_manager(cloak_browser_license_key=LICENSE_KEY)

        fetched = asyncio.run(main.get_cloak_browser())
        prepared = asyncio.run(main.prepare_cloak_browser(
            main.CloakPrepareIn(refresh_license=True)))

        for payload in (fetched, prepared):
            for key in ("edition", "runtime_id", "label", "version",
                        "executable_path", "installed", "detail", "available",
                        "allow_headless", "platform", "release_channel",
                        "auto_download", "cache_dir", "custom_executable",
                        "download_url_override", "wrapper_version", "python",
                        "license", "license_source", "is_default"):
                self.assertIn(key, payload)
        self.assertFalse(fetched["available"])       # 只读磁盘,不下载
        self.assertTrue(prepared["available"])       # prepare 才会落地内核
        self.assertEqual(prepared["edition"], "pro")

    def test_test_endpoint_probes_a_disposable_profile(self):
        bundle = self.make_wrapper(info=_license_info(valid=True, plan="pro"))
        self.patch_wrapper(bundle)
        manager = self.make_manager(cloak_browser_license_key=LICENSE_KEY)

        probe_ua = "Mozilla/5.0 Chrome/151.0.0.0 Safari/537.36"

        class ProbePage:
            async def evaluate(self, expression):
                if expression.strip() == "navigator.userAgent":
                    return probe_ua
                return {"userAgent": probe_ua, "platform": "Win32",
                        "language": "zh-CN", "webdriver": False}

            async def close(self):
                return None

        class ProbeContext:
            def __init__(self):
                self.pages = [ProbePage()]
                self.closed = False

            async def new_page(self):
                return ProbePage()

            async def cookies(self):
                return []

            async def close(self):
                self.closed = True

            def on(self, *_args):
                return None

        contexts = []

        class ProbeChromium:
            async def launch_persistent_context(self, **kwargs):
                context = ProbeContext()
                context.kwargs = kwargs
                contexts.append(context)
                return context

        manager._pw = type("PW", (), {"chromium": ProbeChromium()})()

        result = asyncio.run(main.test_cloak_browser())

        self.assertTrue(result["ok"])
        self.assertEqual(result["runtime_id"], CLOAK_PRO_RUNTIME_ID)
        self.assertEqual(result["user_agent"], probe_ua)
        self.assertEqual(result["platform"], "Win32")
        self.assertEqual(result["language"], "zh-CN")
        self.assertFalse(result["webdriver"])
        self.assertEqual(result["cloak"]["edition"], "pro")
        self.assertTrue(result["cloak"]["is_default"])
        self.assertTrue(contexts[0].closed)
        # 一次性 Profile 用完即删,不会污染账号目录。
        self.assertFalse(Path(contexts[0].kwargs["user_data_dir"]).exists())
        self.assertNotIn(LICENSE_KEY, json.dumps(result, ensure_ascii=False))

    def test_license_key_priority_is_settings_then_config_then_env(self):
        main.cfg.engine.cloak_browser_license_key = ""

        self.assertEqual(main._cloak_license_key(), "")
        self.assertEqual(main._cloak_license_source(), "")

        os.environ["CLOAKBROWSER_LICENSE_KEY"] = "env-wrapper-key"
        self.assertEqual(main._cloak_license_key(), "env-wrapper-key")
        self.assertEqual(main._cloak_license_source(), "env")

        os.environ["CREATORHUB_CLOAKBROWSER_LICENSE_KEY"] = "env-creatorhub-key"
        self.assertEqual(main._cloak_license_key(), "env-creatorhub-key")

        main.cfg.engine.cloak_browser_license_key = "config-key"
        self.assertEqual(main._cloak_license_key(), "config-key")
        self.assertEqual(main._cloak_license_source(), "config")

        main.set_setting(main.CLOAK_LICENSE_SETTING, "settings-key")
        self.assertEqual(main._cloak_license_key(), "settings-key")
        self.assertEqual(main._cloak_license_source(), "settings")

    def test_custom_binary_path_prefers_config_over_environment(self):
        os.environ["CLOAKBROWSER_BINARY_PATH"] = "/env/chrome"
        self.assertEqual(main._cloak_browser_path(), "/env/chrome")

        main.cfg.engine.cloak_browser_path = "/config/chrome"
        self.assertEqual(main._cloak_browser_path(), "/config/chrome")


if __name__ == "__main__":
    unittest.main()
