"""用已 hydrate 的 BrowserContext 探测平台登录态，避免保活时打开聊天页。

平台 URL、请求头和业务码判定都留在 sidecar，随 Camofox 镜像发布。
探测不得把 Cookie、Token 或响应正文写入日志；不能把 HTTP 200 当成在线。
文心没有免 tk 的用户 JSON 接口，改读首页 HTML 启动数据里的 isUserLogin。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, Error as PlaywrightError

KeepaliveStatus = Literal["ok", "login_required", "verification_required", "uncertain"]

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT_MS = 15_000
_WENXIN_BOOTSTRAP = re.compile(
    r'<script[^>]*\bname=["\']aiTabFrameBaseData["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_XSRF_COOKIE_NAMES = frozenset({"xsrf-token", "xsrftoken", "ctoken", "bx-csrf-token"})


@dataclass(frozen=True)
class SessionProbeSpec:
    """单个平台的登录态探测规格。"""

    method: str
    url: str
    origin: str
    referer: str
    extra_headers: Mapping[str, str] = field(default_factory=dict)
    json_body: Mapping[str, Any] | None = None
    classify: Callable[[int, str], KeepaliveStatus] = field(
        default=lambda status, _body: "uncertain"
    )


def _cookie_matches_host(domain: str, host: str) -> bool:
    normalized_domain = domain.lstrip(".").casefold()
    normalized_host = host.casefold()
    return normalized_host == normalized_domain or normalized_host.endswith(
        "." + normalized_domain
    )


def _xsrf_token(cookies: Sequence[Mapping[str, Any]], host: str) -> str | None:
    for item in cookies:
        name = item.get("name")
        value = item.get("value")
        domain = item.get("domain")
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        if name.casefold() not in _XSRF_COOKIE_NAMES:
            continue
        if isinstance(domain, str) and _cookie_matches_host(domain, host):
            return value
    return None


def _as_object(text: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def classify_deepseek(status: int, body: str) -> KeepaliveStatus:
    payload = _as_object(body)
    if payload is None:
        return "login_required" if status in {401, 403} else "uncertain"
    code = payload.get("code")
    data = payload.get("data")
    if code in {40002, 401, "40002"} or data is None:
        return "login_required"
    if status == 200 and isinstance(data, dict):
        return "ok"
    return "uncertain"


def classify_doubao(status: int, body: str) -> KeepaliveStatus:
    payload = _as_object(body)
    if payload is None:
        return "login_required" if status in {401, 403} else "uncertain"
    data = payload.get("data")
    if not isinstance(data, dict):
        return "login_required"
    error_code = data.get("error_code")
    user_id = data.get("user_id")
    if payload.get("message") == "error" or error_code in {13, 401}:
        return "login_required"
    if isinstance(user_id, int) and user_id > 0:
        return "ok"
    if isinstance(user_id, str) and user_id.isdigit() and int(user_id) > 0:
        return "ok"
    return "login_required"


def classify_kimi(status: int, body: str) -> KeepaliveStatus:
    if status in {401, 403}:
        return "login_required"
    payload = _as_object(body)
    if payload is None:
        return "uncertain"
    if payload.get("code") == "unauthenticated":
        return "login_required"
    if status == 200 and payload.get("user") is not None:
        return "ok"
    return "uncertain"


def classify_qianwen(status: int, body: str) -> KeepaliveStatus:
    payload = _as_object(body)
    if payload is None:
        return "login_required" if status in {401, 403} else "uncertain"
    error_code = str(payload.get("errorCode") or "")
    if error_code == "NOT_LOGIN" or payload.get("failed") is True:
        return "login_required"
    if payload.get("success") is False:
        return "login_required"
    if payload.get("success") is True or (status == 200 and error_code == ""):
        return "ok"
    return "uncertain"


def classify_yuanbao(status: int, body: str) -> KeepaliveStatus:
    if status in {401, 403}:
        return "login_required"
    payload = _as_object(body)
    if payload is None:
        return "uncertain"
    error = payload.get("error")
    if isinstance(error, dict):
        return "login_required"
    if status == 200:
        return "ok"
    return "uncertain"


def classify_wenxin(status: int, body: str) -> KeepaliveStatus:
    if status in {401, 403}:
        return "login_required"
    match = _WENXIN_BOOTSTRAP.search(body)
    if match is None:
        return "uncertain"
    payload = _as_object(match.group(1).strip())
    if payload is None:
        return "uncertain"
    user_info = payload.get("userInfo")
    if not isinstance(user_info, dict):
        return "uncertain"
    flag = user_info.get("isUserLogin")
    if flag in {1, True, "1"}:
        return "ok"
    if flag in {0, False, "0"}:
        return "login_required"
    return "uncertain"


SESSION_PROBE_SPECS: dict[str, SessionProbeSpec] = {
    "deepseek": SessionProbeSpec(
        method="GET",
        url="https://chat.deepseek.com/api/v0/users/current",
        origin="https://chat.deepseek.com",
        referer="https://chat.deepseek.com/",
        extra_headers={
            "x-client-bundle-id": "com.deepseek.chat",
            "x-client-locale": "zh_CN",
            "x-client-platform": "web",
            "x-client-version": "2.4.0",
        },
        classify=classify_deepseek,
    ),
    "doubao": SessionProbeSpec(
        method="GET",
        url="https://accounts.doubao.com/passport/account/info/v2/?aid=497858",
        origin="https://www.doubao.com",
        referer="https://www.doubao.com/",
        classify=classify_doubao,
    ),
    "kimi": SessionProbeSpec(
        method="POST",
        url="https://www.kimi.com/apiv2/kimi.gateway.account.v1.UserService/GetCurrentUser",
        origin="https://www.kimi.com",
        referer="https://www.kimi.com/",
        extra_headers={
            "x-language": "zh-CN",
            "x-msh-platform": "web",
        },
        json_body={},
        classify=classify_kimi,
    ),
    "qianwen": SessionProbeSpec(
        method="POST",
        url="https://api.qianwen.com/growth/user/benefit/user/member/info",
        origin="https://www.qianwen.com",
        referer="https://www.qianwen.com/",
        extra_headers={"x-platform": "pc_tongyi"},
        json_body={},
        classify=classify_qianwen,
    ),
    "yuanbao": SessionProbeSpec(
        method="GET",
        url="https://yuanbao.tencent.com/api/getuserinfo",
        origin="https://yuanbao.tencent.com",
        referer="https://yuanbao.tencent.com/",
        extra_headers={
            "x-source": "web",
            "x-requested-with": "XMLHttpRequest",
            "x-language": "zh-CN",
        },
        classify=classify_yuanbao,
    ),
    # 文心没有免 tk 的用户 JSON 接口；tk 是页面短时签名，不在 storageState。
    # 用首页 HTML 里 aiTabFrameBaseData.userInfo.isUserLogin 判断登录态。
    "wenxin": SessionProbeSpec(
        method="GET",
        url="https://wenxin.baidu.com/",
        origin="https://wenxin.baidu.com",
        referer="https://wenxin.baidu.com/",
        extra_headers={"accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"},
        classify=classify_wenxin,
    ),
}


def _build_headers(
    spec: SessionProbeSpec, cookies: Sequence[Mapping[str, Any]], host: str
) -> dict[str, str]:
    headers = {
        "accept": "application/json, text/plain;q=0.9, */*;q=0.8",
        "origin": spec.origin,
        "referer": spec.referer,
        **spec.extra_headers,
    }
    xsrf = _xsrf_token(cookies, host)
    if xsrf:
        headers["x-xsrf-token"] = xsrf
    return headers


async def probe_browser_context(
    provider: str, context: BrowserContext
) -> KeepaliveStatus:
    """用 Context 里的 Cookie 打平台接口；无法判定时返回 uncertain 以便回退页面保活。"""
    spec = SESSION_PROBE_SPECS.get(provider)
    if spec is None:
        return "uncertain"
    try:
        cookies = await context.cookies()
    except PlaywrightError:
        logger.info(
            "CAMOFOX_SESSION_PROBE provider=%s status=uncertain reason=cookie_error",
            provider,
        )
        return "uncertain"
    if not cookies:
        logger.info(
            "CAMOFOX_SESSION_PROBE provider=%s status=login_required reason=empty_cookies",
            provider,
        )
        return "login_required"
    host = urlparse(spec.url).hostname or ""
    headers = _build_headers(spec, cookies, host)
    try:
        status, body = await _send_probe(context, spec, headers)
    except (PlaywrightError, TimeoutError, OSError, ValueError, TypeError):
        logger.info(
            "CAMOFOX_SESSION_PROBE provider=%s status=uncertain reason=http_error",
            provider,
        )
        return "uncertain"
    result = spec.classify(status, body)
    logger.info(
        "CAMOFOX_SESSION_PROBE provider=%s status=%s http_status=%s",
        provider,
        result,
        status,
    )
    return result


async def _send_probe(
    context: BrowserContext, spec: SessionProbeSpec, headers: Mapping[str, str]
) -> tuple[int, str]:
    kwargs: dict[str, Any] = {
        "method": spec.method,
        "headers": dict(headers),
        "timeout": _PROBE_TIMEOUT_MS,
        "max_redirects": 5,
        "fail_on_status_code": False,
    }
    if spec.json_body is not None:
        kwargs["json"] = dict(spec.json_body)
    response = await context.request.fetch(spec.url, **kwargs)
    return response.status, await response.text()
