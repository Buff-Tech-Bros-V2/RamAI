"""
Explanation layer (PRD section 8.4 / `explain_recommendation` tool).

Provides AI-driven operational decision explanation for merchants:
1. `GeminiLLMExplainer`: uses Google Gemini 1.5 Flash to synthesize human-readable,
   executive-level Indonesian summaries from the `ExplanationPacket`.
2. `DummyLLMExplainer`: deterministic template fallback that works offline or when
   no API key is provided.

Adheres strictly to PRD Section 8.4 (FR-A02/FR-A07):
No invented numbers, no recomputation -- only facts from the ExplanationPacket.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path

from .dataclasses import ExplanationPacket

ACTION_LABELS = {
    "COMMIT_NOW": "commit penuh sekarang",
    "STAGED_COMMITMENT": "commit bertahap",
    "WAIT": "menunggu dulu sebelum commit",
}


def _load_env_file() -> None:
    """Load environment variables from project root .env if present."""
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if env_path.exists():
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ.setdefault(key.strip(), val.strip().strip("'\""))
        except Exception:
            pass


class LLMExplainer(ABC):
    is_llm: bool = False

    @abstractmethod
    def explain(self, packet: ExplanationPacket) -> str:
        """Return a human-readable explanation string."""


class DummyLLMExplainer(LLMExplainer):
    """Deterministic template fallback (PRD 8.4 compliance)."""

    is_llm: bool = False

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


class GeminiLLMExplainer(LLMExplainer):
    """Real LLM explanation powered by Google Gemini (1.5 Flash).

    Uses zero-dependency standard library urllib for maximum reliability.
    Falls back gracefully to DummyLLMExplainer on any network error or quota exhaustion.
    """

    is_llm: bool = True

    def __init__(self, api_key: str, model_name: str = "gemini-1.5-flash", timeout_sec: int = 10):
        self.api_key = api_key
        self.model_name = model_name
        self.timeout_sec = timeout_sec
        self.fallback = DummyLLMExplainer()

    def _build_prompt(self, packet: ExplanationPacket) -> str:
        rec = packet.decision.recommended
        action_label = ACTION_LABELS.get(rec.action, rec.action)

        alternatives_text = "\n".join(
            f"- Opsi '{ACTION_LABELS.get(alt.action, alt.action)}': Untung Bersih Rp{alt.expected_contribution:,.0f}, "
            f"Lost Sales {alt.expected_lost_units:.0f} unit, Risiko Barang Sisa {alt.residual_stock_risk_units:.0f} unit, "
            f"Modal Dibutuhkan Rp{alt.required_capital:,.0f}"
            for alt in packet.decision.alternatives
        )

        assumptions_text = "\n".join(f"- {a}" for a in packet.decision.assumptions)
        warnings_text = (
            "\n".join(f"- {w}" for w in packet.warnings) if packet.warnings else "Tidak ada catatan peringatan."
        )

        return f"""
Anda adalah asisten AI analitik bisnis cerdas untuk merchant TikTok dan e-commerce (VIRALCAST).
Tugas Anda adalah menjelaskan hasil rekomendasi keputusan secara terstruktur, percaya diri, dan mudah dipahami oleh pemilik toko dalam Bahasa Indonesia yang profesional.

DATA KEPUTUSAN TERVERIFIKASI:
- SKU / Produk: {packet.sku_id}
- Rekomendasi Terpilih: {action_label.upper()}
- Komitmen sekarang: {rec.commit_now_units} unit
- Komitmen susulan: {rec.commit_later_units} unit
- Batas waktu evaluasi ulang: pukul {rec.reevaluate_at:%H:%M} WIB
- Modal kerja yang dibutuhkan: Rp{rec.required_capital:,.0f}
- Estimasi keuntungan bersih: Rp{rec.expected_contribution:,.0f}
- Tingkat pemenuhan order (fill rate): {rec.expected_fill_rate * 100:.0f}%
- Potensi penjualan hilang (lost sales): {rec.expected_lost_units:.0f} unit
- Risiko barang sisa: {rec.residual_stock_risk_units:.0f} unit
- Kemungkinan lonjakan demand bertahan 48 jam: {packet.decision.surge_persistence_48h * 100:.0f}%
- Tingkat keyakinan sinyal data: {packet.decision.confidence}

ALTERNATIF YANG TELAH DIUJI:
{alternatives_text}

ASUMSI & BATASAN OPERASIONAL:
{assumptions_text}

CATATAN DATA / PERINGATAN:
{warnings_text}

ATURAN KETAT PENULISAN (COMPLIANCE):
1. HANYA gunakan angka dan data di atas. DILARANG membuat angka perkiraan atau asumsi biaya baru.
2. Tuliskan dengan format yang rapi:
   - Kalimat pembuka rekomendasi yang tegas.
   - Poin "Mengapa Rekomendasi Ini Paling Tepat" (sorot persistensi lonjakan dan perbandingan keuntungan vs risiko barang nyisa).
   - Ringkasan komparasi singkat terhadap opsi alternatif.
   - Poin penting waktu evaluasi ulang berikutnya sebelum batas deadline.
"""

    def explain(self, packet: ExplanationPacket) -> str:
        prompt = self._build_prompt(packet)
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model_name}:generateContent"
            f"?key={self.api_key}"
        )
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": prompt}],
                }
            ],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 800,
            },
        }

        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as response:
                if response.status == 200:
                    data = json.loads(response.read().decode("utf-8"))
                    candidates = data.get("candidates", [])
                    if candidates:
                        parts = candidates[0].get("content", {}).get("parts", [])
                        if parts and "text" in parts[0]:
                            text = parts[0]["text"].strip()
                            if text:
                                return text
        except Exception:
            # On any network timeout, invalid key, or API rate limit,
            # gracefully fall back to deterministic template
            pass

        return self.fallback.explain(packet)


def get_explainer() -> LLMExplainer:
    """Factory: returns Gemini explainer if GEMINI_API_KEY is configured, else fallback."""
    _load_env_file()
    api_key = os.environ.get("GEMINI_API_KEY")
    if api_key:
        return GeminiLLMExplainer(api_key=api_key)
    return DummyLLMExplainer()
