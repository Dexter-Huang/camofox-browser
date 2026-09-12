"""账号 Context 接口探活。"""

from __future__ import annotations

from unittest.mock import AsyncMock

import asyncio

from app.main import BrowserService, SessionState
from app.provider_automation import ProviderName
from app.session_probe import (
    SESSION_PROBE_SPECS,
    _build_headers,
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
