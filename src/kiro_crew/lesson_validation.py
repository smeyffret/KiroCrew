"""Shared validation for durable lesson text."""

from __future__ import annotations

import re

from kiro_crew.model_registry import MODEL_ID_LITERAL_PATTERN

_MODEL_ID_LITERAL_RE = re.compile(MODEL_ID_LITERAL_PATTERN, re.IGNORECASE)
_VOLATILE_MODEL_FACT_RE = re.compile(
    r"\b(?:current|active)\s+model(?:\s+identity)?\s*"
    r"(?:is\b|was\b|changes?\b|shown\b|[:=])"
    rf"|\b(?:selected|session)\s+model(?:\s+identity)?\s*"
    rf"(?:is\b|was\b|[:=])\s*{MODEL_ID_LITERAL_PATTERN}"
    r"|\b(?:selected|session)\s+model\s+identity\s*(?:changes?\b|shown\b)"
    rf"|\brunning\s+as\s+(?:(?:the\s+)?(?:current|active|selected)?\s*"
    rf"(?:model|backend)\b|{MODEL_ID_LITERAL_PATTERN})",
    re.IGNORECASE,
)
_BEHAVIORAL_MODEL_PIN_RE = re.compile(
    rf"(?:\b(?:always|never|should|must)\s+(?:use|choose|select|prefer)\b"
    rf"|(?:^|[.!?]\s+|\n\s*)"
    rf"\s*(?:(?:for|when)\b[^,\n]{{0,120}},\s*)?"
    rf"(?:(?:please|kindly)\s+)?(?:do\s+)?"
    rf"(?:use|choose|select|prefer)\b(?!\s+of\b))"
    rf"[^.\n]{{0,160}}{MODEL_ID_LITERAL_PATTERN}",
    re.IGNORECASE,
)


def contains_volatile_lesson_fact(
    rule: object,
    negative: object = None,
    category: object = None,
) -> bool:
    """Whether either persisted field records volatile model identity or a pin.

    Identity assertions and behavioral imperatives with a concrete ID are volatile in
    every category. A concrete ID mentioned in the rule remains valid only for
    ``knowledge`` facts such as compatibility statements; the NOT-clause never gets
    that exemption because its text is behavioral guidance.
    """
    rule_text = rule if isinstance(rule, str) else ""
    negative_text = negative if isinstance(negative, str) else ""
    texts = (rule_text, negative_text)
    if any(
        _VOLATILE_MODEL_FACT_RE.search(text) or _BEHAVIORAL_MODEL_PIN_RE.search(text)
        for text in texts
        if text
    ):
        return True
    if negative_text and _MODEL_ID_LITERAL_RE.search(negative_text):
        return True
    if isinstance(category, str) and category.strip().lower() == "knowledge":
        return False
    return bool(rule_text and _MODEL_ID_LITERAL_RE.search(rule_text))
