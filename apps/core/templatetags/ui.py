"""Small template helpers for the Phase 10 layout primitives.

`dictlookup` lets the reusable `data_table` component fetch a value
from a row dict by a column-driven key (`{{ row|dictlookup:col.key }}`).
Django's stock `{{ row.something }}` resolves attributes, then dict
keys, then list indices — but only when the key is a literal token in
the template. With a runtime variable key like `col.key`, you need a
filter.

`times` returns a range so partials can iterate N times when N is
passed in. Callers do `{% for _ in 5|times %}...{% endfor %}` instead
of having to construct list literals in the view.
"""
from __future__ import annotations

from typing import Any

from django import template

register = template.Library()


@register.filter(name="dictlookup")
def dictlookup(value: Any, key: Any) -> Any:
    """Return value[key] if value is a dict-like, else getattr(value, key, None)."""
    if value is None:
        return None
    if hasattr(value, "get"):
        try:
            return value.get(key)
        except TypeError:
            return None
    return getattr(value, str(key), None)


@register.filter(name="times")
def times(value: Any) -> range:
    """`{% for _ in 5|times %}` — render N copies of the loop body."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return range(0)
    return range(max(0, n))


@register.filter(name="cents")
def cents(value: Any) -> str:
    """render an int-cents amount as a dollars
    string with two decimal places + thousands separators.

    `12345` → `"123.45"`. `0` → `"0.00"`. `None` / non-numeric → `"0.00"`.
    Negative values keep the sign on the dollar portion. The template
    is responsible for prefixing the currency symbol so this works
    for non-USD too.
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return "0.00"
    sign = "-" if n < 0 else ""
    n_abs = abs(n)
    dollars, c = divmod(n_abs, 100)
    return f"{sign}{dollars:,}.{c:02d}"
