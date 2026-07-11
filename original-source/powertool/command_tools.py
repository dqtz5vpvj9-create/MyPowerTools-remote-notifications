from __future__ import annotations


AOSP_HOST_DIR = "/home/lixr/aosp_host_working_dir/"
AOSP_HOST_URL = "http://r743.ipads-lab.se.sjtu.edu.cn:7112/"
POSTCONDITIONS_DB_RSYNC = (
    "rsync -avP r743-autodroid:/home/lxr2/repo/androidtools/AutoDroid/data/"
    "postconditions_db/ $AutoDroid/data/postconditions_db/"
)


def replace_host_directory(text: str) -> str:
    return text.replace(AOSP_HOST_DIR, AOSP_HOST_URL)


def remove_latex_comment_lines(text: str) -> str:
    return ''.join(
        line for line in text.splitlines(keepends=True)
        if not line.lstrip().startswith('%')
    )


LATEX_BLOCK_COMMANDS = {
    "part",
    "chapter",
    "section",
    "subsection",
    "subsubsection",
    "paragraph",
    "subparagraph",
    "label",
    "begin",
    "end",
    "item",
}


LATEX_STANDALONE_BLOCK_COMMANDS = {
    "emph",
    "textbf",
    "textit",
}


PLAIN_TEXT_PROTECTED_TOKENS = (
    "e.g.",
    "i.e.",
    "etc.",
    "cf.",
    "vs.",
    "Fig.",
    "Eq.",
    "Sec.",
    "Tab.",
    "Dr.",
    "Mr.",
    "Ms.",
    "Prof.",
    "et al.",
)


def _consume_latex_group(source: str, start: int) -> int:
    pairs = {"{": "}", "[": "]"}
    stack = [pairs[source[start]]]
    i = start + 1

    while i < len(source):
        ch = source[i]
        if ch == "\\":
            i += 2
        elif ch in pairs:
            stack.append(pairs[ch])
            i += 1
        elif ch == stack[-1]:
            stack.pop()
            i += 1
            if not stack:
                return i
        else:
            i += 1

    return len(source)


def _consume_latex_command(source: str, start: int) -> tuple[int, str]:
    i = start + 1
    command_name = ""

    if i < len(source) and source[i].isalpha():
        name_start = i
        while i < len(source) and source[i].isalpha():
            i += 1
        command_name = source[name_start:i]
        if i < len(source) and source[i] == "*":
            i += 1
    elif i < len(source):
        command_name = source[i]
        i += 1

    while i < len(source):
        whitespace_start = i
        while i < len(source) and source[i].isspace():
            i += 1
        if i < len(source) and source[i] in ("{", "["):
            i = _consume_latex_group(source, i)
        else:
            i = whitespace_start
            break

    return i, command_name


def _consume_latex_math(source: str, start: int) -> int:
    delimiter = "$$" if source.startswith("$$", start) else "$"
    i = start + len(delimiter)

    while i < len(source):
        if source[i] == "\\":
            i += 2
        elif source.startswith(delimiter, i):
            return i + len(delimiter)
        else:
            i += 1

    return len(source)


def _consume_latex_command_math(source: str, start: int, closer: str) -> int:
    i = start + 2

    while i < len(source):
        if source.startswith(closer, i):
            return i + len(closer)
        if source[i] == "\\":
            i += 2
        else:
            i += 1

    return len(source)


def _consume_latex_comment(source: str, start: int) -> int:
    i = start
    while i < len(source) and source[i] not in ("\r", "\n"):
        i += 1
    return i


def _is_latex_command_alone_on_line(source: str, start: int, end: int) -> bool:
    line_start = source.rfind("\n", 0, start) + 1
    next_lf = source.find("\n", end)
    next_cr = source.find("\r", end)
    line_end_candidates = [
        index for index in (next_lf, next_cr)
        if index != -1
    ]
    line_end = min(line_end_candidates) if line_end_candidates else len(source)

    return (
        not source[line_start:start].strip()
        and not source[end:line_end].strip()
    )


def _trim_output_lines(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


def _match_protected_plain_text_token(source: str, start: int) -> tuple[str, int]:
    before = source[start - 1] if start > 0 else ""
    if before and (before.isalnum() or before in "_~"):
        return "", 0

    for token in PLAIN_TEXT_PROTECTED_TOKENS:
        output: list[str] = []
        i = start
        matched = True

        for idx, token_ch in enumerate(token):
            if token_ch.isspace():
                if i >= len(source) or not source[i].isspace():
                    matched = False
                    break
                while i < len(source) and source[i].isspace():
                    i += 1
                output.append(" ")
                continue

            if i >= len(source) or source[i].lower() != token_ch.lower():
                matched = False
                break

            output.append(source[i])
            i += 1

            next_token_ch = token[idx + 1] if idx + 1 < len(token) else ""
            if next_token_ch and not next_token_ch.isspace() and token_ch == ".":
                while i < len(source) and source[i].isspace():
                    i += 1

        if not matched:
            continue

        end = i
        if end < len(source) and source[end] == ",":
            output.append(source[end])
            end += 1

        after = source[end] if end < len(source) else ""
        if after and (after.isalnum() or after in "_~"):
            continue
        return "".join(output), end - start

    return "", 0


def format_latex_comma_period_lines(text: str) -> str:
    r"""Reflow LaTeX so plain text breaks only after ',' and '.'.

    LaTeX commands and their bracketed/braced arguments are kept intact, so
    constructs such as \section{A, B.} and \textbf{A, B.} are not split.
    """
    result: list[str] = []
    pending_space = False
    i = 0

    def append_space_if_needed() -> None:
        nonlocal pending_space
        if pending_space and result and result[-1] not in (" ", "\n"):
            result.append(" ")
        pending_space = False

    def append_newline() -> None:
        while result and result[-1] == " ":
            result.pop()
        if result and result[-1] != "\n":
            result.append("\n")

    while i < len(text):
        ch = text[i]

        if ch.isspace():
            pending_space = True
            i += 1
            continue

        if ch == "%":
            end = _consume_latex_comment(text, i)
            append_space_if_needed()
            result.append(text[i:end].rstrip())
            append_newline()
            i = end
            continue

        if text.startswith("\\[", i):
            end = _consume_latex_command_math(text, i, "\\]")
            append_newline()
            result.append(_trim_output_lines(text[i:end]))
            append_newline()
            i = end
            continue

        if text.startswith("\\(", i):
            end = _consume_latex_command_math(text, i, "\\)")
            append_space_if_needed()
            result.append(text[i:end])
            i = end
            continue

        if ch == "\\":
            end, command_name = _consume_latex_command(text, i)
            token = text[i:end]

            if (
                command_name in LATEX_BLOCK_COMMANDS
                or (
                    command_name in LATEX_STANDALONE_BLOCK_COMMANDS
                    and _is_latex_command_alone_on_line(text, i, end)
                )
            ):
                append_newline()
                result.append(_trim_output_lines(token))
                append_newline()
            else:
                append_space_if_needed()
                result.append(token)
            i = end
            continue

        if ch == "$":
            end = _consume_latex_math(text, i)
            if text.startswith("$$", i):
                append_newline()
                result.append(_trim_output_lines(text[i:end]))
                append_newline()
            else:
                append_space_if_needed()
                result.append(text[i:end])
            i = end
            continue

        protected_token, protected_len = _match_protected_plain_text_token(text, i)
        if protected_token:
            append_space_if_needed()
            result.append(protected_token)
            i += protected_len
            continue

        if ch in (",", "."):
            pending_space = False
        else:
            append_space_if_needed()
        result.append(ch)
        if ch in (",", "."):
            append_newline()
            pending_space = False
        i += 1

    return _trim_output_lines("".join(result)) + ("\n" if text.endswith("\n") else "")


def add_extract_result_prefix(lines: str) -> str:
    return '\n'.join(
        'extract_result ' + line
        for line in lines.splitlines()
    )


def gen_rsync_from_folders(lines: str) -> str:
    rsync_commands = '\n'.join(
        'rsync -avP r743-autodroid:' + line.strip() + ' $aosp_host_working_dir/'
        for line in lines.strip().splitlines()
        if line.strip()
    )
    return rsync_commands + '\n' + POSTCONDITIONS_DB_RSYNC


def remove_cpp_comments(source: str) -> str:
    result: list[str] = []
    i = 0
    state = "normal"
    quote = ""

    while i < len(source):
        ch = source[i]
        next_ch = source[i + 1] if i + 1 < len(source) else ""

        if state == "normal":
            if ch in ('"', "'"):
                result.append(ch)
                quote = ch
                state = "string"
                i += 1
            elif ch == "/" and next_ch == "/":
                state = "line_comment"
                i += 2
            elif ch == "/" and next_ch == "*":
                state = "block_comment"
                i += 2
            else:
                result.append(ch)
                i += 1
        elif state == "string":
            result.append(ch)
            if ch == "\\":
                if i + 1 < len(source):
                    result.append(source[i + 1])
                    i += 2
                else:
                    i += 1
            elif ch == quote:
                state = "normal"
                i += 1
            else:
                i += 1
        elif state == "line_comment":
            if ch == "\r" and next_ch == "\n":
                result.extend(["\r", "\n"])
                state = "normal"
                i += 2
            elif ch in ("\r", "\n"):
                result.append(ch)
                state = "normal"
                i += 1
            else:
                i += 1
        elif state == "block_comment":
            if ch == "*" and next_ch == "/":
                state = "normal"
                i += 2
            elif ch == "\r" and next_ch == "\n":
                result.extend(["\r", "\n"])
                i += 2
            elif ch in ("\r", "\n"):
                result.append(ch)
                i += 1
            else:
                i += 1

    return ''.join(result)
