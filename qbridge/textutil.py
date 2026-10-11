"""文本处理：Markdown 转纯文本、按 QQ 习惯分条、截断。

移植自 qq-bridge 的 src/md-to-plain.js。QQ 不渲染 Markdown，模型输出里的
``**加粗**``、``# 标题``、表格线等会原样显示，所以发出去之前统一转成纯文本。
"""

from __future__ import annotations

import re

_CODE_BLOCK = re.compile(r"```[a-zA-Z0-9_+-]*\n?([\s\S]*?)```")
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD3 = re.compile(r"\*\*\*([^*]+)\*\*\*")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC = re.compile(r"\*([^*]+)\*")
_STRIKE = re.compile(r"~~([^~]+)~~")
_UNDER = re.compile(r"__([^_]+)__")
_HEADING = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_QUOTE = re.compile(r"^>\s?", re.MULTILINE)
_BULLET = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
_ORDERED = re.compile(r"^\s*\d+\.\s+", re.MULTILINE)
_TABLE_PIPE_EDGE = re.compile(r"^\s*\|", re.MULTILINE)
_TABLE_PIPE_END = re.compile(r"\|\s*$", re.MULTILINE)
_TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")
_BLANK_RUN = re.compile(r"\n{3,}")


def md_to_plain(text: str) -> str:
    """把 Markdown 压成适合直接发到 QQ 的纯文本。"""
    s = str(text or "")
    s = _CODE_BLOCK.sub(lambda m: m.group(1).rstrip("\n"), s)
    s = _INLINE_CODE.sub(r"\1", s)
    s = _IMAGE.sub(lambda m: m.group(1) or m.group(2), s)
    s = _LINK.sub(r"\1 (\2)", s)
    s = _BOLD3.sub(r"\1", s)
    s = _BOLD.sub(r"\1", s)
    s = _ITALIC.sub(r"\1", s)
    s = _STRIKE.sub(r"\1", s)
    s = _UNDER.sub(r"\1", s)
    s = _HEADING.sub("", s)
    s = _QUOTE.sub("", s)
    s = _BULLET.sub("• ", s)
    s = _ORDERED.sub(lambda m: m.group(0).strip() + " ", s)
    # 丢掉只有分隔线的表格行，再去掉两侧竖线
    s = "\n".join(
        line
        for line in s.split("\n")
        if not (_TABLE_SEP.match(line) and not re.search(r"[^\s:|-]", line))
    )
    s = _TABLE_PIPE_EDGE.sub("", s)
    s = _TABLE_PIPE_END.sub("", s)
    s = _BLANK_RUN.sub("\n\n", s)
    return s.strip()


def truncate(text: str, max_len: int, tail: bool = False) -> str:
    """截断到 max_len 个字符。tail=True 时保留尾部（用于最近消息预览）。"""
    s = str(text or "")
    if max_len <= 0 or len(s) <= max_len:
        return s
    if tail:
        return "…" + s[-(max_len - 1):]
    return s[: max_len - 1] + "…"


def split_for_qq(text: str, max_len: int = 4000) -> list[str]:
    """超长文本按段落/句子边界切成多条，避免单条超限被平台截断。

    与 qq-bridge 的 splitForQQ 行为一致：优先在换行处切，其次在中文标点处切，
    最后才硬切。
    """
    s = str(text or "").strip()
    if not s:
        return []
    if len(s) <= max_len:
        return [s]

    parts: list[str] = []
    rest = s
    while len(rest) > max_len:
        window = rest[:max_len]
        cut = -1
        for sep in ("\n\n", "\n", "。", "！", "？", "；", ".", "!", "?", ";", "，", ","):
            idx = window.rfind(sep)
            if idx > max_len * 0.5:
                cut = idx + len(sep)
                break
        if cut <= 0:
            cut = max_len
        parts.append(rest[:cut].strip())
        rest = rest[cut:].lstrip()
    if rest:
        parts.append(rest)
    return [p for p in parts if p]


def clean_model_text(text: str, max_chars: int = 500) -> str:
    """模型输出 → 可发送文本：去 Markdown、去零宽字符、按上限截断。"""
    s = md_to_plain(text)
    s = s.replace("\u200b", "").strip()
    if max_chars > 0:
        s = truncate(s, max_chars)
    return s
