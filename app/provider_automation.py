"""六个平台随 Camofox 镜像发布的页面自动化规则。

本模块是平台 DOM、入口、提交和结果提取的唯一归属。调用方只能传入
provider 标识和查询文本，不能下发 selector、URL 或浏览器脚本。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Literal, TypedDict
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import Locator, Page, Response


class ProviderName(str, Enum):
    """受控镜像支持的六个平台；枚举值是后端协议中的稳定英文标识。"""

    DOUBAO = "doubao"
    KIMI = "kimi"
    QIANWEN = "qianwen"
    YUANBAO = "yuanbao"
    DEEPSEEK = "deepseek"
    WENXIN = "wenxin"


class SubmissionMethod(str, Enum):
    """平台提交动作的固定类型。

    ``ENTER`` 表示该平台已通过真实页面验收，焦点在输入框时按 Enter 即发送；
    ``BUTTON`` 表示必须点击 Camofox 镜像内规则声明的发送控件。用枚举替代布尔值，
    可以避免规则阅读者误把 ``False`` 理解成“未定义”或“允许两种方式”。
    """

    ENTER = "enter"
    BUTTON = "button"


class PostSubmitDialogStrategy(str, Enum):
    """提交后命中普通提示白名单时的固定处理方式。

    ``MANUAL_INTERVENTION``：关闭提示后不再动作，由人工确认；
    ``DISMISS_AND_RETRY_ONCE``：仅在已知提示吞掉第一次 Enter 时，关闭后重新按一次
    Enter。第二次仍出现提示即停止，绝不循环重试或尝试未知控件。
    """

    MANUAL_INTERVENTION = "manual_intervention"
    DISMISS_AND_RETRY_ONCE = "dismiss_and_retry_once"


class AnswerExtractionMode(str, Enum):
    """回答节点文本的固定提取方式。

    ``DIRECT_TEXT``：定位结果本身就是单条助手回答，直接读取其文本。
    ``DOUBAO_MAIN_CONVERSATION``：豆包新版没有可审查的回答 class，使用 main 中固定的
    “搜索关键词/参考资料”元数据作为分界，只保留其后的助手正文。
    """

    DIRECT_TEXT = "direct_text"
    DOUBAO_MAIN_CONVERSATION = "doubao_main_conversation"


class NetworkPayloadFormat(str, Enum):
    """平台回答接口的受限响应格式。

    ``SSE`` 表示 ``data:`` 分帧的文本流；``JSON`` 表示一次性 JSON 响应；
    ``CONNECT`` 表示 Kimi 的 Connect/gRPC-web 文本帧。枚举不向后端暴露，避免
    调度层依赖易变的平台传输细节。
    """

    SSE = "sse"
    JSON = "json"
    CONNECT = "connect"


# 六个平台的单次执行最多允许十分钟；这是从提交到最终结果（含有限重试和 DOM 兜底）
# 的总预算，后端只能等待该任务终态，不能通过任务参数改变 sidecar 的规则。
PLATFORM_EXECUTION_TIMEOUT_SECONDS = 10 * 60.0
# 网络监听的首段等待只给两分钟。这样网络流异常时能尽快进入同一 execution 的 DOM 兜底，
# 而不是把十分钟总预算全部消耗在“首个网络响应尚未出现”这一阶段。
NETWORK_FIRST_RESPONSE_TIMEOUT_SECONDS = 2 * 60.0
# 元宝会把搜索卡片和最终正文拆到相邻的两个 SSE 响应中；正文到达后保留短暂
# 收集窗口，等待同一 execution 的搜索卡片，不延长整体十分钟执行预算。
YUANBAO_CITATION_GRACE_SECONDS = 2.0


class NetworkAnswerParser(str, Enum):
    """平台响应正文的静态解析器类型。"""

    JSON_TEXT_FIELDS = "json_text_fields"
    DOUBAO_CONTENT_BLOCK_EVENT = "doubao_content_block_event"
    DEEPSEEK_CONTENT_EVENT = "deepseek_content_event"
    QIANWEN_IFRAME_EVENT = "qianwen_iframe_event"
    YUANBAO_TEXT_EVENT = "yuanbao_text_event"
    WENXIN_MARKDOWN_EVENT = "wenxin_markdown_event"


@dataclass(frozen=True)
class NetworkEndpoint:
    """一个平台聊天结果接口的固定匹配条件。

    规则只匹配已审查的 HTTPS 主机、POST 方法及路径前缀。查询参数、请求头、
    Cookie、请求正文均不参与匹配或保存，避免将账号信息带入执行结果。
    """

    host: str
    path_prefix: str
    payload_format: NetworkPayloadFormat

    def matches(self, url: str, method: str) -> bool:
        parsed = urlsplit(url)
        return (
            method.upper() == "POST"
            and parsed.scheme == "https"
            and parsed.hostname is not None
            and parsed.hostname.lower() == self.host
            and parsed.path.startswith(self.path_prefix)
        )


@dataclass(frozen=True)
class NetworkAnswerPolicy:
    """网络回答的完整性约束。

    ``minimum_answer_characters`` 防止错误接口只返回状态码、角色名或思考占位文字时
    被当作正式回答。所有限制是镜像内静态规则，调用方不能覆盖。
    """

    endpoints: tuple[NetworkEndpoint, ...]
    parser: NetworkAnswerParser = NetworkAnswerParser.JSON_TEXT_FIELDS
    minimum_answer_characters: int = 20
    timeout_seconds: float = NETWORK_FIRST_RESPONSE_TIMEOUT_SECONDS
    max_response_bytes: int = 2 * 1024 * 1024

    def __post_init__(self) -> None:
        if not self.endpoints:
            raise ValueError("Network answer policy requires at least one endpoint")
        if self.minimum_answer_characters < 1:
            raise ValueError("Network answer minimum length must be positive")
        if not 1 <= self.max_response_bytes <= 2 * 1024 * 1024:
            raise ValueError("Network answer byte limit is out of range")


@dataclass(frozen=True)
class AnswerWaitPolicy:
    """单个平台回答完成的判定规则。

    流式回答并不存在跨平台统一的“完成事件”。因此 Sidecar 只依赖已审查的页面信号：
    回答文本在指定时间内保持不变，且（若平台提供）不再显示生成中的停止按钮。这里的
    静默时间必须保守设置，宁可多等几秒，也不能把服务端流在短暂停顿时的中间片段写入
    品牌监测结果。
    """

    quiet_seconds: float = 2.0
    # 超时属于平台规则的一部分：后端只等待 execution 终态，不推断任一平台的流式时长。
    timeout_seconds: float = NETWORK_FIRST_RESPONSE_TIMEOUT_SECONDS
    generation_active_selectors: tuple[str, ...] = ()
    # 仅将达到稳定窗口后的有效回答视为结果。默认值为 1，避免将空的占位容器持久化为回答。
    # 不能把此值盲目设大，因为平台确实可能对简单问题给出很短的有效回答。
    minimum_answer_characters: int = 1
    # 页面初次渲染时常有“思考中”“Quick Answer”等骨架文本。它们不是模型输出，
    # 即使长度达到阈值也必须拒绝，避免错误写入品牌监测结果。
    rejected_text_fragments: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """在镜像启动阶段拒绝不可执行的完成策略，防止静默改坏流式采集语义。"""

        if self.quiet_seconds < 0:
            raise ValueError("Answer quiet duration must not be negative")
        if self.timeout_seconds <= 0:
            raise ValueError("Answer timeout must be positive")
        if self.minimum_answer_characters < 1:
            raise ValueError("Answer minimum characters must be positive")
        if any(not fragment.strip() for fragment in self.rejected_text_fragments):
            raise ValueError("Rejected answer fragments must not be empty")

    def accepts(self, text: str) -> bool:
        """判断当前 DOM 文本是否能作为平台最终回答。

        比较统一采用大小写折叠，规则本身仍是每个平台随镜像发布的固定值。后端只接收
        Camofox 已确认的结构化结果，无法改变此判断。
        """

        normalized = text.casefold()
        return len(text) >= self.minimum_answer_characters and not any(
            fragment.casefold() in normalized
            for fragment in self.rejected_text_fragments
        )


@dataclass(frozen=True)
class InteractionPacingPolicy:
    """已授权页面交互的稳定性节奏。

    此策略只让富文本编辑器和提交控件有可预测的渲染时间。它不伪造鼠标轨迹、指纹、
    网络位置或真人身份，也不会对验证码、登录页和账号限制做自动重试。
    """

    focus_settle_seconds: float = 0.2
    # 页面 DOMContentLoaded 后到编辑器最终挂载前的稳定时间。仅个别平台需要，
    # 默认不额外等待，避免把所有任务的启动延迟固定拉长。
    entry_settle_seconds: float = 0.0
    key_delay_milliseconds: int = 90
    characters_per_chunk: int = 8
    chunk_pause_seconds: float = 0.35
    submit_settle_seconds: float = 0.35
    # 提交后若迟迟没有观察到受限聊天请求，允许在该窗口内进行有界重试。
    # 重试间隔必须大于零，且只适用于尚未确认提交的平台规则。
    submission_retry_window_seconds: float = 0.0
    submission_retry_interval_seconds: float = 10.0
    pointer_motion_steps: int = 6
    # 默认完成“指针移动 -> hover -> focus”。少数页面会将可输入 textarea 置于会
    # 拦截 hover 的过渡层下，此时仍保留受控指针移动，但直接调用 Locator.focus。
    hover_before_focus: bool = True

    def __post_init__(self) -> None:
        """拒绝会导致忙等或超长阻塞的静态规则值。"""

        if (
            self.focus_settle_seconds < 0
            or self.entry_settle_seconds < 0
            or self.submit_settle_seconds < 0
            or self.submission_retry_window_seconds < 0
            or self.submission_retry_interval_seconds <= 0
        ):
            raise ValueError("Interaction settle durations must not be negative")
        if self.key_delay_milliseconds < 0:
            raise ValueError("Interaction key delay must not be negative")
        if self.characters_per_chunk < 1:
            raise ValueError("Interaction chunk size must be positive")
        if self.chunk_pause_seconds < 0:
            raise ValueError("Interaction chunk pause must not be negative")
        if self.pointer_motion_steps < 1:
            raise ValueError("Pointer motion steps must be positive")


# 固定且可审计的节奏优于运行时随机行为，便于测试、容量估算和故障回放。
DEFAULT_INTERACTION_PACING = InteractionPacingPolicy()


@dataclass(frozen=True)
class ProviderRule:
    """单个平台受审查的浏览器自动化契约。"""

    provider: ProviderName
    entry_url: str
    conversation_hosts: tuple[str, ...]
    conversation_paths: tuple[str, ...]
    prompt_selectors: tuple[str, ...]
    submit_selectors: tuple[str, ...]
    answer_selectors: tuple[str, ...]
    # 提交方法属于平台页面契约，后端任务协议不会也不能覆盖它。
    submission_method: SubmissionMethod = SubmissionMethod.BUTTON
    answer_wait: AnswerWaitPolicy = AnswerWaitPolicy()
    answer_extraction: AnswerExtractionMode = AnswerExtractionMode.DIRECT_TEXT
    # v4 正常任务只从该策略监听到的受限聊天响应交付回答。answer_selectors/
    # answer_wait 仍保留给旧的人工诊断和迁移期单元测试，但执行路径不能调用它们。
    network_answer: NetworkAnswerPolicy | None = None
    # 只接受提交后新增的回答节点。豆包当前以单一 main 承载会话，
    # 无法用计数分界，仍由其专用元数据分界函数处理。
    require_new_dom_answer: bool = True
    interaction_pacing: InteractionPacingPolicy = DEFAULT_INTERACTION_PACING
    # 部分页面的全局 SVG 按钮很多，不能按文档顺序挑选。启用后只选择输入框同行右半区
    # 最靠右的可用控件，通常是平台本身的发送箭头。
    submit_near_prompt: bool = False
    # 元宝在搜索阶段后才展示的快速回答选项。它仅是提交模式选择，绝不用于答案读取。
    post_submit_selectors: tuple[str, ...] = ()
    post_submit_wait_seconds: float = 0.0
    # 仅允许关闭已审查、非认证且不改变任务语义的站内提示。验证码、登录、授权和
    # 风控页面绝不放入此列表，仍由人工会话处理。
    dismiss_selectors: tuple[str, ...] = ()
    # 平台首屏异步挂载普通提示时，允许为其声明有限等待窗口。值为 0 表示只进行一次
    # 白名单检查；上限防止页面改版后把正常任务无限阻塞在非业务提示上。
    dismiss_wait_seconds: float = 0.0
    # 该策略只作用于已在本规则白名单声明的普通提示。默认值保持保守，避免其他平台
    # 因弹出未知业务确认框而再次发送问题。
    post_submit_dialog_strategy: PostSubmitDialogStrategy = PostSubmitDialogStrategy.MANUAL_INTERVENTION
    # v4 execution 创建专用 Tab 时已导航到 entry_url。默认仍在 prepare 阶段重载，
    # 以兼容会在填充前依赖完整首屏初始化的平台；豆包的长连接首页例外，重复导航会
    # 让已就绪编辑器被 SPA 重新卸载，因此由其静态规则明确关闭。
    navigate_on_prepare: bool = True

    def __post_init__(self) -> None:
        """校验平台提示关闭窗口，保持它是有限、可审查的页面规则。"""

        if not 0.0 <= self.dismiss_wait_seconds <= 15.0:
            raise ValueError("Dialog dismissal wait must be between 0 and 15 seconds")
        if not 0.0 <= self.post_submit_wait_seconds <= 20.0:
            raise ValueError("Post-submit selection wait must be between 0 and 20 seconds")
        if self.network_answer is None:
            raise ValueError("Every provider must declare a network answer policy")


class Citation(TypedDict):
    """回答 DOM 中提取并允许回传给后端的最小引用信息。"""

    url: str
    title: str


# 回答接口路由在本模块随镜像发布。这里允许同一平台在已验证的主站 API 前缀下
# 进行小范围版本迭代，但不接受后端下发的任意 URL，也不匹配图片、埋点或第三方域名。
DOUBAO_NETWORK_ANSWER = NetworkAnswerPolicy(
    endpoints=(
        # 已由本地真实请求夹具验证。不能放宽为 /api/，否则会误收初始化、埋点或
        # 工作台响应，并把非回答字段误当作 Markdown。
        NetworkEndpoint("www.doubao.com", "/chat/completion", NetworkPayloadFormat.SSE),
    ),
    parser=NetworkAnswerParser.DOUBAO_CONTENT_BLOCK_EVENT,
    # 豆包 completion 在页面已完成渲染后可能继续保持 SSE。正常情况下仍优先等待
    # 网络终态；超过这个有限窗口即转入同一 execution 的严格 main 兜底，而不是把
    # 调度器阻塞到长连接自然关闭。
    # 豆包检索型问题首段可能延迟，但首个网络响应最多等两分钟；剩余时间归总执行预算管理。
    timeout_seconds=NETWORK_FIRST_RESPONSE_TIMEOUT_SECONDS,
)
KIMI_NETWORK_ANSWER = NetworkAnswerPolicy(
    endpoints=(
        NetworkEndpoint("www.kimi.com", "/apiv2/kimi.gateway.chat.v1.ChatService/", NetworkPayloadFormat.CONNECT),
        NetworkEndpoint("www.kimi.com", "/api/chat/", NetworkPayloadFormat.SSE),
    ),
)
QIANWEN_NETWORK_ANSWER = NetworkAnswerPolicy(
    endpoints=(
        NetworkEndpoint("chat2.qianwen.com", "/api/v2/chat", NetworkPayloadFormat.SSE),
    ),
    parser=NetworkAnswerParser.QIANWEN_IFRAME_EVENT,
)
YUANBAO_NETWORK_ANSWER = NetworkAnswerPolicy(
    endpoints=(
        NetworkEndpoint("yuanbao.tencent.com", "/api/", NetworkPayloadFormat.SSE),
        NetworkEndpoint("yuanbao.tencent.com", "/chat/", NetworkPayloadFormat.SSE),
    ),
    parser=NetworkAnswerParser.YUANBAO_TEXT_EVENT,
)
DEEPSEEK_NETWORK_ANSWER = NetworkAnswerPolicy(
    endpoints=(
        NetworkEndpoint("chat.deepseek.com", "/api/v0/chat/completion", NetworkPayloadFormat.SSE),
    ),
    parser=NetworkAnswerParser.DEEPSEEK_CONTENT_EVENT,
)
WENXIN_NETWORK_ANSWER = NetworkAnswerPolicy(
    endpoints=(
        NetworkEndpoint("chat.baidu.com", "/aichat/api/conversation", NetworkPayloadFormat.SSE),
    ),
    parser=NetworkAnswerParser.WENXIN_MARKDOWN_EVENT,
)


RULES: dict[ProviderName, ProviderRule] = {
    # 规则只允许在 Camofox 仓库随镜像发版变更。后端若把 selector 或 URL 作为
    # 请求参数传入，会让未审核的平台改版直接触及已授权账号，故协议中没有该入口。
    ProviderName.DOUBAO: ProviderRule(
        ProviderName.DOUBAO,
        "https://www.doubao.com/chat?channel=xiazais",
        ("doubao.com",),
        ("/chat/",),
        # 豆包会保留一个动画切换中的 textarea。它在 focus 后可能被卸载，实际稳定
        # 编辑器是 contenteditable，故必须优先选择后者，不能让隐藏 textarea 抢占。
        ("[contenteditable='true']", "textarea"),
        # 豆包当前发送箭头位于封闭组件内，实页可见 DOM 只有该 SVG 按钮；Enter
        # 只保留在输入框草稿中，不能视为提交成功。
        ("button:has(svg)",),
        # 2026-09 的豆包回答由无稳定 class 的语义 main 直接承载。文本边界由下方
        # answer_extraction 的固定元数据规则处理，不能退回全局 class 模糊匹配。
        # 先读取页面在部分版本仍保留的最后一张助手 Markdown 卡片；只有该精确节点
        # 不存在时，DOM 兜底才会使用受清洗的 main，避免把工作台文案当作正文。
        (".bot-message .markdown", ".message-content .markdown", "main"),
        SubmissionMethod.BUTTON,
        answer_extraction=AnswerExtractionMode.DOUBAO_MAIN_CONVERSATION,
        network_answer=DOUBAO_NETWORK_ANSWER,
        require_new_dom_answer=False,
        # 当前豆包页面的 textarea 可接收 focus，却会被过渡遮罩长期拦截 hover。保留
        # 受控鼠标移动，只跳过这个不影响输入语义且会阻塞任务的前置 hover。
        interaction_pacing=InteractionPacingPolicy(
            # 后端 RPA 问题上限为 500 字符。豆包会在第一个输入分段后替换 textarea，
            # 因此必须把一条问题交给同一次原生逐键输入，不能在旧 Locator 上继续下一段。
            entry_settle_seconds=1.0,
            characters_per_chunk=500,
            hover_before_focus=False,
            # 豆包发送按钮点击后可能需要较长时间创建 completion 流；最多在一分钟内
            # 每十秒确认一次，只有未观察到聊天请求时才允许再次点击发送。
            submission_retry_window_seconds=60.0,
            submission_retry_interval_seconds=10.0,
        ),
        # 豆包首页会异步弹出“下载电脑版”提示。该弹窗是普通产品推广，不涉及登录、
        # 验证码或订阅授权；只允许点击当前弹窗明确提供的关闭/稍后提醒控件，不能使用
        # 全局坐标或模糊的 ``.close`` 选择器，以免误关会话内容。
        dismiss_selectors=(
            "button[aria-label='关闭']",
            "[role='button'][aria-label='关闭']",
            "button[aria-label='关闭弹窗']",
            "[role='button'][aria-label='关闭弹窗']",
            "button:has-text('下次提醒我')",
            "[role='button']:has-text('下次提醒我')",
            ":text-is('下次提醒我')",
        ),
        # 关闭控件可能在首屏动画期间才挂载；这里只轮询 3 秒，未出现即继续正常流程。
        dismiss_wait_seconds=3.0,
        submit_near_prompt=True,
        navigate_on_prepare=False,
    ),
    ProviderName.KIMI: ProviderRule(
        ProviderName.KIMI,
        "https://www.kimi.com/",
        ("kimi.com",),
        ("/chat/",),
        ("textarea", "[contenteditable='true']"),
        (),
        (".chat-content-item-assistant .markdown-container",),
        SubmissionMethod.ENTER,
        # Kimi 本轮实测中页面曾把“用户”标签暴露为旧回答节点。品牌监测问题不会以
        # 两三个字构成可交付答案，因此该阈值用于拒绝这种伪节点并转为失败退款。
        answer_wait=AnswerWaitPolicy(minimum_answer_characters=20),
        network_answer=KIMI_NETWORK_ANSWER,
        # Kimi 首次进入时可能出现会员权益提示；“Got it”仅关闭提示层，不涉及登录、
        # 验证码或订阅操作，关闭后才允许定位输入框。
        # 当前页面会在主题和实验分支间切换原生 button、role=button 与内部文本节点。
        # 三个候选仍严格绑定同一不可变文案；顺序优先实际可点击语义，最后才回退到文本
        # 节点（点击会冒泡到其所属控件），不扩展为模糊的“任意弹窗关闭”规则。
        dismiss_selectors=(
            "[role='button']:has-text('Got it')",
            "button:has-text('Got it')",
            ":text-is('Got it')",
        ),
        # Kimi 的权益提示可能在编辑器完成初始渲染后才出现。10 秒足以覆盖实测动画，
        # 同时仍小于任务级超时且不会影响其它平台。
        dismiss_wait_seconds=10.0,
        # Kimi 的 ``Got it`` 是可安全关闭的普通权益提示，可能恰好在第一次 Enter 时
        # 覆盖编辑器并吞掉按键。关闭后只重新发送这一次未派发的同一草稿。
        post_submit_dialog_strategy=PostSubmitDialogStrategy.DISMISS_AND_RETRY_ONCE,
    ),
    ProviderName.QIANWEN: ProviderRule(
        ProviderName.QIANWEN,
        "https://www.qianwen.com/chat",
        ("qianwen.com", "tongyi.com"),
        ("/chat/",),
        ("[contenteditable='true']", "textarea"),
        (),
        (".mainContent-FPQMqL",),
        # 2026-09 实页中发送箭头位于封闭组件内，旧 CSS 无法稳定定位；输入框
        # 获得焦点后的 Enter 是该页已验证的提交动作，避免以脆弱按钮索引提交。
        SubmissionMethod.ENTER,
        network_answer=QIANWEN_NETWORK_ANSWER,
    ),
    ProviderName.YUANBAO: ProviderRule(
        ProviderName.YUANBAO,
        "https://yuanbao.tencent.com/",
        ("yuanbao.tencent.com",),
        ("/chat/",),
        (".ql-editor", "[contenteditable='true']", "textarea"),
        (),
        # 完整正文挂在 markdown-body；骨架节点 agent-chat__bubble--ai 会先出现，
        # 必须降级而非优先命中，避免“正在思考 / Quick Answer”覆盖最终回答。
        (".markdown-body", ".agent-chat__bubble--ai"),
        SubmissionMethod.ENTER,
        # 真实页面会先显示“正在思考 / Quick Answer”占位节点。它既不是最终回答，
        # 也不会包含可交付引用；必须等到正文替换该节点后才允许完成。
        answer_wait=AnswerWaitPolicy(
            minimum_answer_characters=40,
            rejected_text_fragments=("正在思考", "Quick Answer"),
        ),
        network_answer=YUANBAO_NETWORK_ANSWER,
        # 页面当前可能显示英文 Quick Answer 或中文“快速回答”；两者都只匹配精确
        # 可点击控件，且只取本轮提交后新增的最后一个选项。
        post_submit_selectors=(
            "button:has-text('Quick Answer')",
            "[role='button']:has-text('Quick Answer')",
            ":text-is('Quick Answer')",
            "button:has-text('快速回答')",
            "[role='button']:has-text('快速回答')",
            ":text-is('快速回答')",
        ),
        post_submit_wait_seconds=20.0,
    ),
    ProviderName.DEEPSEEK: ProviderRule(
        ProviderName.DEEPSEEK,
        "https://chat.deepseek.com/",
        ("deepseek.com",),
        ("/a/chat/",),
        ("textarea", "[contenteditable='true']"),
        (),
        (".ds-markdown",),
        SubmissionMethod.ENTER,
        # DeepSeek 的生成流在段落、检索和推理切换时常出现数秒空档。此前通用的
        # 1.5 秒稳定判定会把这类中间片段误作最终答案，造成正文和引用同时缺失。
        answer_wait=AnswerWaitPolicy(
            quiet_seconds=8.0,
            # DeepSeek 的检索、推理与正文之间可能出现数秒无文本更新；较长超时只由
            # sidecar 内的固定规则拥有，避免后端重新获得平台特定的完成判断职责。
            timeout_seconds=NETWORK_FIRST_RESPONSE_TIMEOUT_SECONDS,
            generation_active_selectors=(
                "button:has-text('停止生成')",
                "button[aria-label*='Stop']",
                "button[aria-label*='stop']",
            ),
        ),
        network_answer=DEEPSEEK_NETWORK_ANSWER,
    ),
    ProviderName.WENXIN: ProviderRule(
        ProviderName.WENXIN,
        "https://yiyan.baidu.com/",
        ("baidu.com",),
        ("/search/",),
        ("textarea", "[contenteditable='true']"),
        (),
        (".markdown-yiyan", ".cosd-markdown-content"),
        # 实页蓝色发送箭头没有稳定的 CSS/ARIA 属性；按 Enter 可以由输入控件本身
        # 触发提交，因而比猜测 class 名称更可维护。
        SubmissionMethod.ENTER,
        network_answer=WENXIN_NETWORK_ANSWER,
    ),
}


class ProviderAutomationError(RuntimeError):
    """可安全映射到稳定 RPA 错误码的平台自动化失败。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def rule_for(provider: ProviderName | str) -> ProviderRule:
    try:
        return RULES[ProviderName(provider)]
    except (KeyError, ValueError) as exc:
        raise ProviderAutomationError("provider_error", "Unsupported RPA provider") from exc


def is_allowed_provider_url(rule: ProviderRule, raw_url: str) -> bool:
    parsed = urlsplit(raw_url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and any(host == suffix or host.endswith(f".{suffix}") for suffix in rule.conversation_hosts)


def conversation_url(rule: ProviderRule, raw_url: str) -> str | None:
    """只返回无查询参数、无片段的已确认原会话地址。"""
    parsed = urlsplit(raw_url)
    if not is_allowed_provider_url(rule, raw_url) or not any(parsed.path.startswith(prefix) for prefix in rule.conversation_paths):
        return None
    return urlunsplit(("https", parsed.netloc, parsed.path, "", ""))


async def _first_visible(page: Page, selectors: tuple[str, ...], timeout_ms: int = 20_000) -> Locator:
    # 多个 selector 表示按优先级回退，而不是把它们拼接为一个宽松选择器。这样平台
    # 改版时能明确知道哪条候选规则生效，避免意外命中页面中不相关的编辑器或回复。
    deadline = time.monotonic() + timeout_ms / 1_000
    while time.monotonic() < deadline:
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if await locator.is_visible(timeout=500):
                    return locator
            except Exception:
                continue
        await asyncio.sleep(0.2)
    raise ProviderAutomationError("page_unavailable", "Provider input or answer control is unavailable")


async def _last_visible(page: Page, selectors: tuple[str, ...], timeout_ms: int = 20_000) -> Locator:
    """返回最后一条可见回答，而不是页面中最早出现的一条。

    同一 profile 可能保留会话历史；回答生成过程中平台也可能临时插入思考或检索节点。
    从末尾反向查找可见节点，可以将本次提交的最新助手回答与历史回答隔离。
    """

    deadline = time.monotonic() + timeout_ms / 1_000
    while time.monotonic() < deadline:
        for selector in selectors:
            locators = page.locator(selector)
            try:
                count = await locators.count()
            except Exception:
                continue
            for index in range(count - 1, -1, -1):
                candidate = locators.nth(index)
                try:
                    if await candidate.is_visible(timeout=500):
                        return candidate
                except Exception:
                    continue
        await asyncio.sleep(0.2)
    raise ProviderAutomationError("page_unavailable", "Provider answer control is unavailable")


async def answer_dom_baseline(page: Page, rule: ProviderRule) -> int:
    """提交前记录候选回答节点数，DOM 兜底只读取本轮新增节点。"""

    if not rule.require_new_dom_answer:
        return 0
    for selector in rule.answer_selectors:
        try:
            return await page.locator(selector).count()
        except Exception:
            continue
    return 0


async def _last_visible_after_baseline(
    page: Page, rule: ProviderRule, baseline_count: int, timeout_ms: int
) -> Locator:
    """定位本次提交后新增的最后一条可见回答，避免读取历史、侧栏和工具卡。"""

    if not rule.require_new_dom_answer:
        return await _last_visible(page, rule.answer_selectors, timeout_ms)
    deadline = time.monotonic() + timeout_ms / 1_000
    while time.monotonic() < deadline:
        for selector in rule.answer_selectors:
            locators = page.locator(selector)
            try:
                count = await locators.count()
            except Exception:
                continue
            for index in range(count - 1, baseline_count - 1, -1):
                candidate = locators.nth(index)
                try:
                    if await candidate.is_visible(timeout=500):
                        return candidate
                except Exception:
                    continue
        await asyncio.sleep(0.2)
    raise ProviderAutomationError("answer_timeout", "Provider did not render a new answer node")


async def _generation_is_active(page: Page, selectors: tuple[str, ...]) -> bool:
    """检查已知“生成中”控件；未知或变更后的控件不能阻断静默回退。"""

    for selector in selectors:
        try:
            if await page.locator(selector).first.is_visible(timeout=300):
                return True
        except Exception:
            continue
    return False


async def _move_pointer_to_locator(
    page: Page, locator: Locator, policy: InteractionPacingPolicy
) -> None:
    """将浏览器原生鼠标移动到已审查元素中心。

    坐标仅从当前可见的已定位控件边界计算，既不扫描页面，也不接受后端下发的坐标。
    ``Mouse.move(..., steps=...)`` 由浏览器在当前文档内分步派发真实鼠标事件，随后仍由
    Locator 的 hover/click 负责可操作性校验；无法读取边界时直接退回 Locator 操作。
    """

    try:
        box = await locator.bounding_box()
        if box is None:
            return
        center_x = box["x"] + box["width"] / 2
        center_y = box["y"] + box["height"] / 2
        await page.mouse.move(center_x, center_y, steps=policy.pointer_motion_steps)
    except Exception:
        # 鼠标移动只是已授权操作的稳定性增强。元素在动画、重绘或导航中失效时，
        # 不应掩盖后续 Locator hover/click 给出的受控失败状态。
        return


async def _focus_prompt(
    page: Page, prompt: Locator, policy: InteractionPacingPolicy
) -> None:
    """让已定位输入框完成 hover、聚焦和短暂稳定后再写入文本。

    仅调用浏览器原生 Locator 操作，不计算坐标或合成鼠标轨迹。平台若显示验证或遮罩，
    后续可见性/点击检查会失败并收敛为人工处理，绝不尝试绕过。
    """

    await _move_pointer_to_locator(page, prompt, policy)
    # Playwright 的 Locator 默认动作超时较长。对于 SPA 过渡节点，继续等待既不会
    # 让该节点重新挂载，也会占用一次执行的准备窗口；这里使用短时界限把控制权交回
    # 给调用方，由调用方在“尚未输入”时决定是否重选稳定控件。
    if policy.hover_before_focus:
        await asyncio.wait_for(prompt.hover(), timeout=5.0)
    await asyncio.sleep(policy.focus_settle_seconds)
    await asyncio.wait_for(prompt.focus(), timeout=5.0)


async def _first_stable_prompt(page: Page, rule: ProviderRule) -> Locator:
    """定位并聚焦一个稳定输入框，且只允许在写入问题前有限重选。

    某些 SPA 会先渲染过渡 textarea，随后替换为真正的富文本编辑器。可见性并不能保证
    Locator 在 hover/focus 时仍然挂载，因此最多尝试三次“定位 -> 聚焦”。本函数绝不
    负责输入或提交：一旦问题已开始写入，调用方不得重试，以免在平台侧形成重复内容。
    """

    last_error: Exception | None = None
    for attempt in range(3):
        prompt = await _first_visible(page, rule.prompt_selectors, timeout_ms=5_000)
        try:
            await _focus_prompt(page, prompt, rule.interaction_pacing)
            return prompt
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                # 仅为等待同一已审查 selector 所对应的下一次页面稳定渲染，不读取或
                # 操作页面上任何未列入 ProviderRule 的控件。
                await asyncio.sleep(0.2)

    raise ProviderAutomationError(
        "page_unavailable", "Provider prompt could not become stable"
    ) from last_error


async def _type_in_chunks(
    prompt: Locator, query: str, policy: InteractionPacingPolicy
) -> None:
    """按固定分段写入文本，保留富文本编辑器需要的键盘输入事件。"""

    for offset in range(0, len(query), policy.characters_per_chunk):
        chunk = query[offset : offset + policy.characters_per_chunk]
        await prompt.press_sequentially(chunk, delay=policy.key_delay_milliseconds)
        if offset + policy.characters_per_chunk < len(query):
            await asyncio.sleep(policy.chunk_pause_seconds)


_DOUBAO_REFERENCE_METADATA_PATTERN = re.compile(
    r"搜索\s*\d+\s*个关键词\s*[,，]?\s*参考\s*\d+\s*篇资料\s*"
)
_DOUBAO_COMPOSER_BOUNDARY_PATTERN = re.compile(
    r"\n\s*(?:发消息或按住空格说话(?:[.。…]+)?|豆包\s*快速)"
)
_DOUBAO_WORKBENCH_MENU_PATTERN = re.compile(
    r"\n\s*对话\s*\n\s*帮我写作\s*\n\s*PPT\s*生成\s*\n\s*图像生成\s*\n\s*"
    r"视频生成\s*\n\s*深入研究\s*\n\s*录音转写\s*\n\s*更多\s*$"
)
# 豆包首页工作台会因实验分支插入“工作”等额外条目，不能只依赖完整菜单顺序。
# 这组固定文案同时出现时，main 尚未进入会话回答态；DOM 兜底必须继续等待而非交付。
_DOUBAO_WORKBENCH_SIGNATURE_PATTERN = re.compile(
    r"(?:有什么我能帮你的吗？|对话)\s*[\r\n]+(?:工作\s*[\r\n]+)?对话\s*[\r\n]+"
    r"帮我写作\s*[\r\n]+PPT\s*生成",
    re.MULTILINE,
)

# 平台最终回答会使用的字段名。解析器只读取这些固定字段中的 JSON 字符串；不会
# 递归遍历未知对象，也不会把页面配置、工具调用参数或导航标签拼进 Markdown。
_ANSWER_FIELD_PATTERN = re.compile(
    r'"(?:answer_markdown|answerMarkdown|output_text|content|answer|text)"\s*:\s*"((?:\\.|[^"\\])*)"'
)
_CITATION_PATTERN = re.compile(
    r'"url"\s*:\s*"((?:\\.|[^"\\])*)"[^{}]{0,1200}?"(?:title|name)"\s*:\s*"((?:\\.|[^"\\])*)"'
)
_SSE_DATA_PATTERN = re.compile(r"(?m)^data:\s*(.+)$")
# 元宝正文里的内部下划线标注只服务于其网页渲染器，不能进入持久化 Markdown。
_YUANBAO_ANNOTATION_MARK_PATTERN = re.compile(r"\[\]\(@mark_underline=\d+\)")
_YUANBAO_BUBBLE_CITATION_PATTERN = re.compile(
    r'"text"\s*:\s*"((?:\\.|[^"\\])*)"\s*,\s*"link"\s*:\s*"((?:\\.|[^"\\])*)"'
)
# DeepSeek 的真实回答增量在 p=response/content 事件的 d 字段中；普通 content
# 字段可能只是免责声明或界面状态，不能作为交付正文。


def _decode_json_string(raw: str) -> str:
    """解码一个 JSON 字符串字段，坏帧只返回空值而不能终止整个任务。"""

    try:
        value = json.loads(f'"{raw}"')
    except json.JSONDecodeError:
        return ""
    return value if isinstance(value, str) else ""


def _merge_stream_fragments(fragments: list[str]) -> str:
    """合并增量或累计帧，同时避免累计格式重复拼接正文。"""

    merged = ""
    for fragment in fragments:
        text = fragment.strip("\x00")
        if not text:
            continue
        if text.startswith(merged):
            merged = text
        elif merged.endswith(text):
            continue
        else:
            merged += text
    return merged.strip()


def _yuanbao_answer_from_payload(payload: str) -> str:
    """只读取元宝 SSE 的 type=text/msg 与深度搜索正文片段。"""

    fragments: list[str] = []
    for raw_event in _SSE_DATA_PATTERN.findall(payload):
        if raw_event == "[DONE]":
            continue
        try:
            event = json.loads(raw_event)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "text" and isinstance(event.get("msg"), str):
            fragments.append(event["msg"])
            continue
        if event.get("type") != "deepSearchAgent":
            continue
        contents = event.get("contents")
        if not isinstance(contents, list):
            continue
        for item in contents:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                fragments.append(item["text"])
    answer = _merge_stream_fragments(fragments)
    return _YUANBAO_ANNOTATION_MARK_PATTERN.sub("", answer)


def _deepseek_answer_from_payload(payload: str) -> str:
    """重建 DeepSeek JSON Patch SSE 的 RESPONSE 片段，排除搜索进度。"""
    fragments: list[str] = []
    path: str | None = None
    operation: str | None = None
    for raw_event in _SSE_DATA_PATTERN.findall(payload):
        try:
            event = json.loads(raw_event)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if isinstance(event.get("p"), str):
            path = event["p"]
        if isinstance(event.get("o"), str):
            operation = event["o"]
        if path == "response" and operation == "BATCH" and isinstance(event.get("v"), list):
            for item in event["v"]:
                if isinstance(item, dict) and item.get("type") == "RESPONSE" and isinstance(item.get("content"), str):
                    fragments.append(item["content"])
        elif path == "response/fragments/-1/content" and operation in {"APPEND", "SET"} and isinstance(event.get("v"), str):
            fragments.append(event["v"])
    return _merge_stream_fragments(fragments)


def _doubao_content_block_texts(value: object) -> list[str]:
    """只读取豆包助手 content_block 内明确的 text_block 文本。

    completion 流还会携带用户回显、会话元数据、追问和工具事件。这里不递归遍历
    任意 JSON，确保这些字段即使包含 ``text`` 也不会污染回答。
    """

    if not isinstance(value, list):
        return []

    texts: list[str] = []
    for block in value:
        if not isinstance(block, dict):
            continue
        content = block.get("content")
        if not isinstance(content, dict):
            continue
        text_block = content.get("text_block")
        if isinstance(text_block, dict) and isinstance(text_block.get("text"), str):
            texts.append(text_block["text"])
    return texts


def _doubao_answer_from_payload(payload: str) -> str:
    """合并豆包 /chat/completion SSE 中三个已验证的助手正文载体。"""

    fragments: list[str] = []
    for raw_event in _SSE_DATA_PATTERN.findall(payload):
        if raw_event == "[DONE]":
            continue
        try:
            event = json.loads(raw_event)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue

        # 部分版本直接给出最终或累计文本；这是豆包 completion 的顶层正文，不是
        # 任意嵌套 text 字段。
        if isinstance(event.get("text"), str):
            fragments.append(event["text"])
            continue

        content = event.get("content")
        meta = event.get("meta")
        if isinstance(content, dict) and isinstance(meta, dict) and meta.get("user_type") == 2:
            fragments.extend(_doubao_content_block_texts(content.get("content_block")))
            continue

        patch_operations = event.get("patch_op")
        if not isinstance(patch_operations, list):
            continue
        for patch in patch_operations:
            if not isinstance(patch, dict) or patch.get("patch_object") != 1:
                continue
            patch_value = patch.get("patch_value")
            if isinstance(patch_value, dict):
                fragments.extend(_doubao_content_block_texts(patch_value.get("content_block")))
    return _merge_stream_fragments(fragments)


def _qianwen_answer_from_payload(payload: str) -> str:
    """仅合并千问 multi_load/iframe 消息的累积 Markdown。

    ``signal/post``、``bar/workflow`` 等同一 SSE 内的检索进度不属于模型回答，
    即使它们含有可读文本也必须丢弃。
    """

    fragments: list[str] = []
    for raw_event in _SSE_DATA_PATTERN.findall(payload):
        try:
            event = json.loads(raw_event)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        response_data = event.get("data")
        if not isinstance(response_data, dict):
            continue
        messages = response_data.get("messages")
        if not isinstance(messages, list):
            continue
        for message in messages:
            if not isinstance(message, dict) or message.get("mime_type") != "multi_load/iframe":
                continue
            content = message.get("content")
            if isinstance(content, str):
                fragments.append(content)
    return _merge_stream_fragments(fragments)


def _wenxin_answer_from_payload(payload: str) -> str:
    """仅合并文心 markdown-yiyan entry 的正文增量。

    文心会把思考过程、检索结果、追问建议和状态包放进同一个流；只有这个固定
    generator 组合是最终 Markdown 回答，禁止用泛化 ``content`` 回退。
    """

    fragments: list[str] = []
    for raw_event in _SSE_DATA_PATTERN.findall(payload):
        try:
            event = json.loads(raw_event)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        response_data = event.get("data")
        if not isinstance(response_data, dict):
            continue
        message = response_data.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, dict):
            continue
        generator = content.get("generator")
        if (
            not isinstance(generator, dict)
            or generator.get("component") != "markdown-yiyan"
            or generator.get("type") != "entry"
        ):
            continue
        generator_data = generator.get("data")
        if isinstance(generator_data, dict) and isinstance(generator_data.get("value"), str):
            fragments.append(generator_data["value"])
    return _merge_stream_fragments(fragments)


def _network_answer_from_payload(payload: str, policy: NetworkAnswerPolicy) -> str:
    """从受限聊天响应中提取 Markdown 正文。

    原始响应仅在此函数调用链中短暂存在。字段白名单和最小长度共同防止把协议状态、
    工具元数据或网站工作台文案作为回答交付。
    """

    if policy.parser is NetworkAnswerParser.DOUBAO_CONTENT_BLOCK_EVENT:
        answer = _doubao_answer_from_payload(payload)
    elif policy.parser is NetworkAnswerParser.DEEPSEEK_CONTENT_EVENT:
        answer = _deepseek_answer_from_payload(payload)
    elif policy.parser is NetworkAnswerParser.QIANWEN_IFRAME_EVENT:
        answer = _qianwen_answer_from_payload(payload)
    elif policy.parser is NetworkAnswerParser.YUANBAO_TEXT_EVENT:
        answer = _yuanbao_answer_from_payload(payload)
    elif policy.parser is NetworkAnswerParser.WENXIN_MARKDOWN_EVENT:
        answer = _wenxin_answer_from_payload(payload)
    else:
        fragments = [_decode_json_string(match) for match in _ANSWER_FIELD_PATTERN.findall(payload)]
        answer = _merge_stream_fragments(fragments)
    return answer if len(answer) >= policy.minimum_answer_characters else ""


def _append_citation(
    citations: list[Citation], seen_urls: set[str], url: object, title: object
) -> None:
    """校验并追加一条公开引用，拒绝非 HTTP 链接和空标题。"""

    if not isinstance(url, str) or not isinstance(title, str):
        return
    normalized_url = url.strip()
    normalized_title = title.strip()
    parsed = urlsplit(normalized_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or not normalized_title
        or normalized_url in seen_urls
    ):
        return
    seen_urls.add(normalized_url)
    citations.append({"url": normalized_url, "title": normalized_title})


def _collect_doubao_citations(
    value: object, citations: list[Citation], seen_urls: set[str]
) -> None:
    """从豆包已审查的 ``text_card`` 与 ``meta_info`` 节点提取引用。

    豆包把检索卡片放在 ``search_query_result_block.results``，把正文内联引用
    放在 ``meta_info[].info`` 的 JSON 字符串中。只遍历这两个明确节点，不扫描任意
    ``url`` 字段，避免把图标、埋点或会话资源地址当成来源。
    """

    if isinstance(value, Mapping):
        text_card = value.get("text_card")
        if isinstance(text_card, Mapping):
            _append_citation(
                citations,
                seen_urls,
                text_card.get("url"),
                text_card.get("title"),
            )
        meta_info = value.get("meta_info")
        if isinstance(meta_info, list):
            for item in meta_info:
                if not isinstance(item, Mapping) or item.get("type") != 2:
                    continue
                info = item.get("info")
                if not isinstance(info, str):
                    continue
                try:
                    info_value = json.loads(info)
                except json.JSONDecodeError:
                    continue
                if isinstance(info_value, Mapping):
                    _append_citation(
                        citations,
                        seen_urls,
                        info_value.get("url"),
                        info_value.get("title"),
                    )
        for child in value.values():
            _collect_doubao_citations(child, citations, seen_urls)
    elif isinstance(value, list):
        for child in value:
            _collect_doubao_citations(child, citations, seen_urls)


def _collect_yuanbao_citations(
    value: object, citations: list[Citation], seen_urls: set[str]
) -> None:
    """从元宝 ``deepSearchAgent`` 的已审查搜索卡片中提取引用。

    元宝搜索结果使用 ``bubbles[].link`` 保存地址、``bubbles[].text`` 保存标题，
    与其他平台常见的 ``url/title`` 命名不同。这里只处理明确的 ``web_search``
    工具节点和其 ``bubbleList`` 项，避免把正文、图标或会话元数据里的任意 URL
    错当成引用来源。
    """

    if not isinstance(value, Mapping):
        if isinstance(value, list):
            for child in value:
                _collect_yuanbao_citations(child, citations, seen_urls)
        return

    if value.get("type") == "searchGuid":
        # 正文完成后元宝会额外发送 searchGuid 事件，docs 才是右侧“引用来源”面板
        # 使用的稳定来源清单；优先使用 title，缺失时再使用 webSiteSource。
        docs = value.get("docs")
        if isinstance(docs, list):
            for doc in docs:
                if not isinstance(doc, Mapping):
                    continue
                title = doc.get("title")
                if not isinstance(title, str) or not title.strip():
                    title = doc.get("webSiteSource")
                _append_citation(citations, seen_urls, doc.get("url"), title)

    if value.get("type") == "deepSearchAgent":
        contents = value.get("contents")
        if isinstance(contents, list):
            for content in contents:
                if not isinstance(content, Mapping):
                    continue
                if content.get("type") != "toolCall" or content.get("tcname") != "web_search":
                    continue
                items = content.get("items")
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, Mapping) or item.get("type") != "bubbleList":
                        continue
                    bubbles = item.get("bubbles")
                    if not isinstance(bubbles, list):
                        continue
                    for bubble in bubbles:
                        if not isinstance(bubble, Mapping):
                            continue
                        _append_citation(
                            citations,
                            seen_urls,
                            bubble.get("link"),
                            bubble.get("text"),
                        )

    # 某些版本把 deepSearchAgent 包装成字符串后再放进 SSE，无法还原为 Mapping；
    # 对这类响应仅在 Yuanbao 专属解析器中读取紧邻的 text/link 对，作为协议兼容层。

    # 同一响应可能把工具节点包在 data、payload 等容器中；继续遍历容器，
    # 但不会对普通字符串做 URL 正则扫描。
    for child in value.values():
        if isinstance(child, (Mapping, list)):
            _collect_yuanbao_citations(child, citations, seen_urls)


def _collect_wenxin_citations(
    value: object, citations: list[Citation], seen_urls: set[str]
) -> None:
    """提取文心 ``thinkingSteps.referenceList`` 中的公开来源。

    文心将检索结果放在思考事件的 ``referenceList``，正文事件本身只有
    ``markdown-yiyan`` 增量。仅接受该固定组件下的 ``url + text/source`` 字段，
    避免把图标地址、分享地址或正文中的链接误当成引用。
    """

    if not isinstance(value, Mapping):
        if isinstance(value, list):
            for child in value:
                _collect_wenxin_citations(child, citations, seen_urls)
        return

    generator = value.get("generator")
    if isinstance(generator, Mapping) and generator.get("component") == "thinkingSteps":
        data = generator.get("data")
        if isinstance(data, Mapping):
            references = data.get("referenceList")
            if isinstance(references, list):
                for reference in references:
                    if not isinstance(reference, Mapping):
                        continue
                    title = reference.get("text")
                    if not isinstance(title, str) or not title.strip():
                        title = reference.get("source")
                    _append_citation(
                        citations,
                        seen_urls,
                        reference.get("url"),
                        title,
                    )

    for child in value.values():
        if isinstance(child, (Mapping, list)):
            _collect_wenxin_citations(child, citations, seen_urls)


def _network_citations_from_payload(
    payload: str, parser: NetworkAnswerParser | None = None
) -> list[Citation]:
    """提取响应中紧邻 URL 的标题字段，并按 URL 去重。"""

    citations: list[Citation] = []
    seen_urls: set[str] = set()
    for raw_url, raw_title in _CITATION_PATTERN.findall(payload):
        url = _decode_json_string(raw_url).strip()
        title = _decode_json_string(raw_title).strip()
        _append_citation(citations, seen_urls, url, title)
    if parser is NetworkAnswerParser.DOUBAO_CONTENT_BLOCK_EVENT:
        for raw_event in _SSE_DATA_PATTERN.findall(payload):
            if raw_event == "[DONE]":
                continue
            try:
                event = json.loads(raw_event)
            except json.JSONDecodeError:
                continue
            _collect_doubao_citations(event, citations, seen_urls)
    elif parser is NetworkAnswerParser.YUANBAO_TEXT_EVENT:
        for raw_title, raw_url in _YUANBAO_BUBBLE_CITATION_PATTERN.findall(payload):
            _append_citation(
                citations,
                seen_urls,
                _decode_json_string(raw_url),
                _decode_json_string(raw_title),
            )
        for raw_event in _SSE_DATA_PATTERN.findall(payload):
            if raw_event == "[DONE]":
                continue
            try:
                event = json.loads(raw_event)
            except json.JSONDecodeError:
                continue
            _collect_yuanbao_citations(event, citations, seen_urls)
    elif parser is NetworkAnswerParser.WENXIN_MARKDOWN_EVENT:
        for raw_event in _SSE_DATA_PATTERN.findall(payload):
            if raw_event == "[DONE]":
                continue
            try:
                event = json.loads(raw_event)
            except json.JSONDecodeError:
                continue
            _collect_wenxin_citations(event, citations, seen_urls)
    return citations[:50]


@dataclass(frozen=True)
class NetworkAnswer:
    """已通过完整性校验、可交付给后端的最小网络结果。"""

    markdown: str
    citations: list[Citation]


class NetworkAnswerListener:
    """绑定单个 execution 页面的网络结果监听器。

    此深模块把 Playwright 回调、路由白名单、长度限制、原始正文清理和终态通知封装
    在一个极小接口内。调用者只能 `arm`、`wait_result`、`close`，无法读取任何原始
    响应、修改路由或让后端插入脚本。
    """

    def __init__(self, page: Page, rule: ProviderRule) -> None:
        if rule.network_answer is None:
            raise ProviderAutomationError("provider_error", "Provider network rule is unavailable")
        self._page = page
        self._policy = rule.network_answer
        self._armed = False
        self._closed = False
        self._result: NetworkAnswer | None = None
        # 元宝通常先返回 web_search 卡片、再返回正文；在正文尚未形成时暂存已清洗
        # 的引用，绝不暂存原始响应字节。该列表只存在于当前 execution 内存中。
        self._pending_citations: list[Citation] = []
        self._pending_citation_urls: set[str] = set()
        self._error_code: str | None = None
        self._completed = asyncio.Event()
        # 仅记录是否出现了本轮匹配聊天请求，不保存请求内容；用于提交后的短时确认。
        self._submission_observed = asyncio.Event()
        self._tasks: set[asyncio.Task[None]] = set()

        def on_response(response: Response) -> None:
            self._schedule(response)

        self._on_response = on_response
        page.on("response", on_response)

    async def arm(self) -> None:
        """在提交动作之前开启首个匹配聊天请求的接收。"""

        if self._closed:
            raise ProviderAutomationError("page_unavailable", "Network listener is already closed")
        self._armed = True

    def reset_submission_observed(self) -> None:
        """清除本次点击的观测标记；仅在尚未看到聊天请求时允许再次点击。"""

        if self._closed:
            raise ProviderAutomationError("page_unavailable", "Network listener is already closed")
        self._submission_observed.clear()

    def _schedule(self, response: Response) -> None:
        if not self._armed or self._closed:
            return
        # 元宝的引用卡片可能晚于正文响应到达；其他平台一旦形成结果即停止读取后续
        # 响应，避免把页面状态或第二轮会话内容混入结果。
        if self._result is not None and self._policy.parser is not NetworkAnswerParser.YUANBAO_TEXT_EVENT:
            return
        if not any(endpoint.matches(response.url, response.request.method) for endpoint in self._policy.endpoints):
            return
        self._submission_observed.set()
        task = asyncio.create_task(self._consume(response))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _consume(self, response: Response) -> None:
        """在内存上限内读取一个已匹配响应，并立即丢弃原始字节。"""

        try:
            if response.status < 200 or response.status >= 300:
                return
            body = await response.body()
            if len(body) > self._policy.max_response_bytes:
                self._error_code = "answer_incomplete"
                self._completed.set()
                return
            payload = body.decode("utf-8", errors="replace")
            answer = _network_answer_from_payload(payload, self._policy)
            citations = _network_citations_from_payload(payload, self._policy.parser)
            if not answer and self._result is None:
                if self._policy.parser is NetworkAnswerParser.YUANBAO_TEXT_EVENT:
                    for citation in citations:
                        if citation["url"] in self._pending_citation_urls:
                            continue
                        self._pending_citation_urls.add(citation["url"])
                        self._pending_citations.append(citation)
                return
            # HTTP 响应已完整交给 Playwright，且正文满足严格字段与长度校验，才允许
            # 产生最终结果。SSE 的 [DONE] 是额外终态证据；Connect 与部分平台不提供
            # 通用结束帧，因此以受限响应自然结束作为其唯一可审计的完成信号。
            if self._result is None:
                merged_citations = [*self._pending_citations, *citations]
                self._result = NetworkAnswer(markdown=answer, citations=merged_citations[:50])
                if self._policy.parser is NetworkAnswerParser.YUANBAO_TEXT_EVENT:
                    # 让后续搜索响应有机会补齐来源；期间仍只保留清洗后的结构化数据。
                    await asyncio.sleep(YUANBAO_CITATION_GRACE_SECONDS)
                self._completed.set()
            elif citations:
                merged = list(self._result.citations)
                seen_urls = {item["url"] for item in merged}
                for citation in citations:
                    if citation["url"] in seen_urls:
                        continue
                    seen_urls.add(citation["url"])
                    merged.append(citation)
                self._result = NetworkAnswer(markdown=self._result.markdown, citations=merged[:50])
        except Exception:
            # 不记录 URL、正文或异常字符串；这些内容可能含会话标识。失败只在任务级
            # 收敛为稳定错误码，由既有截图和人工认证流程处理。
            self._error_code = "answer_incomplete"
            self._completed.set()
        finally:
            # 明确断开原始字节引用，执行完成后监听器只保留已清洗的结构化结果。
            body = b"" if "body" in locals() else b""

    async def wait_result(self, timeout_seconds: float | None = None) -> NetworkAnswer:
        """等待网络终态；可传入总执行预算剩余时间，避免阶段超时叠加。"""

        effective_timeout = (
            self._policy.timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        if effective_timeout <= 0:
            raise ProviderAutomationError("answer_timeout", "Provider network answer timed out")
        deadline = time.monotonic() + effective_timeout
        while not self._completed.is_set() and time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                await asyncio.wait_for(self._completed.wait(), timeout=min(0.25, remaining))
            except TimeoutError:
                continue
        if not self._completed.is_set():
            raise ProviderAutomationError("answer_timeout", "Provider network answer timed out")
        if self._result is not None:
            return self._result
        raise ProviderAutomationError(
            self._error_code or "answer_incomplete",
            "Provider network answer was incomplete",
        )

    async def wait_submission_observed(self, timeout_seconds: float) -> None:
        """等待本轮提交对应的受限聊天请求出现，避免提交后无条件停顿。"""

        try:
            await asyncio.wait_for(
                self._submission_observed.wait(), timeout=timeout_seconds
            )
        except TimeoutError as exc:
            raise ProviderAutomationError(
                "submission_not_dispatched",
                "Provider chat request was not observed after submission",
            ) from exc

    async def close(self) -> None:
        """取消未完成的读取并移除页面回调，防止跨 execution 保留任何数据。"""

        if self._closed:
            return
        self._closed = True
        self._page.remove_listener("response", self._on_response)
        for task in tuple(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()


def extract_answer_text(
    rule: ProviderRule, raw_text: str, *, submitted_query: str | None = None
) -> str:
    """按平台静态提取规则从已定位节点得到可持久化回答正文。

    返回空字符串表示当前页面尚未出现可确认的助手回答，调用方会继续等待，绝不将问题、
    导航或输入框占位文本写入结果。规则不接受任何远端文本模式或运行时 selector。
    """

    if rule.answer_extraction is AnswerExtractionMode.DIRECT_TEXT:
        return raw_text.strip()
    if rule.answer_extraction is AnswerExtractionMode.DOUBAO_MAIN_CONVERSATION:
        boundary = _DOUBAO_REFERENCE_METADATA_PATTERN.search(raw_text)
        if boundary is not None:
            answer = raw_text[boundary.end() :]
        else:
            # 豆包新版有时不再渲染“搜索 N 个关键词，参考 N 篇资料”这段元数据，
            # 但仍把本轮用户消息和回答连续放在同一 main。仅在调用方提供本次已提交
            # 的精确问题时，才允许按最后一次出现的位置取其后的新增内容；问题缺失或
            # 页面未回显问题时，当前豆包的 ``main`` 本身就是本轮回答区域。此路径只在
            # 精确 completion 监听超时后启用，并且下方仍会剔除固定编辑器与完整工作台
            # 菜单；它不适用于其他平台，也不允许使用任意页面或侧栏作为兜底来源。
            if submitted_query:
                query_offset = raw_text.rfind(submitted_query)
                answer = (
                    raw_text[query_offset + len(submitted_query) :]
                    if query_offset >= 0
                    else raw_text
                )
            else:
                answer = raw_text
        composer = _DOUBAO_COMPOSER_BOUNDARY_PATTERN.search(answer)
        if composer is not None:
            answer = answer[: composer.start()]
        # v4 已不从 DOM 交付答案；仍保护迁移期诊断辅助函数，避免新版固定工作台菜单
        # 被误判成模型正文。
        workbench = _DOUBAO_WORKBENCH_MENU_PATTERN.search(answer)
        if workbench is not None:
            answer = answer[: workbench.start()]
        # 不完整菜单也说明 main 仍是首页工作台。与其把导航文本写入品牌监测，
        # 不如继续等待真实助手卡片或由任务按 answer_timeout 退款收敛。
        if _DOUBAO_WORKBENCH_SIGNATURE_PATTERN.search(answer):
            return ""
        return answer.strip()
    raise AssertionError(f"Unsupported answer extraction mode: {rule.answer_extraction}")


async def _wait_before_submit(policy: InteractionPacingPolicy) -> None:
    """提交前留出可审计的页面稳定窗口，不承担重试或规避验证职责。"""

    await asyncio.sleep(policy.submit_settle_seconds)


async def _submit_near_prompt(
    page: Page, prompt: Locator, policy: InteractionPacingPolicy
) -> None:
    """点击输入框同一行、右半区最靠右的可用按钮。

    该关系来自页面布局而非按钮在全局 DOM 中的顺序。豆包同时存在导航搜索和输入框发送
    两类 SVG 按钮，宽泛的 ``button:has(svg)`` 会错误命中搜索按钮；发送箭头则稳定地
    位于输入框右半区的最右端。候选集合固定为 button/role=button，且不接受后端参数。
    """

    prompt_box = await prompt.bounding_box()
    if prompt_box is None:
        raise ProviderAutomationError(
            "submission_not_dispatched", "Provider prompt has no visible bounds"
        )
    controls = page.locator("button, [role='button']")
    selected: Locator | None = None
    rightmost = float("-inf")
    # 上限避免页面意外渲染超量按钮时把正常执行变成无界扫描；当前目标页面远低于此值。
    for index in range(min(await controls.count(), 128)):
        candidate = controls.nth(index)
        try:
            if not await candidate.is_visible(timeout=500) or not await candidate.is_enabled(timeout=500):
                continue
            box = await candidate.bounding_box()
        except Exception:
            continue
        if box is None:
            continue
        is_same_row = (
            box["y"] + box["height"] >= prompt_box["y"] - 64
            and box["y"] <= prompt_box["y"] + prompt_box["height"] + 64
        )
        right = box["x"] + box["width"]
        is_right_half = box["x"] >= prompt_box["x"] + prompt_box["width"] / 2
        if is_same_row and is_right_half and right > rightmost:
            selected, rightmost = candidate, right
    if selected is None:
        raise ProviderAutomationError(
            "submission_not_dispatched", "Provider submit control is unavailable"
        )
    try:
        await _move_pointer_to_locator(page, selected, policy)
        await _wait_before_submit(policy)
        await selected.click(timeout=12_000, no_wait_after=True)
    except Exception as exc:
        if "intercepts pointer events" in str(exc):
            raise ProviderAutomationError(
                "manual_intervention_required", "A platform dialog is blocking submission"
            ) from exc
        raise ProviderAutomationError(
            "submission_not_dispatched", "Provider submit control is unavailable"
        ) from exc


async def _dismiss_non_auth_dialogs(page: Page, rule: ProviderRule) -> bool:
    """关闭规则明确声明的普通站内提示，并返回本次是否实际关闭了提示。"""

    if not rule.dismiss_selectors:
        return False

    # Kimi 的权益提示会在首屏渲染、编辑器聚焦或键入后异步淡入。只检查一次会在
    # 动画开始前漏掉它，使后续 Enter 被遮罩层截获。这里保留平台规则声明的有限窗口，
    # 每轮仍只查询规则白名单中的精确候选；没有任何模糊 selector、强制点击或认证操作。
    deadline = time.monotonic() + rule.dismiss_wait_seconds
    while True:
        selector_supported = False
        for selector in rule.dismiss_selectors:
            try:
                button = page.locator(selector).first
                selector_supported = True
                if await button.is_visible(timeout=500) and await button.is_enabled(timeout=500):
                    await _move_pointer_to_locator(page, button, rule.interaction_pacing)
                    await button.click(timeout=5_000, no_wait_after=True)
                    return True
            except Exception:
                # 提示层可能已经由平台关闭或正在重绘。继续同一静态白名单的短时检查，
                # 不能把任意页面异常误判为可以绕过认证的理由。
                continue
        # 测试伪页面或页面重建期间可能暂时不支持某个选择器；没有任何候选能被
        # 解析时直接结束本轮，避免在不具备白名单 DOM 的页面上空转等待窗口。
        if not selector_supported:
            return False
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.2)


async def _post_submit_option_counts(page: Page, selectors: tuple[str, ...]) -> dict[str, int]:
    """记录提交前每个精确模式选项的数量，防止点击历史会话中的旧选项。"""

    counts: dict[str, int] = {}
    for selector in selectors:
        try:
            counts[selector] = await page.locator(selector).count()
        except Exception:
            counts[selector] = 0
    return counts


async def _select_post_submit_mode(page: Page, rule: ProviderRule, baseline: dict[str, int]) -> None:
    """点击本轮新出现的静态模式选项，元宝默认选择快速回答。"""

    if not rule.post_submit_selectors:
        return
    deadline = time.monotonic() + rule.post_submit_wait_seconds
    while time.monotonic() < deadline:
        for selector in rule.post_submit_selectors:
            options = page.locator(selector)
            try:
                count = await options.count()
                if count <= baseline.get(selector, 0):
                    continue
                option = options.nth(count - 1)
                if not await option.is_visible(timeout=500) or not await option.is_enabled(timeout=500):
                    continue
                await _move_pointer_to_locator(page, option, rule.interaction_pacing)
                await option.click(timeout=5_000, no_wait_after=True)
                return
            except Exception:
                continue
        await asyncio.sleep(0.25)
    raise ProviderAutomationError("answer_timeout", "Provider answer mode was not available")


async def prepare(page: Page, rule: ProviderRule, query: str) -> None:
    """打开受审查入口并填写问题；本阶段绝不向平台提交。"""
    if not query:
        raise ProviderAutomationError("provider_error", "RPA prompt must not be empty")
    # 每次执行都从 provider 内置入口开始；profile 仅恢复登录状态，不承载任务 URL，
    # 以免旧会话、重定向参数或历史页面被误当作本次任务的提交目标。
    if rule.navigate_on_prepare:
        await page.goto(rule.entry_url, wait_until="domcontentloaded", timeout=45_000)
    if rule.interaction_pacing.entry_settle_seconds:
        # 豆包等 SPA 会在 DOMContentLoaded 后短暂保留即将卸载的 textarea。等待固定、
        # 可审计的短窗口后再选择输入控件，避免把过渡节点绑定到本次执行。
        await asyncio.sleep(rule.interaction_pacing.entry_settle_seconds)
    body = page.locator("body")
    try:
        text = (await body.inner_text(timeout=5_000)).casefold()
    except Exception:
        text = ""
    if any(marker in text for marker in ("登录", "log in", "sign in")):
        raise ProviderAutomationError("login_required", "Provider login is required")
    await _dismiss_non_auth_dialogs(page, rule)
    prompt = await _first_stable_prompt(page, rule)
    # 无论 textarea 还是 contenteditable，都经由键盘输入事件写入。这样 React、
    # ProseMirror 等编辑器能够维护内部状态；准备阶段不提交问题。
    try:
        await _type_in_chunks(prompt, query, rule.interaction_pacing)
    except Exception as exc:
        # Locator 在平台 React 重绘期间可能失效。这里不泄露原始 Playwright 消息，
        # 只将其收敛成协议稳定错误码，并让上层决定是否需要账号级恢复或积分退款。
        raise ProviderAutomationError(
            "page_unavailable", "Provider prompt could not accept input"
        ) from exc


async def submit(page: Page, rule: ProviderRule) -> None:
    """执行唯一提交动作；页面遮挡明确映射为等待人工处理。"""
    # 有些普通提示并非首屏立即出现，而是在编辑器获得焦点或输入后才由页面异步展示。
    # 提交前按同一份静态白名单再次检查，可以关闭 Kimi 的 "Got it" 权益提示，避免其
    # 截获发送动作；认证、验证码、订阅确认等控件仍不在白名单中，绝不在这里处理。
    await _dismiss_non_auth_dialogs(page, rule)
    post_submit_baseline = await _post_submit_option_counts(page, rule.post_submit_selectors)
    if rule.submission_method is SubmissionMethod.ENTER:
        # Enter 路径需要再次聚焦，但问题已在 prepare 阶段写入。这里只重选/聚焦控件，
        # 不执行任何补写或重复提交动作。
        prompt = await _first_stable_prompt(page, rule)
        await _wait_before_submit(rule.interaction_pacing)
        await prompt.press("Enter")
        # 已知普通提示可能在第一次 Enter 时才覆盖编辑器。只有明确声明“关闭后重试
        # 一次”的平台才允许再次按 Enter；该动作不改写草稿、不切换模型，也不会循环。
        if await _dismiss_non_auth_dialogs(page, rule):
            if rule.post_submit_dialog_strategy is PostSubmitDialogStrategy.DISMISS_AND_RETRY_ONCE:
                retry_prompt = await _first_stable_prompt(page, rule)
                await _wait_before_submit(rule.interaction_pacing)
                await retry_prompt.press("Enter")
                # 第二次仍有白名单提示，不能推断它只是普通遮罩。将其收敛为人工处理，
                # 既保留账号隔离，也保证问题最多只出现一次真实派发尝试。
                if not await _dismiss_non_auth_dialogs(page, rule):
                    return
            raise ProviderAutomationError(
                "manual_intervention_required",
                "Provider plan restriction requires manual account action",
            )
        await _select_post_submit_mode(page, rule, post_submit_baseline)
        return
    prompt = await _first_visible(page, rule.prompt_selectors)
    if rule.submit_near_prompt:
        # 几何关系仅适用于声明该规则的平台；缺失时明确报告“未派发”，绝不回退到
        # 全局 SVG 按钮或不受审查的页面元素。
        await _submit_near_prompt(page, prompt, rule.interaction_pacing)
        return
    # Enter 只为已验收的平台启用；其他平台必须点受审查的发送控件，防止换行输入被
    # 误判为提交。任何路径均只尝试一次，网络或页面异常后由后端状态机决定后续处理。
    for selector in rule.submit_selectors:
        button = page.locator(selector).first
        try:
            if await button.is_visible(timeout=500) and await button.is_enabled(timeout=500):
                await _move_pointer_to_locator(page, button, rule.interaction_pacing)
                await button.hover()
                await _wait_before_submit(rule.interaction_pacing)
                await button.click(timeout=12_000, no_wait_after=True)
                return
        except Exception as exc:
            if "intercepts pointer events" in str(exc):
                raise ProviderAutomationError("manual_intervention_required", "A platform dialog is blocking submission") from exc
    raise ProviderAutomationError("submission_not_dispatched", "Provider submit control is unavailable")


async def wait_result(
    page: Page,
    rule: ProviderRule,
    *,
    timeout_seconds: float | None = None,
    baseline_count: int = 0,
    submitted_query: str | None = None,
) -> tuple[str, list[Citation]]:
    """等候最后一条完整回答，并从其 DOM 提取公开引用链接。

    不能以固定轮数判断完成：不同平台的 SSE 片段间隔不同，网络检索模式尤其可能长时间
    无文本变化。每轮重新定位最后一条回答，并由平台策略要求足够长的静默窗口；DeepSeek
    额外要求其停止生成控件消失，防止保存首个短段落。
    """

    # 形参只服务于本地伪页面测试；生产调用不传入，因此超时始终来自 ProviderRule。
    effective_timeout = (
        rule.answer_wait.timeout_seconds if timeout_seconds is None else timeout_seconds
    )
    if effective_timeout <= 0:
        raise ValueError("Answer timeout must be positive")
    await _last_visible_after_baseline(page, rule, baseline_count, int(effective_timeout * 1_000))
    previous = ""
    quiet_since: float | None = None
    deadline = time.monotonic() + effective_timeout
    while time.monotonic() < deadline:
        answer = await _last_visible_after_baseline(page, rule, baseline_count, timeout_ms=3_000)
        text = extract_answer_text(
            rule,
            await answer.inner_text(timeout=5_000),
            submitted_query=submitted_query,
        )
        now = time.monotonic()
        if not text:
            previous = ""
            quiet_since = None
        elif text != previous:
            previous = text
            quiet_since = now
        elif (
            quiet_since is not None
            and rule.answer_wait.accepts(text)
            and now - quiet_since >= rule.answer_wait.quiet_seconds
            and not await _generation_is_active(page, rule.answer_wait.generation_active_selectors)
        ):
            # 仅在已定位的最终回答节点读取链接；不扫描整页、不读取请求头或 Cookie。
            # 标题优先取正文，再降级到标题和无障碍标签，兼容 DeepSeek 将引文标题放入
            # aria-label 的页面版本；仍只回传公开 HTTP(S) 链接及最小展示标题。
            raw = await answer.evaluate(
                """node => [...node.querySelectorAll('a[href]')].map(link => ({url: new URL(link.getAttribute('href'), document.baseURI).href, title: (link.innerText || link.getAttribute('title') || link.getAttribute('aria-label') || '').trim()})).filter(item => /^https?:/.test(item.url) && item.title)"""
            )
            citations: list[Citation] = []
            if isinstance(raw, list):
                for item in raw:
                    if not isinstance(item, dict):
                        continue
                    url, title = item.get("url"), item.get("title")
                    if isinstance(url, str) and isinstance(title, str):
                        citations.append({"url": url, "title": title})
            return text, citations[:50]
        await asyncio.sleep(0.5)
    raise ProviderAutomationError("answer_timeout", "Provider answer did not become stable")


async def keepalive(
    page: Page, rule: ProviderRule
) -> Literal["ok", "login_required", "verification_required", "uncertain"]:
    """只读确认账号能否获得可编辑输入控件。"""
    await page.goto(rule.entry_url, wait_until="domcontentloaded", timeout=45_000)
    try:
        prompt = await _first_visible(page, rule.prompt_selectors, 15_000)
        return "ok" if await prompt.is_editable() else "uncertain"
    except ProviderAutomationError:
        text = (await page.locator("body").inner_text(timeout=5_000)).casefold()
        return "login_required" if any(marker in text for marker in ("登录", "log in", "sign in")) else "uncertain"
