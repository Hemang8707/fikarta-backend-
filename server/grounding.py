"""Figure check for assistant answers: every rupee amount must be grounded.

An amount is grounded if it appears in what the assistant was given (FACTS, USER CONTEXT, the user's own
messages) or is simple arithmetic on those: a percentage (5%..80%), a sum or a difference of two grounded
amounts. Earlier assistant replies are NOT a source: that stops a wrong figure from being repeated forever.
"""
import re
from typing import Iterable, List, Set

_AMOUNT = re.compile(r"(?:₹|\bRs\.?|\bINR)\s?(\d[\d,]*(?:\.\d+)?)\s*(lakh|lac|crore|cr|k)?", re.I)
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
_MULT = {"lakh": 100_000, "lac": 100_000, "crore": 10_000_000, "cr": 10_000_000, "k": 1000}


def amounts_in_answer(text: str) -> List[int]:
    out = []
    for m in _AMOUNT.finditer(text or ""):
        try:
            v = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        v *= _MULT.get((m.group(2) or "").lower(), 1)
        out.append(int(round(v)))
    return out


def numbers_in(sources: Iterable[str]) -> Set[int]:
    out: Set[int] = set()
    for s in sources:
        for m in _NUM.finditer(s or ""):
            try:
                v = float(m.group(0).replace(",", ""))
            except ValueError:
                continue
            if v >= 100:
                out.add(int(round(v)))
        for m in _AMOUNT.finditer(s or ""):  # '10 lakh', '18k'
            try:
                out.add(int(round(float(m.group(1).replace(",", "")) * _MULT.get((m.group(2) or "").lower(), 1))))
            except ValueError:
                pass
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(lakh|lac|crore|k)\b", s or "", re.I):
            out.add(int(round(float(m.group(1)) * _MULT[m.group(2).lower()])))
    return out


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(2.0, 0.01 * max(abs(a), abs(b)))


def ungrounded(answer: str, sources: Iterable[str]) -> List[int]:
    base = numbers_in(sources)
    if not base:
        return sorted(set(amounts_in_answer(answer)))
    pct = {b * p / 100 for b in base for p in range(10, 65, 5)} | {b / 12 for b in base} | {b * 12 for b in base}
    level1 = base | pct
    bad = []
    # an amount presented as an estimate ("around", "approx", "~", "≈", "estimated") must be a real figure, not derived
    for m in re.finditer(r"(?:≈|~|around|approx\.?|approximately|roughly|estimated?|about|lag\s?bhag|takriban|taqreeban|kar[ie]e?b(?:an)?|अनुमानित|लगभग|करीब|तकरीबन)\s*(?:₹|Rs\.?)\s?(\d[\d,]*)", answer or "", re.I):
        v = int(m.group(1).replace(",", ""))
        if not any(_close(v, x) for x in base):
            bad.append(v)
    for a in set(amounts_in_answer(answer)) - set(bad):
        if any(_close(a, x) for x in level1):
            continue
        if any(_close(a, x + y) or _close(a, abs(x - y)) for x in level1 for y in base):
            continue
        if any(_close(a, abs(x - y - z)) for x in pct for y in base for z in base):
            continue  # e.g. 40% of income minus rent minus an EMI
        bad.append(a)
    return sorted(set(bad))


def drop_lines_with(answer: str, bad: List[int]) -> str:
    """Last resort: remove the lines (bullets, table rows, sentences) that carry ungrounded amounts."""
    keep = []
    for line in answer.split("\n"):
        if any(any(_close(a, b) for b in bad) for a in amounts_in_answer(line)):
            continue
        keep.append(line)
    return "\n".join(keep).strip()
