"""OS-banner helpers for Remote Notifications.

Inbox messages may quote the user request as a leading markdown block:

    > what the user asked

    [session] assistant result

Windows toasts, macOS banners, and phone system notifications should show
the result, not the quoted question. The stored inbox body keeps the quote.
"""


def strip_leading_quoted_request(message: str) -> str:
    """Drop a leading ``> ...`` quote block when a ``[label]`` body follows."""
    if not message:
        return ""
    lines = message.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    index = 0
    while index < len(lines) and _is_quote_line(lines[index]):
        index += 1
    if index == 0:
        return message
    while index < len(lines) and not lines[index].strip():
        index += 1
    remainder = "\n".join(lines[index:])
    return remainder if _has_line_start_label(remainder) else message


def _is_quote_line(line: str) -> bool:
    return line.lstrip().startswith(">")


def _has_line_start_label(message: str) -> bool:
    for line in message.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("[") and "]" in stripped[1:]:
            return True
    return False
