"""用已 hydrate 的 BrowserContext 探测平台登录态，避免保活时打开聊天页。

平台 URL、请求头和业务码判定都留在 sidecar，随 Camofox 镜像发布。
探测不得把 Cookie、Token 或响应正文写入日志；不能把 HTTP 200 当成在线。
Cookie 不够的平台从 LocalStorage 取 Bearer：DeepSeek 解开 userToken.value，
Kimi 直接用 access_token。文心没有免 tk 的用户 JSON 接口，改读首页 HTML
启动数据里的 isUserLogin。
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

logger = logging.getLogger("uvicorn.error")

# 探活过程中旋转到的 LocalStorage，checkpoint 导出时合并进快照。
ORIGIN_LOCAL_STORAGE_UPDATES_ATTR = "_camofox_origin_local_storage_updates"

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
    # Playwright APIRequestContext 遇到已有 Cookie 头就不会再从 Cookie 罐补。
    # DeepSeek 的 ds_session_id 是 SameSite=Strict，不打开聊天页时罐里的自动
    # 附带经常丢它。这里显式带上 Context 里匹配该主机的全部 Cookie。
    attach_cookie_header: bool = False
    # 部分平台用户接口只认 Authorization，Cookie 不够。
    # DeepSeek /users/current 缺 Token 会 40002；userToken 是 AppKit 的
    # {"__version","value"}，必须解开 value。整段 JSON 会被判 40003。
    # Kimi GetCurrentUser 缺 Token 会 401 unauthenticated；access_token 是
    # 裸 JWT，直接当 Bearer。x-msh-session-id 等头不是登录判定条件。
    bearer_local_storage_key: str | None = None
    # Kimi access_token 大约 15 分钟过期。保活要先用 refresh_token 换新票，
    # 并把旋转后的 access/refresh 写回 Context，否则 checkpoint 会留下作废 refresh。
    refresh_url: str | None = None
    refresh_local_storage_key: str | None = None
    refresh_request_field: str = "refreshToken"
    refresh_access_field: str = "accessToken"
    refresh_refresh_field: str = "refreshToken"
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
    if code in {40002, 40003, 401, "40002", "40003"} or data is None:
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
        attach_cookie_header=True,
        bearer_local_storage_key="userToken",
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
            "connect-protocol-version": "1",
        },
        json_body={},
        bearer_local_storage_key="access_token",
        refresh_url="https://auth.kimi.com/api/account.gateway.v1.AuthService/RefreshToken",
        refresh_local_storage_key="refresh_token",
        classify=classify_kimi,
    ),
    "qianwen": SessionProbeSpec(
        method="POST",
        url="https://api.qianwen.com/growth/user/benefit/user/member/info",
        origin="https://www.qianwen.com",
        referer="https://www.qianwen.com/",
        extra_headers={"x-platform": "pc_tongyi"},
        # 登录态在 Cookie tongyi_sso_ticket。页面实际 POST {"clientChannel":"PC"}；
        # 空对象也能通，对齐 HAR 避免网关以后拒空 body。XSRF-TOKEN 由 Cookie 复制。
        json_body={"clientChannel": "PC"},
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


def _unwrap_local_storage_value(value: str) -> str | None:
    """取出可当作 Token 的标量。AppKit 把真实值包在 {"__version","value"} 里。"""
    text = value.strip()
    if not text:
        return None
    if text[:1] in {"{", "["}:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return None
        if isinstance(payload, dict):
            inner = payload.get("value")
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
        return None
    return text


def _origin_local_storage_value(
    state: Mapping[str, Any], origin: str, name: str
) -> str | None:
    origins = state.get("origins")
    if not isinstance(origins, list):
        return None
    for item in origins:
        if not isinstance(item, dict) or item.get("origin") != origin:
            continue
        local_storage = item.get("localStorage")
        if not isinstance(local_storage, list):
            continue
        for entry in local_storage:
            if not isinstance(entry, dict) or entry.get("name") != name:
                continue
            value = entry.get("value")
            if isinstance(value, str):
                return _unwrap_local_storage_value(value)
    return None


def _cookie_header(cookies: Sequence[Mapping[str, Any]], host: str) -> str | None:
    """把 Context 中匹配主机的 Cookie 编成请求头，包含 SameSite=Strict。"""
    parts: list[str] = []
    for item in cookies:
        name = item.get("name")
        value = item.get("value")
        domain = item.get("domain")
        if not isinstance(name, str) or not isinstance(value, str) or not name:
            continue
        if isinstance(domain, str) and not _cookie_matches_host(domain, host):
            continue
        parts.append(f"{name}={value}")
    if not parts:
        return None
    return "; ".join(parts)


def note_origin_local_storage_updates(
    context: BrowserContext, origin: str, updates: Mapping[str, str]
) -> None:
    """把旋转后的 Token 记在 Context 上，供 checkpoint 合并。不写日志。"""
    if not updates:
        return
    current = getattr(context, ORIGIN_LOCAL_STORAGE_UPDATES_ATTR, None)
    merged: dict[str, dict[str, str]] = dict(current) if isinstance(current, dict) else {}
    origin_map = dict(merged.get(origin) or {})
    origin_map.update({key: value for key, value in updates.items() if value})
    merged[origin] = origin_map
    setattr(context, ORIGIN_LOCAL_STORAGE_UPDATES_ATTR, merged)


def merge_origin_local_storage(
    state: Mapping[str, Any], updates: Mapping[str, Mapping[str, str]]
) -> dict[str, Any]:
    """把 origin -> {key: value} 合并进 Playwright storage_state。"""
    merged = dict(state)
    origins = [
        dict(item) if isinstance(item, dict) else item
        for item in (state.get("origins") or [])
    ]
    by_origin: dict[str, dict[str, Any]] = {}
    for item in origins:
        if isinstance(item, dict) and isinstance(item.get("origin"), str):
            by_origin[item["origin"]] = item
    for origin, pairs in updates.items():
        item = by_origin.get(origin)
        if item is None:
            item = {"origin": origin, "localStorage": []}
            origins.append(item)
            by_origin[origin] = item
        local_storage = [
            dict(entry) if isinstance(entry, dict) else entry
            for entry in (item.get("localStorage") or [])
        ]
        by_name: dict[str, dict[str, Any]] = {}
        for entry in local_storage:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                by_name[entry["name"]] = entry
        for name, value in pairs.items():
            if name in by_name:
                by_name[name]["value"] = value
            else:
                local_storage.append({"name": name, "value": value})
        item["localStorage"] = local_storage
    merged["origins"] = origins
    return merged


async def _bearer_from_local_storage(
    context: BrowserContext, spec: SessionProbeSpec
) -> str | None:
    if not spec.bearer_local_storage_key:
        return None
    try:
        state = await context.storage_state()
    except PlaywrightError:
        return None
    if not isinstance(state, dict):
        return None
    return _origin_local_storage_value(
        state, spec.origin, spec.bearer_local_storage_key
    )


def _build_headers(
    spec: SessionProbeSpec,
    cookies: Sequence[Mapping[str, Any]],
    host: str,
    *,
    bearer: str | None = None,
) -> dict[str, str]:
    headers = {
        "accept": "application/json, text/plain;q=0.9, */*;q=0.8",
        "origin": spec.origin,
        "referer": spec.referer,
        **spec.extra_headers,
    }
    if spec.attach_cookie_header:
        cookie_header = _cookie_header(cookies, host)
        if cookie_header:
            headers["cookie"] = cookie_header
    if bearer:
        headers["authorization"] = f"Bearer {bearer}"
    xsrf = _xsrf_token(cookies, host)
    if xsrf:
        headers["x-xsrf-token"] = xsrf
    return headers


async def probe_browser_context(
    provider: str, context: BrowserContext
) -> KeepaliveStatus:
    """用 Context 里的 Cookie 和可选 Bearer 打平台用户接口；无法判定时返回 uncertain。"""
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
    host = urlparse(spec.url).hostname or ""
    try:
        state = await context.storage_state()
    except PlaywrightError:
        state = {}
    if not isinstance(state, dict):
        state = {}
    bearer = (
        _origin_local_storage_value(state, spec.origin, spec.bearer_local_storage_key)
        if spec.bearer_local_storage_key
        else None
    )
    refresh_value = (
        _origin_local_storage_value(state, spec.origin, spec.refresh_local_storage_key)
        if spec.refresh_local_storage_key
        else None
    )
    # Kimi 登录态在 LocalStorage。Cookie 可能只剩统计字段；access_token 15 分钟
    # 就会过期，所以有 refresh 规格时不要仅因 Cookie 罐空或 access 过期就放弃。
    if not cookies and not bearer and not refresh_value:
        logger.info(
            "CAMOFOX_SESSION_PROBE provider=%s status=login_required reason=empty_cookies",
            provider,
        )
        return "login_required"
    if spec.refresh_url and refresh_value:
        try:
            bearer = await _refresh_access_token(
                context, spec, cookies, refresh_value, bearer
            )
        except (PlaywrightError, TimeoutError, OSError, ValueError, TypeError) as exc:
            logger.info(
                "CAMOFOX_SESSION_PROBE provider=%s status=uncertain reason=refresh_error error=%s",
                provider,
                type(exc).__name__,
            )
            return "uncertain"
        if bearer is None and spec.refresh_local_storage_key:
            logger.info(
                "CAMOFOX_SESSION_PROBE provider=%s status=login_required reason=refresh_rejected",
                provider,
            )
            return "login_required"
    if not cookies and not bearer:
        logger.info(
            "CAMOFOX_SESSION_PROBE provider=%s status=login_required reason=empty_cookies",
            provider,
        )
        return "login_required"
    headers = _build_headers(spec, cookies, host, bearer=bearer)
    try:
        status, body = await _send_probe(context, spec, headers)
    except (PlaywrightError, TimeoutError, OSError, ValueError, TypeError) as exc:
        logger.info(
            "CAMOFOX_SESSION_PROBE provider=%s status=uncertain reason=http_error error=%s",
            provider,
            type(exc).__name__,
        )
        return "uncertain"
    result = spec.classify(status, body)
    logger.info(
        "CAMOFOX_SESSION_PROBE provider=%s status=%s http_status=%s has_bearer=%s body_kind=%s",
        provider,
        result,
        status,
        bool(bearer),
        "json" if _as_object(body) is not None else "non_json",
    )
    return result


async def _refresh_access_token(
    context: BrowserContext,
    spec: SessionProbeSpec,
    cookies: Sequence[Mapping[str, Any]],
    refresh_value: str,
    current_bearer: str | None,
) -> str | None:
    """用 refresh_token 换新 access/refresh；失败返回 None 表示登录已失效。"""
    if not spec.refresh_url:
        return current_bearer
    host = urlparse(spec.refresh_url).hostname or ""
    headers = _build_headers(spec, cookies, host, bearer=None)
    status, body = await _send_json(
        context,
        method="POST",
        url=spec.refresh_url,
        headers=headers,
        json_body={spec.refresh_request_field: refresh_value},
    )
    if status in {401, 403}:
        return None
    payload = _as_object(body)
    if not isinstance(payload, dict):
        raise ValueError("refresh_non_json")
    access = payload.get(spec.refresh_access_field)
    new_refresh = payload.get(spec.refresh_refresh_field)
    if not isinstance(access, str) or not access.strip():
        return None
    updates = {spec.bearer_local_storage_key or "access_token": access.strip()}
    if isinstance(new_refresh, str) and new_refresh.strip():
        updates[spec.refresh_local_storage_key or "refresh_token"] = new_refresh.strip()
    note_origin_local_storage_updates(context, spec.origin, updates)
    logger.info(
        "CAMOFOX_SESSION_PROBE provider_refresh=ok http_status=%s rotated_refresh=%s",
        status,
        bool(isinstance(new_refresh, str) and new_refresh.strip()),
    )
    return access.strip()


async def _send_probe(
    context: BrowserContext, spec: SessionProbeSpec, headers: Mapping[str, str]
) -> tuple[int, str]:
    return await _send_json(
        context,
        method=spec.method,
        url=spec.url,
        headers=headers,
        json_body=spec.json_body,
    )


async def _send_json(
    context: BrowserContext,
    *,
    method: str,
    url: str,
    headers: Mapping[str, str],
    json_body: Mapping[str, Any] | None,
) -> tuple[int, str]:
    kwargs: dict[str, Any] = {
        "method": method,
        "headers": dict(headers),
        "timeout": _PROBE_TIMEOUT_MS,
        "max_redirects": 5,
        "fail_on_status_code": False,
    }
    # Playwright APIRequestContext.fetch 没有 json=，传了会 TypeError，
    # Kimi/千问的 POST 探活会被收成 uncertain。body 用 JSON 字节，并显式带
    # content-type，避免被当成表单。
    if json_body is not None:
        kwargs["data"] = json.dumps(dict(json_body), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        kwargs["headers"]["content-type"] = "application/json"
    response = await context.request.fetch(url, **kwargs)
    return response.status, await response.text()
