"""AstrBot 版 qq-bridge：把 QQ 群聊仿真能力搬到 AstrBot 上。

这个插件是 qq-bridge（Node.js + DSH + SnowLuma）的 AstrBot 移植版。原来那条
「桥接 ↔ DSH 会话 ↔ MCP 工具」的链路，在这里换成 AstrBot 自己的三段：

    QQ 连接      SnowLuma / OneBot        →  AstrBot 的 aiocqhttp 适配器
    大模型        DSH 会话（预设提示词）    →  AstrBot 的 Provider（context.tool_loop_agent）
    工具面        MCP server（qq_* 工具）  →  本插件的 @filter.llm_tool
    控制台        桥接自带的 3100 网页      →  AstrBot WebUI 的插件配置 + /qq 指令
    人设注入      roles/*.md              →  原样保留（本插件 roles/ 目录）
    黑话/表情/语音/主动消息                 →  原样保留（本插件的 qbridge 包）
    Token 账本    token-ledger.js（自建）   →  改用 AstrBot 自带的用量统计
                                                 （qbridge/usage.py，写入 provider_stats）
    管理员看记忆   原项目无                   →  自带一个小页面（qbridge/memview.py，默认 8899）

行为设计的关键取舍：**由插件独占本轮对话**。收到消息后插件自己调
``context.tool_loop_agent()``，模型的文本输出按原项目约定只算「思考」不直接发出，
发言必须走 qq_send_message 等工具；最后 event.stop_event() 阻止 AstrBot 默认的
对话流程再回一次，避免重复发言。这样才保留了原项目「AI 自主决定说不说话」的语义。
"""

from __future__ import annotations

import asyncio
import contextvars
import random
import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Image, Plain, Reply
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools

try:  # AstrBot 以包方式加载插件（data.plugins.<name>.main），相对导入可用
    from .qbridge.inbox import Inbox, MsgRecord, looks_like_question, match_names
    from .qbridge.memview import MemoryView, is_loopback
    from .qbridge.prompt import (
        MODE_HINTS,
        build_system_prompt,
        build_turn_prompt,
        list_role_cards,
        load_role_card,
    )
    from .qbridge.slang import STATUS_CANDIDATE, STATUS_CONFIRMED, SlangStore
    from .qbridge.stickers import StickerStore
    from .qbridge.store import JsonStore, ensure_dir, now_iso
    from .qbridge.textutil import clean_model_text, split_for_qq, truncate
    from .qbridge.usage import (
        format_session_usage,
        panel_usage,
        read_session_usage,
        record_turn,
        resolve_db,
    )
    from .qbridge.voice import VoiceLimiter, check_voice_limits, list_voices, resolve_voice
except ImportError:  # 兜底：万一被当作顶层模块加载
    from qbridge.inbox import Inbox, MsgRecord, looks_like_question, match_names
    from qbridge.memview import MemoryView, is_loopback
    from qbridge.prompt import (
        MODE_HINTS,
        build_system_prompt,
        build_turn_prompt,
        list_role_cards,
        load_role_card,
    )
    from qbridge.slang import STATUS_CANDIDATE, STATUS_CONFIRMED, SlangStore
    from qbridge.stickers import StickerStore
    from qbridge.store import JsonStore, ensure_dir, now_iso
    from qbridge.textutil import clean_model_text, split_for_qq, truncate
    from qbridge.usage import (
        format_session_usage,
        panel_usage,
        read_session_usage,
        record_turn,
        resolve_db,
    )
    from qbridge.voice import VoiceLimiter, check_voice_limits, list_voices, resolve_voice

PLUGIN_NAME = "astrbot_plugin_qq_bridge"

# 主动回合（冷场自己开口）没有 AstrMessageEvent，但工具仍然需要知道该往哪个会话发消息。
# 用 ContextVar 传递：每个 asyncio 任务各持一份，多会话并发跑回合时不会串。
_CURRENT_UMO: contextvars.ContextVar[str] = contextvars.ContextVar("qq_bridge_umo", default="")

# 本插件注册的全部工具名（按顺序；ToolSet 按名字从工具管理器里取）
TOOL_NAMES = (
    "qq_send_message",
    "qq_reply",
    "qq_dive",
    "qq_wait_for_messages",
    "qq_get_recent_messages",
    "qq_get_active_members",
    "qq_get_message_images",
    "qq_get_forward_msg",
    "qq_send_poke",
    "qq_list_stickers",
    "qq_send_sticker",
    "qq_sticker_note",
    "qq_collect_sticker",
    "qq_list_voices",
    "qq_send_voice",
    "qq_slang_query",
    "qq_slang_submit",
    "qq_memory_append",
    "qq_memory_query",
    "qq_token_usage",
)


class QQBridgePlugin(Star):
    """群聊仿真 Agent：人设 + 黑话 + 表情 + 语音 + 主动消息；用量走 AstrBot 统计；记忆带管理员查看页。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.data_dir = ensure_dir(StarTools.get_data_dir(PLUGIN_NAME))
        self.role_dir = Path(__file__).resolve().parent / "roles"

        # ── 持久化 ────────────────────────────────────────────────────────────
        self.slang = SlangStore(self.data_dir / "slang.json")
        self.stickers = StickerStore(self.data_dir / "stickers.json")
        self.memory = JsonStore(self.data_dir / "memory.json", default={})
        self.runtime = JsonStore(self.data_dir / "runtime.json", default={})

        # 记忆查看页（管理员）：默认只监听回环地址，端口 8899
        self.memview = MemoryView(
            host=str(self._cfg("memview.host", "127.0.0.1") or "127.0.0.1"),
            port=int(self._cfg("memview.port", 8899) or 8899),
            token=str(self._cfg("memview.token", "") or ""),
            allow_delete=bool(self._cfg("memview.allowDelete", False)),
            page_path=Path(__file__).resolve().parent / "webui" / "memory.html",
            snapshot_fn=self._memory_snapshot,
            delete_fn=self._memory_delete,
            clear_fn=self._memory_clear,
        )

        # ── 运行时 ────────────────────────────────────────────────────────────
        self.inbox = Inbox(replay_window_sec=self._cfg("context.replayWindowSec", 30))
        self.voice_limiter = VoiceLimiter(
            max_per_minute=int(self._cfg("voice.maxPerMinute", 2) or 0),
            max_per_hour=int(self._cfg("voice.maxPerHour", 10) or 0),
        )
        self._turn_locks: dict[str, asyncio.Lock] = {}
        self._sent_this_turn: dict[str, bool] = {}
        self._dived_this_turn: dict[str, bool] = {}
        self._after_dive_blocks = 0  # 收尾后又调发言工具的次数（/qq 状态 可自查）
        self._pending_wake: dict[str, tuple] = {}  # 上一轮还在跑时进来的消息，等回合结束补跑
        # 每个会话「模型已经看到过」的最大消息序号。用于两件事：① 判断排队的那条要不要补跑
        # （上一轮已经看到就别补，否则同一句会被回两次）；② 快照里标出哪些是【新】消息。
        self._turn_seen_seq: dict[str, int] = {}
        self._queue_stats = {"queued": 0, "replayed": 0, "skipped": 0}
        self._usage_writes: dict = {"ok": 0, "fail": 0, "lastError": ""}
        self._warned_self_id = False
        self._tasks: list[asyncio.Task] = []
        self._started = False
        self._slang_busy = False

        logger.info(
            f"[qq-bridge] 已加载｜数据目录 {self.data_dir}｜人设卡 {len(list_role_cards(self.role_dir))} 张"
        )

    # ══════════════════════════════════════════════════════════════════════════
    #  配置读取
    # ══════════════════════════════════════════════════════════════════════════
    def _cfg(self, path: str, default=None):
        """按 "a.b.c" 读嵌套配置；任何一层缺失都返回 default。"""
        node = self.config
        for part in path.split("."):
            if isinstance(node, dict):
                if part not in node:
                    return default
                node = node[part]
            else:
                try:
                    node = node[part]
                except (KeyError, IndexError, TypeError):
                    return default
        return default if node is None else node

    @staticmethod
    def _as_list(value) -> list[str]:
        if isinstance(value, (list, tuple)):
            return [str(v).strip() for v in value if str(v).strip()]
        if isinstance(value, str):
            return [p.strip() for p in value.replace("，", ",").split(",") if p.strip()]
        return []

    def _cfg_list(self, path: str) -> list[str]:
        return self._as_list(self._cfg(path, []))

    def _db(self):
        """AstrBot 的数据库对象（用量统计写读都用它）。

        优先用 ``astrbot.core.db_helper``：统计页读的就是它，用它写才能保证面板看得见。
        """
        fallback = None
        try:
            fallback = self.context.get_db()
        except Exception as exc:
            logger.debug(f"[qq-bridge] 取 context.get_db() 失败：{exc}")
        return resolve_db(self.context, fallback)

    async def _usage_text(self, umo: str, label: str = "") -> str:
        """本会话用量 + 统计页面板口径 + 写入状态（用来确认面板上真的看得到）。"""
        db = self._db()
        try:
            info = await read_session_usage(db, umo)
        except Exception as exc:
            logger.debug(f"[qq-bridge] 读取用量失败：{exc}")
            info = None
        try:
            panel = await panel_usage(db, 1)
        except Exception as exc:
            logger.debug(f"[qq-bridge] 读取面板口径失败：{exc}")
            panel = None
        return format_session_usage(info, label, panel=panel, write_state=self._usage_writes)

    def _mode(self, umo: str = "") -> str:
        mode = str(self._cfg("wake.mode", "mention") or "mention")
        if mode not in MODE_HINTS:
            mode = "mention"
        return mode

    # ══════════════════════════════════════════════════════════════════════════
    #  准入与唤醒判定
    # ══════════════════════════════════════════════════════════════════════════
    def _session_allowed(self, event: AstrMessageEvent) -> bool:
        """白名单/黑名单。对应原项目的 allow.private / allow.groups / deny.*。"""
        group_id = str(event.get_group_id() or "")
        sender_id = str(event.get_sender_id() or "")
        is_group = bool(group_id)

        deny = self._cfg_list("access.denyGroups") if is_group else self._cfg_list("access.denyPrivate")
        if sender_id and sender_id in self._cfg_list("access.denyPrivate"):
            return False
        if is_group and group_id in deny:
            return False

        allow = self._cfg_list("access.groups") if is_group else self._cfg_list("access.private")
        if allow:
            return (group_id in allow) if is_group else (sender_id in allow)
        # 白名单为空：按 allowAllWhenEmpty 决定，默认放行（与原项目默认值不同，
        # 原项目默认 false —— 这里默认放行是因为 AstrBot 自己已有平台级白名单）
        return bool(self._cfg("access.allowAllWhenEmpty", True))

    def _session_allowed_umo(self, umo: str) -> bool:
        """没有 event 时的白名单判断（主动回合、黑话抽取用）。

        umo 形如 ``aiocqhttp:GroupMessage:123456``；解析不出来的一律按「不允许」处理，
        宁可漏发也不能往被拉黑/不在白名单的群里主动说话。
        """
        parts = str(umo or "").split(":")
        if len(parts) < 3:
            return False
        kind, ident = parts[1], parts[2]
        if not ident:
            return False
        is_group = kind.lower().startswith("group")
        deny = self._cfg_list("access.denyGroups") if is_group else self._cfg_list("access.denyPrivate")
        if ident in deny:
            return False
        allow = self._cfg_list("access.groups") if is_group else self._cfg_list("access.private")
        if allow:
            return ident in allow
        return bool(self._cfg("access.allowAllWhenEmpty", True))

    def _is_at_self(self, event: AstrMessageEvent) -> bool:
        self_id = str(event.get_self_id() or "")
        if not self_id and not self._warned_self_id:
            # 拿不到自己的 QQ 号时，下面只能「见到 At 就算 @ 我」——这在群里会误唤醒。
            # 记一次日志，方便排查「怎么 @ 别人它也回」。
            self._warned_self_id = True
            logger.warning(
                "[qq-bridge] 事件里拿不到机器人自己的 QQ 号（self_id 为空），"
                "无法区分「@ 我」和「@ 别人」，群聊里可能误唤醒；请检查适配器配置"
            )
        for comp in event.get_messages() or []:
            name = type(comp).__name__
            if name == "AtAll":
                return True
            if name == "At":
                qq = str(getattr(comp, "qq", "") or "")
                if qq and (not self_id or qq == self_id):
                    return True
        return False

    def _is_reply_to_self(self, event: AstrMessageEvent) -> bool:
        self_id = str(event.get_self_id() or "")
        if not self_id:
            return False
        for comp in event.get_messages() or []:
            if type(comp).__name__ == "Reply":
                target = str(getattr(comp, "sender_id", "") or getattr(comp, "qq", "") or "")
                if target and target == self_id:
                    return True
        return False

    def _should_wake(self, event: AstrMessageEvent) -> tuple[bool, str]:
        """决定这条消息要不要交给 AI。返回 (是否唤醒, 原因)。"""
        text = event.message_str or ""
        sender_id = str(event.get_sender_id() or "")
        self_id = str(event.get_self_id() or "")
        if sender_id and self_id and sender_id == self_id:
            return False, "自己发的消息"

        # 私聊默认总是唤醒（对应原项目私聊不参与潜水调度）
        if not event.get_group_id() and bool(self._cfg("wake.privateAlways", True)):
            return True, "私聊"

        if self._is_at_self(event):
            return True, "被 @"
        if self._is_reply_to_self(event) and bool(self._cfg("wake.replyToBot", True)):
            return True, "回复了机器人"
        names = self._cfg_list("wake.names")
        if names and match_names(text, names):
            return True, "被叫到名字"
        if bool(self._cfg("wake.question", True)) and looks_like_question(text):
            return True, "在提问"

        mode = self._mode()
        if mode == "anyMessage":
            return True, "活跃模式（每条都唤醒）"
        if mode == "probability":
            p = float(self._cfg("wake.probability", 0.1) or 0)
            if random.random() < max(0.0, min(1.0, p)):
                return True, "随机路过"
        return False, "未命中唤醒条件"

    # ══════════════════════════════════════════════════════════════════════════
    #  消息记录
    # ══════════════════════════════════════════════════════════════════════════
    def _record_message(self, event: AstrMessageEvent) -> MsgRecord:
        self_id = str(event.get_self_id() or "")
        sender_id = str(event.get_sender_id() or "")
        umo = event.unified_msg_origin
        self.inbox.set_self_id(umo, self_id)

        texts: list[str] = []
        images: list[str] = []
        forward_ids: list[str] = []
        reply_to_id = reply_to_name = reply_to_text = ""
        poked = False
        has_sticker = False

        for comp in event.get_messages() or []:
            name = type(comp).__name__
            if name == "Plain":
                texts.append(str(getattr(comp, "text", "") or ""))
            elif name == "Image":
                url = str(getattr(comp, "url", "") or "")
                path = str(getattr(comp, "file", "") or "")
                images.append(url or path)
            elif name == "Face":
                has_sticker = True
                images.append("")
            elif name == "Record":
                texts.append("[语音]")
            elif name == "Reply":
                reply_to_id = str(getattr(comp, "id", "") or "")
                reply_to_name = str(
                    getattr(comp, "sender_nickname", "") or getattr(comp, "sender_name", "") or ""
                )
                reply_to_text = str(getattr(comp, "message_str", "") or "")
            elif name in ("Forward", "Nodes"):
                fid = str(getattr(comp, "id", "") or "")
                if fid:
                    forward_ids.append(fid)
            elif name == "Poke":
                poked = True
            elif name in ("At", "AtAll"):
                qq = str(getattr(comp, "qq", "") or "")
                texts.append("@全体成员" if name == "AtAll" else f"@{qq or '某人'}")

        group_id = str(event.get_group_id() or "")
        label = f"{group_id}" if group_id else "私聊"
        self.inbox.set_label(umo, label)

        return self.inbox.append(
            MsgRecord(
                seq=0,
                umo=umo,
                message_id=str(getattr(event.message_obj, "message_id", "") or ""),
                sender_id=sender_id,
                sender_name=str(event.get_sender_name() or ""),
                group_id=group_id,
                text="".join(texts).strip(),
                images=images,
                is_sticker=has_sticker,
                reply_to_id=reply_to_id,
                reply_to_name=reply_to_name,
                reply_to_text=reply_to_text,
                forward_ids=forward_ids,
                poked=poked,
                is_self=bool(self_id and sender_id == self_id),
            )
        )

    # ══════════════════════════════════════════════════════════════════════════
    #  主事件入口
    # ══════════════════════════════════════════════════════════════════════════
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """所有消息先入缓冲，再按唤醒规则决定是否交给 AI。"""
        if not bool(self._cfg("enabled", True)):
            return
        # 重复投递判定窗口跟随配置（改配置后无需重启插件）
        try:
            self.inbox.replay_window_sec = max(
                0.0, float(self._cfg("context.replayWindowSec", 30) or 0)
            )
        except (TypeError, ValueError):
            pass
        try:
            record = self._record_message(event)
        except Exception as exc:  # 记录失败不影响 AstrBot 其它插件
            logger.warning(f"[qq-bridge] 记录消息失败：{exc}")
            return

        if record.is_duplicate:
            # 同一条消息被投递了两次（适配器重连/重放）。第一次已经处理过，再跑一轮就是重复回复。
            logger.info(
                f"[qq-bridge] 同一条消息重复投递（id={record.message_id}），本轮跳过以免重复回复"
            )
            event.stop_event()
            return
        if record.is_self or not self._session_allowed(event):
            return
        if bool(self._cfg("agent.enabled", True)) is False:
            return

        wake, reason = self._should_wake(event)
        if not wake:
            return

        self._ensure_background_tasks()
        try:
            await self._run_agent_turn(event, wake_reason=reason)
        except Exception as exc:
            logger.error(f"[qq-bridge] 回合执行失败：{exc}", exc_info=True)
        finally:
            # 本插件独占本轮：阻止 AstrBot 默认对话流程再回一次
            event.stop_event()

    # ══════════════════════════════════════════════════════════════════════════
    #  Agent 回合
    # ══════════════════════════════════════════════════════════════════════════
    async def _provider_id(self, umo: str) -> str:
        override = str(self._cfg("agent.providerId", "") or "").strip()
        if override:
            return override
        try:
            return await self.context.get_current_chat_provider_id(umo=umo) or ""
        except Exception as exc:
            logger.warning(f"[qq-bridge] 取当前对话模型失败：{exc}")
            return ""

    def _tool_set(self):
        """从工具管理器取出本插件注册的工具，组成本轮专用 ToolSet。"""
        try:
            from astrbot.core.agent.tool import ToolSet
        except ImportError:
            return None
        try:
            manager = self.context.get_llm_tool_manager()
        except Exception:
            return None
        tools = []
        for name in TOOL_NAMES:
            try:
                tool = manager.get_func(name)
            except Exception:
                tool = None
            if tool is not None:
                tools.append(tool)
        return ToolSet(tools=tools) if tools else None

    def _build_system_prompt(self, mode: str, umo: str = "") -> str:
        """组本轮系统提示词。umo 用来取该会话自己的记忆——不传就只剩全局记忆。"""
        role_name = str(self._cfg("persona.roleCard", "") or "").strip()
        role_card = load_role_card(
            self.role_dir, role_name, max_chars=int(self._cfg("persona.maxInjectChars", 6000) or 6000)
        )
        if role_name and not role_card:
            logger.warning(f"[qq-bridge] 人设卡「{role_name}」不存在，本轮不注入人设")
        memory_context = self._memory_context(event_umo=umo)
        if not memory_context and bool(self._cfg("memory.enabled", True)):
            # 冷启动死角：记忆为空时若整块都不出现，模型就不知道有记忆这回事，于是永远不写。
            memory_context = "（目前还是空的。遇到值得记的话题、对群友的印象、想说未说的话，用 qq_memory_append 记一条。）"
        return build_system_prompt(
            role_card=role_card,
            mode_hint=MODE_HINTS.get(mode, ""),
            slang_context=self.slang.build_context(int(self._cfg("slang.injectMax", 8) or 0))
            if bool(self._cfg("slang.enabled", True))
            else "",
            sticker_context=self.stickers.build_context(int(self._cfg("stickers.injectMax", 8) or 0))
            if bool(self._cfg("stickers.enabled", True))
            else "",
            memory_context=memory_context,
            extra_prompt=str(self._cfg("persona.extraPrompt", "") or ""),
            max_chars=int(self._cfg("persona.maxSystemChars", 20000) or 20000),
        )

    async def _provider_model(self, provider_id: str) -> str:
        """取模型名（面板上要显示它）。取不到就回落配置里的标签。"""
        label = str(self._cfg("agent.modelLabel", "") or "")
        if not provider_id:
            return label
        try:
            manager = getattr(self.context, "provider_manager", None)
            prov = await manager.get_provider_by_id(provider_id) if manager else None
            if prov is not None:
                return str(prov.get_model() or "") or label
        except Exception as exc:
            logger.debug(f"[qq-bridge] 取模型名失败：{exc}")
        return label

    async def _record_usage(
        self,
        *,
        umo: str,
        provider_id: str,
        resp,
        started: float,
        status: str = "completed",
    ) -> None:
        """把一轮用量写进 AstrBot 自带的统计（provider_stats 表）。

        WebUI「数据与日志 → 统计」读的就是这张表，所以这里有三条硬要求：
          · 用 ``astrbot.core.db_helper`` 写 —— 和面板读的是同一张表（见 qbridge/usage.py）；
          · **即使 provider 没回报 token 也照写一行** —— 面板的「调用次数」靠的就是这些行；
          · **模型调用失败也写**（``status="error"``、token 记 0）—— AstrBot 自己那条路同样如此，
            否则模型端点不通时，面板上连「这个插件跑过」都看不到。
        """
        if not bool(self._cfg("usage.enabled", True)):
            return
        try:
            result = await record_turn(
                db=self._db(),
                conv_manager=getattr(self.context, "conversation_manager", None),
                umo=umo,
                provider_id=provider_id,
                provider_model=await self._provider_model(provider_id),
                resp=resp,
                started=started,
                ended=time.time(),
                status=status,
            )
        except Exception as exc:
            self._usage_writes["fail"] += 1
            self._usage_writes["lastError"] = f"{type(exc).__name__}: {exc}"
            logger.warning(f"[qq-bridge] 写入用量统计异常：{exc}", exc_info=True)
            return
        if result.get("written"):
            self._usage_writes["ok"] += 1
        else:
            self._usage_writes["fail"] += 1
            self._usage_writes["lastError"] = str(result.get("error") or "")

    async def _run_agent_turn(self, event: AstrMessageEvent, *, wake_reason: str) -> None:
        """跑一轮 Agent。同一会话串行执行。

        上一轮还没结束时进来的消息**不丢**：记进待跑队列，等当前回合一结束就补跑。
        （模型调完工具后往往还要再问一次模型，本地模型这一步可能好几秒到几十秒；
        旧版在这段时间里直接丢弃新消息，用户看到的就是「发消息没反应」。）
        """
        umo = event.unified_msg_origin
        lock = self._turn_locks.setdefault(umo, asyncio.Lock())
        if lock.locked():
            self._pending_wake[umo] = (event, wake_reason or "排队等待", self.inbox.last_seq(umo))
            self._queue_stats["queued"] += 1
            logger.info(f"[qq-bridge] {umo} 上一轮仍在进行，本条排队，回合结束后补跑")
            return
        async with lock:
            await self._turn_locked(event, wake_reason)
        await self._drain_pending(umo)

    async def _drain_pending(self, umo: str) -> None:
        """补跑排队中的消息。迭代处理（最多 5 条），不递归，避免消息密集时把调用栈吃光。"""
        for _ in range(5):
            queued = self._pending_wake.pop(umo, None)
            if not queued:
                return
            event, reason, seq = queued
            if seq and seq <= self._turn_seen_seq.get(umo, 0):
                # 上一轮其实已经看到这条了（快照里包含它，或模型用 qq_wait_for_messages 收到了）。
                # 再补跑一轮只会让它被回复第二次——这正是「同一句历史消息回复两次以上」的来源。
                self._queue_stats["skipped"] += 1
                logger.info(f"[qq-bridge] 排队消息已被上一轮看到，跳过补跑：{umo}（seq={seq}）")
                continue
            lock = self._turn_locks.setdefault(umo, asyncio.Lock())
            if lock.locked():  # 期间又被别的调用占住，放回去交给它处理
                self._pending_wake[umo] = queued
                return
            self._queue_stats["replayed"] += 1
            logger.info(f"[qq-bridge] 补跑排队消息：{umo}（{reason}）")
            async with lock:
                await self._turn_locked(event, reason)

    async def _turn_locked(self, event: AstrMessageEvent, wake_reason: str) -> None:
        """真正执行一轮（调用方必须已持有该会话的锁）。"""

        umo = event.unified_msg_origin
        provider_id = await self._provider_id(umo)
        if not provider_id:
            logger.warning("[qq-bridge] 没有可用的对话模型，跳过本轮（请在 AstrBot 里配置模型提供商）")
            return

        mode = self._mode(umo)
        snapshot_limit = int(self._cfg("context.snapshotLimit", 20) or 20)
        is_group = bool(event.get_group_id())
        label = str(event.get_group_id() or event.get_sender_name() or "私聊")

        seen_before = self._turn_seen_seq.get(umo, 0)
        snapshot, seen_now = self.inbox.render(
            self.inbox.recent(umo, limit=snapshot_limit),
            new_after_seq=seen_before or None,  # 本会话第一轮：全是历史，不标【新】
        )
        if seen_now:
            self._turn_seen_seq[umo] = seen_now

        prompt = build_turn_prompt(
            session_label=label,
            snapshot=snapshot,
            wake_reason=wake_reason,
            is_group=is_group,
            now_str=time.strftime("%Y-%m-%d %H:%M:%S"),
            mark_new=bool(seen_before),  # 第一轮没有「旧消息」概念，不用加说明
        )
        system_prompt = self._build_system_prompt(mode, umo)

        self._sent_this_turn[umo] = False
        self._dived_this_turn[umo] = False
        max_steps = int(self._cfg("agent.maxSteps", 30) or 30)
        timeout_s = int(float(self._cfg("agent.toolCallTimeoutMs", 600000) or 600000) / 1000)

        started = time.time()
        token = _CURRENT_UMO.set(umo)
        self._last_umo = umo
        try:
            resp = await self.context.tool_loop_agent(
                event=event,
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=system_prompt,
                tools=self._tool_set(),
                max_steps=max_steps,
                tool_call_timeout=max(10, timeout_s),
            )
        except Exception as exc:
            logger.error(f"[qq-bridge] 模型调用失败：{exc}", exc_info=True)
            # 失败也记一行（status="error"、token 记 0）——AstrBot 自己那条路同样如此。
            # 否则模型端点不通时，面板上连「这个插件调用过」都看不到，无从排查。
            await self._record_usage(
                umo=umo,
                provider_id=provider_id,
                resp=None,
                started=started,
                status="error",
            )
            return
        finally:
            elapsed = time.time() - started
            _CURRENT_UMO.reset(token)

        await self._record_usage(
            umo=umo,
            provider_id=provider_id,
            resp=resp,
            started=started,
        )

        # 兜底：模型既没调用发言工具、也没被 dive，就把「思考」当回复发出去，
        # 否则用户会觉得机器人坏了。可在配置里关掉（agent.fallbackSendThinking）。
        # 注意：显式 qq_dive 过的回合不发——模型已经决定潜水，兜底不能替它改主意。
        if (
            bool(self._cfg("agent.fallbackSendThinking", True))
            and not self._sent_this_turn.get(umo)
            and not self._dived_this_turn.get(umo)
        ):
            text = clean_model_text(
                getattr(resp, "completion_text", "") or "",
                int(self._cfg("reply.maxChars", 500) or 500),
            )
            if text:
                await self._send_text(event, text)
                logger.info(f"[qq-bridge] 模型未调用工具，已用兜底发送（{len(text)} 字，{elapsed:.1f}s）")

    # ══════════════════════════════════════════════════════════════════════════
    #  发送
    # ══════════════════════════════════════════════════════════════════════════
    async def _send_chain(self, event: AstrMessageEvent | None, umo: str, chain) -> bool:
        """发消息。有 event 时走 event.send（计入本会话），否则用主动发送。"""
        try:
            if event is not None:
                await event.send(chain)
            else:
                await self.context.send_message(umo, chain)
            return True
        except Exception as exc:
            logger.error(f"[qq-bridge] 发送失败：{exc}")
            return False

    async def _send_text(
        self,
        event: AstrMessageEvent | None,
        text: str,
        umo: str = "",
        prefix: list | None = None,
    ) -> bool:
        """发一条文本（过长按 splitMaxChars 分段）。

        ``prefix`` 里的消息组件（At / Reply）只挂在**第一段**前面——这是 @ 与引用回复
        真正生效的地方：调用方必须把它们传进来，光构造 chain 不发是没有用的。
        """
        from astrbot.api.event import MessageChain

        if not text:
            return False
        target = umo or self._turn_umo(event)
        if not target:
            return False
        parts = split_for_qq(text, int(self._cfg("reply.splitMaxChars", 4000) or 4000))
        delay = max(0, int(self._cfg("reply.sendDelayMs", 300) or 0)) / 1000.0
        head = list(prefix or [])
        ok = True
        for index, part in enumerate(parts):
            if index and delay:
                await asyncio.sleep(delay)
            components = [*head, Plain(text=part)] if index == 0 else [Plain(text=part)]
            ok = await self._send_chain(event, target, MessageChain(chain=components)) and ok
        # 无论是用户回合还是主动回合，发了文本就标记——否则主动回合兜底会把思考再发一条。
        if target:
            self._sent_this_turn[target] = True
        return ok

    # ══════════════════════════════════════════════════════════════════════════
    #  记忆
    # ══════════════════════════════════════════════════════════════════════════
    def _memory_all(self) -> dict:
        raw = self.memory.load()
        return raw if isinstance(raw, dict) else {}

    def _memory_context(self, event_umo: str = "", max_items: int = 0) -> str:
        if not bool(self._cfg("memory.enabled", True)):
            return ""
        limit = max_items or int(self._cfg("memory.injectMax", 12) or 12)
        data = self._memory_all()
        items: list[dict] = []
        for key in ("global", event_umo):
            if key and isinstance(data.get(key), list):
                items.extend(data[key])
        if not items:
            return ""
        items = items[-limit:]
        return "\n".join(f"- {truncate(str(i.get('text', '')), 120)}" for i in items if i.get("text"))

    def _memory_append(self, umo: str, text: str) -> None:
        data = self._memory_all()
        bucket = data.setdefault(umo or "global", [])
        if not isinstance(bucket, list):
            bucket = []
            data[umo or "global"] = bucket
        bucket.append({"t": now_iso(), "text": truncate(text, 300)})
        data[umo or "global"] = bucket[-200:]
        self.memory.save(data)

    # ── 记忆的管理员视图（快照 / 删除 / 清空，供查看页与指令共用）────────────
    def _memory_label(self, umo: str) -> str:
        """把 umo 变成人看的标签：aiocqhttp:GroupMessage:123456 → 群 123456。"""
        parts = str(umo or "").split(":")
        if len(parts) >= 3:
            kind = {"GroupMessage": "群", "FriendMessage": "私聊"}.get(parts[1], parts[1])
            return f"{kind} {parts[2]}"
        return umo or "(未知会话)"

    def _memory_snapshot(self) -> dict:
        """整份记忆快照。查看页原样 JSON 返回，不做二次加工。"""
        data = self._memory_all()
        sessions: list[dict] = []
        total = 0
        for umo, bucket in data.items():
            if not isinstance(bucket, list):
                continue
            items = []
            for index, entry in enumerate(bucket):
                if not isinstance(entry, dict):
                    continue
                items.append(
                    {
                        "index": index,
                        "t": str(entry.get("t", "") or ""),
                        "text": str(entry.get("text", "") or ""),
                    }
                )
            total += len(items)
            is_global = umo == "global"
            sessions.append(
                {
                    "umo": umo,
                    "label": "全局记忆" if is_global else self._memory_label(umo),
                    "kind": "global" if is_global else "session",
                    "count": len(items),
                    "updated": items[-1]["t"] if items else "",
                    "items": items,
                }
            )
        sessions.sort(key=lambda s: s["updated"], reverse=True)  # 最近的在前
        sessions.sort(key=lambda s: 0 if s["kind"] == "global" else 1)  # 全局记忆置顶（稳定排序）
        return {
            "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            "totalSessions": len(sessions),
            "totalItems": total,
            "allowDelete": self.memview.allow_delete,
            "sessions": sessions,
        }

    def _memory_delete(self, umo: str, index: int) -> bool:
        data = self._memory_all()
        bucket = data.get(umo)
        if not isinstance(bucket, list) or not (0 <= index < len(bucket)):
            return False
        del bucket[index]
        data[umo] = bucket
        self.memory.save(data)
        return True

    def _memory_clear(self, umo: str) -> bool:
        data = self._memory_all()
        if umo not in data:
            return False
        data[umo] = []
        self.memory.save(data)
        return True

    # ══════════════════════════════════════════════════════════════════════════
    #  后台任务：主动消息 + 黑话学习
    # ══════════════════════════════════════════════════════════════════════════
    def _ensure_background_tasks(self) -> None:
        if self._started:
            return
        self._started = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if bool(self._cfg("proactive.enabled", False)):
            self._tasks.append(loop.create_task(self._proactive_loop()))
        if bool(self._cfg("slang.autoLearn", False)):
            self._tasks.append(loop.create_task(self._slang_loop()))
        if self._tasks:
            logger.info(f"[qq-bridge] 已启动 {len(self._tasks)} 个后台任务")

    async def _proactive_loop(self) -> None:
        """冷场时按概率主动开口。对应原项目的 proactive / 主动机会唤醒。"""
        while True:
            try:
                interval = max(60, int(self._cfg("proactive.checkIntervalMs", 1800000) or 1800000) / 1000)
                await asyncio.sleep(interval)
                idle_threshold = max(60, int(self._cfg("proactive.idleThresholdMs", 900000) or 900000) / 1000)
                probability = float(self._cfg("proactive.probability", 0.3) or 0)
                for umo in list(self.inbox.known_sessions()):
                    if not self._session_allowed_umo(umo):
                        continue  # 消息缓冲记的是全部消息，主动开口必须再过一次白名单
                    if self.inbox.idle_seconds(umo) < idle_threshold:
                        continue
                    if random.random() >= max(0.0, min(1.0, probability)):
                        continue
                    await self._proactive_turn(umo)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[qq-bridge] 主动消息循环异常：{exc}")
                await asyncio.sleep(60)

    async def _proactive_turn(self, umo: str) -> None:
        # 用户回合正在跑就让路：两条回合并发会各调一次模型、消息还可能交错发出
        lock = self._turn_locks.setdefault(umo, asyncio.Lock())
        if lock.locked():
            logger.debug(f"[qq-bridge] {umo} 有用户回合在跑，这次主动机会跳过")
            return
        provider_id = await self._provider_id(umo)
        if not provider_id:
            return
        # 每次主动回合开始前重置发言/潜水标记——不重置的话，上一轮残留为 True 会让本轮
        # 兜底判定失效，模型没说话也不会被兜底发出。
        self._sent_this_turn[umo] = False
        self._dived_this_turn[umo] = False
        snapshot, seen_now = self.inbox.render(
            self.inbox.recent(umo, limit=int(self._cfg("context.snapshotLimit", 20) or 20)),
            new_after_seq=self._turn_seen_seq.get(umo, 0),
        )
        if seen_now:
            self._turn_seen_seq[umo] = seen_now
        system_prompt = self._build_system_prompt("proactive", umo)
        prompt = build_turn_prompt(
            session_label=umo,
            snapshot=snapshot,
            wake_reason="群里安静了一段时间，这是一次主动机会",
            is_group=True,
            now_str=time.strftime("%Y-%m-%d %H:%M:%S"),
            mark_new=True,  # 主动机会没有新消息，更要提醒别回头回复旧消息
        )
        logger.info(f"[qq-bridge] 主动机会：{umo}")
        # 主动回合没有 event，工具靠 ContextVar 拿到目标会话，用 context 直接发送
        token = _CURRENT_UMO.set(umo)
        self._last_umo = umo
        started = time.time()
        resp = None
        try:
            resp = await self.context.tool_loop_agent(
                event=None,
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=system_prompt,
                tools=self._tool_set(),
                max_steps=int(self._cfg("agent.maxSteps", 30) or 30),
                tool_call_timeout=max(10, int(float(self._cfg("agent.toolCallTimeoutMs", 600000) or 600000) / 1000)),
            )
        except Exception as exc:
            logger.warning(f"[qq-bridge] 主动回合失败：{exc}")
            await self._record_usage(umo=umo, provider_id=provider_id, resp=None, started=started, status="error")
        else:
            await self._record_usage(umo=umo, provider_id=provider_id, resp=resp, started=started)
        finally:
            _CURRENT_UMO.reset(token)

        # 兜底：和用户回合保持一致——模型既没发言也没 dive 时，把思考文本发出去。
        # 主动回合也要走这步，否则「模型没调用工具也没报错」时会表现为完全没反应。
        if (
            resp is not None
            and bool(self._cfg("agent.fallbackSendThinking", True))
            and not self._sent_this_turn.get(umo)
            and not self._dived_this_turn.get(umo)
        ):
            thinking = clean_model_text(
                getattr(resp, "completion_text", "") or "",
                int(self._cfg("reply.maxChars", 500) or 500),
            ).strip()
            if thinking:
                await self._send_text(None, thinking, umo=umo)

    async def _slang_loop(self) -> None:
        """定期从最近的群聊记录里抽黑话候选。"""
        while True:
            try:
                interval = max(300, int(self._cfg("slang.extractIntervalMs", 3600000) or 3600000) / 1000)
                await asyncio.sleep(interval)
                await self._learn_slang_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[qq-bridge] 黑话学习异常：{exc}")
                await asyncio.sleep(300)

    async def _learn_slang_once(self) -> str:
        """抽一次黑话。返回给指令用的简短结果。"""
        if self._slang_busy:
            return "上一次黑话抽取还在进行中。"
        min_messages = int(self._cfg("slang.extractMinMessages", 10) or 10)
        umo = ""
        for candidate in self.inbox.known_sessions():
            if not self._session_allowed_umo(candidate):
                continue  # 不在白名单的会话，连它的聊天记录都不拿来学
            if len(self.inbox.recent(candidate, limit=200)) >= min_messages:
                umo = candidate
                break
        if not umo:
            return f"最近消息不足 {min_messages} 条，暂不抽取。"
        self._slang_busy = True
        try:
            records = self.inbox.recent(umo, limit=200)
            lines = [r.line() for r in records if not r.is_self and r.text][-80:]
            prompt = SlangStore.build_extraction_prompt(lines)
            provider_id = await self._provider_id(umo)
            if not provider_id:
                return "没有可用的对话模型。"
            started = time.time()
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt="你是一个中文网络用语分析助手，只输出 JSON 数组。",
            )
            # 抽取也要记用量：它同样花 token，不记就等于面板上看不见这笔开销
            await self._record_usage(umo=umo, provider_id=provider_id, resp=resp, started=started)
            items = SlangStore.parse_extraction_json(getattr(resp, "completion_text", "") or "")
            added = 0
            for item in items:
                if self.slang.find(item["content"]):
                    self.slang.upsert(item["content"], {"evidence": [{"umo": umo}]})
                    continue
                self.slang.upsert(
                    item["content"],
                    {
                        "meaning": item.get("meaning", ""),
                        "usage": item.get("usage", ""),
                        "risk": item.get("risk", ""),
                        "status": STATUS_CANDIDATE,
                        "evidence": [{"umo": umo}],
                    },
                )
                added += 1
            self.slang.save()
            return f"抽取完成：新增 {added} 条候选（共 {len(items)} 条返回）。用 /qq 黑话 查看，/qq 黑话确认 <词> 启用。"
        finally:
            self._slang_busy = False

    # ══════════════════════════════════════════════════════════════════════════
    #  指令
    # ══════════════════════════════════════════════════════════════════════════
    @filter.command("qq")
    async def qq_command(self, event: AstrMessageEvent):
        """qq-bridge 管理指令。用法：/qq 状态|开|关|人设|黑话|表情|用量|唤醒|记忆 [清空|全部]|重置"""
        text = (event.message_str or "").strip()
        for prefix in ("/qq", "qq"):
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
                break
        parts = text.split()
        sub = parts[0] if parts else "状态"
        args = parts[1:]

        if sub in ("状态", "status", ""):
            yield event.plain_result(self._status_text())
        elif sub in ("开", "on"):
            self.config["enabled"] = True
            self._save_config()
            yield event.plain_result("已开启 qq-bridge。")
        elif sub in ("关", "off"):
            self.config["enabled"] = False
            self._save_config()
            yield event.plain_result("已关闭 qq-bridge（消息不再交给 AI）。")
        elif sub == "人设":
            yield event.plain_result(self._role_text(args))
        elif sub == "黑话":
            if args and args[0] == "学习":
                # 真的跑一次抽取（旧版只回一句「已开始」但什么都没做）
                yield event.plain_result(await self._slang_learn_text())
            else:
                yield event.plain_result(self._slang_text(args))
        elif sub == "表情":
            yield event.plain_result(self._sticker_text(args))
        elif sub in ("用量", "usage"):
            yield event.plain_result(await self._usage_text(event.unified_msg_origin))
        elif sub == "唤醒":
            yield event.plain_result(self._wake_text(args))
        elif sub == "记忆":
            yield event.plain_result(
                self._memory_text(args, event.unified_msg_origin, self._is_admin(event))
            )
        elif sub == "重置":
            self.inbox = Inbox()
            yield event.plain_result("已清空本会话的消息缓冲。")
        else:
            yield event.plain_result(
                "未知子指令。可用：状态 / 开 / 关 / 人设 [名字] / 黑话 [确认|删除|学习 <词>] / "
                "表情 [备注|删除] / 用量 / 唤醒 [模式 概率] / 记忆 [清空|全部] / 重置"
            )

    def _save_config(self) -> None:
        try:
            self.config.save_config()
        except Exception as exc:
            logger.warning(f"[qq-bridge] 保存配置失败：{exc}")

    def _memview_status(self) -> str:
        if self.memview.running:
            line = f"已启动 {self.memview.url(token=False)}"
            if self.memview.token:
                # 不回显令牌本身，只说清「要带令牌、带一次就行」
                line += "（已启用访问令牌：首次访问请在网址后加 ?token=你的令牌，之后浏览器会记住）"
            if is_loopback(self.memview.host):
                # 只监听回环时，AstrBot 若在容器/WSL 里，宿主机浏览器是访问不到的
                line += "（只监听本机回环；AstrBot 跑在容器/WSL 里时宿主机浏览器连不上）"
            if self.memview.empty_requests:
                line += f"｜收到空连接 {self.memview.empty_requests} 次"
            return line
        if self.memview.last_error:
            return f"未启动（{self.memview.last_error}）"
        return "未启用"

    async def _start_memview(self) -> bool:
        """按配置起记忆查看页。配置改动需要重载插件才生效。"""
        if not bool(self._cfg("memview.enabled", True)):
            logger.info("[qq-bridge] 记忆查看页已关闭（memview.enabled=false）")
            return False
        self.memview.host = str(self._cfg("memview.host", "127.0.0.1") or "127.0.0.1")
        self.memview.port = int(self._cfg("memview.port", 8899) or 8899)
        self.memview.token = str(self._cfg("memview.token", "") or "")
        self.memview.allow_delete = bool(self._cfg("memview.allowDelete", False))
        return await self.memview.start()

    def _status_text(self) -> str:
        slang = self.slang.stats()
        lines = [
            "【qq-bridge 状态】",
            f"插件：{'开启' if self._cfg('enabled', True) else '关闭'}｜"
            f"Agent：{'开启' if self._cfg('agent.enabled', True) else '关闭'}",
            f"唤醒模式：{self._mode()}（概率 {self._cfg('wake.probability', 0.1)}）",
            f"人设卡：{self._cfg('persona.roleCard', '') or '（未设置）'}",
            f"白名单：私聊 {len(self._cfg_list('access.private'))} 个 / 群 {len(self._cfg_list('access.groups'))} 个",
            f"黑话库：确认 {slang['confirmed']}｜候选 {slang['candidate']}",
            f"表情收藏：{self.stickers.stats()['total']} 个",
            f"用量写入：{'开' if self._cfg('usage.enabled', True) else '关'}"
            f"（成功 {self._usage_writes['ok']} / 失败 {self._usage_writes['fail']}）",
            f"回合排队：排队 {self._queue_stats['queued']} 次｜补跑 {self._queue_stats['replayed']} 次"
            + (f"｜跳过重复 {self._queue_stats['skipped']} 次" if self._queue_stats["skipped"] else "")
            + ("（有消息在上一轮结束前到达，已自动补跑）" if self._queue_stats["queued"] else "")
            + (f"｜收尾后拦截 {self._after_dive_blocks} 次" if self._after_dive_blocks else ""),
            f"记忆查看页：{self._memview_status()}",
            f"语音：{'开启' if self._cfg('voice.enabled', False) else '关闭'}｜"
            f"主动消息：{'开启' if self._cfg('proactive.enabled', False) else '关闭'}",
            f"数据目录：{self.data_dir}",
        ]
        return "\n".join(lines)

    def _role_text(self, args: list[str]) -> str:
        cards = list_role_cards(self.role_dir)
        if not args:
            current = str(self._cfg("persona.roleCard", "") or "")
            listing = "、".join(cards) if cards else "（roles/ 目录下没有 .md 人设卡）"
            return f"当前人设：{current or '（未设置）'}\n可用人设：{listing}\n用法：/qq 人设 <名字>；/qq 人设 无 可清空"
        name = args[0]
        if name in ("无", "none", "off"):
            self.config["persona"]["roleCard"] = ""
            self._save_config()
            return "已清空人设。"
        if name not in cards:
            return f"没有这张人设卡：{name}\n可用：{'、'.join(cards) or '（无）'}"
        self.config["persona"]["roleCard"] = name
        self._save_config()
        return f"已切换人设：{name}"

    async def _slang_learn_text(self) -> str:
        """指令触发的黑话抽取。模型慢就先如实说「还在跑」，不假装已经完成。"""
        try:
            return await asyncio.wait_for(self._learn_slang_once(), timeout=90)
        except asyncio.TimeoutError:
            return "抽取还在进行（模型响应较慢）。稍后用 /qq 黑话 看候选，完成后日志里也会提示。"
        except Exception as exc:
            return f"抽取失败：{exc}"

    def _slang_text(self, args: list[str]) -> str:
        if not args:
            stats = self.slang.stats()
            items = self.slang.list_candidates(limit=10)
            lines = [f"【黑话库】确认 {stats['confirmed']}｜候选 {stats['candidate']}"]
            if items:
                lines.append("待确认候选：")
                lines += [f"  {e['content']}（出现 {e['count']} 次）{e['meaning'][:40]}" for e in items]
            lines.append("用法：/qq 黑话确认 <词>｜/qq 黑话删除 <词>｜/qq 黑话学习")
            return "\n".join(lines)
        action = args[0]
        term = " ".join(args[1:]).strip()
        if not term:
            return "请给出词条，例如：/qq 黑话确认 蚌埠住了"
        if action == "确认":
            entry = self.slang.set_status(term, STATUS_CONFIRMED)
            if not entry:
                return f"黑话库里没有「{term}」。"
            self.slang.save()
            return f"已确认「{term}」，之后会注入提示词。"
        if action == "删除":
            ok = self.slang.remove(term)
            if ok:
                self.slang.save()
            return f"已删除「{term}」。" if ok else f"黑话库里没有「{term}」。"
        return "未知操作，可用：确认 / 删除 / 学习"

    def _sticker_text(self, args: list[str]) -> str:
        if not args:
            return self.stickers.format_list(limit=20) + "\n用法：/qq 表情备注 <id> <备注>｜/qq 表情删除 <id>"
        action = args[0]
        if action in ("备注", "note") and len(args) >= 3:
            entry = self.stickers.note(args[1], note=" ".join(args[2:]))
            if not entry:
                return f"没有这个表情：{args[1]}"
            self.stickers.save()
            return f"已给 {args[1]} 添加备注。"
        if action in ("删除", "del") and len(args) >= 2:
            ok = self.stickers.remove(args[1])
            if ok:
                self.stickers.save()
            return "已删除。" if ok else f"没有这个表情：{args[1]}"
        return "用法：/qq 表情备注 <id> <备注>｜/qq 表情删除 <id>"

    def _wake_text(self, args: list[str]) -> str:
        if not args:
            return (
                f"当前唤醒模式：{self._mode()}｜概率 {self._cfg('wake.probability', 0.1)}\n"
                "用法：/qq 唤醒 <mention|probability|anyMessage> [概率]"
            )
        mode = args[0]
        if mode not in MODE_HINTS:
            return f"不支持的唤醒模式：{mode}（可选 mention / probability / anyMessage）"
        self.config["wake"]["mode"] = mode
        if len(args) >= 2:
            try:
                self.config["wake"]["probability"] = max(0.0, min(1.0, float(args[1])))
            except ValueError:
                pass
        self._save_config()
        return f"已设置唤醒模式：{mode}｜概率 {self._cfg('wake.probability', 0.1)}"

    def _memory_text(self, args: list[str], umo: str, is_admin: bool = False) -> str:
        if args and args[0] == "清空":
            self._memory_clear(umo)
            return "已清空本会话记忆。"
        if args and args[0] in ("全部", "all"):
            if not is_admin:
                return "只有管理员能查看全部会话的记忆。"
            return self._memory_overview()
        data = self._memory_all()
        items = data.get(umo) or []
        if not items:
            return "本会话还没有记忆。"
        return "【记忆】\n" + "\n".join(f"- {i.get('t', '')} {i.get('text', '')}" for i in items[-20:])

    def _memory_overview(self) -> str:
        """管理员视角：所有会话的记忆概览（完整内容在查看页里看）。"""
        data = self._memory_all()
        rows = [(key, value) for key, value in data.items() if isinstance(value, list) and value]
        if not rows:
            return "还没有任何记忆。"

        def latest_of(bucket: list) -> str:
            return bucket[-1].get("t", "") if bucket and isinstance(bucket[-1], dict) else ""

        rows.sort(key=lambda kv: latest_of(kv[1]), reverse=True)
        total = sum(len(bucket) for _, bucket in rows)
        lines = [f"【全部记忆】{len(rows)} 个会话 / {total} 条"]
        for key, bucket in rows[:15]:
            label = "全局" if key == "global" else self._memory_label(key)
            lines.append(f"- {label}（{key}）：{len(bucket)} 条｜最近 {latest_of(bucket)}")
        if len(rows) > 15:
            lines.append(f"…另有 {len(rows) - 15} 个会话未列出")
        if self.memview.running:
            lines.append(f"搜索与完整内容：{self.memview.url(token=False)}")
        else:
            lines.append(f"查看页未启动：{self.memview.last_error or '未启用'}")
        return "\n".join(lines)

    @staticmethod
    def _is_admin(event: AstrMessageEvent) -> bool:
        """取管理员身份。取不到一律按非管理员处理。"""
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    # ══════════════════════════════════════════════════════════════════════════
    #  默认对话流程的人设注入
    # ══════════════════════════════════════════════════════════════════════════
    @filter.on_llm_request()
    async def inject_persona(self, event: AstrMessageEvent, req: ProviderRequest):
        """当消息走 AstrBot 默认对话流程时（本插件没接管），也把协议与人设注入进去。"""
        if not bool(self._cfg("persona.injectIntoDefaultPipeline", True)):
            return
        try:
            req.system_prompt = (
                (req.system_prompt or "")
                + "\n\n"
                + self._build_system_prompt(self._mode(), event.unified_msg_origin)
            )
        except Exception as exc:
            logger.debug(f"[qq-bridge] 注入人设失败：{exc}")

    # ══════════════════════════════════════════════════════════════════════════
    #  工具面
    # ══════════════════════════════════════════════════════════════════════════
    def _turn_umo(self, event: AstrMessageEvent | None) -> str:
        """本轮的目标会话。主动回合没有 event，靠 ContextVar（其次最近会话）兜底。"""
        if event is not None:
            return event.unified_msg_origin
        return _CURRENT_UMO.get() or getattr(self, "_last_umo", "")

    def _guard(self, event: AstrMessageEvent | None) -> str:
        """工具准入：主动回合（event 为 None）或未授权会话都拒绝。"""
        if event is None:
            return ""
        if not self._session_allowed(event):
            return "当前会话不在白名单里，操作被拒绝。"
        return ""

    def _closed_this_turn(self, umo: str, action: str = "发言") -> str:
        """本轮是否已经用 qq_dive 收尾。收尾后返回拒绝理由，否则空串。

        qq_dive 的意思是「本轮结束」，但工具调用并不会真的终止 Agent 循环——模型收尾后
        仍然能继续调工具。旧版不拦，于是出现「已经收尾、紧接着又发两条」，用户看到的就是
        同一句话被回复两次以上。这里把收尾之后的发言/等待直接拦掉。
        """
        if not (umo and self._dived_this_turn.get(umo)):
            return ""
        self._after_dive_blocks += 1
        logger.info(f"[qq-bridge] 本轮已收尾（qq_dive），拦下收尾后的{action}调用：{umo}")
        return (
            f"本轮已经用 qq_dive 收尾了，这个{action}不会生效。本轮到此结束，"
            f"要补充内容请等下一轮，现在直接给出结束语即可。"
        )

    def _mark_seen(self, umo: str, *seqs: int) -> None:
        """把「模型已经看到过」的水位推到这些序号。

        排队补跑（_drain_pending）就是拿这个水位判断「上一轮是不是已经看到过这条消息」：
        水位之内跳过补跑，否则同一条消息会被回第二遍。所以**凡是把消息内容交给模型的工具
        都要在这里登记**——回合开始的快照和 qq_wait_for_messages 早就登记了，
        qq_get_recent_messages 这类读取工具以前漏了：模型明明用它看到了中途到达的新消息并
        回了它，水位却停在旧值，回合结束又把那条补跑一轮 → 再回一遍。
        """
        if not umo:
            return
        top = max((int(x) for x in seqs if x), default=0)
        if top > self._turn_seen_seq.get(umo, 0):
            self._turn_seen_seq[umo] = top

    def _mark_spoken(self, umo: str) -> None:
        """标记本轮已经发过东西。

        表情、拍一拍、语音这些「用 event.send 直接发」的工具必须调它，否则
        agent.fallbackSendThinking 会把模型的思考文本再发一条出去。
        """
        if umo:
            self._sent_this_turn[umo] = True

    @filter.llm_tool(name="qq_send_message")
    async def qq_send_message(
        self,
        event: AstrMessageEvent,
        messages: list,
        atUserId: str = "",
        replyToMessageId: str = "",
    ) -> str:
        """发送一条或多条 QQ 消息。这是唯一的发言方式——你的文本输出不会自动发出去。

        Args:
            messages(array[string]): 要发送的内容。传一项发一条；多项会依次分条发送。
            atUserId(string): 可选。要 @ 的 QQ 号，从成员列表或消息快照里取，不凭记忆填。
            replyToMessageId(string): 可选。要引用的消息 id，用消息快照里的 id。
        """
        denied = self._guard(event)
        if denied:
            return denied
        umo = self._turn_umo(event)
        if not umo:
            return "拿不到会话信息，操作取消。"
        closed = self._closed_this_turn(umo, "发言")
        if closed:
            return closed
        if not bool(self._cfg("agent.allowSend", True)):
            return "管理员已关闭发言工具。"

        items = messages if isinstance(messages, list) else [messages]
        max_per_turn = int(self._cfg("reply.maxMessagesPerTurn", 8) or 8)
        items = [str(m).strip() for m in items if str(m).strip()][:max_per_turn]
        if not items:
            return "没有可发送的内容。"

        sent = 0
        for text in items:
            text = clean_model_text(text, int(self._cfg("reply.maxChars", 500) or 500))
            if not text:
                continue
            # @ 和引用只挂在第一条上。这两个组件必须真的传给发送函数——
            # 早期版本在这里构造了 chain 却发的是纯文本，@ 与引用回复静默失效。
            prefix: list = []
            if replyToMessageId and sent == 0:
                prefix.append(Reply(id=str(replyToMessageId)))
            if atUserId and sent == 0:
                prefix.append(At(name="", qq=str(atUserId)))
            if await self._send_text(event, text, prefix=prefix or None):
                sent += 1
        self._sent_this_turn[umo] = sent > 0 or self._sent_this_turn.get(umo, False)
        return f"已发送 {sent} 条。" if sent else "发送失败。"

    @filter.llm_tool(name="qq_reply")
    async def qq_reply(self, event: AstrMessageEvent, text: str, messageId: str = "") -> str:
        """引用某条消息回复。要针对较早的消息、或同时面对不同的人时用它区分对象。

        Args:
            text(string): 回复内容。
            messageId(string): 要引用的消息 id，从消息快照里取。
        """
        return await self.qq_send_message(event, [text], "", messageId)

    @filter.llm_tool(name="qq_dive")
    async def qq_dive(self, event: AstrMessageEvent, reason: str = "") -> str:
        """结束本轮，不说话（潜水）。不想参与、没话可接、或这轮已经聊完时用它收尾。

        Args:
            reason(string): 可选。一句话说明为什么潜水，只写进日志，不会发给群友。
        """
        if event is not None and reason:
            logger.debug(f"[qq-bridge] 潜水：{reason}")
        # 记下「本轮是主动选择不说话」，否则兜底发言会把模型的思考当回复发出去
        umo = self._turn_umo(event)
        if umo:
            self._dived_this_turn[umo] = True
        # 措辞要硬：模型看到「已结束」后若还继续调工具，本轮不会真的结束，
        # 会话锁会一直被占住，这段时间里用户新发的消息只能排队（旧版则直接丢）。
        return (
            "已收尾，本轮结束。注意：本轮之后调用的发言/等待工具都会被忽略、不会发出去，"
            "所以不要再调用任何工具，直接给出结束语即可。"
        )

    @filter.llm_tool(name="qq_wait_for_messages")
    async def qq_wait_for_messages(
        self,
        event: AstrMessageEvent,
        timeoutMs: int = 30000,
        quietMs: int = 8000,
        purpose: str = "messages",
        speakerIds: list = None,
    ) -> str:
        """等待新消息。对方明显没说完、或刚回复完想看有没有下文时用它。

        Args:
            timeoutMs(number): 最长等待毫秒数，默认 30000，上限由管理员配置。
            quietMs(number): 静默毫秒数。新消息之后安静这么久就返回。
            purpose(string): 用途。messages=等下文（默认）；reply=首答前确认对方说完，已安静足够会立即返回。
            speakerIds(array[string]): 可选。只等这几个 QQ 号发言，普通潜水不要传。
        """
        if not self._turn_umo(event):
            return "拿不到会话信息，操作取消。"
        closed = self._closed_this_turn(self._turn_umo(event), "等待")
        if closed:
            return closed
        denied = self._guard(event)
        if denied:
            return denied
        max_ms = int(self._cfg("wait.maxMs", 600000) or 600000)
        timeout = max(1000, min(int(timeoutMs or 30000), max_ms))
        quiet = max(0, min(int(quietMs or 8000), timeout))
        umo = self._turn_umo(event)
        records, timed_out, quiet_ok = await self.inbox.wait(
            umo,
            timeout_ms=timeout,
            quiet_ms=quiet,
            speaker_ids=[str(s) for s in (speakerIds or [])],
            purpose="reply" if str(purpose) == "reply" else "messages",
        )
        if records:
            # 模型已经看到这些消息了 → 推进水位，回合结束时不再为它们补跑（否则会回第二遍）
            top = max(r.seq for r in records)
            if top > self._turn_seen_seq.get(umo, 0):
                self._turn_seen_seq[umo] = top
        if not records:
            if timed_out:
                return f"等了 {timeout / 1000:.0f} 秒没有新消息（不是错误，继续做你的事）。"
            return "已经安静足够，对方大概说完了。"
        body = "\n".join(r.line() for r in records[-20:])
        tail = "（等待超时，但期间有新消息）" if timed_out else "（对方已安静，可能说完了）"
        return f"等到 {len(records)} 条新消息{tail}：\n{body}"

    @filter.llm_tool(name="qq_get_recent_messages")
    async def qq_get_recent_messages(self, event: AstrMessageEvent, limit: int = 20) -> str:
        """查看最近的聊天记录（含每条消息的 id 和发言人 QQ 号）。

        Args:
            limit(number): 取最近多少条，默认 20，上限 100。
        """
        if not self._turn_umo(event):
            return "拿不到会话信息，操作取消。"
        denied = self._guard(event)
        if denied:
            return denied
        umo = self._turn_umo(event)
        snapshot = self.inbox.snapshot(umo, limit=max(1, min(int(limit or 20), 100)))
        # 模型已经看到最近的记录了 → 推进「已看过」水位。不推的话，本轮中途到达的新消息
        # 在回合结束时还会被当成「上一轮没看到」补跑一轮，同一条被回第二遍。
        self._mark_seen(umo, self.inbox.last_seq(umo))
        return snapshot or "没有可用的历史消息。"

    @filter.llm_tool(name="qq_get_active_members")
    async def qq_get_active_members(self, event: AstrMessageEvent, limit: int = 10) -> str:
        """查看最近说过话的人（含 QQ 号），用来取 @ 或等待用的 userId。

        Args:
            limit(number): 最多返回几个人，默认 10。
        """
        if not self._turn_umo(event):
            return "拿不到会话信息，操作取消。"
        members = self.inbox.active_members(self._turn_umo(event), limit=max(1, min(int(limit or 10), 30)))
        if not members:
            return "最近没人说话。"
        return "\n".join(f"{m['name'] or '未知'}({m['userId']})｜发言 {m['count']} 次" for m in members)

    @filter.llm_tool(name="qq_get_message_images")
    async def qq_get_message_images(self, event: AstrMessageEvent, messageId: str) -> str:
        """取某条消息里的图片或表情，用于看图接梗。

        Args:
            messageId(string): 消息 id，从消息快照里取。
        """
        umo = self._turn_umo(event)
        if not umo:
            return "拿不到会话信息，操作取消。"
        record = self.inbox.find(umo, messageId)
        if not record:
            return f"找不到消息 {messageId}（只能用本会话收到过的 id）。"
        self._mark_seen(umo, record.seq)  # 看过这条了，别再为它补跑
        urls = [u for u in record.images if u]
        if not urls:
            return "这条消息里没有可取的图片。"
        # 主动回合（event=None）没有 event.send 可用，只能返回 URL 让模型自己"看"描述
        if event is None:
            preview = "\n".join(f"- {u}" for u in urls[:3])
            return f"图片 URL（主动回合无法直接送进上下文，按链接自行判断）：\n{preview}"
        from astrbot.api.event import MessageChain
        for url in urls[:3]:
            try:
                chain = MessageChain(chain=[Image.fromURL(url)])
                await event.send(chain)
            except Exception as exc:
                return f"取图失败：{exc}"
        return f"已把 {len(urls[:3])} 张图片放进上下文。"

    @filter.llm_tool(name="qq_get_forward_msg")
    async def qq_get_forward_msg(self, event: AstrMessageEvent, messageId: str) -> str:
        """查看合并转发消息的内容。

        Args:
            messageId(string): 转发消息的 id，从消息快照里的 [转发消息 id=...] 取。
        """
        umo = self._turn_umo(event)
        if not umo:
            return "拿不到会话信息，操作取消。"
        record = self.inbox.find(umo, messageId)
        if not record or not record.forward_ids:
            return f"找不到转发消息 {messageId}（只查本会话确实收到过的 id）。"
        self._mark_seen(umo, record.seq)
        return (
            "转发消息 id="
            + ",".join(record.forward_ids)
            + "：当前 AstrBot 适配器没有提供转发消息正文接口，无法展开内容。"
        )

    @filter.llm_tool(name="qq_send_poke")
    async def qq_send_poke(self, event: AstrMessageEvent, targetUserId: str = "") -> str:
        """拍一拍某人。回应 [拍一拍]、偶尔逗熟人时用，别频繁拍。

        Args:
            targetUserId(string): 要拍的 QQ 号；私聊可省略（默认拍对方）。
        """
        from astrbot.api.event import MessageChain
        from astrbot.api.message_components import Poke

        umo = self._turn_umo(event)
        if not umo:
            return "拿不到会话信息，操作取消。"
        closed = self._closed_this_turn(umo, "拍一拍")
        if closed:
            return closed
        denied = self._guard(event)
        if denied:
            return denied
        # 主动回合没有 event.get_sender_id()，必须显式传 targetUserId
        if event is not None:
            target = str(targetUserId or event.get_sender_id() or "")
        else:
            target = str(targetUserId or "")
        if not target:
            return (
                "拿不到要拍的 QQ 号（主动回合必须显式传 targetUserId，不能默认拍对方）。"
                if event is None
                else "拿不到要拍的 QQ 号。"
            )
        try:
            chain = MessageChain(chain=[Poke(id=target)])
            await self._send_chain(event, umo, chain)
            self._mark_spoken(umo)
            return "已拍一拍。"
        except Exception as exc:
            return f"拍一拍失败：{exc}"

    @filter.llm_tool(name="qq_list_stickers")
    async def qq_list_stickers(self, event: AstrMessageEvent, query: str = "", limit: int = 20) -> str:
        """查看表情包收藏。选图前先看这里，按备注和标签挑与语境相符的。

        Args:
            query(string): 可选。按关键词过滤备注/标签。
            limit(number): 最多返回几个，默认 20。
        """
        if not bool(self._cfg("stickers.enabled", True)):
            return "表情功能已关闭。"
        return self.stickers.format_list(query, max(1, min(int(limit or 20), 50)))

    @filter.llm_tool(name="qq_send_sticker")
    async def qq_send_sticker(self, event: AstrMessageEvent, stickerId: str) -> str:
        """发一个表情包。每条只能一张，不能附带文字；文字请另外用 qq_send_message。

        Args:
            stickerId(string): 表情 id，从 qq_list_stickers 里取。
        """
        from astrbot.api.event import MessageChain

        umo = self._turn_umo(event)
        if not umo:
            return "拿不到会话信息，操作取消。"
        closed = self._closed_this_turn(umo, "发表情")
        if closed:
            return closed
        denied = self._guard(event)
        if denied:
            return denied
        if not bool(self._cfg("stickers.enabled", True)):
            return "表情功能已关闭。"
        entry = self.stickers.find(stickerId)
        if not entry:
            return f"收藏里没有 id={stickerId} 的表情。"
        try:
            if entry.get("url"):
                chain = MessageChain(chain=[Image.fromURL(entry["url"])])
            else:
                chain = MessageChain(chain=[Image.fromFileSystem(entry["path"])])
            await self._send_chain(event, umo, chain)
        except Exception as exc:
            return f"发送失败：{exc}"
        self._mark_spoken(umo)
        self.stickers.mark_used(stickerId)
        self.stickers.save()
        return "已发送表情。"

    @filter.llm_tool(name="qq_sticker_note")
    async def qq_sticker_note(
        self, event: AstrMessageEvent, stickerId: str, note: str, tags: list = None
    ) -> str:
        """给表情写备注和标签，方便以后按语境挑图。

        Args:
            stickerId(string): 表情 id。
            note(string): 这个表情表达什么情绪、适合什么场合。
            tags(array[string]): 可选。标签，例如 ["无语", "吐槽"]。
        """
        entry = self.stickers.note(stickerId, note=note, tags=[str(t) for t in (tags or [])])
        if not entry:
            return f"收藏里没有 id={stickerId} 的表情。"
        self.stickers.save()
        return "已记录备注。"

    @filter.llm_tool(name="qq_collect_sticker")
    async def qq_collect_sticker(self, event: AstrMessageEvent, messageId: str, note: str = "") -> str:
        """收藏别人发的图片/表情，以后可以自己发。别频繁收藏。

        Args:
            messageId(string): 含图片的消息 id，从消息快照里取。
            note(string): 可选。简短备注，说明这图什么情绪、什么时候用。
        """
        if not self._turn_umo(event):
            return "拿不到会话信息，操作取消。"
        if not bool(self._cfg("stickers.collectEnabled", True)):
            return "管理员关闭了收藏功能。"
        record = self.inbox.find(self._turn_umo(event), messageId)
        if not record:
            return f"找不到消息 {messageId}。"
        urls = [u for u in record.images if u]
        if not urls:
            return "这条消息里没有可收藏的图片。"
        entry = self.stickers.add(url=urls[0], desc=record.text[:80], note=note)
        if not entry:
            return "收藏失败。"
        self.stickers.save()
        return f"已收藏，id={entry['id']}。"

    @filter.llm_tool(name="qq_list_voices")
    async def qq_list_voices(self, event: AstrMessageEvent, limit: int = 20) -> str:
        """查看语音库。语音默认关闭，报「未启用」是正常状态，不要重试。

        Args:
            limit(number): 最多返回几个，默认 20。
        """
        if not bool(self._cfg("voice.enabled", False)):
            return "语音功能已关闭。"
        items = list_voices(self.data_dir / "voices", limit=max(1, min(int(limit or 20), 50)))
        if not items:
            return f"语音库是空的。把 wav 文件放进 {self.data_dir / 'voices'} 即可。"
        return "\n".join(f"{i['id']}｜{i['size'] / 1024:.0f} KB" for i in items)

    @filter.llm_tool(name="qq_send_voice")
    async def qq_send_voice(self, event: AstrMessageEvent, voiceId: str) -> str:
        """把语音库里的音频当语音条发出去。默认关闭；偶尔一条，别连着发。

        Args:
            voiceId(string): 语音 id，从 qq_list_voices 里取。
        """
        from astrbot.api.event import MessageChain
        from astrbot.api.message_components import Record  # noqa: F401  （fromFileSystem 用到）

        umo = self._turn_umo(event)
        if not umo:
            return "拿不到会话信息，操作取消。"
        closed = self._closed_this_turn(umo, "发语音")
        if closed:
            return closed
        denied = self._guard(event)
        if denied:
            return denied
        if not bool(self._cfg("voice.enabled", False)):
            return "语音功能未启用。"
        item = resolve_voice(self.data_dir / "voices", voiceId)
        if not item:
            return f"语音库里没有 {voiceId}。"
        ok, hint = check_voice_limits(item, int(self._cfg("voice.maxBytes", 20971520) or 20971520))
        if not ok:
            return hint
        allowed, reason = self.voice_limiter.allow()
        if not allowed:
            return reason
        try:
            chain = MessageChain(chain=[Record.fromFileSystem(item["path"])])
            await self._send_chain(event, umo, chain)
        except Exception as exc:
            return f"发送失败：{exc}"
        self._mark_spoken(umo)
        self.voice_limiter.mark()
        return "已发送语音。" + (f"（{hint}）" if hint else "")

    @filter.llm_tool(name="qq_slang_query")
    async def qq_slang_query(self, event: AstrMessageEvent, term: str) -> str:
        """查一个词是不是群里的黑话、什么意思。不确定的词先查再用。

        Args:
            term(string): 要查的词。
        """
        if not bool(self._cfg("slang.enabled", True)):
            return "黑话功能已关闭。"
        entry = self.slang.find(term)
        if not entry:
            return f"「{term}」还没收录。如果它确实经常出现，可以用 qq_slang_submit 提交。"
        status = "已确认" if entry["status"] == STATUS_CONFIRMED else "待确认"
        text = f"「{entry['content']}」（{status}，出现 {entry['count']} 次）：{entry['meaning'] or '暂无释义'}"
        if entry.get("usage"):
            text += f"\n用法：{entry['usage']}"
        if entry.get("risk"):
            text += f"\n注意：{entry['risk']}"
        return text

    @filter.llm_tool(name="qq_slang_submit")
    async def qq_slang_submit(
        self, event: AstrMessageEvent, term: str, meaning: str, usage: str = ""
    ) -> str:
        """提交一个没收录的黑话候选，等管理员确认后才生效。同一个词不要重复提交。

        Args:
            term(string): 词本身。
            meaning(string): 你理解的含义。
            usage(string): 可选。什么语境下用。
        """
        if not bool(self._cfg("slang.enabled", True)):
            return "黑话功能已关闭。"
        existing = self.slang.find(term)
        entry = self.slang.upsert(
            term,
            {
                "meaning": meaning,
                "usage": usage,
                "status": STATUS_CANDIDATE,
                "source": "ai",
                "countIncrement": 1,
            },
        )
        if not entry:
            return "词条不合法。"
        self.slang.save()
        if existing:
            return f"「{term}」已在库中（出现 {entry['count']} 次），未重复新增。"
        return f"已提交候选「{term}」，等管理员用 /qq 黑话确认 {term} 启用。"

    @filter.llm_tool(name="qq_memory_append")
    async def qq_memory_append(self, event: AstrMessageEvent, text: str) -> str:
        """记一条轻量记忆：进行中的话题、想说未说的话、对某个群友的印象。别逐条记笔记。

        Args:
            text(string): 要记的内容，一句话。
        """
        if not bool(self._cfg("memory.enabled", True)):
            return "记忆功能已关闭。"
        umo = self._turn_umo(event) or "global"
        self._memory_append(umo, text)
        return "已记住。"

    @filter.llm_tool(name="qq_memory_query")
    async def qq_memory_query(self, event: AstrMessageEvent, keyword: str = "") -> str:
        """翻自己的记忆。想捡起之前的话题、确认某人喜好时用。

        Args:
            keyword(string): 可选。按关键词过滤。
        """
        umo = self._turn_umo(event)
        data = self._memory_all()
        items = [
            i
            for i in (list(data.get(umo) or []) + list(data.get("global") or []))
            if isinstance(i, dict)
        ]
        if not items:
            # 如实区分「压根没记过」和「功能被关了」，否则模型会以为记忆丢了
            if not bool(self._cfg("memory.enabled", True)):
                return "记忆功能已关闭（配置 memory.enabled = false），所以没有记忆。"
            return "还没有任何记忆（本会话与全局都为空）。要记东西请用 qq_memory_append。"
        if keyword:
            matched = [i for i in items if str(keyword) in str(i.get("text", ""))]
            if not matched:
                return f"没有匹配「{keyword}」的记忆（现有 {len(items)} 条）。"
            items = matched
        return "\n".join(f"- {i.get('text', '')}" for i in items[-20:])

    @filter.llm_tool(name="qq_token_usage")
    async def qq_token_usage(self, event: AstrMessageEvent) -> str:
        """查看本会话累计消耗的 token 数（取自 AstrBot 自带的用量统计）。

        没有参数。
        """
        return await self._usage_text(self._turn_umo(event))

    # ══════════════════════════════════════════════════════════════════════════
    #  生命周期
    # ══════════════════════════════════════════════════════════════════════════
    async def initialize(self) -> None:
        """插件加载完成后由 AstrBot 调用（此时有事件循环），在这里起记忆查看页。"""
        try:
            await self._start_memview()
        except Exception as exc:  # 查看页起不来不能影响插件本体
            logger.warning(f"[qq-bridge] 记忆查看页启动失败：{exc}")
        # 用量统计能不能写，取决于拿不拿得到 AstrBot 的数据库句柄。
        # 这里先探一次并写进日志：启动时就能看出「面板上会不会有数据」。
        if bool(self._cfg("usage.enabled", True)):
            if self._db() is None:
                logger.warning(
                    "[qq-bridge] 拿不到 AstrBot 数据库句柄，token 用量无法写进统计（WebUI 数据与日志 → 统计 里不会有本插件的数据）"
                )
            else:
                logger.info("[qq-bridge] 用量统计已接入 AstrBot 数据库（provider_stats）")

    async def terminate(self):
        """插件被卸载/停用时保存数据并停掉后台任务。"""
        try:
            await self.memview.stop()
        except Exception as exc:
            logger.debug(f"[qq-bridge] 关闭记忆查看页失败：{exc}")
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        try:
            self.slang.save()
            self.stickers.save()
        except Exception as exc:
            logger.warning(f"[qq-bridge] 退出时保存失败：{exc}")
        logger.info("[qq-bridge] 已卸载。")


def MessageChainWithImage(url: str):
    """把一张图片放进消息链（供 qq_get_message_images 使用）。"""
    from astrbot.api.event import MessageChain

    return MessageChain(chain=[Image.fromURL(url)])
