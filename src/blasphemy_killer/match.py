"""Pure phrase-matching and mute-interval logic. No I/O, no ffmpeg, no whisper."""

from __future__ import annotations

import re
from dataclasses import dataclass

_NORMALIZE_RE = re.compile(r"[^a-z0-9']+")


@dataclass(frozen=True)
class Word:
    text: str  # raw whisper token text, e.g. " God-damn,"
    start: float  # seconds
    end: float


@dataclass(frozen=True)
class Match:
    phrase: str  # the configured phrase that fired
    text: str  # the actual transcript words that matched, joined
    start: float
    end: float


def normalize_word(word: str) -> str:
    """Lowercase and strip everything except letters, digits, and apostrophes."""
    return _NORMALIZE_RE.sub("", word.lower())


def normalize_phrase(phrase: str) -> str:
    """Normalize a configured phrase and squash spaces: "God damn" -> "goddamn"."""
    return "".join(normalize_word(part) for part in phrase.split())


def find_matches(words: list[Word], phrases: list[str]) -> list[Match]:
    """Find every phrase occurrence in the word stream.

    Matching is space-insensitive but anchored to word boundaries: the squashed
    phrase must equal the concatenation of consecutive normalized words exactly,
    ending at a word end. So "god damn" matches the single token "goddamn" and
    the pair "god","damn", but "christ" never matches inside "christmas".

    Matches whose span is contained within a longer match are dropped.
    """
    normalized = [(i, normalize_word(w.text)) for i, w in enumerate(words)]
    normalized = [(i, n) for i, n in normalized if n]
    targets = {normalize_phrase(p): p for p in phrases if normalize_phrase(p)}
    if not targets:
        return []
    max_len = max(len(t) for t in targets)

    matches: list[Match] = []
    for pos in range(len(normalized)):
        concat = ""
        for end_pos in range(pos, len(normalized)):
            concat += normalized[end_pos][1]
            if len(concat) > max_len:
                break
            phrase = targets.get(concat)
            if phrase is not None:
                first = words[normalized[pos][0]]
                last = words[normalized[end_pos][0]]
                text = " ".join(
                    words[normalized[k][0]].text.strip()
                    for k in range(pos, end_pos + 1)
                )
                matches.append(Match(phrase, text, first.start, last.end))

    return _drop_contained(matches)


def _drop_contained(matches: list[Match]) -> list[Match]:
    """Drop matches fully contained in another match's span (e.g. "jesus" inside "jesus christ")."""
    kept: list[Match] = []
    for m in matches:
        contained = any(
            o is not m and o.start <= m.start and m.end <= o.end
            and (o.end - o.start) > (m.end - m.start)
            for o in matches
        )
        if not contained:
            kept.append(m)
    kept.sort(key=lambda m: (m.start, m.end))
    return kept


def build_intervals(
    matches: list[Match],
    *,
    pad_before: float,
    pad_after: float,
    merge_gap: float = 0.2,
    clamp_end: float | None = None,
) -> list[tuple[float, float]]:
    """Turn matches into padded, clamped, merged (start, end) mute intervals."""
    intervals = []
    for m in matches:
        start = max(0.0, m.start - pad_before)
        end = m.end + pad_after
        if clamp_end is not None:
            end = min(end, clamp_end)
        if end > start:
            intervals.append((start, end))
    intervals.sort()

    merged: list[tuple[float, float]] = []
    for start, end in intervals:
        if merged and start - merged[-1][1] <= merge_gap:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged
