"""会话消息缓冲：快照、等待、唤醒判定。

这一层是 qq-bridge 里「会话/未读/唤醒」的对应物。原项目由桥接主动唤醒 AI 并让
AI 用工具去读未读消息；AstrBot 是事件驱动、每条消息直接送达插件，所以这里改成：

  · 插件把收到的每条消息追加进缓冲（相当于原项目的「消息快照」）；
  · 唤醒判定（wake）决定这条消息要不要交给 AI（相当于原项目的唤醒配置）；
  · qq_wait_for_messages 工具在缓冲上等待新消息（相当于原项目的等待工具）。

对应关系：
    原 qq_get_unread_messages / qq_get_recent_messages  →  snapshot()
    原 qq_get_active_members                            →  active_members()
    原 qq_get_message_detail                            →  find()
    原 qq_wait_for_messages                             →  wait()
    原 qq_mark_read（已读水位）                          →  AstrBot 无此概念，取消
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field

SNAPSHOT_MAX_RECORDS = 200
REPLAY_WINDOW_SEC = 30.0  # 判定「重复投递」的默认时间窗口（秒），可由配置覆盖


@dataclass
class MsgRecord:
    """一条消息的本地记录。"""

    seq: int
    umo: str
    message_id: str = ""
    sender_id: str = ""
    sender_name: str = ""
    group_id: str = ""
    text: str = ""
    images: list[str] = field(default_factory=list)
    is_sticker: bool = False
    reply_to_id: str = ""
    reply_to_name: str = ""
    reply_to_text: str = ""
    forward_ids: list[str] = field(default_factory=list)
    poked: bool = False
    is_self: bool = False
    is_duplicate: bool = False
    ts: float = field(default_factory=time.time)

    def line(self, self_id: str = "", is_new: bool = False) -> str:
        """压成一行提示词文本。``is_new`` 时前面加【新】，让模型分清新消息与历史消息。"""
        who = self.sender_name or self.sender_id or "未知"
        tag = f"{who}({self.sender_id})" if self.sender_id else who
        if self.is_self:
            tag += "[自己]"
        parts = [f"{'【新】' if is_new else ''}[#{self.seq} id={self.message_id or '-'}] {tag}:"]
        body = self.text.strip()
        if self.reply_to_id or self.reply_to_text:
            ref_who = self.reply_to_name or self.reply_to_id or "某条消息"
            ref_text = (self.reply_to_text or "").strip()[:40]
            parts.append(f"[引用 {ref_who}：{ref_text}]")
        if self.poked:
            parts.append("[拍一拍]")
        if body:
            parts.append(body)
        for _ in self.images:
            parts.append("[表情]" if self.is_sticker else "[图片]")
        if self.forward_ids:
            parts.append(f"[转发消息 id={','.join(self.forward_ids)}]")
        return " ".join(parts)


class _Session:
    def __init__(self) -> None:
        self.records: deque[MsgRecord] = deque(maxlen=SNAPSHOT_MAX_RECORDS)
        self.seq = 0
        self.last_incoming_ts = 0.0
        self.last_activity_ts = 0.0
        self.self_id = ""
        self.session_label = ""

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq


class Inbox:
    """按 unified_msg_origin 分桶的消息缓冲。"""

    def __init__(self, replay_window_sec: float = REPLAY_WINDOW_SEC) -> None:
        self._sessions: dict[str, _Session] = {}
        # 同一 message_id + 同一发言人 + 同一内容，在这个窗口内再次出现就判为「重复投递」。
        # 窗口不能太短：适配器重连/服务端补发的间隔经常超过几秒，超窗就会被当成新消息再回一遍。
        # 0 表示不限时间（只按 id + 发言人 + 内容判重）。
        self.replay_window_sec = max(0.0, float(replay_window_sec or 0))

    # ── 写入 ──────────────────────────────────────────────────────────────────
    def append(self, record: MsgRecord) -> MsgRecord:
        sess = self._sessions.setdefault(record.umo, _Session())
        if record.message_id and self._is_replay(sess, record):
            # 同一条消息又被投递了一次（重连/适配器重放）。重复入缓冲会让它再被唤醒一次，
            # 用户看到的就是「同一句话被回复两次以上」，所以这里标成重复、不入缓冲。
            record.is_duplicate = True
            return record
        record.seq = sess.next_seq()
        sess.records.append(record)
        now = time.time()
        sess.last_activity_ts = now
        if not record.is_self:
            sess.last_incoming_ts = now
        return record

    def _is_replay(self, sess: _Session, record: MsgRecord) -> bool:
        """这条是不是「同一条消息又被投递了一次」。

        四个条件必须同时成立：消息 id 相同、发言人相同、内容相同、时间挨得很近。
        只凭消息 id 判断太危险——万一某个平台/适配器的 id 不唯一，真正的新消息会被当成
        重复吞掉，机器人就彻底不吭声了，那比重复回复严重得多。
        """
        for old in reversed(sess.records):
            if old.message_id != record.message_id:
                continue
            if self.replay_window_sec > 0 and abs(record.ts - old.ts) > self.replay_window_sec:
                continue
            if old.sender_id == record.sender_id and old.text == record.text:
                return True
        return False

    def set_self_id(self, umo: str, self_id: str) -> None:
        if self_id:
            self._sessions.setdefault(umo, _Session()).self_id = self_id

    def set_label(self, umo: str, label: str) -> None:
        if label:
            self._sessions.setdefault(umo, _Session()).session_label = label

    # ── 读取 ──────────────────────────────────────────────────────────────────
    def _sess(self, umo: str) -> _Session | None:
        return self._sessions.get(umo)

    def recent(self, umo: str, limit: int = 20) -> list[MsgRecord]:
        sess = self._sess(umo)
        if not sess:
            return []
        items = list(sess.records)
        return items[-max(1, limit) :]

    def last_seq(self, umo: str) -> int:
        """该会话当前最大序号（0 表示还没收到过消息）。"""
        sess = self._sess(umo)
        return sess.seq if sess else 0

    def render(
        self, records: list[MsgRecord], new_after_seq: int | None = 0
    ) -> tuple[str, int]:
        """把记录渲染成提示词文本，并返回其中最大序号。

        ``new_after_seq`` 之后的消息标【新】，让模型分清「刚到的」和「已经看过/回过」的。
        传 ``None`` 表示不标（该会话还没有水位，例如本会话第一轮——那时全是历史，标了反而误导）。
        """
        if not records:
            return "", 0
        sess = self._sess(records[0].umo)
        self_id = sess.self_id if sess else ""
        text = "\n".join(
            r.line(self_id, is_new=new_after_seq is not None and r.seq > new_after_seq)
            for r in records
        )
        return text, max(r.seq for r in records)

    def snapshot(self, umo: str, limit: int = 20) -> str:
        items = self.recent(umo, limit)
        if not items:
            return ""
        sess = self._sess(umo)
        self_id = sess.self_id if sess else ""
        return "\n".join(r.line(self_id) for r in items)

    def find(self, umo: str, message_id: str) -> MsgRecord | None:
        key = str(message_id or "").strip()
        if not key:
            return None
        sess = self._sess(umo)
        if not sess:
            return None
        for record in reversed(sess.records):
            if record.message_id == key:
                return record
        return None

    def active_members(self, umo: str, limit: int = 10, window_sec: int = 1800) -> list[dict]:
        """最近 window_sec 内说过话的人，按最后发言时间倒序。"""
        sess = self._sess(umo)
        if not sess:
            return []
        since = time.time() - max(60, window_sec)
        seen: dict[str, dict] = {}
        for record in sess.records:
            if record.ts < since or record.is_self or not record.sender_id:
                continue
            entry = seen.setdefault(
                record.sender_id,
                {"userId": record.sender_id, "name": record.sender_name, "count": 0, "lastTs": record.ts},
            )
            entry["count"] += 1
            entry["lastTs"] = record.ts
            if record.sender_name:
                entry["name"] = record.sender_name
        items = sorted(seen.values(), key=lambda x: -x["lastTs"])
        return items[: max(1, limit)]

    def idle_seconds(self, umo: str) -> float:
        sess = self._sess(umo)
        if not sess or not sess.last_activity_ts:
            return float("inf")
        return max(0.0, time.time() - sess.last_activity_ts)

    def known_sessions(self) -> list[str]:
        return list(self._sessions.keys())

    # ── 等待 ──────────────────────────────────────────────────────────────────
    async def wait(
        self,
        umo: str,
        *,
        timeout_ms: int = 30000,
        quiet_ms: int = 8000,
        speaker_ids: list[str] | None = None,
        purpose: str = "messages",
    ) -> tuple[list[MsgRecord], bool, bool]:
        """等新消息。

        返回 (新消息列表, 是否超时, 是否已达静默)。

        purpose="messages"：等到新消息、且新消息之后再安静 quiet_ms 才返回；
                            一直没有新消息就等到 timeout。
        purpose="reply"   ：只要求「距最后一条来信已安静 quiet_ms」——
                            思考/读取花掉的时间也算在内，已经够安静就立即返回；
                            期间到达的新消息会一并返回。
        """
        sess = self._sess(umo)
        start_seq = sess.seq if sess else 0
        start_ts = time.time()
        deadline = start_ts + max(0, timeout_ms) / 1000.0
        quiet_sec = max(0, quiet_ms) / 1000.0
        wanted = {str(s) for s in (speaker_ids or []) if str(s).strip()}

        while True:
            sess = self._sess(umo)
            now = time.time()

            new: list[MsgRecord] = []
            if sess:
                for record in sess.records:
                    if record.seq <= start_seq:
                        continue
                    if record.is_self:
                        continue
                    if wanted and record.sender_id not in wanted:
                        continue
                    new.append(record)

            last_incoming = sess.last_incoming_ts if sess else 0.0
            quiet_ok = (now - last_incoming) >= quiet_sec if last_incoming else True

            if new and quiet_ok:
                return new, False, True
            if purpose == "reply" and not new and quiet_ok:
                return [], False, True
            if now >= deadline:
                return new, True, quiet_ok

            await asyncio.sleep(min(0.5, max(0.05, deadline - now)))


# ── 唤醒判定 ────────────────────────────────────────────────────────────────
def match_names(text: str, names: list[str]) -> bool:
    """文本里是否出现了唤醒用的名字/关键词（大小写不敏感）。"""
    low = str(text or "").lower()
    for name in names or []:
        key = str(name or "").strip().lower()
        if key and key in low:
            return True
    return False


def looks_like_question(text: str) -> bool:
    s = str(text or "").strip()
    if not s:
        return False
    if s.endswith(("?", "？")):
        return True
    return any(k in s for k in ("吗", "呢", "怎么", "为什么", "为啥", "如何", "多少", "什么", "是不是"))
