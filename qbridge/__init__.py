"""qq-bridge 的 AstrBot 移植版：内部实现包。

对外只有 main.py 里的 Star 类；这里按职责拆成若干纯逻辑模块，便于单独测试。
"""

__all__ = [
    "inbox",
    "memview",
    "prompt",
    "slang",
    "stickers",
    "store",
    "textutil",
    "usage",
    "voice",
]
