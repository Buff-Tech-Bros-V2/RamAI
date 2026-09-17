"""
Explanation layer (PRD section 8.4 / `explain_recommendation` tool).

`DummyLLMExplainer` is a PLACEHOLDER: it fills a deterministic template
using ONLY numbers already present in the `ExplanationPacket`, which is
exactly the constraint a real LLM must also respect (FR-A02/FR-A07: no
invented numbers, no recomputation).

TODO (teammate): replace `DummyLLMExplainer.explain` with a real LLM
call (schema-validated prompt built from the same ExplanationPacket).
Keep the same input/output contract -- `apps.dashboard` only calls
`explain(packet) -> str` and does not care how the text was produced.
"""

from abc import ABC, abstractmethod

from .dataclasses import ExplanationPacket

ACTION_LABELS = {
    "COMMIT_NOW": "commit penuh sekarang",
    "STAGED_COMMITMENT": "commit bertahap",
    "WAIT": "menunggu dulu sebelum commit",
}


class LLMExplainer(ABC):
    @abstractmethod
    def explain(self, packet: ExplanationPacket) -> str:
        """Return a human-readable explanation string."""


class DummyLLMExplainer(LLMExplainer):
    """PLACEHOLDER explanation implementation -- see module docstring."""

    def explain(self, packet: ExplanationPacket) -> str:
        decision = packet.decision
        rec = decision.recommended
        action_label = ACTION_LABELS.get(rec.action, rec.action)

        headline = f"Rekomendasi: {action_label}"
        if rec.commit_now_units:
            headline += f", {rec.commit_now_units} unit sekarang"
        if rec.commit_later_units:
            headline += f" dan {rec.commit_later_units} unit susulan"
        headline += f". Evaluasi ulang pukul {rec.reevaluate_at:%H:%M}."

        reasons = [
            f"- {decision.surge_persistence_48h * 100:.0f}% skenario menunjukkan demand "
            "kemungkinan masih di atas baseline dalam 48 jam.",
            f"- Expected contribution Rp{rec.expected_contribution:,.0f} dengan "
            f"fill rate {rec.expected_fill_rate * 100:.0f}%.",
        ]
        for alt in decision.alternatives:
            alt_label = ACTION_LABELS.get(alt.action, alt.action)
            reasons.append(
                f"- Alternatif '{alt_label}': expected contribution "
                f"Rp{alt.expected_contribution:,.0f}, lost sales "
                f"{alt.expected_lost_units:.0f} unit, residual-stock risk "
                f"{alt.residual_stock_risk_units:.0f} unit."
            )

        assumptions = "\n".join(f"- {a}" for a in decision.assumptions)

        warnings_block = ""
        if packet.warnings:
            warnings_block = "\n\nPeringatan:\n" + "\n".join(
                f"- {w}" for w in packet.warnings
            )

        return (
            f"{headline}\n\n"
            f"Mengapa:\n" + "\n".join(reasons) + "\n\n"
            f"Asumsi utama:\n{assumptions}"
            f"{warnings_block}"
        )


def get_explainer() -> LLMExplainer:
    """Factory so callers don't import the concrete placeholder directly."""
    return DummyLLMExplainer()
