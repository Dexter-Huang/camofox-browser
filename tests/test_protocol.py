"""无需真实 Camoufox 的协议安全回归。真实浏览器由人工平台验收覆盖。"""

from __future__ import annotations

from pathlib import Path

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest
import httpx
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from app.main import (
    BrowserService,
    Capture,
    Execution,
    ManagedWindow,
    ProtocolError,
    SessionState,
    TabState,
    app,
    bounded_port,
    env_flag,
    ensure_allowed_url,
    error_response,
    http_exception_response,
    manual_window_features,
    normalize_spec,
    request_object,
    service,
    tab_operation,
    task_window_features,
    wait_for_manual_window_paint,
)
from app.main import ExecutionCreateRequest
from app.provider_automation import (
    AnswerWaitPolicy,
    NetworkAnswerListener,
    _network_answer_from_payload,
    _network_citations_from_payload,
    ProviderAutomationError,
    ProviderName,
    RULES,
    SubmissionMethod,
    extract_answer_text,
    conversation_url,
    prepare,
    rule_for,
    submit,
    wait_result,
)
from app.window_vnc import top_level_window_ids


def test_health_always_declares_v4() -> None:
    route = next(route for route in app.routes if route.path == "/health")
    response = asyncio.run(route.endpoint())

    assert response["geoRpaProtocolVersion"] == 4


def test_v4_idle_reaper_keeps_empty_recent_session_for_next_account_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """刚关闭最后一个 Tab 的账号仍可被下一次保活或任务安全复用。

    这里覆盖的不是浏览器页面行为，而是 Context 生命周期边界：保活完成到下一次
    create_tab 之间可能没有 Tab，回收器只能按会话超时回收，不能立即关闭 Context。
    """

    async def verify() -> None:
        browser_service = BrowserService()
        recent_session = SessionState(user_id="profile-key", context=Mock())
        browser_service.sessions[recent_session.user_id] = recent_session
        browser_service.close_session = AsyncMock()  # type: ignore[method-assign]
        sleep_calls = 0

        async def one_reaper_pass(_: float) -> None:
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls > 1:
                raise asyncio.CancelledError

        monkeypatch.setattr("app.main.asyncio.sleep", one_reaper_pass)

        with pytest.raises(asyncio.CancelledError):
            await browser_service._idle_reaper()

        browser_service.close_session.assert_not_awaited()

    asyncio.run(verify())


def test_v4_provider_rules_are_complete_and_use_network_as_the_result_contract() -> None:
    """平台规则只声明页面自动化，不保留响应体解析这一条不稳定采集路径。"""

    assert set(RULES) == {"doubao", "kimi", "qianwen", "yuanbao", "deepseek", "wenxin"}
    assert all(rule.entry_url.startswith("https://") for rule in RULES.values())
    assert all(rule.prompt_selectors and rule.answer_selectors for rule in RULES.values())
    assert all(rule.network_answer is not None for rule in RULES.values())


def test_v4_doubao_extracts_only_the_answer_after_its_reference_metadata() -> None:
    """豆包新版 main 包含标题、提问和输入区，持久化时只保留中间回答正文。"""

    page_text = (
        "GEO 销量最好的店铺推荐\n"
        "AI 生成可能有误 注意核实\n"
        "GEO 推荐哪一家？ 搜索 3 个关键词，参考 16 篇资料\n"
        "## 先说重点\n\n这里是完整的模型回答。\n"
        "发消息或按住空格说话...\n豆包 快速"
    )

    assert extract_answer_text(rule_for("doubao"), page_text) == "## 先说重点\n\n这里是完整的模型回答。"


def test_v4_doubao_excludes_the_new_workbench_menu_appended_after_an_answer() -> None:
    """新版豆包将工作台菜单放在 main 尾部，不能把它们持久化为模型回答。"""

    page_text = (
        "GEO 推荐哪一家？ 搜索 3 个关键词，参考 16 篇资料\n"
        "## 正式回答\n\n这里是应该保留的模型正文。\n\n"
        "对话\n帮我写作\nPPT 生成\n图像生成\n视频生成\n深入研究\n录音转写\n更多"
    )

    assert extract_answer_text(rule_for("doubao"), page_text) == "## 正式回答\n\n这里是应该保留的模型正文。"


def test_v4_doubao_dom_fallback_uses_submitted_query_when_metadata_is_absent() -> None:
    """新版豆包省略检索元数据时，兜底仍只能读取本轮问题后的正文。"""

    query = "GEO 推荐哪一家？"
    page_text = (
        f"{query}\n"
        "## 正式回答\n\n这是应当保留的模型正文。\n\n"
        "对话\n帮我写作\nPPT 生成\n图像生成\n视频生成\n深入研究\n录音转写\n更多"
    )

    assert extract_answer_text(
        rule_for("doubao"), page_text, submitted_query=query
    ) == "## 正式回答\n\n这是应当保留的模型正文。"
    # 仅在网络监听已超时的豆包专属兜底中，main 没有问题回显时仍可从其回答区域
    # 读取正文；固定工作台菜单必须继续被剔除。
    assert extract_answer_text(rule_for("doubao"), page_text) == (
        "GEO 推荐哪一家？\n## 正式回答\n\n这是应当保留的模型正文。"
    )


def test_v4_doubao_dom_fallback_rejects_an_incomplete_workbench_menu() -> None:
    """实验分支菜单缺少尾项时也不能被 main 兜底持久化。"""

    page_text = "有什么我能帮你的吗？\n对话\n工作\n对话\n帮我写作\nPPT 生成\n图像生成"

    assert extract_answer_text(rule_for("doubao"), page_text) == ""


def test_network_answer_preserves_markdown_and_returns_only_structured_citations() -> None:
    """流式累计帧交付原始 Markdown，页面工作台文本没有进入网络结果。"""

    payload = "\n".join(
        (
            'data: {"content":"## 推荐\\n\\n这是网络返回的正式第一段内容。"}',
            'data: {"content":"## 推荐\\n\\n这是网络返回的正式第一段内容。\\n\\n- 第二项建议"}',
            'data: {"url":"https://example.com/source","title":"公开来源"}',
            'data: {"done":true}',
        )
    )

    policy = rule_for("kimi").network_answer
    assert policy is not None
    assert _network_answer_from_payload(payload, policy) == "## 推荐\n\n这是网络返回的正式第一段内容。\n\n- 第二项建议"
    assert _network_citations_from_payload(payload) == [
        {"url": "https://example.com/source", "title": "公开来源"}
    ]


def test_doubao_network_citations_extract_search_cards_and_inline_meta() -> None:
    """豆包检索卡片和正文内联引用都应转换为统一来源模型。"""

    inline_info = json.dumps(
        {
            "insert_text": "(官方资料)",
            "url": "https://example.com/official",
            "title": "官方资料",
        },
        ensure_ascii=False,
    )
    payload = "data: " + json.dumps(
        {
            "content": {
                "content_block": [
                    {
                        "content": {
                            "search_query_result_block": {
                                "results": [
                                    {
                                        "text_card": {
                                            "url": "https://example.com/search",
                                            "title": "检索来源",
                                        }
                                    }
                                ]
                            }
                        },
                        "meta_info": [{"type": 2, "info": inline_info}],
                    }
                ]
            }
        },
        ensure_ascii=False,
    )
    policy = rule_for("doubao").network_answer
    assert policy is not None

    assert _network_citations_from_payload(payload, policy.parser) == [
        {"url": "https://example.com/search", "title": "检索来源"},
        {"url": "https://example.com/official", "title": "官方资料"},
    ]


def test_yuanbao_network_citations_extract_search_bubbles() -> None:
    """元宝搜索卡片的 ``link/text`` 应映射为统一引用模型。"""

    payload = "data: " + json.dumps(
        {
            "type": "deepSearchAgent",
            "contents": [
                {
                    "type": "toolCall",
                    "tcname": "web_search",
                    "items": [
                        {
                            "type": "bubbleList",
                            "bubbles": [
                                {
                                    "text": "官方资料",
                                    "link": "https://example.com/official",
                                },
                                {
                                    "text": "重复来源",
                                    "link": "https://example.com/official",
                                },
                                {"text": "图标而非来源", "link": "https://"},
                            ],
                        }
                    ],
                }
            ],
        },
        ensure_ascii=False,
    )
    policy = rule_for("yuanbao").network_answer
    assert policy is not None

    assert _network_citations_from_payload(payload, policy.parser) == [
        {"url": "https://example.com/official", "title": "官方资料"}
    ]


def test_wenxin_network_citations_extract_reference_list() -> None:
    """文心思考事件中的 referenceList 应转换为统一引用模型。"""

    payload = "data:" + json.dumps(
        {
            "data": {
                "message": {
                    "content": {
                        "generator": {
                            "component": "thinkingSteps",
                            "data": {
                                "referenceList": [
                                    {
                                        "url": "https://example.com/dji",
                                        "text": "DJI 官方网站",
                                        "source": "DJI",
                                    }
                                ]
                            },
                        }
                    }
                }
            }
        },
        ensure_ascii=False,
    )
    policy = rule_for("wenxin").network_answer
    assert policy is not None

    assert _network_citations_from_payload(payload, policy.parser) == [
        {"url": "https://example.com/dji", "title": "DJI 官方网站"}
    ]


def test_yuanbao_network_citations_extract_search_guid_docs() -> None:
    """元宝 searchGuid 事件的 docs 是引用面板来源清单。"""

    payload = "data: " + json.dumps(
        {
            "type": "searchGuid",
            "docs": [
                {
                    "title": "DJI 官方网站",
                    "url": "https://www.dji.com/cn",
                    "webSiteSource": "DJI",
                }
            ],
        },
        ensure_ascii=False,
    )
    policy = rule_for("yuanbao").network_answer
    assert policy is not None

    assert _network_citations_from_payload(payload, policy.parser) == [
        {"url": "https://www.dji.com/cn", "title": "DJI 官方网站"}
    ]


def test_network_answer_uses_yuanbao_text_events_only() -> None:
    """元宝搜索状态和 follow-up 事件不能混入快速回答的 Markdown。"""

    payload = "\n".join(
        (
            'data: {"type":"step","msg":"正在搜索资料"}',
            'data: {"type":"text","msg":"## 正式回答\\n\\n第一段"}',
            'data: {"type":"text","msg":"\\n\\n- 第二项建议"}',
            'data: {"type":"replace","replace":{"display":"follow_up_form"}}',
            'data: [DONE]',
        )
    )

    policy = rule_for("yuanbao").network_answer
    assert policy is not None
    assert _network_answer_from_payload(payload, policy) == "## 正式回答\n\n第一段\n\n- 第二项建议"


def test_network_answer_uses_only_doubao_assistant_content_blocks() -> None:
    """豆包同流中的用户文本和推荐字段不能进入最终 Markdown。"""

    payload = "\n".join(
        (
            'data: {"content":{"content_block":[{"content":{"text_block":{"text":"用户回显"}}}]},"meta":{"user_type":1}}',
            'data: {"content":{"content_block":[{"content":{"text_block":{"text":"## Official answer\\n\\nThe first verified paragraph."}}}]},"meta":{"user_type":2}}',
            'data: {"patch_op":[{"patch_object":1,"patch_value":{"content_block":[{"content":{"text_block":{"text":"## Official answer\\n\\nThe first verified paragraph.\\n\\nThe second verified paragraph."}}}]}}]}',
        )
    )

    policy = rule_for("doubao").network_answer
    assert policy is not None
    assert _network_answer_from_payload(payload, policy) == "## Official answer\n\nThe first verified paragraph.\n\nThe second verified paragraph."


def test_network_listener_arms_without_page_script_injection() -> None:
    """网络优先监听只订阅浏览器外部 response 事件，不改写网页运行时 API。"""

    class Page:
        def __init__(self) -> None:
            self.events: list[str] = []

        def on(self, event: str, _: object) -> None:
            self.events.append(event)

    page = Page()
    listener = NetworkAnswerListener(page, rule_for("doubao"))  # type: ignore[arg-type]

    asyncio.run(listener.arm())

    assert page.events == ["response"]


def test_network_answer_uses_only_qianwen_iframe_messages() -> None:
    """千问的检索进度即使有 content，也不属于助手回答。"""

    payload = "\n".join(
        (
            'data: {"data":{"messages":[{"mime_type":"signal/post","content":"正在搜索"}]}}',
            'data: {"data":{"messages":[{"mime_type":"bar/workflow","content":"工作流进度"}]}}',
            'data: {"data":{"messages":[{"mime_type":"multi_load/iframe","content":"## Official answer\\n\\nThe first verified paragraph."}]}}',
            'data: {"data":{"messages":[{"mime_type":"multi_load/iframe","content":"## Official answer\\n\\nThe first verified paragraph.\\n\\nThe second verified paragraph."}]}}',
        )
    )

    policy = rule_for("qianwen").network_answer
    assert policy is not None
    assert _network_answer_from_payload(payload, policy) == "## Official answer\n\nThe first verified paragraph.\n\nThe second verified paragraph."


def test_network_answer_uses_only_wenxin_markdown_generator() -> None:
    """文心的思考过程和追问建议必须与 markdown-yiyan 正文隔离。"""

    payload = "\n".join(
        (
            'data: {"data":{"message":{"content":{"generator":{"component":"thinking","type":"entry","data":{"value":"思考过程"}}}}}}',
            'data: {"data":{"message":{"content":{"generator":{"component":"markdown-yiyan","type":"entry","data":{"value":"## Official answer\\n\\nThe first verified paragraph."}}}}}}',
            'data: {"data":{"message":{"content":{"generator":{"component":"markdown-yiyan","type":"entry","data":{"value":"## Official answer\\n\\nThe first verified paragraph.\\n\\nThe second verified paragraph."}}}}}}',
        )
    )

    policy = rule_for("wenxin").network_answer
    assert policy is not None
    assert _network_answer_from_payload(payload, policy) == "## Official answer\n\nThe first verified paragraph.\n\nThe second verified paragraph."


def test_network_answer_endpoints_are_exact_platform_completion_routes() -> None:
    """监听范围必须是完成接口，而不是平台 API 前缀。"""

    expected = {
        "doubao": ("https://www.doubao.com/chat/completion", True),
        "qianwen": ("https://chat2.qianwen.com/api/v2/chat", True),
        "wenxin": ("https://chat.baidu.com/aichat/api/conversation", True),
    }
    for provider, (url, matched) in expected.items():
        policy = rule_for(provider).network_answer
        assert policy is not None
        assert any(endpoint.matches(url, "POST") is matched for endpoint in policy.endpoints)
        assert not any(
            endpoint.matches(url.replace("/chat", "/telemetry"), "POST")
            for endpoint in policy.endpoints
        )


def test_v4_submission_contracts_are_strongly_typed_and_kimi_rejects_short_labels() -> None:
    """提交动作和最小回答长度都必须留在随镜像发布的平台规则中。"""

    assert all(isinstance(rule.submission_method, SubmissionMethod) for rule in RULES.values())
    assert RULES[ProviderName.QIANWEN].submission_method is SubmissionMethod.ENTER
    assert RULES[ProviderName.WENXIN].submission_method is SubmissionMethod.ENTER
    assert RULES[ProviderName.DOUBAO].submission_method is SubmissionMethod.BUTTON
    assert RULES[ProviderName.KIMI].answer_wait.minimum_answer_characters == 20
    assert RULES[ProviderName.KIMI].dismiss_selectors == (
        "[role='button']:has-text('Got it')",
        "button:has-text('Got it')",
        ":text-is('Got it')",
    )
    assert RULES[ProviderName.KIMI].dismiss_wait_seconds == 10.0
    # 豆包动画 textarea 在 focus 后会消失，必须优先使用稳定的富文本输入框。
    assert RULES[ProviderName.DOUBAO].prompt_selectors[0] == "[contenteditable='true']"
    assert RULES[ProviderName.DOUBAO].interaction_pacing.entry_settle_seconds == 1.0
    assert "button[aria-label='关闭']" in RULES[ProviderName.DOUBAO].dismiss_selectors
    assert ":text-is('下次提醒我')" in RULES[ProviderName.DOUBAO].dismiss_selectors
    assert RULES[ProviderName.DOUBAO].dismiss_wait_seconds == 3.0
    # 六个平台统一使用十分钟网络等待窗口，覆盖检索型问题的长首段延迟。
    assert RULES[ProviderName.DOUBAO].network_answer.timeout_seconds == 120.0
    assert all(
        rule.network_answer is not None and rule.network_answer.timeout_seconds == 120.0
        for rule in RULES.values()
    )
    assert RULES[ProviderName.DOUBAO].interaction_pacing.submission_retry_window_seconds == 60.0
    assert RULES[ProviderName.DOUBAO].interaction_pacing.submission_retry_interval_seconds == 10.0


def test_answer_wait_policy_rejects_platform_placeholder_text() -> None:
    """占位文案即使长度足够，也不能被误写为元宝的正式回答。"""

    policy = AnswerWaitPolicy(
        minimum_answer_characters=10,
        rejected_text_fragments=("正在思考", "Quick Answer"),
    )

    assert not policy.accepts("正在思考\nQuick Answer")
    assert policy.accepts("这是已经稳定展示的正式回答内容。")


def test_v4_conversation_url_is_sanitized_by_sidecar_rule() -> None:
    rule = rule_for("deepseek")

    assert conversation_url(rule, "https://chat.deepseek.com/a/chat/abc?token=secret#fragment") == "https://chat.deepseek.com/a/chat/abc"
    assert conversation_url(rule, "https://example.com/a/chat/abc") is None


def test_v4_prepare_uses_bounded_keyboard_input_and_locator_derived_pointer_motion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """指针坐标只从已审查 Locator 边界派生，随后用浏览器原生事件完成聚焦与输入。"""

    class Prompt:
        def __init__(self) -> None:
            self.events: list[tuple[str, object]] = []

        async def hover(self) -> None:
            self.events.append(("hover", None))

        async def focus(self) -> None:
            self.events.append(("focus", None))

        async def press_sequentially(self, value: str, *, delay: int) -> None:
            self.events.append(("type", (value, delay)))

        async def bounding_box(self) -> dict[str, float]:
            return {"x": 10.0, "y": 20.0, "width": 100.0, "height": 40.0}

    class Body:
        async def inner_text(self, timeout: int) -> str:
            return "authorized chat page"

    class Mouse:
        def __init__(self) -> None:
            self.moves: list[tuple[float, float, int]] = []

        async def move(self, x: float, y: float, *, steps: int) -> None:
            self.moves.append((x, y, steps))

    class Page:
        def __init__(self) -> None:
            self.mouse = Mouse()

        async def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
            assert url == rule_for("deepseek").entry_url
            assert wait_until == "domcontentloaded"
            assert timeout == 45_000

        def locator(self, selector: str) -> Body:
            assert selector == "body"
            return Body()

    import app.provider_automation as automation

    prompt = Prompt()
    pauses: list[float] = []

    async def first_visible(*_: object, **__: object) -> Prompt:
        return prompt

    async def record_sleep(seconds: float) -> None:
        pauses.append(seconds)

    monkeypatch.setattr(automation, "_first_visible", first_visible)
    monkeypatch.setattr(automation.asyncio, "sleep", record_sleep)

    page = Page()
    asyncio.run(prepare(page, rule_for("deepseek"), "abcdefghij"))

    assert prompt.events == [
        ("hover", None),
        ("focus", None),
        ("type", ("abcdefgh", 90)),
        ("type", ("ij", 90)),
    ]
    assert pauses == [0.2, 0.35]
    assert page.mouse.moves == [(60.0, 40.0, 6)]


def test_v4_prepare_reselects_a_stable_prompt_before_typing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """豆包过渡 textarea 被卸载时，只在写入前重选稳定编辑器。

    豆包的 React 首屏可能短暂暴露一个即将被替换的 textarea。它通过可见性检查后仍
    可能在 focus 时脱离 DOM。准备阶段尚未向平台提交问题，因此允许有限次数地重新
    定位和聚焦；一旦开始输入则绝不重试，避免重复写入问题。
    """

    class DetachedPrompt:
        async def focus(self) -> None:
            raise RuntimeError("Element is detached from DOM")

    class StablePrompt:
        def __init__(self) -> None:
            self.events: list[str] = []

        async def hover(self) -> None:
            self.events.append("hover")

        async def focus(self) -> None:
            self.events.append("focus")

        async def press_sequentially(self, value: str, *, delay: int) -> None:
            self.events.append(f"type:{value}:{delay}")

    class Body:
        async def inner_text(self, timeout: int) -> str:
            return "authorized doubao chat page"

    class Page:
        async def goto(self, *_: object, **__: object) -> None:
            return None

        def locator(self, selector: str) -> Body:
            assert selector == "body"
            return Body()

    import app.provider_automation as automation

    prompts = [DetachedPrompt(), StablePrompt()]
    stable_prompt = prompts[1]

    async def first_visible(*_: object, **__: object) -> object:
        return prompts.pop(0)

    async def record_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(automation, "_first_visible", first_visible)
    monkeypatch.setattr(automation.asyncio, "sleep", record_sleep)

    asyncio.run(prepare(Page(), rule_for("doubao"), "问题"))

    assert prompts == []
    assert stable_prompt.events == ["focus", "type:问题:90"]


def test_v4_doubao_focuses_after_pointer_motion_without_waiting_for_hover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """豆包 textarea 的 hover 受遮罩拦截时，直接聚焦仍可安全准备问题。"""

    class Prompt:
        def __init__(self) -> None:
            self.events: list[str] = []

        async def hover(self) -> None:
            raise AssertionError("豆包的过渡 textarea 不应依赖 hover 完成")

        async def focus(self) -> None:
            self.events.append("focus")

        async def press_sequentially(self, value: str, *, delay: int) -> None:
            self.events.append(f"type:{value}:{delay}")

    class Body:
        async def inner_text(self, timeout: int) -> str:
            return "authorized doubao chat page"

    class Page:
        async def goto(self, *_: object, **__: object) -> None:
            return None

        def locator(self, selector: str) -> Body:
            assert selector == "body"
            return Body()

    import app.provider_automation as automation

    prompt = Prompt()

    async def first_visible(*_: object, **__: object) -> Prompt:
        return prompt

    async def record_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(automation, "_first_visible", first_visible)
    monkeypatch.setattr(automation.asyncio, "sleep", record_sleep)

    asyncio.run(prepare(Page(), rule_for("doubao"), "问题"))

    assert prompt.events == ["focus", "type:问题:90"]


def test_v4_doubao_types_a_short_prompt_in_one_native_keyboard_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """豆包输入框会在首段输入后重绘，短问题不能拆成两个旧 Locator 操作。"""

    class Prompt:
        def __init__(self) -> None:
            self.values: list[tuple[str, int]] = []

        async def focus(self) -> None:
            return None

        async def press_sequentially(self, value: str, *, delay: int) -> None:
            self.values.append((value, delay))

    class Body:
        async def inner_text(self, timeout: int) -> str:
            return "authorized doubao chat page"

    class Page:
        async def goto(self, *_: object, **__: object) -> None:
            return None

        def locator(self, selector: str) -> Body:
            assert selector == "body"
            return Body()

    import app.provider_automation as automation

    prompt = Prompt()

    async def first_visible(*_: object, **__: object) -> Prompt:
        return prompt

    async def record_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(automation, "_first_visible", first_visible)
    monkeypatch.setattr(automation.asyncio, "sleep", record_sleep)

    asyncio.run(prepare(Page(), rule_for("doubao"), "一二三四五六七八九十"))

    assert prompt.values == [("一二三四五六七八九十", 90)]


def test_v4_doubao_submits_with_the_rightmost_control_adjacent_to_the_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """豆包页面同时存在搜索 SVG 与发送箭头时，必须选择输入框旁的发送按钮。"""

    class Prompt:
        async def bounding_box(self) -> dict[str, float]:
            return {"x": 480.0, "y": 836.0, "width": 1_000.0, "height": 98.0}

    class Button:
        def __init__(self, name: str, box: dict[str, float], events: list[str]) -> None:
            self.name = name
            self.box = box
            self.events = events

        @property
        def first(self) -> Button:
            return self

        async def is_visible(self, *, timeout: int) -> bool:
            return True

        async def is_enabled(self, *, timeout: int) -> bool:
            return True

        async def bounding_box(self) -> dict[str, float]:
            return self.box

        async def hover(self) -> None:
            self.events.append(f"hover:{self.name}")

        async def click(self, *, timeout: int, no_wait_after: bool) -> None:
            self.events.append(f"click:{self.name}")

    class Buttons:
        def __init__(self, values: list[Button]) -> None:
            self.values = values

        async def count(self) -> int:
            return len(self.values)

        def nth(self, index: int) -> Button:
            return self.values[index]

    class Page:
        def __init__(self, search: Button, buttons: Buttons) -> None:
            self.search = search
            self.buttons = buttons

        def locator(self, selector: str) -> Button | Buttons:
            if selector == "button:has(svg)":
                return self.search
            if selector == "button, [role='button']":
                return self.buttons
            raise AssertionError(f"unexpected selector: {selector}")

    import app.provider_automation as automation

    events: list[str] = []
    search = Button("search", {"x": 230.0, "y": 20.0, "width": 40.0, "height": 40.0}, events)
    send = Button("send", {"x": 1_430.0, "y": 870.0, "width": 48.0, "height": 48.0}, events)
    page = Page(search, Buttons([search, send]))

    async def first_visible(*_: object, **__: object) -> Prompt:
        return Prompt()

    async def record_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(automation, "_first_visible", first_visible)
    monkeypatch.setattr(automation.asyncio, "sleep", record_sleep)

    asyncio.run(submit(page, rule_for("doubao")))

    assert events == ["click:send"]


def test_v4_enter_submit_waits_after_focus_with_locator_derived_pointer_motion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enter 提交仍只有一次，并在输入框聚焦和页面稳定后执行。"""

    class Prompt:
        def __init__(self) -> None:
            self.events: list[str] = []

        async def hover(self) -> None:
            self.events.append("hover")

        async def focus(self) -> None:
            self.events.append("focus")

        async def press(self, key: str) -> None:
            self.events.append(key)

    import app.provider_automation as automation

    prompt = Prompt()
    pauses: list[float] = []

    async def first_visible(*_: object, **__: object) -> Prompt:
        return prompt

    async def record_sleep(seconds: float) -> None:
        pauses.append(seconds)

    monkeypatch.setattr(automation, "_first_visible", first_visible)
    monkeypatch.setattr(automation.asyncio, "sleep", record_sleep)

    class Mouse:
        def __init__(self) -> None:
            self.moves: list[tuple[float, float, int]] = []

        async def move(self, x: float, y: float, *, steps: int) -> None:
            self.moves.append((x, y, steps))

    class Page:
        def __init__(self) -> None:
            self.mouse = Mouse()

    page = Page()
    asyncio.run(submit(page, rule_for("deepseek")))

    assert prompt.events == ["hover", "focus", "Enter"]
    assert pauses == [0.2, 0.35]
    # Prompt 夹具没有边界框时应自动回退到 Locator 操作，不能让指针增强阻断一次性提交。
    assert page.mouse.moves == []


def test_v4_submit_rechecks_only_the_provider_allowlisted_dialogs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """输入期间出现的普通提示必须在提交前再检查一次，且不引入动态 selector。"""

    class Prompt:
        def __init__(self) -> None:
            self.events: list[str] = []

        async def hover(self) -> None:
            self.events.append("hover")

        async def focus(self) -> None:
            self.events.append("focus")

        async def press(self, key: str) -> None:
            self.events.append(key)

    class Mouse:
        async def move(self, x: float, y: float, *, steps: int) -> None:
            del x, y, steps

    class Page:
        def __init__(self) -> None:
            self.mouse = Mouse()

    import app.provider_automation as automation

    prompt = Prompt()
    dismissed: list[ProviderName] = []

    async def first_visible(*_: object, **__: object) -> Prompt:
        return prompt

    async def dismiss_dialog(page: object, rule: object) -> bool:
        del page
        assert rule is RULES[ProviderName.KIMI]
        dismissed.append(ProviderName.KIMI)
        return False

    monkeypatch.setattr(automation, "_first_visible", first_visible)
    monkeypatch.setattr(automation, "_dismiss_non_auth_dialogs", dismiss_dialog)

    asyncio.run(submit(Page(), RULES[ProviderName.KIMI]))

    assert dismissed == [ProviderName.KIMI, ProviderName.KIMI]
    assert prompt.events == ["hover", "focus", "Enter"]


def test_v4_kimi_retries_once_after_closing_its_allowlisted_dialog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kimi 普通提示吞掉首次 Enter 时，关闭后只重试同一草稿一次。"""

    class Prompt:
        def __init__(self) -> None:
            self.presses = 0

        async def hover(self) -> None:
            return None

        async def focus(self) -> None:
            return None

        async def press(self, key: str) -> None:
            assert key == "Enter"
            self.presses += 1

    class Page:
        mouse = object()

    import app.provider_automation as automation

    dismiss_calls = 0
    prompt = Prompt()

    async def first_visible(*_: object, **__: object) -> Prompt:
        return prompt

    async def dismiss_dialog(page: object, rule: object) -> bool:
        nonlocal dismiss_calls
        del page
        assert rule is RULES[ProviderName.KIMI]
        dismiss_calls += 1
        # 第一次检查发生在按键前；第二次是 Enter 后的普通提示，第三次确认遮罩
        # 已关闭，允许任务继续等候网络完成结果。
        return dismiss_calls == 2

    monkeypatch.setattr(automation, "_first_visible", first_visible)
    monkeypatch.setattr(automation, "_dismiss_non_auth_dialogs", dismiss_dialog)

    asyncio.run(submit(Page(), RULES[ProviderName.KIMI]))

    assert dismiss_calls == 3
    assert prompt.presses == 2


def test_v4_kimi_repeated_dialog_after_retry_requires_manual_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """第二次 Enter 仍触发同一提示时停止，避免循环提交。"""

    class Prompt:
        async def hover(self) -> None:
            return None

        async def focus(self) -> None:
            return None

        async def press(self, key: str) -> None:
            assert key == "Enter"

    class Page:
        mouse = object()

    import app.provider_automation as automation

    dismiss_calls = 0

    async def first_visible(*_: object, **__: object) -> Prompt:
        return Prompt()

    async def dismiss_dialog(page: object, rule: object) -> bool:
        nonlocal dismiss_calls
        del page
        assert rule is RULES[ProviderName.KIMI]
        dismiss_calls += 1
        return dismiss_calls in {2, 3}

    monkeypatch.setattr(automation, "_first_visible", first_visible)
    monkeypatch.setattr(automation, "_dismiss_non_auth_dialogs", dismiss_dialog)

    with pytest.raises(ProviderAutomationError) as error:
        asyncio.run(submit(Page(), RULES[ProviderName.KIMI]))

    assert error.value.code == "manual_intervention_required"
    assert dismiss_calls == 3


def test_v4_dialog_dismissal_waits_for_a_late_allowlisted_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """异步出现的权益提示必须在短窗口内关闭，但不得检索白名单之外的元素。"""

    import app.provider_automation as automation

    class Clock:
        value = 0.0

        def monotonic(self) -> float:
            return self.value

    class Button:
        def __init__(self, selector: str, clock: Clock) -> None:
            self.selector = selector
            self.clock = clock
            self.clicked = False

        async def is_visible(self, *, timeout: int) -> bool:
            del timeout
            return self.selector == ":text-is('Got it')" and self.clock.value >= 0.2

        async def is_enabled(self, *, timeout: int) -> bool:
            del timeout
            return True

        async def bounding_box(self) -> None:
            return None

        async def click(self, *, timeout: int, no_wait_after: bool) -> None:
            assert timeout == 5_000
            assert no_wait_after is True
            self.clicked = True

    class LocatorSet:
        def __init__(self, button: Button) -> None:
            self.first = button

    class Page:
        def __init__(self, clock: Clock) -> None:
            self.mouse = object()
            self.selectors: list[str] = []
            self.buttons: dict[str, Button] = {}
            self.clock = clock

        def locator(self, selector: str) -> LocatorSet:
            self.selectors.append(selector)
            button = self.buttons.setdefault(selector, Button(selector, self.clock))
            return LocatorSet(button)

    clock = Clock()
    page = Page(clock)

    async def advance_clock(seconds: float) -> None:
        clock.value += seconds

    monkeypatch.setattr(automation.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(automation.asyncio, "sleep", advance_clock)

    asyncio.run(automation._dismiss_non_auth_dialogs(page, RULES[ProviderName.KIMI]))

    assert page.buttons[":text-is('Got it')"].clicked is True
    assert set(page.selectors) == set(RULES[ProviderName.KIMI].dismiss_selectors)


def test_deepseek_waits_through_a_stream_pause_before_persisting_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DeepSeek 首段短暂停顿时，不能把未完成片段和空引用持久化。"""

    class Clock:
        value = 0.0

        def monotonic(self) -> float:
            return self.value

        async def sleep(self, seconds: float) -> None:
            self.value += seconds

    class AnswerLocator:
        def __init__(self) -> None:
            self.reads = iter(("首段回答", "首段回答", "最终完整回答", "最终完整回答"))
            self.last_text = "最终完整回答"

        async def is_visible(self, timeout: int) -> bool:
            return True

        async def inner_text(self, timeout: int) -> str:
            self.last_text = next(self.reads, self.last_text)
            return self.last_text

        async def evaluate(self, script: str) -> list[dict[str, str]]:
            assert "aria-label" in script
            return [{"url": "https://example.com/reference", "title": "可展示引用"}]

    class AnswerLocators:
        def __init__(self, answer: AnswerLocator) -> None:
            self.answer = answer

        async def count(self) -> int:
            return 1

        def nth(self, index: int) -> AnswerLocator:
            assert index == 0
            return self.answer

    class EmptyLocators:
        first: EmptyLocators

        def __init__(self) -> None:
            self.first = self
            self.generation_checks = 0

        async def count(self) -> int:
            return 0

        def nth(self, index: int) -> EmptyLocators:
            raise AssertionError(f"不应选择生成中控件: {index}")

        async def is_visible(self, timeout: int) -> bool:
            # 第一次达到静默阈值时，页面仍显示“停止生成”。该信号必须优先于
            # 文本静默，等控件消失后才允许将结果标记为完成。
            self.generation_checks += 1
            return self.generation_checks == 1

    class Page:
        def __init__(self) -> None:
            self.answer = AnswerLocator()
            self.answers = AnswerLocators(self.answer)
            self.empty = EmptyLocators()

        def locator(self, selector: str) -> AnswerLocators | EmptyLocators:
            return self.answers if selector == ".ds-markdown" else self.empty

    import app.provider_automation as automation

    clock = Clock()
    monkeypatch.setattr(automation.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(automation.asyncio, "sleep", clock.sleep)

    answer, citations = asyncio.run(
        wait_result(Page(), rule_for("deepseek"), timeout_seconds=20)
    )

    assert answer == "最终完整回答"
    assert citations == [{"url": "https://example.com/reference", "title": "可展示引用"}]
    # 8 秒静默窗口从“最终完整回答”出现后才开始，避免旧的 1.5 秒过早完成。
    assert clock.value >= 9.0


def test_v4_execution_preview_is_bound_to_an_active_execution() -> None:
    """预览只能读取执行中的页面，不能退化为按账号或 Tab 查询。"""

    async def verify() -> None:
        browser_service = BrowserService()
        page = Mock()
        page.screenshot = AsyncMock(return_value=b"png")
        session = SessionState(user_id="profile-key", context=Mock())
        tab = TabState(tab_id="tab-1", page=page, session=session)
        session.tabs[tab.tab_id] = tab
        browser_service.executions["task:task-1"] = Execution(
            execution_id="task:task-1",
            provider=ProviderName.DEEPSEEK,
            profile_key="profile-key",
            query="question",
            tab=tab,
            state="generating",
        )

        assert await browser_service.capture_execution_preview("task:task-1") == b"png"
        page.screenshot.assert_awaited_once_with(type="png", full_page=True)

        browser_service.executions["task:task-1"].state = "completed"
        with pytest.raises(ProtocolError) as exc_info:
            await browser_service.capture_execution_preview("task:task-1")
        assert exc_info.value.code == "execution_preview_unavailable"

    asyncio.run(verify())


def test_v4_execution_request_rejects_browser_control_fields() -> None:
    """后端只能传任务级字段，不能把会变化的 DOM 规则重新下放给 sidecar。"""
    with pytest.raises(ValidationError):
        ExecutionCreateRequest.model_validate(
            {
                "executionId": "task-1",
                "provider": ProviderName.DEEPSEEK,
                "profileKey": "geo-rpa-deepseek-account-1",
                "query": "question",
                "selector": "textarea",
            }
        )


def test_v4_openapi_declares_the_six_provider_values() -> None:
    schema = app.openapi()
    request_schema = schema["paths"]["/rpa/executions"]["post"]["requestBody"][
        "content"
    ]["application/json"]["schema"]

    assert request_schema["$defs"]["ProviderName"]["enum"] == [
        "doubao",
        "kimi",
        "qianwen",
        "yuanbao",
        "deepseek",
        "wenxin",
    ]


def test_window_publisher_is_an_optional_v4_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_WINDOW_PUBLISHER", "true")

    assert BrowserService().protocol_version == 4


def test_manual_window_allocates_only_a_private_rfb_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """人工认证由 GEO 直连 RFB，不得为每个窗口分配 bridge 端口。"""
    monkeypatch.setenv("WINDOW_PUBLISHER_RFB_BASE_PORT", "5902")
    monkeypatch.setenv("WINDOW_PUBLISHER_WS_BASE_PORT", "6082")
    instance = BrowserService()

    manual_rfb_port, manual_websocket_port = instance._next_window_ports(
        websocket_required=False
    )
    task_rfb_port, task_websocket_port = instance._next_window_ports(
        websocket_required=True
    )

    assert (manual_rfb_port, manual_websocket_port) == (5902, None)
    assert (task_rfb_port, task_websocket_port) == (5903, 6082)


def test_manual_window_features_follow_valid_xvfb_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """人工 popup 必须跟随 Xvfb 尺寸，避免 VNC 右侧产生未绘制区域。"""
    monkeypatch.setenv("VNC_RESOLUTION", "1280x720x24")
    assert manual_window_features() == (
        "popup=yes,width=1280,height=649,left=0,top=0,location=no,toolbar=no,menubar=no,status=no,"
        "personalbar=no,scrollbars=yes,resizable=yes"
    )

    monkeypatch.setenv("VNC_RESOLUTION", "invalid")
    assert manual_window_features() == (
        "popup=yes,width=1920,height=1009,left=0,top=0,location=no,toolbar=no,menubar=no,status=no,"
        "personalbar=no,scrollbars=yes,resizable=yes"
    )


def test_manual_window_features_place_each_publisher_in_a_non_overlapping_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """共享 Xvfb 中的原生窗口必须平铺，否则被遮挡窗口的 x11vnc 帧会变黑。"""
    monkeypatch.setenv("VNC_RESOLUTION", "1280x720x24")
    monkeypatch.setenv("WINDOW_PUBLISHER_GRID_COLUMNS", "3")

    assert "left=0,top=0" in manual_window_features(0)
    assert "left=1280,top=0" in manual_window_features(1)
    assert "left=0,top=720" in manual_window_features(3)
    assert "left=1280,top=720" in manual_window_features(4)

    task_features = task_window_features(4)
    assert "width=1280,height=649" in task_features
    assert "left=1280,top=720" in task_features


def test_window_slot_allocator_reuses_a_released_gap() -> None:
    """关闭人工窗口后应复用空槽，而不是无限扩大 Xvfb 坐标。"""
    service = BrowserService()
    session = SessionState(user_id="profile-key", context=Mock())
    tab = TabState(tab_id="tab-1", page=Mock(), session=session)
    for slot_index in (0, 2):
        window = ManagedWindow(
            handle=f"window-{slot_index}",
            user_id=session.user_id,
            tab=tab,
            window_id=f"0x{slot_index + 1:x}",
            kind="manual",
            slot_index=slot_index,
        )
        service.windows[window.handle] = window

    assert service._next_window_slot() == 1


def test_manual_window_waits_for_first_paint_before_vnc_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """人工 VNC 不能在 popup 仅提交导航、尚未进入首帧合成时启动。"""

    async def run() -> None:
        page = Mock()
        page.is_closed.return_value = False
        page.wait_for_load_state = AsyncMock()
        page.evaluate = AsyncMock()
        settle = AsyncMock()
        monkeypatch.setattr("app.main.asyncio.sleep", settle)

        await wait_for_manual_window_paint(page)

        page.wait_for_load_state.assert_awaited_once_with(
            "domcontentloaded", timeout=5_000
        )
        page.evaluate.assert_awaited_once()
        assert "requestAnimationFrame" in page.evaluate.await_args.args[0]
        settle.assert_awaited_once_with(0.35)

    asyncio.run(run())


def test_manual_window_paint_gate_runs_before_window_publisher_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """发布器只能在人工窗口的首帧门槛完成后读取 X11 framebuffer。"""

    async def run() -> None:
        events: list[str] = []
        publisher_options: dict[str, object] = {}

        async def wait_for_paint(_: object) -> None:
            events.append("paint-ready")

        class Publisher:
            running = False

            def __init__(self, **options: object) -> None:
                publisher_options.update(options)

            async def start(self) -> None:
                events.append("publisher-start")

            async def stop(self) -> None:
                return None

        instance = BrowserService()
        page = Mock()
        page.is_closed.return_value = False
        session = SessionState(user_id="profile-key", context=Mock())
        tab = TabState(tab_id="tab-1", page=page, session=session)
        window = ManagedWindow(
            handle="window-1",
            user_id="profile-key",
            tab=tab,
            window_id="0x123",
            kind="manual",
        )
        instance.windows[window.handle] = window
        monkeypatch.setattr("app.main.wait_for_manual_window_paint", wait_for_paint)
        monkeypatch.setattr("app.main.WindowPublisher", Publisher)

        published = await instance.publish_window(window.handle, window.user_id)

        assert published.state == "published"
        assert events == ["paint-ready", "publisher-start"]
        assert publisher_options["capture_wait_ms"] == 40
        assert publisher_options["capture_defer_ms"] == 40

    asyncio.run(run())


def test_manual_window_creation_is_bounded_without_limiting_online_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """初始化峰值受闸门约束，已发布窗口不会占用后续请求的创建槽位。"""

    async def run() -> None:
        monkeypatch.setattr("app.main.MANUAL_WINDOW_CREATE_CONCURRENCY", 2)
        instance = BrowserService()
        active_creations = 0
        peak_creations = 0
        first_wave_ready = asyncio.Event()
        release_first_wave = asyncio.Event()

        async def create_tab(profile_key: str, _: str) -> Mock:
            nonlocal active_creations, peak_creations
            active_creations += 1
            peak_creations = max(peak_creations, active_creations)
            if active_creations == 2:
                first_wave_ready.set()
            await release_first_wave.wait()
            active_creations -= 1
            return Mock(tab_id=f"tab-{profile_key}")

        async def promote(profile_key: str, _: str, *, kind: str) -> Mock:
            assert kind == "manual"
            return Mock(handle=f"window-{profile_key}")

        async def publish(handle: str, _: str) -> Mock:
            return Mock(handle=handle, state="published")

        monkeypatch.setattr(instance, "create_tab", create_tab)
        monkeypatch.setattr(instance, "promote_tab_to_window", promote)
        monkeypatch.setattr(instance, "publish_window", publish)

        tasks = [
            asyncio.create_task(
                instance.create_manual_session(
                    ProviderName.DEEPSEEK, f"profile-{index}"
                )
            )
            for index in range(5)
        ]
        await asyncio.wait_for(first_wave_ready.wait(), timeout=1)
        assert sum(task.done() for task in tasks) == 0
        release_first_wave.set()
        windows = await asyncio.gather(*tasks)

        assert peak_creations == 2
        assert len(windows) == 5
        assert all(window.state == "published" for window in windows)

    asyncio.run(run())


def test_v4_sidecar_is_not_ready_without_the_window_publisher(
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


def test_hydrate_creates_context_from_storage_state() -> None:
    """GEO snapshot is applied only when the account Context does not exist yet."""

    async def run() -> None:
        service = BrowserService()
        browser = AsyncMock()
        created = []

        async def new_context(**kwargs):
            created.append(kwargs)
            return AsyncMock()

        browser.new_context.side_effect = new_context
        service.browser = browser
        service.browser_ready = True
        state = {"cookies": [{"name": "sid", "value": "1", "domain": "example.com", "path": "/"}]}
        await service.hydrate_account_session("profile-key", state)
        await service.hydrate_account_session("profile-key", {"cookies": []})
        assert len(created) == 1
        assert created[0]["storage_state"]["cookies"][0]["name"] == "sid"

    asyncio.run(run())


def test_invalid_hydrate_snapshot_creates_logged_out_context() -> None:
    """Illegal snapshots hydrate as an empty Context rather than failing closed."""

    async def run() -> None:
        service = BrowserService()
        browser = AsyncMock()
        created = []

        async def new_context(**kwargs):
            created.append(kwargs)
            return AsyncMock()

        browser.new_context.side_effect = new_context
        service.browser = browser
        service.browser_ready = True
        await service.hydrate_account_session("profile-key", {"cookies": "bad"})
        assert created == [{}]

    asyncio.run(run())


def test_checkpoint_returns_storage_state_without_indexed_db() -> None:
    """Manual and task checkpoints export Cookie + LocalStorage only."""

    async def run() -> None:
        context = AsyncMock()
        context.storage_state.return_value = {"cookies": [], "origins": []}
        instance = BrowserService()
        instance.sessions["user-1"] = SessionState(user_id="user-1", context=context)
        state = await instance.checkpoint_account_session("user-1")
        assert state == {"cookies": [], "origins": []}
        context.storage_state.assert_awaited_once_with(indexed_db=False)

    asyncio.run(run())


def test_storage_state_checkpoint_times_out_without_blocking_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Firefox StorageState RPC hang must fail the checkpoint in bounded time."""

    async def run() -> None:
        context = AsyncMock()
        stalled = asyncio.Event()

        async def storage_state(*, indexed_db: bool) -> dict[str, object]:
            assert indexed_db is False
            await stalled.wait()
            return {"cookies": [], "origins": []}

        context.storage_state.side_effect = storage_state
        instance = BrowserService()
        monkeypatch.setattr("app.main.STORAGE_STATE_TIMEOUT_SECONDS", 0.01)
        assert await instance._export_storage_state(SessionState(user_id="user-1", context=context)) is None

    asyncio.run(run())


def test_close_session_does_not_write_storage_state_files(tmp_path: Path) -> None:
    """Idle recycle closes the Context without a local profile volume."""

    async def run() -> None:
        instance = BrowserService()
        context = AsyncMock()
        context.close = AsyncMock()
        instance.sessions["user-1"] = SessionState(user_id="user-1", context=context)
        await instance.close_session("user-1")
        context.storage_state.assert_not_called()
        assert list(tmp_path.glob("**/*")) == []

    asyncio.run(run())


    asyncio.run(run())


def test_manual_window_close_times_out_without_blocking_session_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """人工 popup 的关闭 RPC 卡住时，删除请求仍需进入后续 Context 回收。"""

    async def run() -> None:
        service = BrowserService()
        page = Mock()
        page.is_closed.return_value = False
        stalled = asyncio.Event()

        async def close_page() -> None:
            await stalled.wait()

        page.close = AsyncMock(side_effect=close_page)
        session = SessionState(user_id="user-1", context=AsyncMock())
        tab = TabState(tab_id="tab-1", page=page, session=session)
        session.tabs[tab.tab_id] = tab
        window = ManagedWindow(
            handle="window-1",
            user_id=session.user_id,
            tab=tab,
            window_id="42",
            kind="manual",
        )
        service.windows[window.handle] = window
        service._window_handles_by_tab[(session.user_id, tab.tab_id)] = window.handle
        monkeypatch.setattr("app.main.TAB_CLOSE_TIMEOUT_SECONDS", 0.01)

        assert await service.close_managed_window(window.handle, session.user_id) is True
        assert window.handle not in service.windows
        assert tab.tab_id not in session.tabs

    asyncio.run(run())


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


def test_capture_spec_allows_zero_bytes_for_status_only_health_checks() -> None:
    """零字节采集只观察受保护接口状态，不能被误判为非法完成流规格。"""
    spec = normalize_spec(
        {
            "method": "POST",
            "host": "www.kimi.com",
            "path": "/api/user/subscription",
            "maxBytes": 0,
        }
    )

    assert spec["maxBytes"] == 0


def test_status_only_capture_does_not_read_the_response_body() -> None:
    """零字节规格只记录认证状态，不能把受保护响应正文带入 sidecar 内存。"""
    session = SessionState(user_id="user-1", context=Mock())
    tab = TabState(tab_id="tab-1", page=Mock(), session=session)
    capture = Capture(
        capture_id="capture-1",
        spec={
            "method": "POST",
            "host": "www.kimi.com",
            "path": "/api/user/subscription",
            "pathPrefix": None,
            "maxBytes": 0,
            "activateOnSubmit": False,
        },
        armed=True,
    )
    tab.captures[capture.capture_id] = capture
    response = Mock()
    response.request.method = "POST"
    response.url = "https://www.kimi.com/api/user/subscription"
    response.status = 200
    response.headers = {"content-type": "application/json"}
    response.body = AsyncMock()

    asyncio.run(BrowserService()._on_response(tab, response))

    assert capture.state == "complete"
    assert capture.status == 200
    assert capture.body == b""
    response.body.assert_not_awaited()


def test_error_response_keeps_adapter_error_at_top_level() -> None:
    response = asyncio.run(http_exception_response(None, HTTPException(409, {"error": "active", "code": "network_capture_active"})))

    assert response.status_code == 409
    assert json.loads(response.body) == {"error": "active", "code": "network_capture_active"}


def test_error_response_does_not_expose_unapproved_error_codes() -> None:
    response = error_response(ProtocolError(400, "invalid", "internal_error"))

    assert response.detail == {"error": "invalid"}


def test_error_response_exposes_manual_intervention_for_platform_dialogs() -> None:
    """弹窗拦截提交属于人工处理状态，adapter 需要稳定识别该错误码。"""
    response = error_response(
        ProtocolError(409, "manual handling required", "manual_intervention_required")
    )

    assert response.detail == {
        "error": "manual handling required",
        "code": "manual_intervention_required",
    }


def test_locator_read_returns_empty_body_text_while_document_is_initializing() -> None:
    """task window 尚未创建 body 时，登录检查应继续等待而不是中止任务。"""

    class EmptyLocator:
        async def count(self) -> int:
            return 0

        def nth(self, index: int) -> EmptyLocator:
            assert index == 0
            return self

    class InitializingPage:
        def locator(self, selector: str) -> EmptyLocator:
            assert selector == "body"
            return EmptyLocator()

    async def request_locator_text() -> httpx.Response:
        original_sessions = service.sessions
        session = SessionState(user_id="user-1", context=Mock())
        tab = TabState(tab_id="tab-1", page=InitializingPage(), session=session)
        session.tabs[tab.tab_id] = tab
        service.sessions = {session.user_id: session}
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://sidecar") as client:
                return await client.post(
                    "/rpa/tabs/tab-1/locator-read",
                    json={
                        "userId": "user-1",
                        "operation": "inner_text",
                        "target": {"selector": "body", "index": 0},
                    },
                )
        finally:
            service.sessions = original_sessions

    response = asyncio.run(request_locator_text())

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "result": ""}


def test_route_inventory_exposes_only_controlled_geo_rpa_operations() -> None:
    routes = {(route.path, method) for route in app.routes for method in getattr(route, "methods", set())}

    assert {
        ("/health", "GET"),
        ("/tabs", "GET"),
        ("/tabs", "POST"),
        ("/tabs/{tab_id}", "DELETE"),
        ("/tabs/{tab_id}/current-url", "GET"),
        ("/sessions/{user_id}", "DELETE"),
        ("/tabs/{tab_id}/navigate", "POST"),
        ("/tabs/{tab_id}/screenshot", "GET"),
        ("/tabs/{tab_id}/snapshot", "GET"),
        ("/rpa/tabs/{tab_id}/locator-read", "POST"),
        ("/rpa/tabs/{tab_id}/locator-clear", "POST"),
        ("/rpa/tabs/{tab_id}/checkpoint", "POST"),
        ("/rpa/tabs/{tab_id}/manual-input", "POST"),
        ("/rpa/tabs/{tab_id}/locator-screenshot", "POST"),
        ("/rpa/tabs/{tab_id}/manual-window", "POST"),
        ("/rpa/manual-windows/{handle}", "GET"),
        ("/rpa/manual-windows/{handle}", "DELETE"),
        ("/rpa/manual-windows/{handle}/vnc", "POST"),
        ("/rpa/manual-windows", "DELETE"),
        ("/rpa/tabs/{tab_id}/task-window", "POST"),
        ("/rpa/task-windows/{handle}/observer", "POST"),
        ("/rpa/task-windows/{handle}/observer", "DELETE"),
        ("/vnc/status", "GET"),
    }.issubset(routes)
    assert not any("eval" in path or "mcp" in path for path, _ in routes)
    assert not any("storage_state" in path for path, _ in routes)
    assert {(path, method) for path, method in routes if path.startswith("/vnc/")} == {("/vnc/status", "GET")}


def test_v4_task_routes_are_high_level_only() -> None:
    routes = {(route.path, method) for route in app.routes for method in getattr(route, "methods", set())}

    assert {
        ("/rpa/executions", "POST"),
        ("/rpa/executions/{execution_id}/submit", "POST"),
        ("/rpa/executions/{execution_id}", "GET"),
        ("/rpa/executions/{execution_id}", "DELETE"),
        ("/rpa/accounts/keepalive", "POST"),
        ("/rpa/accounts/session", "POST"),
        ("/rpa/accounts/session/checkpoint", "POST"),
        ("/rpa/manual-sessions", "POST"),
    }.issubset(routes)


def test_execution_create_schema_excludes_storage_state() -> None:
    schema = ExecutionCreateRequest.model_json_schema(by_alias=True)
    assert "storageState" not in schema.get("properties", {})
    assert "profileKey" in schema.get("properties", {})


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


def test_session_for_schedules_browser_recovery_after_target_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """浏览器子进程被 OOM Killer 终止后，下一次请求应触发 sidecar 自愈。"""

    class DisconnectedBrowser:
        def is_connected(self) -> bool:
            return False

        async def new_context(self, **_: object) -> object:
            raise RuntimeError("Browser.new_context: Target ... browser has been closed")

    async def run() -> None:
        instance = BrowserService()
        instance.browser = DisconnectedBrowser()  # type: ignore[assignment]
        instance.browser_ready = True
        restart = AsyncMock()
        monkeypatch.setattr(instance, "_restart_browser", restart)

        with pytest.raises(ProtocolError, match="could not create a session"):
            await instance.session_for("user-oom")

        await asyncio.sleep(0)
        restart.assert_awaited_once()
        assert instance.browser_ready is False

    asyncio.run(run())
