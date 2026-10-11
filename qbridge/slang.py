"""黑话库：从群聊里抽取候选词，管理员确认后注入提示词。

移植自 qq-bridge 的 src/slang-learner.js。数据结构与状态机保持一致：

    status = candidate   AI 抽取出来、等管理员确认（不注入提示词）
    status = confirmed   管理员确认过（注入提示词）

条目字段：id / content / meaning / usage / example / risk / sources / status /
source / evidence / count / createdAt / updatedAt。
"""

from __future__ import annotations

import json
import re
import uuid

from .store import JsonStore, now_iso

SLANG_MAX_ENTRIES = 2000
SLANG_FIELD_MAX = 200

STATUS_CANDIDATE = "candidate"
STATUS_CONFIRMED = "confirmed"
STATUS_REJECTED = "rejected"


def _clip(value: object, limit: int = SLANG_FIELD_MAX) -> str:
    s = str(value or "").strip()
    return s[:limit]


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def normalize_entry(raw: object) -> dict | None:
    """把任意来源的一条记录规整成标准结构；没有 content 的直接丢弃。"""
    if not isinstance(raw, dict):
        return None
    content = _clip(raw.get("content"), 80)
    if not content:
        return None
    sources = raw.get("sources")
    evidence = raw.get("evidence")
    status = str(raw.get("status") or STATUS_CANDIDATE)
    if status not in (STATUS_CANDIDATE, STATUS_CONFIRMED, STATUS_REJECTED):
        status = STATUS_CANDIDATE
    return {
        "id": str(raw.get("id") or _new_id()),
        "content": content,
        "meaning": _clip(raw.get("meaning")),
        "usage": _clip(raw.get("usage")),
        "example": _clip(raw.get("example"), 300),
        "risk": _clip(raw.get("risk"), 80),
        "sources": [str(s) for s in sources][:10] if isinstance(sources, list) else [],
        "status": status,
        "source": _clip(raw.get("source"), 40) or "ai",
        "evidence": evidence[:20] if isinstance(evidence, list) else [],
        "count": max(0, int(raw.get("count") or 0)),
        "createdAt": str(raw.get("createdAt") or now_iso()),
        "updatedAt": str(raw.get("updatedAt") or now_iso()),
    }


def merge_evidence(current: list, incoming: list) -> list:
    """去重合并证据列表（按 JSON 串判重）。"""
    out = list(current or [])
    seen = {json.dumps(e, ensure_ascii=False, sort_keys=True) for e in out}
    for item in incoming or []:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out[-20:]


class SlangStore:
    """黑话库的读写与查询。"""

    def __init__(self, path) -> None:
        self._store = JsonStore(path, default=[])
        self.entries: list[dict] = []
        self.reload()

    def reload(self) -> None:
        raw = self._store.load()
        items = raw if isinstance(raw, list) else raw.get("entries", []) if isinstance(raw, dict) else []
        cleaned = [normalize_entry(x) for x in items]
        self.entries = [x for x in cleaned if x][:SLANG_MAX_ENTRIES]

    def save(self) -> bool:
        self.entries = self._cap(self.entries)
        return self._store.save(self.entries)

    @staticmethod
    def _cap(entries: list[dict], max_entries: int = SLANG_MAX_ENTRIES) -> list[dict]:
        """超量时保留「确认过的优先、其次出现次数多」的条目。"""
        if len(entries) <= max_entries:
            return entries
        ordered = sorted(
            entries,
            key=lambda e: (
                0 if e.get("status") == STATUS_CONFIRMED else 1,
                -(int(e.get("count") or 0)),
            ),
        )
        return ordered[:max_entries]

    # ── 查询 ──────────────────────────────────────────────────────────────────
    def find(self, term: str) -> dict | None:
        key = _clip(term, 80)
        if not key:
            return None
        for entry in self.entries:
            if entry["content"] == key:
                return entry
        low = key.lower()
        for entry in self.entries:
            if entry["content"].lower() == low:
                return entry
        return None

    def upsert(self, content: str, patch: dict | None = None) -> dict | None:
        """新增或累加一条候选；已存在时 count+1 并合并补全字段。"""
        key = _clip(content, 80)
        if not key:
            return None
        patch = patch or {}
        existing = self.find(key)
        if existing:
            existing["count"] = int(existing.get("count") or 0) + int(
                patch.get("countIncrement", 1)
            )
            for field in ("meaning", "usage", "example", "risk"):
                if not existing.get(field) and patch.get(field):
                    existing[field] = _clip(patch[field], 300 if field == "example" else 200)
            if isinstance(patch.get("sources"), list):
                merged = set(existing.get("sources") or []) | set(patch["sources"])
                existing["sources"] = list(merged)[:10]
            if isinstance(patch.get("evidence"), list):
                existing["evidence"] = merge_evidence(
                    existing.get("evidence") or [], patch["evidence"]
                )
            existing["updatedAt"] = now_iso()
            return existing

        entry = normalize_entry(
            {
                "content": key,
                "meaning": patch.get("meaning", ""),
                "usage": patch.get("usage", ""),
                "example": patch.get("example", ""),
                "risk": patch.get("risk", ""),
                "sources": patch.get("sources", []),
                "evidence": patch.get("evidence", []),
                "status": patch.get("status", STATUS_CANDIDATE),
                "source": patch.get("source", "ai"),
                "count": int(patch.get("countIncrement", 1)),
            }
        )
        if entry:
            self.entries.append(entry)
        return entry

    def set_status(self, term: str, status: str) -> dict | None:
        entry = self.find(term)
        if not entry:
            return None
        entry["status"] = status
        entry["updatedAt"] = now_iso()
        return entry

    def remove(self, term: str) -> bool:
        entry = self.find(term)
        if not entry:
            return False
        self.entries = [e for e in self.entries if e["id"] != entry["id"]]
        return True

    def stats(self) -> dict:
        confirmed = [e for e in self.entries if e["status"] == STATUS_CONFIRMED]
        candidate = [e for e in self.entries if e["status"] == STATUS_CANDIDATE]
        return {
            "total": len(self.entries),
            "confirmed": len(confirmed),
            "candidate": len(candidate),
        }

    def list_candidates(self, limit: int = 20) -> list[dict]:
        items = [e for e in self.entries if e["status"] == STATUS_CANDIDATE]
        items.sort(key=lambda e: (-int(e.get("count") or 0), e.get("updatedAt", "")))
        return items[:limit]

    # ── 注入 ──────────────────────────────────────────────────────────────────
    def build_context(self, max_items: int = 8) -> str:
        """把已确认的黑话压成提示词片段，按出现次数取前 N 条。"""
        items = [
            e
            for e in self.entries
            if e["status"] == STATUS_CONFIRMED and e["content"] and e["meaning"]
        ]
        items.sort(key=lambda e: -(int(e.get("count") or 0)))
        lines = []
        for entry in items[: max(0, max_items)]:
            line = f"- {entry['content']}：{entry['meaning']}"
            if entry.get("usage"):
                line += f"（用法：{entry['usage']}）"
            lines.append(line)
        return "\n".join(lines)

    # ── 抽取 ──────────────────────────────────────────────────────────────────
    @staticmethod
    def build_extraction_prompt(messages: list[str]) -> str:
        """构造给模型的黑话抽取请求，要求严格返回 JSON 数组。"""
        body = "\n".join(str(m) for m in messages if str(m).strip())
        return (
            "下面是 QQ 群聊记录。请找出其中「群里在用的、非字面意思的」黑话/梗/缩写/外号，"
            "只输出严格 JSON 数组，不要解释、不要 Markdown 代码块。\n"
            "每项字段：content（词本身，≤20 字）、meaning（含义，基于真实网络用法）、"
            "usage（使用场景/语气，可空）、risk（若有冒犯性或风险则说明，可空）。\n"
            "没有就输出 []。最多 5 项。\n\n"
            f"聊天记录：\n{body}"
        )

    @staticmethod
    def parse_extraction_json(text: str) -> list[dict]:
        """从模型回复里抽出 JSON 数组；容忍前后夹带的说明文字与 ``` 围栏。"""
        raw = str(text or "").strip()
        if not raw:
            return []
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        candidates = [raw]
        start, end = raw.find("["), raw.rfind("]")
        if start >= 0 and end > start:
            candidates.append(raw[start : end + 1])
        for candidate in candidates:
            try:
                data = json.loads(candidate)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(data, list):
                continue
            out = []
            for item in data[:5]:
                entry = normalize_entry(
                    {
                        "content": (item or {}).get("content") if isinstance(item, dict) else item,
                        "meaning": (item or {}).get("meaning", "") if isinstance(item, dict) else "",
                        "usage": (item or {}).get("usage", "") if isinstance(item, dict) else "",
                        "risk": (item or {}).get("risk", "") if isinstance(item, dict) else "",
                        "source": "ai",
                    }
                )
                if entry:
                    out.append(entry)
            return out
        return []
