"""Merchant-facing labels for the decision engine's action enum.

The engine speaks in `COMMIT_NOW` / `STAGED_COMMITMENT` / `WAIT`; a seller
reading the dashboard should never see either the enum or the word "commit".
Templates render the enum through these filters instead.
"""

import re
from django import template
from django.utils.safestring import mark_safe

try:
    import markdown as md_lib
except ImportError:
    md_lib = None

try:
    import bleach
except ImportError:
    bleach = None

# Explanation text is our own template output OR an LLM provider response, so
# it is not fully trusted: strip anything Markdown didn't generate itself
# (script tags, event handlers, javascript: URLs) before mark_safe.
_ALLOWED_TAGS = [
    "h1", "h2", "h3", "h4", "p", "strong", "em", "ul", "ol", "li",
    "br", "hr", "code", "pre", "blockquote", "a",
]
_ALLOWED_ATTRS = {"a": ["href", "title", "rel"]}
_ALLOWED_PROTOCOLS = ["http", "https", "mailto"]

register = template.Library()

ACTION_LABELS = {
    "COMMIT_NOW": "Siapkan stok sekarang",
    "STAGED_COMMITMENT": "Siapkan bertahap",
    "WAIT": "Tunggu dulu",
    "NO_BUY_NEEDED": "Stok sudah cukup",
    "NO_BUY_POSSIBLE": "Belum bisa menambah stok",
    "NO_BUY_UNPROFITABLE": "Tahan dulu, belum menguntungkan",
}

# Same meaning, for cells too narrow for the full phrase.
ACTION_LABELS_SHORT = {
    "COMMIT_NOW": "Siapkan sekarang",
    "STAGED_COMMITMENT": "Bertahap",
    "WAIT": "Tunggu dulu",
    "NO_BUY_NEEDED": "Stok cukup",
    "NO_BUY_POSSIBLE": "Belum bisa tambah",
    "NO_BUY_UNPROFITABLE": "Tahan dulu",
}


@register.filter
def action_label(value):
    return ACTION_LABELS.get(str(value), "Belum tersedia")


@register.filter
def action_label_short(value):
    return ACTION_LABELS_SHORT.get(str(value), "Belum tersedia")


@register.filter(name="render_markdown")
def render_markdown(value):
    if not value:
        return ""
    text = str(value).strip()
    # Ensure lists following headers or text have a preceding newline for standard markdown parsers
    text = re.sub(r'([^\n])\n([*-] |\d+\. )', r'\1\n\n\2', text)
    if md_lib:
        html = md_lib.markdown(
            text,
            extensions=["extra", "nl2br", "sane_lists"],
        )
        if bleach:
            html = bleach.clean(
                html,
                tags=_ALLOWED_TAGS,
                attributes=_ALLOWED_ATTRS,
                protocols=_ALLOWED_PROTOCOLS,
                strip=True,
            )
        else:
            # No sanitizer available -- fail closed to escaped plain text
            # rather than risk rendering unsanitized HTML.
            from django.utils.html import escape, linebreaks
            return mark_safe(linebreaks(escape(text)))
        return mark_safe(html)
    from django.utils.html import escape, linebreaks
    return mark_safe(linebreaks(escape(text)))
