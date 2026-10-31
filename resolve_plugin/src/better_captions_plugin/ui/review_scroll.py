"""Bounded-height helpers for the "review speakers" dialog (stdlib only, no UI toolkit).

With many speakers the dialog would grow taller than the screen. Tk shows the speaker rows in a
scrolled area no taller than VISIBLE_ROWS rows; UIManager (which has no scroll container) shows
them VISIBLE_ROWS at a time with Prev/Next.
"""

from __future__ import annotations

VISIBLE_ROWS = 10


def bounded_height(row_count: int, row_height: int, visible: int = VISIBLE_ROWS) -> int:
    """Pixel height of a scrolled rows area: every row up to ``visible`` rows, never more."""
    return max(0, min(row_count, visible)) * max(0, row_height)


def page_count(total: int, per_page: int = VISIBLE_ROWS) -> int:
    return max(1, -(-max(0, total) // max(1, per_page)))


def clamp_page(page: int, total: int, per_page: int = VISIBLE_ROWS) -> int:
    return max(0, min(int(page), page_count(total, per_page) - 1))


def page_bounds(page: int, total: int, per_page: int = VISIBLE_ROWS) -> tuple[int, int]:
    """(start, stop) indexes of ``page`` (clamped) into a list of ``total`` rows."""
    page = clamp_page(page, total, per_page)
    return page * per_page, min(total, (page + 1) * per_page)


def page_label(page: int, total: int, per_page: int = VISIBLE_ROWS) -> str:
    start, stop = page_bounds(page, total, per_page)
    return "Speakers %d–%d of %d" % (start + 1, stop, total) if total else "No speakers"


def wheel_units(event) -> int:
    """Scroll units for a mouse-wheel event: Windows/macOS ``delta`` or X11 buttons 4 (up) and 5 (down)."""
    num = getattr(event, "num", None)
    if num == 4:
        return -1
    if num == 5:
        return 1
    delta = getattr(event, "delta", 0) or 0
    if delta == 0:
        return 0
    # Windows reports multiples of 120; macOS small integers. Either way: one row per notch, at least one.
    return -max(1, abs(int(delta)) // 120) if delta > 0 else max(1, abs(int(delta)) // 120)
