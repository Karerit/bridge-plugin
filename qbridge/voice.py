"""语音库：扫描音频目录、按名字取文件、限流。

移植自 qq-bridge 的 src/voice-core.js（原项目支持响度归一化、ffmpeg 转码、
时长探测等；这里保留目录扫描 + 限流 + 大小/时长上限校验）。

注意 AstrBot 的语音消息组件（Comp.Record）目前只稳定支持 wav，其他格式需要
自行转码后再放进来。
"""

from __future__ import annotations

import time
from pathlib import Path

AUDIO_EXTS = {".wav", ".mp3", ".ogg", ".amr", ".silk", ".m4a", ".flac"}


def list_voices(directory: str | Path, limit: int = 50) -> list[dict]:
    """扫描语音目录，返回 [{id, name, path, size}]，按文件名排序。"""
    root = Path(directory)
    if not root.is_dir():
        return []
    items: list[dict] = []
    try:
        files = sorted(p for p in root.iterdir() if p.is_file())
    except OSError:
        return []
    for path in files:
        if path.suffix.lower() not in AUDIO_EXTS:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        items.append(
            {
                "id": path.stem,
                "name": path.name,
                "path": str(path),
                "size": size,
            }
        )
        if len(items) >= max(1, limit):
            break
    return items


def resolve_voice(directory: str | Path, ref: str) -> dict | None:
    """按 id（文件名去扩展名）或完整文件名找一个语音；找不到返回 None。"""
    key = str(ref or "").strip()
    if not key:
        return None
    # 只允许目录内的文件，挡住路径穿越
    if "/" in key or "\\" in key or key.startswith("."):
        return None
    root = Path(directory)
    for candidate in (root / key, root / f"{key}.wav", root / f"{key}.mp3"):
        if candidate.is_file():
            try:
                size = candidate.stat().st_size
            except OSError:
                continue
            return {"id": candidate.stem, "name": candidate.name, "path": str(candidate), "size": size}
    # 再按 stem 精确匹配一轮
    for item in list_voices(root, limit=500):
        if item["id"] == key:
            return item
    return None


class VoiceLimiter:
    """按分钟/小时限流，避免语音刷屏。"""

    def __init__(self, max_per_minute: int = 2, max_per_hour: int = 10) -> None:
        self.max_per_minute = max(0, max_per_minute)
        self.max_per_hour = max(0, max_per_hour)
        self._sent: list[float] = []

    def allow(self) -> tuple[bool, str]:
        now = time.time()
        self._sent = [t for t in self._sent if now - t < 3600]
        if self.max_per_hour and len(self._sent) >= self.max_per_hour:
            return False, f"本小时语音额度已用完（上限 {self.max_per_hour} 条）"
        if self.max_per_minute:
            recent = [t for t in self._sent if now - t < 60]
            if len(recent) >= self.max_per_minute:
                return False, f"本分钟语音额度已用完（上限 {self.max_per_minute} 条）"
        return True, ""

    def mark(self) -> None:
        self._sent.append(time.time())


def check_voice_limits(item: dict, max_bytes: int = 20 * 1024 * 1024) -> tuple[bool, str]:
    """大小校验。时长校验需要 ffprobe，本版本不做（原项目可配 ffmpegPath）。"""
    size = int(item.get("size") or 0)
    if max_bytes and size > max_bytes:
        return False, f"音频过大（{size / 1024 / 1024:.1f} MB > 上限 {max_bytes / 1024 / 1024:.1f} MB）"
    if not str(item.get("path", "")).lower().endswith(".wav"):
        return True, "提示：非 wav 格式在部分平台可能发不出去，建议先转成 wav"
    return True, ""
