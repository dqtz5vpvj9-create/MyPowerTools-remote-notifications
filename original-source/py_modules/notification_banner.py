"""Helpers for OS banners that must not lead with the quoted user request."""
from __future__ import annotations

import re

_LABEL_RE = re.compile(r"^\[([^\]]+)\]", flags=re.MULTILINE)
_UNLABELED = "(unlabeled)"
_REFERENCE_HEADINGS = {"for reference", "原文仅供参考"}


def extract_label(message: str) -> str:
    match = _LABEL_RE.search(message or "")
    return match.group(1) if match else _UNLABELED


def _is_quote_line(line: str) -> bool:
    return line.lstrip().startswith(">")


def _is_horizontal_rule(line: str) -> bool:
    trimmed = line.strip()
    return len(trimmed) >= 3 and (
        all(character == "-" for character in trimmed)
        or all(character == "*" for character in trimmed)
        or all(character == "_" for character in trimmed)
    )


def _is_reference_heading(line: str) -> bool:
    trimmed = line.strip()
    while trimmed.startswith("#"):
        trimmed = trimmed[1:].lstrip()
    trimmed = trimmed.strip("*_ ").rstrip(":：").strip()
    return trimmed.casefold() in _REFERENCE_HEADINGS or trimmed == "原文仅供参考"


def _split_trailing_reference(lines: list[str]) -> tuple[list[str], str]:
    end = len(lines)
    while end > 0 and not lines[end - 1].strip():
        end -= 1
    quote_end = end
    quote_start = end
    while quote_start > 0 and _is_quote_line(lines[quote_start - 1]):
        quote_start -= 1
    if quote_start == quote_end:
        return lines, ""

    before_quotes = quote_start
    while before_quotes > 0 and not lines[before_quotes - 1].strip():
        before_quotes -= 1
    if before_quotes == 0 or not _is_reference_heading(lines[before_quotes - 1]):
        return lines, ""

    cut = before_quotes - 1
    while cut > 0 and not lines[cut - 1].strip():
        cut -= 1
    if cut > 0 and _is_horizontal_rule(lines[cut - 1]):
        cut -= 1
        while cut > 0 and not lines[cut - 1].strip():
            cut -= 1
    quote = "\n".join(lines[quote_start:quote_end]).rstrip()
    return lines[:cut], quote


def split_quoted_request(message: str) -> tuple[str, str]:
    if not message:
        return message or "", ""
    lines = message.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    body_start = 0
    leading_quote = ""
    index = 0
    while index < len(lines) and _is_quote_line(lines[index]):
        index += 1
    if index > 0:
        after_quotes = index
        while after_quotes < len(lines) and not lines[after_quotes].strip():
            after_quotes += 1
        remainder = "\n".join(lines[after_quotes:])
        if extract_label(remainder) != _UNLABELED:
            leading_quote = "\n".join(lines[:index]).rstrip()
            body_start = after_quotes
    reply_lines, trailing_quote = _split_trailing_reference(lines[body_start:])
    reply = "\n".join(reply_lines).rstrip()
    return reply, trailing_quote or leading_quote


def strip_leading_quoted_request(message: str) -> str:
    """Return the [label] reply, dropping a leading or trailing user quote."""
    return split_quoted_request(message)[0]
