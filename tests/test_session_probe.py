"""账号 Context 接口探活。"""

from __future__ import annotations

from unittest.mock import AsyncMock

import asyncio

from app.main import BrowserService, SessionState
from app.provider_automation import ProviderName
from app.session_probe import (
    ORIGIN_LOCAL_STORAGE_UPDATES_ATTR,
    SESSION_PROBE_SPECS,
    _build_headers,
    _origin_local_storage_value,
    merge_origin_local_storage,
    classify_deepseek,
    classify_doubao,
    classify_kimi,
    classify_qianwen,
    classify_wenxin,
    classify_yuanbao,
    probe_browser_context,
)


def test_yuanbao_headers_include_source_and_skip_cookie_header() -> None:
    headers = _build_headers(
        SESSION_PROBE_SPECS["yuanbao"],
        [{"name": "sid", "value": "1", "domain": "yuanbao.tencent.com"}],
        "yuanbao.tencent.com",
    )
    assert headers["x-source"] == "web"
    assert "cookie" not in {key.casefold() for key in headers}


def test_qianwen_headers_copy_xsrf_cookie() -> None:
    headers = _build_headers(
        SESSION_PROBE_SPECS["qianwen"],
        [
            {"name": "XSRF-TOKEN", "value": "tok", "domain": ".qianwen.com"},
            {"name": "sid", "value": "1", "domain": ".qianwen.com"},
        ],
        "api.qianwen.com",
    )
    assert headers["x-xsrf-token"] == "tok"


def test_classify_deepseek_missing_token() -> None:
    assert (
        classify_deepseek(200, '{"code":40002,"msg":"Missing Token","data":null}')
        == "login_required"
    )
    assert (
        classify_deepseek(
            200, '{"code":40003,"msg":"Authorization Failed (invalid token)","data":null}'
        )
        == "login_required"
    )
    assert classify_deepseek(200, '{"code":0,"data":{"id":"u1"}}') == "ok"


def test_classify_doubao_expired_session() -> None:
    body = '{"data":{"description":"session expired","error_code":13,"user_id":0},"message":"error"}'
    assert classify_doubao(200, body) == "login_required"
    assert classify_doubao(200, '{"data":{"user_id":12345}}') == "ok"


def test_classify_kimi_unauthenticated() -> None:
    assert classify_kimi(401, '{"code":"unauthenticated"}') == "login_required"
    assert classify_kimi(200, '{"user":{"id":"u1"}}') == "ok"


def test_classify_qianwen_not_login() -> None:
    body = '{"errorCode":"NOT_LOGIN","errorMsg":"not login","failed":true,"success":false}'
    assert classify_qianwen(200, body) == "login_required"
    assert classify_qianwen(200, '{"success":true,"data":{}}') == "ok"


def test_classify_yuanbao_http_status() -> None:
    assert classify_yuanbao(401, '{"error":{"code":"20000"}}') == "login_required"
    assert classify_yuanbao(200, '{"userId":"u1"}') == "ok"


def test_classify_wenxin_bootstrap_flag() -> None:
    html_off = (
        '<script type="application/json" name="aiTabFrameBaseData">'
        '{"userInfo":{"name":"","isUserLogin":0,"baiduid":""}}</script>'
    )
    html_on = (
        '<script type="application/json" name="aiTabFrameBaseData">'
        '{"userInfo":{"name":"n","isUserLogin":1,"baiduid":"bd"}}</script>'
    )
    assert classify_wenxin(200, html_off) == "login_required"
    assert classify_wenxin(200, html_on) == "ok"
    assert classify_wenxin(200, "<html></html>") == "uncertain"


def test_probe_empty_cookies_is_login_required_without_http() -> None:
    async def run() -> None:
        context = AsyncMock()
        context.cookies = AsyncMock(return_value=[])
        context.request.fetch = AsyncMock()
        assert await probe_browser_context("deepseek", context) == "login_required"
        context.request.fetch.assert_not_called()

    asyncio.run(run())


def test_probe_unknown_provider_is_uncertain() -> None:
    assert asyncio.run(probe_browser_context("chatgpt", AsyncMock())) == "uncertain"


def test_probe_deepseek_classifies_ok() -> None:
    async def run() -> None:
        context = AsyncMock()
        context.cookies = AsyncMock(
            return_value=[{"name": "sid", "value": "alive", "domain": ".deepseek.com"}]
        )
        response = AsyncMock()
        response.status = 200
        response.text = AsyncMock(return_value='{"code":0,"data":{"id":"u1"}}')
        context.request.fetch = AsyncMock(return_value=response)
        assert await probe_browser_context("deepseek", context) == "ok"
        kwargs = context.request.fetch.await_args
        assert kwargs.args[0].endswith("/api/v0/users/current")
        assert kwargs.kwargs["method"] == "GET"

    asyncio.run(run())


def test_keepalive_uses_http_probe_without_opening_tab() -> None:
    async def run() -> None:
        service = BrowserService()
        context = AsyncMock()
        context.cookies = AsyncMock(
            return_value=[{"name": "sid", "value": "alive", "domain": ".deepseek.com"}]
        )
        response = AsyncMock()
        response.status = 200
        response.text = AsyncMock(return_value='{"code":0,"data":{"id":"u1"}}')
        context.request.fetch = AsyncMock(return_value=response)
        service.sessions["profile-key"] = SessionState(user_id="profile-key", context=context)
        service.create_tab = AsyncMock()
        status = await service.account_keepalive(ProviderName.DEEPSEEK, "profile-key")
        assert status == "ok"
        service.create_tab.assert_not_called()

    asyncio.run(run())


def test_keepalive_keeps_uncertain_without_opening_tab() -> None:
    """接口无法判定时保持 uncertain，不再打开聊天页。"""

    async def run() -> None:
        service = BrowserService()
        context = AsyncMock()
        context.cookies = AsyncMock(
            return_value=[{"name": "sid", "value": "1", "domain": ".baidu.com"}]
        )
        response = AsyncMock()
        response.status = 200
        response.text = AsyncMock(return_value="<html></html>")
        context.request.fetch = AsyncMock(return_value=response)
        service.sessions["profile-key"] = SessionState(user_id="profile-key", context=context)
        service.create_tab = AsyncMock()
        status = await service.account_keepalive(ProviderName.WENXIN, "profile-key")
        assert status == "uncertain"
        service.create_tab.assert_not_called()

    asyncio.run(run())


def test_keepalive_trusts_doubao_api_login_required() -> None:
    """豆包 passport 接口判定未登录时直接返回，不打开聊天页。"""

    async def run() -> None:
        service = BrowserService()
        context = AsyncMock()
        context.cookies = AsyncMock(
            return_value=[{"name": "ttwid", "value": "anon", "domain": ".doubao.com"}]
        )
        response = AsyncMock()
        response.status = 200
        response.text = AsyncMock(return_value='{"data":{"error_code":13},"message":"error"}')
        context.request.fetch = AsyncMock(return_value=response)
        service.sessions["profile-key"] = SessionState(user_id="profile-key", context=context)
        service.create_tab = AsyncMock()
        status = await service.account_keepalive(ProviderName.DOUBAO, "profile-key")
        assert status == "login_required"
        service.create_tab.assert_not_called()

    asyncio.run(run())


def test_keepalive_trusts_deepseek_api_login_required() -> None:
    """DeepSeek 接口 Missing Token 视为未登录，不再回退打开聊天页。"""

    async def run() -> None:
        service = BrowserService()
        context = AsyncMock()
        context.cookies = AsyncMock(
            return_value=[
                {"name": "ds_session_id", "value": "alive", "domain": "chat.deepseek.com"}
            ]
        )
        response = AsyncMock()
        response.status = 200
        response.text = AsyncMock(
            return_value='{"code":40002,"msg":"Missing Token","data":null}'
        )
        context.request.fetch = AsyncMock(return_value=response)
        service.sessions["profile-key"] = SessionState(user_id="profile-key", context=context)
        service.create_tab = AsyncMock()
        status = await service.account_keepalive(ProviderName.DEEPSEEK, "profile-key")
        assert status == "login_required"
        service.create_tab.assert_not_called()

    asyncio.run(run())


def test_keepalive_without_session_is_uncertain() -> None:
    async def run() -> None:
        service = BrowserService()
        service.create_tab = AsyncMock()
        status = await service.account_keepalive(ProviderName.DOUBAO, "missing-profile")
        assert status == "uncertain"
        service.create_tab.assert_not_called()

    asyncio.run(run())

def test_probe_deepseek_sends_unwrapped_user_token_and_cookies() -> None:
    """DeepSeek 探活必须带解开后的 userToken，并显式附上 Strict Cookie。"""

    async def run() -> None:
        context = AsyncMock()
        context.cookies = AsyncMock(
            return_value=[
                {
                    "name": "ds_session_id",
                    "value": "alive",
                    "domain": "chat.deepseek.com",
                    "sameSite": "Strict",
                },
                {
                    "name": "HWWAFSESID",
                    "value": "waf",
                    "domain": "chat.deepseek.com",
                },
            ]
        )
        context.storage_state = AsyncMock(
            return_value={
                "cookies": [],
                "origins": [
                    {
                        "origin": "https://chat.deepseek.com",
                        "localStorage": [
                            {
                                "name": "userToken",
                                "value": '{"__version":0,"value":"real-token"}',
                            }
                        ],
                    }
                ],
            }
        )
        response = AsyncMock()
        response.status = 200
        response.text = AsyncMock(return_value='{"code":0,"data":{"biz_code":0,"biz_data":{},"biz_msg":""}}')
        context.request.fetch = AsyncMock(return_value=response)
        assert await probe_browser_context("deepseek", context) == "ok"
        headers = context.request.fetch.await_args.kwargs["headers"]
        assert headers["authorization"] == "Bearer real-token"
        assert "ds_session_id=alive" in headers["cookie"]
        assert "HWWAFSESID=waf" in headers["cookie"]

    asyncio.run(run())


def test_appkit_local_storage_unwraps_value() -> None:
    """DeepSeek userToken 是 AppKit JSON，不能整段拿去当 Bearer。"""
    state = {
        "origins": [
            {
                "origin": "https://chat.deepseek.com",
                "localStorage": [
                    {
                        "name": "userToken",
                        "value": '{"__version":0,"value":"real-token"}',
                    }
                ],
            }
        ]
    }
    assert (
        _origin_local_storage_value(state, "https://chat.deepseek.com", "userToken")
        == "real-token"
    )
    assert _origin_local_storage_value(
        {
            "origins": [
                {
                    "origin": "https://chat.deepseek.com",
                    "localStorage": [
                        {"name": "userToken", "value": '{"__version":0,"value":null}'}
                    ],
                }
            ]
        },
        "https://chat.deepseek.com",
        "userToken",
    ) is None

def test_probe_kimi_sends_access_token_bearer() -> None:
    """Kimi GetCurrentUser 只认 Authorization；Cookie 不够会 401 unauthenticated。"""

    async def run() -> None:
        context = AsyncMock()
        context.cookies = AsyncMock(
            return_value=[
                {"name": "theme", "value": "dark", "domain": "www.kimi.com"},
            ]
        )
        context.storage_state = AsyncMock(
            return_value={
                "cookies": [],
                "origins": [
                    {
                        "origin": "https://www.kimi.com",
                        "localStorage": [
                            {"name": "access_token", "value": "kimi-access"},
                        ],
                    }
                ],
            }
        )
        response = AsyncMock()
        response.status = 200
        response.text = AsyncMock(return_value='{"user":{"id":"u1"}}')
        context.request.fetch = AsyncMock(return_value=response)
        assert await probe_browser_context("kimi", context) == "ok"
        kwargs = context.request.fetch.await_args.kwargs
        assert "json" not in kwargs
        assert kwargs["data"] == b"{}"
        assert kwargs["method"] == "POST"
        headers = kwargs["headers"]
        assert headers["authorization"] == "Bearer kimi-access"
        assert headers["x-msh-platform"] == "web"
        assert headers["content-type"] == "application/json"

    asyncio.run(run())


def test_probe_kimi_empty_cookies_still_sends_bearer() -> None:
    """Kimi 登录态在 access_token，Cookie 罐空时仍应打探活接口。"""

    async def run() -> None:
        context = AsyncMock()
        context.cookies = AsyncMock(return_value=[])
        context.storage_state = AsyncMock(
            return_value={
                "cookies": [],
                "origins": [
                    {
                        "origin": "https://www.kimi.com",
                        "localStorage": [
                            {"name": "access_token", "value": "kimi-access"},
                        ],
                    }
                ],
            }
        )
        response = AsyncMock()
        response.status = 200
        response.text = AsyncMock(return_value='{"user":{"id":"u1"}}')
        context.request.fetch = AsyncMock(return_value=response)
        assert await probe_browser_context("kimi", context) == "ok"
        headers = context.request.fetch.await_args.kwargs["headers"]
        assert headers["authorization"] == "Bearer kimi-access"

    asyncio.run(run())

def test_probe_kimi_refreshes_rotated_tokens_then_probes_user() -> None:
    """Kimi access_token 15 分钟过期，探活必须先刷新并记下旋转后的 refresh。"""

    async def run() -> None:
        context = AsyncMock()
        context.cookies = AsyncMock(
            return_value=[{"name": "theme", "value": "dark", "domain": "www.kimi.com"}]
        )
        context.storage_state = AsyncMock(
            return_value={
                "cookies": [],
                "origins": [
                    {
                        "origin": "https://www.kimi.com",
                        "localStorage": [
                            {"name": "access_token", "value": "expired-access"},
                            {"name": "refresh_token", "value": "kimi-refresh"},
                        ],
                    }
                ],
            }
        )
        refresh_response = AsyncMock()
        refresh_response.status = 200
        refresh_response.text = AsyncMock(
            return_value='{"accessToken":"new-access","refreshToken":"new-refresh"}'
        )
        user_response = AsyncMock()
        user_response.status = 200
        user_response.text = AsyncMock(return_value='{"user":{"id":"u1"}}')
        context.request.fetch = AsyncMock(side_effect=[refresh_response, user_response])
        assert await probe_browser_context("kimi", context) == "ok"
        refresh_call, user_call = context.request.fetch.await_args_list
        assert refresh_call.args[0].endswith("AuthService/RefreshToken")
        assert refresh_call.kwargs["data"] == b'{"refreshToken":"kimi-refresh"}'
        assert "authorization" not in {key.casefold() for key in refresh_call.kwargs["headers"]}
        assert user_call.kwargs["headers"]["authorization"] == "Bearer new-access"
        assert user_call.kwargs["data"] == b"{}"
        updates = getattr(context, ORIGIN_LOCAL_STORAGE_UPDATES_ATTR)
        assert updates["https://www.kimi.com"]["access_token"] == "new-access"
        assert updates["https://www.kimi.com"]["refresh_token"] == "new-refresh"

    asyncio.run(run())


def test_probe_kimi_refresh_rejected_is_login_required() -> None:
    async def run() -> None:
        context = AsyncMock()
        context.cookies = AsyncMock(
            return_value=[{"name": "theme", "value": "dark", "domain": "www.kimi.com"}]
        )
        context.storage_state = AsyncMock(
            return_value={
                "cookies": [],
                "origins": [
                    {
                        "origin": "https://www.kimi.com",
                        "localStorage": [
                            {"name": "refresh_token", "value": "dead-refresh"},
                        ],
                    }
                ],
            }
        )
        refresh_response = AsyncMock()
        refresh_response.status = 401
        refresh_response.text = AsyncMock(return_value='{"code":"unauthenticated"}')
        context.request.fetch = AsyncMock(return_value=refresh_response)
        assert await probe_browser_context("kimi", context) == "login_required"
        assert context.request.fetch.await_count == 1

    asyncio.run(run())


def test_merge_origin_local_storage_updates_existing_keys() -> None:
    state = {
        "cookies": [],
        "origins": [
            {
                "origin": "https://www.kimi.com",
                "localStorage": [
                    {"name": "access_token", "value": "old"},
                    {"name": "theme", "value": "dark"},
                ],
            }
        ],
    }
    merged = merge_origin_local_storage(
        state, {"https://www.kimi.com": {"access_token": "new", "refresh_token": "r"}}
    )
    values = {
        item["name"]: item["value"]
        for item in merged["origins"][0]["localStorage"]
    }
    assert values["access_token"] == "new"
    assert values["refresh_token"] == "r"
    assert values["theme"] == "dark"

def test_probe_qianwen_posts_client_channel_as_data() -> None:
    """千问探活必须走 data= JSON，不能传 Playwright 不认的 json=。"""

    async def run() -> None:
        context = AsyncMock()
        context.cookies = AsyncMock(
            return_value=[
                {"name": "tongyi_sso_ticket", "value": "sso", "domain": ".qianwen.com"},
                {"name": "XSRF-TOKEN", "value": "tok", "domain": "api.qianwen.com"},
            ]
        )
        context.storage_state = AsyncMock(return_value={"cookies": [], "origins": []})
        response = AsyncMock()
        response.status = 200
        response.text = AsyncMock(
            return_value='{"success":true,"failed":false,"data":{"userId":"u1"}}'
        )
        context.request.fetch = AsyncMock(return_value=response)
        assert await probe_browser_context("qianwen", context) == "ok"
        kwargs = context.request.fetch.await_args.kwargs
        assert "json" not in kwargs
        assert kwargs["method"] == "POST"
        assert kwargs["data"] == b'{"clientChannel":"PC"}'
        headers = kwargs["headers"]
        assert headers["x-platform"] == "pc_tongyi"
        assert headers["x-xsrf-token"] == "tok"
        assert headers["content-type"] == "application/json"

    asyncio.run(run())

