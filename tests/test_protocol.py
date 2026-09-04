"""无需真实 Camoufox 的协议安全回归。真实浏览器由人工平台验收覆盖。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
import httpx
from fastapi import HTTPException
from starlette.requests import Request

from app.main import (
    BrowserService,
    ProtocolError,
    SessionState,
    TabState,
    app,
    bounded_port,
    env_flag,
    ensure_allowed_url,
    error_response,
    http_exception_response,
    normalize_spec,
    request_object,
    service,
    tab_operation,
)
from app.window_vnc import top_level_window_ids


def test_health_always_declares_v3() -> None:
    route = next(route for route in app.routes if route.path == "/health")
    response = asyncio.run(route.endpoint())

    assert response["geoRpaProtocolVersion"] == 3


def test_window_publisher_is_an_optional_v3_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_WINDOW_PUBLISHER", "true")

    assert BrowserService().protocol_version == 3


def test_v3_sidecar_is_not_ready_without_the_window_publisher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ENABLE_WINDOW_PUBLISHER", raising=False)

    instance = BrowserService()
    asyncio.run(instance.start())

    assert instance.browser_ready is False


def test_x11_parser_returns_only_direct_root_children() -> None:
    tree = """\
0x50d (the root window) (has no name): ()  1920x1080+0+0
  0x200007 \"Firefox\": ()  1440x900+0+0
    0x200008 (has no name): ()  1440x860+0+40
  0x200010 \"Firefox\": ()  1440x900+10+10
"""

    assert top_level_window_ids(tree) == ["0x200007", "0x200010"]


def test_profile_path_matches_node_persistence_layout(tmp_path: Path) -> None:
    service = BrowserService()
    service.profile_dir = tmp_path
    user_id = "geo-rpa-deepseek-account-1"

    assert service._state_path(user_id) == (
        tmp_path / hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:32] / "storage-state.json"
    )


def test_invalid_state_is_quarantined(tmp_path: Path) -> None:
    service = BrowserService()
    service.profile_dir = tmp_path
    state_path = service._state_path("user-1")
    state_path.parent.mkdir(parents=True)
    state_path.write_text("not-json", encoding="utf-8")

    assert asyncio.run(service._load_state("user-1")) is None
    assert not state_path.exists()
    assert list(state_path.parent.glob("storage-state.invalid-*.json"))


@pytest.mark.parametrize(
    "url",
    [
        "https://www.doubao.com/chat",
        "https://chat.deepseek.com/",
        "https://bot.sannysoft.com/",
    ],
)
def test_provider_allowlist_accepts_only_reviewed_hosts(url: str) -> None:
    ensure_allowed_url(url)


@pytest.mark.parametrize("url", ["http://chat.deepseek.com/", "https://example.com/", "https://deepseek.com.evil.test/"])
def test_provider_allowlist_rejects_other_navigation(url: str) -> None:
    with pytest.raises(ProtocolError):
        ensure_allowed_url(url)


def test_capture_spec_is_bounded_and_exact() -> None:
    spec = normalize_spec({"method": "post", "host": "chat.example.com", "path": "/completion", "maxBytes": 64})

    assert spec == {
        "method": "POST",
        "host": "chat.example.com",
        "path": "/completion",
        "pathPrefix": None,
        "maxBytes": 64,
        "activateOnSubmit": False,
    }
    with pytest.raises(ProtocolError):
        normalize_spec({"method": "POST", "host": "chat.example.com", "path": "/completion", "maxBytes": 2 * 1024 * 1024 + 1})


def test_error_response_keeps_adapter_error_at_top_level() -> None:
    response = asyncio.run(http_exception_response(None, HTTPException(409, {"error": "active", "code": "network_capture_active"})))

    assert response.status_code == 409
    assert json.loads(response.body) == {"error": "active", "code": "network_capture_active"}


def test_error_response_does_not_expose_unapproved_error_codes() -> None:
    response = error_response(ProtocolError(400, "invalid", "internal_error"))

    assert response.detail == {"error": "invalid"}


def test_route_inventory_exposes_only_controlled_geo_rpa_operations() -> None:
    routes = {(route.path, method) for route in app.routes for method in getattr(route, "methods", set())}

    assert {
        ("/health", "GET"),
        ("/tabs", "GET"),
        ("/tabs", "POST"),
        ("/tabs/{tab_id}", "DELETE"),
        ("/sessions/{user_id}", "DELETE"),
        ("/tabs/{tab_id}/navigate", "POST"),
        ("/tabs/{tab_id}/screenshot", "GET"),
        ("/tabs/{tab_id}/snapshot", "GET"),
        ("/rpa/tabs/{tab_id}/locator-read", "POST"),
        ("/rpa/tabs/{tab_id}/manual-input", "POST"),
        ("/rpa/tabs/{tab_id}/locator-screenshot", "POST"),
        ("/rpa/tabs/{tab_id}/manual-window", "POST"),
        ("/rpa/manual-windows/{handle}", "GET"),
        ("/rpa/manual-windows/{handle}", "DELETE"),
        ("/rpa/tabs/{tab_id}/task-window", "POST"),
        ("/rpa/task-windows/{handle}/observer", "POST"),
        ("/rpa/task-windows/{handle}/observer", "DELETE"),
        ("/vnc/status", "GET"),
    }.issubset(routes)
    assert not any("eval" in path or "mcp" in path for path, _ in routes)
    assert not any("storage_state" in path for path, _ in routes)
    assert {(path, method) for path, method in routes if path.startswith("/vnc/")} == {("/vnc/status", "GET")}


def test_vnc_environment_parser_is_explicit_and_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_VNC", "true")
    monkeypatch.setenv("VNC_PORT", "not-a-port")

    assert env_flag("ENABLE_VNC") is True
    assert bounded_port("VNC_PORT", 5900) == 5900


def test_request_object_rejects_non_object_json() -> None:
    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"[]", "more_body": False}

    request = Request(
        {"type": "http", "method": "POST", "headers": [(b"content-type", b"application/json")]},
        receive=receive,
    )

    with pytest.raises(ProtocolError, match="object is required"):
        asyncio.run(request_object(request))


def test_invalid_json_uses_protocol_error_envelope() -> None:
    async def request_tabs() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://sidecar") as client:
            return await client.post("/tabs", content=b"{")

    response = asyncio.run(request_tabs())

    assert response.status_code == 400
    assert response.json() == {"error": "Request JSON is invalid"}


def test_tab_operation_serializes_tabs_within_one_account() -> None:
    class FakePage:
        pass

    async def run() -> list[str]:
        original_sessions = service.sessions
        session = SessionState(user_id="user-1", context=object())  # type: ignore[arg-type]
        first = TabState(tab_id="first", page=FakePage(), session=session)  # type: ignore[arg-type]
        second = TabState(tab_id="second", page=FakePage(), session=session)  # type: ignore[arg-type]
        session.tabs = {first.tab_id: first, second.tab_id: second}
        service.sessions = {session.user_id: session}
        events: list[str] = []

        async def operate(tab_id: str) -> None:
            async with tab_operation(session.user_id, tab_id):
                events.append(f"start-{tab_id}")
                await asyncio.sleep(0.01)
                events.append(f"end-{tab_id}")

        try:
            await asyncio.gather(operate(first.tab_id), operate(second.tab_id))
        finally:
            service.sessions = original_sessions
        return events

    events = asyncio.run(run())

    assert events in (["start-first", "end-first", "start-second", "end-second"], ["start-second", "end-second", "start-first", "end-first"])


def test_session_for_waits_for_the_existing_close_barrier() -> None:
    class FakeBrowser:
        async def new_context(self, **_: object) -> object:
            return object()

    async def run() -> None:
        instance = BrowserService()
        instance.browser = FakeBrowser()  # type: ignore[assignment]
        instance.browser_ready = True
        started = asyncio.Event()
        release = asyncio.Event()

        async def closing() -> None:
            started.set()
            await release.wait()

        barrier = asyncio.create_task(closing())
        instance.closing_sessions["user-1"] = barrier
        waiter = asyncio.create_task(instance.session_for("user-1"))
        await started.wait()
        assert not waiter.done()
        release.set()
        await barrier
        await waiter

    asyncio.run(run())
