"""测试期的全局护栏。

CloakBrowser 是默认内核,而 ``BrowserManager`` 在首次启动内核时会调用官方
wrapper 去下载约 200MB 的二进制。测试必须是离线且可重复的,所以这里把真实的
下载与授权校验入口堵死:需要走这两条路径的测试一律 mock
``app.browser.cloak_runtime.wrapper_modules``,漏掉 mock 的测试会直接报错,
而不是悄悄联网下载。
"""
import pytest


class RealNetworkCallInTest(AssertionError):
    """测试里触发了真实的 CloakBrowser 网络调用。"""


_BLOCKED = {
    "download": ("ensure_binary", "测试禁止真实下载 CloakBrowser 内核"),
    "license": ("validate_license", "测试禁止真实校验 CloakBrowser License"),
}


@pytest.fixture(autouse=True)
def block_real_cloakbrowser_network():
    try:
        from cloakbrowser import download as cb_download
        from cloakbrowser import license as cb_license
    except Exception:  # 未安装 cloakbrowser 时无需护栏
        yield
        return

    modules = {"download": cb_download, "license": cb_license}
    saved = {}
    for key, (name, message) in _BLOCKED.items():
        module = modules[key]
        saved[key] = getattr(module, name)

        def _blocked(*_args, __message=message, **_kwargs):
            raise RealNetworkCallInTest(
                f"{__message};请 mock app.browser.cloak_runtime.wrapper_modules")

        setattr(module, name, _blocked)
    try:
        yield
    finally:
        for key, (name, _message) in _BLOCKED.items():
            setattr(modules[key], name, saved[key])
