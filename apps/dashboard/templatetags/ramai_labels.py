"""Merchant-facing labels for the decision engine's action enum.

The engine speaks in `COMMIT_NOW` / `STAGED_COMMITMENT` / `WAIT`; a seller
reading the dashboard should never see either the enum or the word "commit".
Templates render the enum through these filters instead.
"""

from django import template

register = template.Library()

ACTION_LABELS = {
    "COMMIT_NOW": "Siapkan stok sekarang",
    "STAGED_COMMITMENT": "Siapkan bertahap",
    "WAIT": "Tunggu dulu",
}

# Same meaning, for cells too narrow for the full phrase.
ACTION_LABELS_SHORT = {
    "COMMIT_NOW": "Siapkan sekarang",
    "STAGED_COMMITMENT": "Bertahap",
    "WAIT": "Tunggu dulu",
}


@register.filter
def action_label(value):
    return ACTION_LABELS.get(str(value), "Belum tersedia")


@register.filter
def action_label_short(value):
    return ACTION_LABELS_SHORT.get(str(value), "Belum tersedia")
