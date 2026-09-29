"""
Sanitize and enforce Factory Briefing response-length rules.

Preserves {keyword} highlight markers used by the frontend.
"""
from __future__ import annotations

import re

MAX_PARAGRAPHS = 4
MAX_WORDS = 300
FALLBACK_BRIEFING = (
    "Unable to generate a detailed factory briefing right now. "
    "Please try opening Trooba AI again in a moment. "
    "If the problem continues, confirm the model has been calculated successfully "
    "and try again after the next simulation run."
)

_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_BULLET_RE = re.compile(r"^\s*([-*•]|\d+[.)])\s+", re.MULTILINE)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_CODE_RE = re.compile(r"`([^`]+)`")
_MULTI_NL_RE = re.compile(r"\n{3,}")
_SENTENCE_END_RE = re.compile(r"[.!?…][\"')\]]*\s+")


def count_words(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text or ""))


def strip_formatting(text: str) -> str:
    """Remove markdown-style formatting while keeping {keyword} markers."""
    if not text:
        return ""
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _HEADING_RE.sub("", cleaned)
    cleaned = _BULLET_RE.sub("", cleaned)
    cleaned = _BOLD_RE.sub(r"\1", cleaned)
    cleaned = _ITALIC_RE.sub(r"\1", cleaned)
    cleaned = _CODE_RE.sub(r"\1", cleaned)
    cleaned = cleaned.replace("|", " ")
    cleaned = _MULTI_NL_RE.sub("\n\n", cleaned)
    return cleaned.strip()


def split_paragraphs(text: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    if len(parts) <= 1:
        # Fallback: split on single newlines when the model ignored blank lines.
        lines = [ln.strip() for ln in (text or "").split("\n") if ln.strip()]
        if len(lines) > 1 and len(parts) <= 1:
            return lines
    return parts


def truncate_at_sentence(text: str, max_words: int = MAX_WORDS) -> str:
    """Trim to max_words without cutting mid-sentence when possible."""
    text = (text or "").strip()
    if not text or count_words(text) <= max_words:
        return text

    words = re.findall(r"\S+|\s+", text)
    kept: list[str] = []
    word_count = 0
    for token in words:
        if re.match(r"\S+", token):
            if word_count >= max_words:
                break
            word_count += 1
        kept.append(token)
    truncated = "".join(kept).strip()

    # Prefer ending on the last complete sentence inside the truncated window.
    ends = list(_SENTENCE_END_RE.finditer(truncated + " "))
    if ends:
        cut = ends[-1].end() - 1
        candidate = truncated[:cut].strip()
        if candidate and count_words(candidate) >= max(40, max_words // 3):
            return candidate
    return truncated


def sanitize_briefing(raw: str) -> tuple[str, list[str]]:
    """
    Clean AI text into at most 4 paragraphs and <= 300 words.

    Returns (briefing_text, paragraphs).
    """
    cleaned = strip_formatting(raw)
    paragraphs = split_paragraphs(cleaned)
    paragraphs = [p for p in paragraphs if p][:MAX_PARAGRAPHS]

    briefing = "\n\n".join(paragraphs).strip()
    if count_words(briefing) > MAX_WORDS:
        briefing = truncate_at_sentence(briefing, MAX_WORDS)
        paragraphs = split_paragraphs(briefing)[:MAX_PARAGRAPHS]
        briefing = "\n\n".join(paragraphs).strip()

    return briefing, paragraphs


def needs_shorten(raw: str) -> bool:
    cleaned = strip_formatting(raw)
    paragraphs = split_paragraphs(cleaned)
    if len(paragraphs) > MAX_PARAGRAPHS:
        return True
    return count_words(cleaned) > MAX_WORDS
