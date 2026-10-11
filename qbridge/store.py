"""JSON 持久化：原子写 + 损坏自愈。

对应 qq-bridge 里散落在各模块的 load*/save* 函数（sticker-lib.js / slang-learner.js /
token-ledger.js 各有一套）。这里统一成一个小类，行为保持一致：

- 写盘用「临时文件 + os.replace」原子替换，避免进程被杀时留下半个 JSON；
- 读到损坏文件时不抛异常，把原文件改名成 ``*.corrupt-<时间戳>`` 备份后当作空数据继续，
  这样一次磁盘问题不会让整个插件加载失败。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


def now_iso() -> str:
    """本地时区的 ISO 时间戳（秒精度），与原项目的 nowIso() 对齐。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class JsonStore:
    """一个 JSON 文件的读写封装。

    Args:
        path: 文件路径；父目录不存在会自动创建。
        default: 文件缺失或损坏时使用的默认值（会做一次浅拷贝）。
    """

    def __init__(self, path: str | Path, default: Any = None) -> None:
        self.path = Path(path)
        self._default = default

    # ── 读 ────────────────────────────────────────────────────────────────────
    def load(self) -> Any:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return self._empty()
        except OSError:
            return self._empty()
        if not text.strip():
            return self._empty()
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            self._backup_corrupt()
            return self._empty()

    def _empty(self) -> Any:
        if isinstance(self._default, dict):
            return dict(self._default)
        if isinstance(self._default, list):
            return list(self._default)
        return self._default

    def _backup_corrupt(self) -> None:
        try:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            self.path.replace(self.path.with_suffix(self.path.suffix + f".corrupt-{stamp}"))
        except OSError:
            pass

    # ── 写 ────────────────────────────────────────────────────────────────────
    def save(self, data: Any) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
            return True
        except OSError:
            return False


def ensure_dir(path: str | Path) -> Path:
    """建目录并返回 Path（失败时不抛异常，交给调用方处理）。"""
    p = Path(path)
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return p
