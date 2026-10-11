"""把每轮 token 用量交给 AstrBot 自带的统计。

原项目 qq-bridge 自带一套 token 账本（src/token-ledger.js，含分层定价与金额换算）。
移植到 AstrBot 之后不再自建这套东西，改为写进 AstrBot 自己已有的两处用量数据：

  1) ``provider_stats`` 表 —— WebUI「数据与日志 → 统计」读的就是它
  2) 对话级 ``Conversation.token_usage`` —— conversation_manager 可读写

下面三条是 AstrBot 源码里的事实，不是猜测，它们决定了本模块为什么长这样：

  · 统计页接口（``/api/stats/provider-tokens``）只查 ``agent_type == "internal"``
    的记录。插件写进去时若用别的取值，数据落了库也不会出现在页面上，所以这里用
    ``STATS_AGENT_TYPE = "internal"``。
  · AstrBot 的用量模型只有 token（``input_other`` / ``input_cached`` / ``output``），
    没有价格字段。所以插件不再提供「成本」——那个数字 AstrBot 算不出来，硬报就是编的。
  · 插件的回合走 ``context.tool_loop_agent()``，AstrBot 不会替它记账（自动记账只发生在
    它自己的 internal agent 路径里），所以要插件自己写。多步工具调用的回合，公开接口
    只暴露最后一步的 usage，因此这里记录的是那一步的用量。
  · **即使 usage 为空也要写一行**（token 记 0）。AstrBot 自己就是这么做的，面板上的
    「调用次数」靠的就是这些行；早期版本 token 为 0 时直接跳过，结果是只要 provider
    不回报 usage，插件的流量在面板上就完全不存在。
  · 数据库句柄优先取 ``astrbot.core.db_helper``（AstrBot 写统计用的那个单例），
    这样写入与面板读取必然是同一张表。

本模块全程「尽力而为」：AstrBot 内部结构随版本有变化时，记账失败绝不影响发消息这类主流程，
但会把失败原因记进日志并回报给调用方（``record_turn`` 返回写入结果），方便排查「面板上
为什么没有」。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("astrbot_plugin_qq_bridge")

# 统计页只认这个取值，改掉就等于把数据写进一个没人看的角落
STATS_AGENT_TYPE = "internal"


def usage_of(resp: Any) -> dict:
    """从 LLMResponse 里取出 token 用量，字段缺失一律按 0，绝不抛错。

    返回 AstrBot 的 TokenUsage 口径：``{input_other, input_cached, output}``。
    """
    usage = getattr(resp, "usage", None)
    if usage is None:
        return {"input_other": 0, "input_cached": 0, "output": 0}

    def pick(name: str) -> int:
        value = getattr(usage, name, None)
        return int(value) if isinstance(value, (int, float)) else 0

    other = pick("input_other")
    cached = pick("input_cached")
    output = pick("output")
    if not (other or cached or output):
        # 兼容旧字段名：input 是「未命中 + 命中」的合计
        total_input = pick("input") or pick("prompt_tokens")
        output = pick("completion_tokens") or pick("output_tokens")
        other = max(0, total_input - cached)
    return {"input_other": other, "input_cached": cached, "output": output}


def total_of(buckets: dict) -> int:
    """一轮用量的 token 合计。"""
    return sum(int(buckets.get(k) or 0) for k in ("input_other", "input_cached", "output"))


def resolve_db(context: Any = None, fallback: Any = None) -> Any:
    """拿 AstrBot 的数据库句柄。

    优先用 ``astrbot.core.db_helper``——AstrBot 自己在内部 agent 里写统计用的就是它，
    统计页读的也是它，用它能保证「写进去」和「面板读出来」是同一张表。
    """
    try:
        from astrbot.core import db_helper  # type: ignore

        if db_helper is not None:
            return db_helper
    except Exception as exc:
        logger.debug(f"[qq-bridge] 取 astrbot.core.db_helper 失败：{exc}")
    if fallback is not None:
        return fallback
    try:
        return context.get_db()
    except Exception as exc:
        logger.debug(f"[qq-bridge] 取 context.get_db() 失败：{exc}")
        return None


async def record_turn(
    *,
    db: Any,
    conv_manager: Any = None,
    umo: str,
    provider_id: str = "",
    provider_model: str = "",
    conversation_id: str = "",
    resp: Any,
    started: float = 0.0,
    ended: float = 0.0,
    status: str = "completed",
) -> dict:
    """把一轮的用量写进 AstrBot 自带的统计。

    返回 ``{"written": bool, "total": int, "error": str}``。

    **即使 provider 没回报 token（usage 为空）也照样写一行。** AstrBot 自己在内部 agent 里
    就是这么做的——统计页的「调用次数」靠的就是这些行。上一版在 total 为 0 时直接跳过，
    结果只要模型端点不回 usage，插件的流量在面板上就完全不存在。

    ``resp`` 允许为 None（模型调用直接失败时），此时 token 全记 0、``status`` 传 ``"error"``——
    AstrBot 自己那条路也是这么处理的，这样面板上至少能看到「这个插件在跑、但调用失败了」。
    """
    buckets = usage_of(resp)
    total = total_of(buckets)
    result: dict = {"written": False, "total": total, "error": ""}

    if conv_manager is not None and not conversation_id:
        try:
            conversation_id = await conv_manager.get_curr_conversation_id(umo) or ""
        except Exception as exc:  # 拿不到就按没有对话处理
            logger.debug(f"[qq-bridge] 取当前对话 ID 失败：{exc}")
            conversation_id = ""

    if db is None:
        result["error"] = "拿不到 AstrBot 数据库句柄"
        logger.warning(f"[qq-bridge] 用量没记上：{result['error']}")
        return result

    # 1) 对话级计数：AstrBot 自己在 internal agent 里也是这么写的
    if conv_manager is not None and conversation_id:
        try:
            await conv_manager.update_conversation(umo, conversation_id, token_usage=total)
        except Exception as exc:
            logger.debug(f"[qq-bridge] 更新对话用量失败：{exc}")

    # 2) 明细表：统计页读的就是它
    try:
        await db.insert_provider_stat(
            umo=umo,
            conversation_id=conversation_id or None,
            provider_id=provider_id or "unknown",
            provider_model=provider_model or None,
            status=status or "completed",
            stats={
                "token_usage": buckets,
                "start_time": float(started or 0.0),
                "end_time": float(ended or 0.0),
                "time_to_first_token": 0.0,
            },
            agent_type=STATS_AGENT_TYPE,
        )
        result["written"] = True
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        logger.warning(f"[qq-bridge] 写入用量统计失败：{result['error']}", exc_info=True)

    return result


async def panel_usage(db: Any, days: int = 1) -> dict | None:
    """按「数据与日志 → 统计」的口径读回来，确认插件的流量真的进了面板。

    用的是统计页同一套过滤：``agent_type == "internal"`` 且 ``created_at`` 在时间范围内。
    """
    if db is None:
        return None
    try:
        from datetime import datetime, timedelta, timezone

        from sqlmodel import func, select

        from astrbot.core.db.po import ProviderStat
    except Exception as exc:
        logger.debug(f"[qq-bridge] 统计读取模块不可用：{exc}")
        return None

    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))
    try:
        async with db.get_db() as session:
            stmt = select(
                func.count(ProviderStat.id),
                func.coalesce(func.sum(ProviderStat.token_input_other), 0),
                func.coalesce(func.sum(ProviderStat.token_input_cached), 0),
                func.coalesce(func.sum(ProviderStat.token_output), 0),
            ).where(
                ProviderStat.agent_type == STATS_AGENT_TYPE,
                ProviderStat.created_at >= cutoff,
            )
            row = (await session.execute(stmt)).one()
    except Exception as exc:
        logger.debug(f"[qq-bridge] 按统计页口径读取失败：{exc}")
        return None

    other = int(row[1] or 0)
    cached = int(row[2] or 0)
    output = int(row[3] or 0)
    return {
        "days": max(1, int(days)),
        "calls": int(row[0] or 0),
        "inputOther": other,
        "inputCached": cached,
        "output": output,
        "total": other + cached + output,
    }


async def read_session_usage(db: Any, umo: str) -> dict | None:
    """从 AstrBot 的 provider_stats 里读本会话累计用量；读不到返回 None。

    AstrBot 没有对外暴露「查用量」的方法，所以这里用它的数据库会话读同一张表。
    任何一步失败都返回 None，由调用方降级成「请到 WebUI 统计页看」。
    """
    if db is None or not umo:
        return None
    try:
        from sqlmodel import func, select

        from astrbot.core.db.po import ProviderStat
    except Exception as exc:
        logger.debug(f"[qq-bridge] 用量统计模块不可用：{exc}")
        return None

    try:
        async with db.get_db() as session:
            stmt = select(
                func.count(ProviderStat.id),
                func.coalesce(func.sum(ProviderStat.token_input_other), 0),
                func.coalesce(func.sum(ProviderStat.token_input_cached), 0),
                func.coalesce(func.sum(ProviderStat.token_output), 0),
            ).where(ProviderStat.umo == umo)
            row = (await session.execute(stmt)).one()
    except Exception as exc:
        logger.debug(f"[qq-bridge] 读取用量统计失败：{exc}")
        return None

    calls = int(row[0] or 0)
    other = int(row[1] or 0)
    cached = int(row[2] or 0)
    output = int(row[3] or 0)
    return {
        "calls": calls,
        "inputOther": other,
        "inputCached": cached,
        "output": output,
        "total": other + cached + output,
    }


def format_write_state(write_state: dict | None) -> str:
    """写入状态：确认插件到底有没有把用量写进 AstrBot 的表。"""
    state = write_state or {}
    ok = int(state.get("ok") or 0)
    fail = int(state.get("fail") or 0)
    if not ok and not fail:
        return "用量写入：本次启动后还没有跑过 Agent 回合（没产生用量）"
    line = f"用量写入：成功 {ok} 次"
    if fail:
        line += f"｜失败 {fail} 次（{state.get('lastError') or '未知原因'}）"
    return line


def format_session_usage(
    info: dict | None,
    label: str = "",
    panel: dict | None = None,
    write_state: dict | None = None,
) -> str:
    """把用量渲染成一段文字。

    info  —— 本会话在 provider_stats 里的累计（可能为 None：读不到）
    panel —— 按统计页口径（agent_type=="internal" + 时间范围）读回来的最近用量，
             它就是「数据与日志 → 统计」里能看到的那部分
    """
    scope = f"会话 {label}" if label else "本会话"
    lines: list[str] = []
    if info is None:
        lines.append(f"【{scope}用量】没读到 AstrBot 的统计记录。")
    else:
        lines += [
            f"【{scope}用量｜AstrBot 统计口径】",
            f"调用次数：{info['calls']}",
            f"输入（未命中缓存）：{info['inputOther']:,}",
            f"输入（缓存命中）：{info['inputCached']:,}",
            f"输出：{info['output']:,}",
            f"合计：{info['total']:,} tokens",
        ]

    lines.append("")
    if panel is None:
        lines.append("【数据与日志 → 统计】读不到面板口径的数据（数据库不可用）。")
    else:
        lines.append(
            f"【数据与日志 → 统计｜最近 {panel['days']} 天】"
            f"调用 {panel['calls']} 次 / {panel['total']:,} tokens"
            f"（未命中 {panel['inputOther']:,}｜缓存命中 {panel['inputCached']:,}｜输出 {panel['output']:,}）"
        )
        if panel["calls"]:
            lines.append("面板上能看到这些记录，说明写入生效。")
        else:
            lines.append("面板口径下还没有记录——先发一条能唤醒机器人的消息，再看这里。")

    lines.append(format_write_state(write_state))
    lines.append("明细（分时段、分模型）在 WebUI「数据与日志 → 统计」。")
    return "\n".join(lines)
