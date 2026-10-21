"""Highlight words as they're spoken (pure functions; nothing here touches Resolve).

Text+ mode can split each cue into one clip per word step: every step is a copy of the
user's template showing the whole cue, with the word being spoken coloured by a Fusion
Character Level Styling (CLS) run. See docs/RESOLVE_INTEGRATION.md §8 for why it is split
clips with static styling rather than one clip with keyframed styling.

- ``cue_words``: which transcript words make up a cue's text, with their character range
- ``highlight_steps``: the cue's frames cut into contiguous steps, one per word (gaps too)
- ``add_highlight_to_comp``: the CLS tool and link added to a clip's exported ``.comp`` text
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from better_captions_plugin.timing import TimelineClock

DEFAULT_HIGHLIGHT_COLOR = "#FFD400"  # a bright accent; the other words keep the template's colour
DEFAULT_HOLD_SECONDS = 0.5  # a pause up to this long keeps the previous word lit
HIGHLIGHT_TOOL = "BetterCaptionsHighlight"

# CLS property ids for the fill colour (red, green, blue; 0..1), from a hand-styled Text+
# dumped on Resolve Studio 21 (KathaCut ADR 0011). Ranges are 0-based code points, end inclusive.
CLS_RED, CLS_GREEN, CLS_BLUE = 2401, 2402, 2403

_HEX_RE = re.compile(r"^\s*#?([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\s*$")
_LOOKAHEAD = 6  # once a token matched, the next skips at most this many words (crosstalk, a dropped word)
_LONG_STRING = re.compile(r"\[(=*)\[")
_LONG_COMMENT = re.compile(r"--\[(=*)\[")


class HighlightUnsupported(Exception):
    """The template (or Resolve) can't take the highlight; the captions are placed without it."""


# ---------------------------------------------------------------------------
# Colour


def normalize_hex_color(value) -> str:
    """``"#RRGGBB"`` (upper case) from ``"#rgb"``, ``"rrggbb"``, ...; ValueError otherwise."""
    match = _HEX_RE.match(str(value or ""))
    if not match:
        raise ValueError("not a colour like #FFD400: %r" % (value,))
    digits = match.group(1)
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    return "#" + digits.upper()


def parse_hex_color(value) -> "tuple[float, float, float]":
    """``(red, green, blue)`` in Fusion's 0..1 range."""
    digits = normalize_hex_color(value)[1:]
    return tuple(round(int(digits[i : i + 2], 16) / 255.0, 6) for i in (0, 2, 4))


# ---------------------------------------------------------------------------
# Words of a cue


@dataclass(frozen=True)
class CueWord:
    text: str
    char_start: int  # code-point index into the cue text ("\n" between lines counts as one)
    char_end: int  # inclusive, as CLS ranges are
    start: float  # engine seconds
    end: float


def tokenize_cue(text: str) -> "list[tuple[str, int, int]]":
    """``(token, first, last)`` for each whitespace-separated token; ``last`` is inclusive."""
    return [(m.group(0), m.start(), m.end() - 1) for m in re.finditer(r"\S+", text)]


def _loose(text: str) -> str:
    return re.sub(r"[^\w']", "", str(text).casefold())


def _same_word(token: str, word_text: str) -> bool:
    plain = str(word_text).strip()
    if token == plain:
        return True
    loose = _loose(token)
    return bool(loose) and loose == _loose(plain)


def _match_tokens(tokens, pool) -> list:
    """For each token, the pool word it is (or None); greedy, in order."""
    matched = [None] * len(tokens)
    position = 0
    for index, (token, _first, _last) in enumerate(tokens):
        limit = len(pool) if position == 0 and not any(matched) else min(len(pool), position + _LOOKAHEAD + 1)
        for k in range(position, limit):
            if _same_word(token, pool[k].text):
                matched[index] = pool[k]
                position = k + 1
                break
    return matched


def _fill_gaps(matched, start: float, end: float) -> "list[tuple[float, float]]":
    """Times for every token: matched ones keep theirs, runs of unmatched ones share the gap evenly."""
    times = [None] * len(matched)
    index = 0
    while index < len(matched):
        word = matched[index]
        if word is not None:
            times[index] = (word.start, word.end)
            index += 1
            continue
        run_end = index
        while run_end < len(matched) and matched[run_end] is None:
            run_end += 1
        lo = times[index - 1][1] if index > 0 else start
        hi = matched[run_end].start if run_end < len(matched) else end
        hi = max(hi, lo)
        step = (hi - lo) / (run_end - index)
        for offset, k in enumerate(range(index, run_end)):
            times[k] = (lo + offset * step, lo + (offset + 1) * step)
        index = run_end
    return times


def cue_words(
    text: str, start: float, end: float, words, speaker: "str | None" = None, tolerance: float = 0.3
) -> "list[CueWord]":
    """The cue's tokens with their times, taken from the transcript's timed ``words``.

    The engine builds a cue's text by joining its words' stripped text, so the tokens are
    matched to the words around the cue in order (the cue speaker's words first, then
    anyone's: speaker fixes relabel segments only roughly). Tokens without a matching word
    share the time between their neighbours. Returns [] when no word matches at all; that
    cue is then placed as one plain clip rather than highlighted on guessed times.
    """
    tokens = tokenize_cue(text)
    if not tokens:
        return []
    window = [w for w in words if w.end > start - tolerance and w.start < end + tolerance]
    pools = [[w for w in window if w.speaker == speaker]] if speaker is not None else []
    pools.append(window)
    best_count, best = 0, None
    for pool in pools:
        matched = _match_tokens(tokens, pool)
        count = sum(1 for m in matched if m is not None)
        if count > best_count:
            best_count, best = count, matched
        if count == len(tokens):
            break
    if not best:
        return []
    times = _fill_gaps(best, start, end)
    return [CueWord(token, first, last, t0, t1) for (token, first, last), (t0, t1) in zip(tokens, times)]


# ---------------------------------------------------------------------------
# Frames


@dataclass(frozen=True)
class HighlightStep:
    record_frame: int  # absolute timeline frame
    duration: int  # frames, >= 1
    word: "CueWord | None" = None  # the word lit during this step; None for a pause

    @property
    def end_frame(self) -> int:
        return self.record_frame + self.duration


def hold_frames(clock: TimelineClock, seconds: float = DEFAULT_HOLD_SECONDS) -> int:
    return max(0, int(round(seconds * clock.fps)))


def highlight_steps(
    record_frame: int, duration: int, words, clock: TimelineClock, origin_frame: "int | None" = None, hold: int = 0
) -> "list[HighlightStep]":
    """Cut a placed cue's frames ``[record_frame, record_frame + duration)`` into steps.

    The steps are contiguous and cover the cue exactly, so the caption never blinks. Each
    word lights from its first frame until it ends; a pause of up to ``hold`` frames after
    it (or before the first word) stays on that word, a longer one is a step with no word
    lit. Words are clamped to the cue's frames (a cue trimmed to make room for the next one,
    or pushed a frame later, cuts its words too); a word wholly outside is dropped, and a
    word shorter than a frame still gets one frame when there is room.
    """
    first, last = int(record_frame), int(record_frame) + max(1, int(duration))
    spans = []  # [word, start, end)
    for word in sorted(words, key=lambda w: (w.start, w.end)):
        start = clock.frame_at(word.start, origin_frame)
        end = clock.frame_at(word.end, origin_frame)
        if start >= last or end < first or (end == first and start < first):
            continue
        start = max(start, first)
        if spans and start <= spans[-1][1]:
            start = spans[-1][1] + 1
        if start >= last:
            break
        spans.append([word, start, min(max(end, start + 1), last)])
    for index in range(len(spans) - 1):
        spans[index][2] = min(spans[index][2], spans[index + 1][1])

    steps = []  # [word, start, end)
    cursor = first
    for word, start, end in spans:
        if start > cursor:
            if start - cursor <= hold:
                if steps:
                    steps[-1][2] = start
                else:
                    start = cursor
            else:
                steps.append([None, cursor, start])
        steps.append([word, start, end])
        cursor = end
    if cursor < last:
        if steps and steps[-1][0] is not None and last - cursor <= hold:
            steps[-1][2] = last
        else:
            steps.append([None, cursor, last])
    return [HighlightStep(start, end - start, word) for word, start, end in steps]


# ---------------------------------------------------------------------------
# Character Level Styling in an exported comp


def cls_entries(char_start: int, char_end: int, rgb) -> "list[tuple[int, int, int, float]]":
    """The CLS ``Array`` entries that colour ``[char_start, char_end]`` (inclusive)."""
    red, green, blue = rgb
    return [
        (CLS_RED, char_start, char_end, red),
        (CLS_GREEN, char_start, char_end, green),
        (CLS_BLUE, char_start, char_end, blue),
    ]


def lua_string(text: str) -> str:
    """A double-quoted Lua string literal (UTF-8 kept as is, as Fusion writes it)."""
    out = ['"']
    for ch in str(text):
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 32:
            out.append("\\%03d" % ord(ch))
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _lua_number(value: float) -> str:
    text = ("%.6f" % float(value)).rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def cls_tool_block(name: str, text: str, entries) -> str:
    """A ``StyledTextCLS`` tool shaped like the one Resolve saves for hand-applied styling."""
    array = "".join(
        "\t\t\t\t\t\t{ %d, %d, %d, Value = %s },\n" % (pid, first, last, _lua_number(value))
        for pid, first, last, value in entries
    )
    return (
        "%s = StyledTextCLS {\n"
        "\t\t\tInputs = {\n"
        "\t\t\t\tCharacterLevelStyling = Input {\n"
        "\t\t\t\t\tValue = StyledText {\n"
        "\t\t\t\t\t\tArray = {\n%s\t\t\t\t\t\t},\n"
        '\t\t\t\t\t\tValue = ""\n'
        "\t\t\t\t\t},\n"
        "\t\t\t\t},\n"
        "\t\t\t\tText = Input { Value = %s, },\n"
        "\t\t\t\tTransformRotation = Input { Value = 1, },\n"
        "\t\t\t\tSoftness = Input { Value = 1, },\n"
        "\t\t\t},\n"
        "\t\t}" % (name, array, lua_string(text))
    )


def _blank_strings(text: str) -> str:
    """``text`` with the inside of string literals and comments replaced by spaces (same length),
    so braces and keys can be found with plain searches and indexes still match ``text``."""
    out = list(text)
    index, length = 0, len(text)

    def blank(start, end):
        for k in range(start, end):
            if out[k] != "\n":
                out[k] = " "

    def long_end(match):
        close = "]" + match.group(1) + "]"
        j = text.find(close, match.end())
        if j < 0:
            raise HighlightUnsupported("the exported comp has an unterminated long string")
        return j, j + len(close)

    while index < length:
        ch = text[index]
        if ch in "\"'":
            j = index + 1
            while j < length and text[j] != ch:
                j += 2 if text[j] == "\\" else 1
            if j >= length:
                raise HighlightUnsupported("the exported comp has an unterminated string")
            blank(index + 1, j)
            index = j + 1
        elif text.startswith("--", index):
            match = _LONG_COMMENT.match(text, index)
            if match:
                end = long_end(match)[1]
            else:
                end = text.find("\n", index)
                end = length if end < 0 else end
            blank(index, end)
            index = end
        elif ch == "[" and _LONG_STRING.match(text, index):
            match = _LONG_STRING.match(text, index)
            inner_end, end = long_end(match)
            blank(match.end(), inner_end)
            index = end
        else:
            index += 1
    return "".join(out)


def _matching_brace(code: str, open_index: int) -> int:
    depth = 0
    for index in range(open_index, len(code)):
        if code[index] == "{":
            depth += 1
        elif code[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    raise HighlightUnsupported("the exported comp has unbalanced braces")


def _enclosing_open(code: str, position: int) -> int:
    depth = 0
    for index in range(position - 1, -1, -1):
        if code[index] == "}":
            depth += 1
        elif code[index] == "{":
            if depth == 0:
                return index
            depth -= 1
    raise HighlightUnsupported("the Text+ tool is not inside a tools table")


def _find_entry(code: str, open_index: int, close_index: int, key: str):
    """``(key_start, value_start, value_end)`` of ``key = <value>`` directly inside the table."""
    for match in re.compile(r"(?<![\w.])%s\s*=\s*" % re.escape(key)).finditer(code, open_index + 1, close_index):
        between = code[open_index + 1 : match.start()]
        if between.count("{") != between.count("}"):
            continue
        value_start = match.end()
        brace = re.compile(r"[\w.]*\s*(?:\(\s*\)\s*)?\{").match(code, value_start)
        if brace:
            value_end = _matching_brace(code, brace.end() - 1) + 1
        else:
            stop = re.compile(r"[,}\n]").search(code, value_start)
            value_end = stop.start() if stop else close_index
        return match.start(), value_start, value_end
    return None


def add_highlight_to_comp(comp_text: str, text: str, entries, tool_name: str = HIGHLIGHT_TOOL) -> str:
    """The exported comp with a CLS tool (``text`` + ``entries``) feeding its Text+ tool's StyledText.

    Everything else in the comp (the template's font, colours, layout, animation) is left
    byte for byte. Raises HighlightUnsupported when there is no Text+ tool, or when its text
    is already driven by another tool (the template's own character styling, a Follower, an
    expression): overriding that would change the template's look.
    """
    code = _blank_strings(comp_text)
    tool = re.compile(r"(?<![\w.])([A-Za-z_]\w*)\s*=\s*TextPlus\s*\{").search(code)
    if not tool:
        raise HighlightUnsupported("the exported comp has no Text+ tool")
    tool_open = tool.end() - 1
    tool_close = _matching_brace(code, tool_open)
    tools_open = _enclosing_open(code, tool.start())

    name, number = tool_name, 2
    while re.search(r"(?<![\w.])%s\s*=" % re.escape(name), code):
        name, number = "%s%d" % (tool_name, number), number + 1
    link = 'StyledText = Input {\n\t\t\t\t\tSourceOp = %s,\n\t\t\t\t\tSource = "StyledText",\n\t\t\t\t}' % lua_string(
        name
    )

    edits = []  # (start, end, replacement)
    inputs = _find_entry(code, tool_open, tool_close, "Inputs")
    if inputs:
        inputs_open = code.index("{", inputs[1])
        inputs_close = _matching_brace(code, inputs_open)
        styled = _find_entry(code, inputs_open, inputs_close, "StyledText")
        if styled and re.search(r"\b(SourceOp|Expression)\b", code[styled[1] : styled[2]]):
            source = re.search(r"SourceOp\s*=\s*\"([^\"]*)\"", comp_text[styled[1] : styled[2]])
            raise HighlightUnsupported(
                "the template's text is driven by %s"
                % ("its own %s tool" % source.group(1) if source else "an expression")
            )
        if styled:
            edits.append((styled[0], styled[2], link))
        else:
            edits.append((inputs_open + 1, inputs_open + 1, "\n\t\t\t\t%s," % link))
    else:
        edits.append((tool_open + 1, tool_open + 1, "\n\t\t\tInputs = {\n\t\t\t\t%s,\n\t\t\t}," % link))
    edits.append((tools_open + 1, tools_open + 1, "\n\t\t%s," % cls_tool_block(name, text, entries)))

    result = comp_text
    for start, end, replacement in sorted(edits, key=lambda e: e[0], reverse=True):
        result = result[:start] + replacement + result[end:]
    return result
