"""与 AstrBot 其它插件的兼容层：事件钩子 + 工具集。

qq-bridge 的原始设计是「插件独占本轮」：自己调 ``context.tool_loop_agent()``，
模型文本只当思考，发言必须走 ``qq_send_message`` 等工具，最后 ``event.stop_event()``
阻止 AstrBot 默认流程再回一次。这个设计保住了「AI 自主决定说不说话」的语义，
但也把 AstrBot 的插件生态整条切断了：

=========================  ======================================================
AstrBot 的标准扩展点        被切断后的后果
=========================  ======================================================
``on_llm_request``         其它插件（好感度、记忆、人设增强……）无法往本轮
                           system_prompt / extra_user_content_parts 注入任何东西；
``on_llm_response``        其它插件看不到模型这一轮「说了什么」，标签解析、
                           统计类逻辑全部失效；
``on_decorating_result``   发送前改写消息链的钩子不会执行（控制标签不会被摘掉、
                           附加内容不会出现），因为发言走的是 ``event.send()`` 裸发送；
工具集                     ``_tool_set()`` 只挂本插件自己的 qq_* 工具，
                           其它插件注册的 llm_tool 模型根本看不见；
事件传播                   ``event.stop_event()`` 让同一事件里后续插件的 handler
                           和后续管道阶段（含 on_decorating_result）全部跳过。
=========================  ======================================================

本模块只做一件事：把这几条链路重新接回 AstrBot 的标准实现，不改变 bridge 自己的
「文本=思考、发言=工具」语义。所有调用都兜底——某个插件的钩子写崩了，不应该把整轮
对话带崩。
"""

from __future__ import annotations

import contextlib
from typing import Any

from astrbot.api import logger

# 钩子分发与事件类型都是 AstrBot 内部模块；拿不到就整体降级为「不做兼容」，
# 不能让插件因为版本差异直接 import 失败。
try:  # pragma: no cover - 取决于 AstrBot 版本
    from astrbot.core.pipeline.context_utils import call_event_hook

    from astrbot.core.star.star_handler import EventType
except Exception:  # pragma: no cover
    call_event_hook = None  # type: ignore[assignment]
    EventType = None  # type: ignore[assignment]


try:  # pragma: no cover
    from astrbot.core.agent.hooks import BaseAgentRunHooks
except Exception:  # pragma: no cover

    class BaseAgentRunHooks:  # type: ignore[no-redef]
        """最小占位：老版本 AstrBot 没有 agent hooks 时也能构造。"""

        async def on_agent_begin(self, run_context: Any) -> None: ...

        async def on_tool_start(self, run_context: Any, tool: Any, tool_args: Any) -> None: ...

        async def on_tool_end(
            self, run_context: Any, tool: Any, tool_args: Any, tool_result: Any
        ) -> None: ...

        async def on_agent_done(self, run_context: Any, llm_response: Any) -> None: ...


HOOKS_AVAILABLE = call_event_hook is not None and EventType is not None


# ══════════════════════════════════════════════════════════════════════════════
#  事件钩子
# ══════════════════════════════════════════════════════════════════════════════
async def _safe_hook(event: Any, hook_type: Any, *args: Any) -> bool:
    """触发一个事件钩子；任何异常都吞掉并记日志。

    返回 True 表示有插件在处理过程中调用了 ``event.stop_event()``——
    这在 AstrBot 里就是「这条消息到此为止」的意思（例如好感度插件的冷暴力拦截）。
    """
    if not HOOKS_AVAILABLE or event is None or hook_type is None:
        return False
    try:
        return bool(await call_event_hook(event, hook_type, *args))
    except Exception as exc:  # 单个插件写崩不能影响本轮
        logger.warning(f"[qq-bridge] 触发钩子 {getattr(hook_type, 'name', hook_type)} 失败：{exc}")
        return False


async def fire_llm_request(event: Any, req: Any) -> bool:
    """把本轮请求交给其它插件改（系统提示词、动态数据、拦截）。

    对应 AstrBot 管道里的 ``OnLLMRequestEvent``。插件可以：

    * 往 ``req.system_prompt`` 追加内容（好感度插件的评分规则就走这里）；
    * 往 ``req.extra_user_content_parts`` 追加临时内容块（当前好感度快照）；
    * 调用 ``event.stop_event()`` 直接拦下这条消息（冷暴力、黑名单）。

    返回 True 表示这条消息被某个插件拦下了，本轮不该再调模型。
    """
    return await _safe_hook(event, EventType.OnLLMRequestEvent if EventType else None, req)


async def fire_llm_response(event: Any, text: str) -> None:
    """把「模型这一轮真正说出去的话」交给其它插件解析。

    bridge 里模型的文本输出只是思考，真正说出去的是 ``qq_send_message`` 的参数。
    所以这里用一个合成的 ``LLMResponse`` 承载**实际要发出去的文本**，让
    ``on_llm_response`` 钩子看到的就是群友将看到的内容——好感度插件正是靠这一步
    从回复末行解析 ``[好感度 上升：3]`` 这类标签。
    """
    body = str(text or "")
    if not body.strip() or event is None or not HOOKS_AVAILABLE:
        return
    try:
        from astrbot.core.provider.entities import LLMResponse
    except ImportError:  # pragma: no cover
        try:
            from astrbot.api.provider import LLMResponse  # type: ignore[no-redef]
        except ImportError:
            return
    try:
        resp = LLMResponse(role="assistant", completion_text=body)
    except Exception as exc:  # pragma: no cover
        logger.debug(f"[qq-bridge] 构造 LLMResponse 失败：{exc}")
        return
    await _safe_hook(event, EventType.OnLLMResponseEvent if EventType else None, resp)


async def decorate_outgoing(event: Any, chain: list) -> list:
    """让待发送的消息链过一遍 ``on_decorating_result``，返回改写后的链。

    AstrBot 的发送前钩子是在 ``ResultDecorateStage`` 里触发的，而 bridge 直接
    ``event.send()`` 绕过了整条管道。这里手动走一次同样的流程：把链塞进
    ``event.get_result()`` → 触发钩子 → 取回可能被改写过的链。

    插件在这一步可以做的事：摘掉控制标签（好感度）、加签名/后缀、转成图片、
    甚至清空链来阻止发送。返回值是**新的组件列表**，调用方负责真正发送。
    """
    components = list(chain or [])
    if event is None or not HOOKS_AVAILABLE:
        return components
    previous = event.get_result()
    try:
        event.set_result(event.chain_result(components))
        await _safe_hook(event, EventType.OnDecoratingResultEvent if EventType else None)
        result = event.get_result()
        decorated = list(result.chain) if result is not None and result.chain else []
        return decorated
    except Exception as exc:  # pragma: no cover
        logger.warning(f"[qq-bridge] 发送前装饰失败，按原样发送：{exc}")
        return components
    finally:
        # 还原成调用前的状态：这条结果只是借来跑钩子的，不能留给后面的管道阶段，
        # 否则 RespondStage 会把它再发一遍。
        try:
            if previous is None:
                event.clear_result()
            else:
                event.set_result(previous)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
#  Agent 循环钩子
# ══════════════════════════════════════════════════════════════════════════════
def _event_of(run_context: Any) -> Any:
    return getattr(getattr(run_context, "context", None), "event", None)


class BridgeAgentHooks(BaseAgentRunHooks):
    """把 Agent 循环的阶段转成 AstrBot 事件钩子。

    和内置 ``MainAgentHooks`` 的唯一区别：**不在这里触发 on_llm_response**。
    bridge 里模型的文本输出只是「思考」，说出去的话是 ``qq_send_message`` 的参数，
    所以 ``on_llm_response`` 由 :func:`fire_llm_response` 用真正发出去的文本触发，
    这样插件解析到的才是模型实际说的话，而不是一段内心独白。
    """

    async def on_agent_begin(self, run_context: Any) -> None:
        event = _event_of(run_context)
        if event is None:
            return
        await _safe_hook(
            event, EventType.OnAgentBeginEvent if EventType else None, run_context
        )

    async def on_tool_start(self, run_context: Any, tool: Any, tool_args: Any) -> None:
        event = _event_of(run_context)
        if event is None:
            return
        await _safe_hook(
            event, EventType.OnUsingLLMToolEvent if EventType else None, tool, tool_args
        )

    async def on_tool_end(
        self, run_context: Any, tool: Any, tool_args: Any, tool_result: Any
    ) -> None:
        event = _event_of(run_context)
        if event is None:
            return
        # 与 AstrBot 内置实现一致：工具执行完清掉可能残留的结果，避免被当成回复发出去
        with contextlib.suppress(Exception):
            event.clear_result()
        await _safe_hook(
            event,
            EventType.OnLLMToolRespondEvent if EventType else None,
            tool,
            tool_args,
            tool_result,
        )

    async def on_agent_done(self, run_context: Any, llm_response: Any) -> None:
        event = _event_of(run_context)
        if event is None:
            return
        if llm_response is not None and getattr(llm_response, "reasoning_content", None):
            with contextlib.suppress(Exception):
                event.set_extra("_llm_reasoning_content", llm_response.reasoning_content)
        await _safe_hook(
            event,
            EventType.OnAgentDoneEvent if EventType else None,
            run_context,
            llm_response,
        )


# ══════════════════════════════════════════════════════════════════════════════
#  工具集
# ══════════════════════════════════════════════════════════════════════════════
def _allowed_by_session(tool: Any, event: Any) -> bool:
    """按「本会话启用的插件」过滤工具，和 AstrBot 默认 agent 的口径一致。

    没有归属信息（MCP 工具、内置工具）的一律保留，避免误伤。
    """
    names = getattr(event, "plugins_name", None) if event is not None else None
    if names is None:
        return True
    module_path = getattr(tool, "handler_module_path", None)
    if not module_path:
        return True
    try:
        from astrbot.core.star.star import star_map

        plugin = star_map.get(module_path)
    except Exception:
        return True
    if not plugin:
        return True
    return getattr(plugin, "name", None) in names or bool(getattr(plugin, "reserved", False))


def collect_tool_set(
    context: Any,
    event: Any,
    own_names: tuple[str, ...] | list[str],
    mode: str = "all",
    exclude: list[str] | None = None,
) -> Any:
    """组本轮的工具集。

    ``mode="own"``：只挂本插件自己的工具（旧行为）；
    ``mode="all"``：挂 AstrBot 里所有已注册、已启用的插件工具 + MCP 工具。

    默认 ``all`` 是兼容性的关键：别的插件把能力注册成 llm_tool（记忆、搜索、
    画图、日程……）时，模型必须看得见才能用；只挂 qq_* 等于把这些插件变成哑巴。
    """
    try:
        from astrbot.core.agent.tool import ToolSet
    except ImportError:  # pragma: no cover
        return None
    try:
        manager = context.get_llm_tool_manager()
    except Exception:
        return None

    own = [str(n) for n in (own_names or [])]
    blocked = {str(x).strip() for x in (exclude or []) if str(x).strip()}
    want_all = str(mode or "all").strip().lower() != "own"

    tools: list = []
    if want_all:
        try:
            tools = [
                tool
                for tool in manager.get_full_tool_set().tools
                if getattr(tool, "name", "") not in blocked
                and _allowed_by_session(tool, event)
            ]
        except Exception as exc:
            logger.warning(f"[qq-bridge] 取全量工具集失败，退回只挂本插件工具：{exc}")
            tools = []

    # 保底：本插件自己的工具必须都在（哪怕全量工具集取失败，或被人误加进 exclude）
    present = {getattr(t, "name", "") for t in tools}
    for name in own:
        if name in present or name in blocked:
            continue
        try:
            tool = manager.get_func(name)
        except Exception:
            tool = None
        if tool is not None:
            tools.append(tool)
            present.add(name)

    if not tools:
        return None
    try:
        return ToolSet(tools=tools)
    except Exception:  # pragma: no cover
        return None
