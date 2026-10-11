"""表情包收藏库。

移植自 qq-bridge 的 src/sticker-lib.js。条目字段与原项目一致：

    id / desc（官方或识别出的描述）/ localNote（本地备注）/ tags /
    url 或 path（图片来源）/ usedCount / addedAt

和原项目的差别：原项目从 SnowLuma 的表情接口同步，这里改为「插件自己攒」——
群里出现的图片可以由 AI 主动收藏（qq_collect_sticker），也可以手工把图片放进
数据目录的 ``stickers/`` 下再执行 /qq 表情 扫描。
"""

from __future__ import annotations

import uuid

from .store import JsonStore, now_iso

STICKER_MAX_ENTRIES = 500


def _clip(value: object, limit: int = 200) -> str:
    return str(value or "").strip()[:limit]


def _new_id() -> str:
    return uuid.uuid4().hex[:10]


def normalize_entry(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    url = _clip(raw.get("url"), 500)
    path = _clip(raw.get("path"), 500)
    if not url and not path:
        return None
    tags = raw.get("tags")
    return {
        "id": str(raw.get("id") or _new_id()),
        "desc": _clip(raw.get("desc")),
        "localNote": _clip(raw.get("localNote")),
        "tags": [str(t)[:20] for t in tags][:10] if isinstance(tags, list) else [],
        "url": url,
        "path": path,
        "usedCount": max(0, int(raw.get("usedCount") or 0)),
        "addedAt": str(raw.get("addedAt") or now_iso()),
    }


class StickerStore:
    def __init__(self, path) -> None:
        self._store = JsonStore(path, default=[])
        self.entries: list[dict] = []
        self.reload()

    def reload(self) -> None:
        raw = self._store.load()
        items = raw if isinstance(raw, list) else []
        cleaned = [normalize_entry(x) for x in items]
        self.entries = [x for x in cleaned if x][:STICKER_MAX_ENTRIES]

    def save(self) -> bool:
        return self._store.save(self.entries)

    # ── 查询 ──────────────────────────────────────────────────────────────────
    def find(self, sticker_id: str) -> dict | None:
        key = _clip(sticker_id, 40)
        if not key:
            return None
        for entry in self.entries:
            if entry["id"] == key:
                return entry
        return None

    def find_by_source(self, url: str = "", path: str = "") -> dict | None:
        for entry in self.entries:
            if url and entry.get("url") == url:
                return entry
            if path and entry.get("path") == path:
                return entry
        return None

    def search(self, query: str = "", limit: int = 20) -> list[dict]:
        q = _clip(query, 50).lower()
        items = self.entries
        if q:
            def hit(entry: dict) -> bool:
                haystack = " ".join(
                    [
                        entry.get("desc", ""),
                        entry.get("localNote", ""),
                        " ".join(entry.get("tags") or []),
                        entry.get("id", ""),
                    ]
                ).lower()
                return q in haystack

            items = [e for e in items if hit(e)]
        return items[: max(0, limit)]

    def stats(self) -> dict:
        return {"total": len(self.entries)}

    # ── 写入 ──────────────────────────────────────────────────────────────────
    def add(
        self,
        *,
        url: str = "",
        path: str = "",
        desc: str = "",
        note: str = "",
        tags: list[str] | None = None,
    ) -> dict | None:
        """新增一条；同来源已存在时只补充空缺字段，不重复添加。"""
        existing = self.find_by_source(url=url, path=path)
        if existing:
            if note and not existing.get("localNote"):
                existing["localNote"] = _clip(note)
            if desc and not existing.get("desc"):
                existing["desc"] = _clip(desc)
            if tags:
                merged = list(dict.fromkeys((existing.get("tags") or []) + [str(t)[:20] for t in tags]))
                existing["tags"] = merged[:10]
            return existing
        entry = normalize_entry(
            {"url": url, "path": path, "desc": desc, "localNote": note, "tags": tags or []}
        )
        if entry:
            self.entries.append(entry)
            if len(self.entries) > STICKER_MAX_ENTRIES:
                # 超量时先淘汰「没备注又没被用过」的
                self.entries.sort(
                    key=lambda e: (
                        0 if (e.get("localNote") or e.get("desc")) else 1,
                        -(int(e.get("usedCount") or 0)),
                    )
                )
                self.entries = self.entries[:STICKER_MAX_ENTRIES]
        return entry

    def note(self, sticker_id: str, note: str = "", tags: list[str] | None = None) -> dict | None:
        entry = self.find(sticker_id)
        if not entry:
            return None
        if note:
            entry["localNote"] = _clip(note)
        if tags:
            entry["tags"] = [str(t)[:20] for t in tags][:10]
        return entry

    def mark_used(self, sticker_id: str) -> None:
        entry = self.find(sticker_id)
        if entry:
            entry["usedCount"] = int(entry.get("usedCount") or 0) + 1

    def remove(self, sticker_id: str) -> bool:
        entry = self.find(sticker_id)
        if not entry:
            return False
        self.entries = [e for e in self.entries if e["id"] != entry["id"]]
        return True

    # ── 注入 ──────────────────────────────────────────────────────────────────
    def build_context(self, max_items: int = 8) -> str:
        """给提示词用的收藏概览：优先带备注的、其次用过的。"""
        if not self.entries:
            return ""
        items = sorted(
            self.entries,
            key=lambda e: (
                0 if (e.get("localNote") or e.get("desc")) else 1,
                -(int(e.get("usedCount") or 0)),
            ),
        )
        lines = []
        for entry in items[: max(0, max_items)]:
            label = entry.get("localNote") or entry.get("desc") or "（无备注）"
            line = f"- id={entry['id']}｜{label}"
            if entry.get("tags"):
                line += f"｜标签：{'/'.join(entry['tags'])}"
            lines.append(line)
        return "\n".join(lines)

    def format_list(self, query: str = "", limit: int = 20) -> str:
        items = self.search(query, limit)
        if not items:
            return "收藏夹里没有匹配的表情包。"
        lines = []
        for entry in items:
            label = entry.get("localNote") or entry.get("desc") or "（无备注）"
            line = f"{entry['id']}｜{label}"
            if entry.get("tags"):
                line += f"｜{'/'.join(entry['tags'])}"
            lines.append(line)
        return f"共 {len(items)} 个：\n" + "\n".join(lines)
