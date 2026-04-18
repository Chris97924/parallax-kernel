"""extract_claims() wrapper + chunk_text() helper.

This module is intentionally thin: the chunking rules migrated from the
a2a ``claim_extractor`` are useful to share across providers, but the
``extract_claims`` entry point just delegates to ``provider.extract_claims``
after an empty-text shortcut. Providers decide whether to chunk internally.
"""

from __future__ import annotations

from parallax.extract.providers.base import Provider
from parallax.extract.types import RawClaim

__all__ = [
    "EXTRACTION_PROMPT_TEMPLATE",
    "chunk_text",
    "extract_claims",
    "render_prompt",
]

_WORDS_PER_TOKEN = 0.75  # rough approximation matching the a2a heuristic

# Uses a {text} sentinel we substitute via str.replace — NOT str.format — so
# braces in user-supplied text can't raise KeyError or inject placeholders.
EXTRACTION_PROMPT_TEMPLATE = """\
從以下文字中提取所有可驗證的事實主張（claims）。

輸出純 JSON 陣列，格式如下（不要任何說明文字，只輸出 JSON）：
[
  {
    "entity": "主張所指的主題（英文，用 - 連接）",
    "claim_text": "一句話陳述這個主張",
    "polarity": 1,
    "confidence": 0.92,
    "claim_type": "feature",
    "evidence": "原文引用片段（最多 100 字）"
  }
]

規則：
- confidence > 0.8：文中明確陳述
- confidence 0.5-0.8：隱含或需推論
- confidence < 0.5：不輸出
- 每個 claim 只包含一個概念
- polarity: 1=正面/優點, -1=負面/缺點, 0=中性事實
- claim_type: feature | risk | opinion | event | causal

文字：
{text}
"""


def render_prompt(text: str) -> str:
    """Safely inject ``text`` into the extraction prompt.

    Uses :meth:`str.replace` so arbitrary braces / format specifiers in the
    caller-supplied text cannot raise ``KeyError`` or be misinterpreted as
    format placeholders.
    """
    return EXTRACTION_PROMPT_TEMPLATE.replace("{text}", text)


def chunk_text(text: str, max_tokens: int = 2000, overlap: int = 200) -> list[str]:
    """Split ``text`` into word-based overlapping chunks.

    Returns ``[text]`` unchanged if the token estimate is under ``max_tokens``.
    Raises :class:`ValueError` if ``overlap >= max_tokens`` to avoid an
    infinite loop in the sliding window.
    """
    if overlap >= max_tokens:
        raise ValueError(
            f"overlap ({overlap}) must be strictly less than "
            f"max_tokens ({max_tokens})"
        )
    words = text.split()
    max_words = int(max_tokens * _WORDS_PER_TOKEN)
    overlap_words = int(overlap * _WORDS_PER_TOKEN)

    if len(words) <= max_words:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(words):
        end = min(start + max_words, len(words))
        chunks.append(" ".join(words[start:end]))
        if end == len(words):
            break
        start = end - overlap_words

    return chunks


def extract_claims(text: str, *, provider: Provider) -> list[RawClaim]:
    """Delegate extraction to ``provider``. Empty text short-circuits to ``[]``.

    ``provider`` is duck-typed: anything with a callable ``extract_claims``
    attribute works. A non-callable attribute raises :class:`TypeError`
    here; a missing attribute raises :class:`AttributeError` from Python
    itself. We intentionally do not ``isinstance(provider, Provider)``
    because ``@runtime_checkable`` Protocols only verify attribute
    existence, not the signature — the check gave false confidence.
    """
    if not text or not text.strip():
        return []
    extract = getattr(provider, "extract_claims", None)
    if not callable(extract):
        raise TypeError(
            f"provider must expose a callable 'extract_claims'; "
            f"got {type(provider).__name__}"
        )
    return list(extract(text))
