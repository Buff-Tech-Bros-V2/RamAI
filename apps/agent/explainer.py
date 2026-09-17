"""
Explanation layer (PRD section 8.4 / `explain_recommendation` tool).

Provides AI-driven operational decision explanation for merchants:
1. `GroqLLMExplainer`: default cloud provider. Groq's free developer tier serves
   open-weight models (gpt-oss-120b and friends) over an OpenAI-compatible API
   with far more generous free quotas than Gemini's flash tier.
2. `GeminiLLMExplainer`: alternative provider (Google Gemini flash-tier), kept for
   deployments that already hold a Gemini key.
3. `DummyLLMExplainer`: deterministic template fallback that works offline or when
   no API key is provided.

Adheres strictly to PRD Section 8.4 (FR-A02/FR-A07):
No invented numbers, no recomputation -- only facts from the ExplanationPacket.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)
from pathlib import Path

from .dataclasses import ExplanationPacket

# Plain-Indonesian phrasing, matched to the dashboard labels in
# apps/dashboard/templatetags/ramai_labels.py. A seller reading the
# explanation should never meet the word "commit".
ACTION_LABELS = {
    "COMMIT_NOW": "siapkan stok sekarang",
    "STAGED_COMMITMENT": "siapkan stok bertahap",
    "WAIT": "tunggu dulu sebelum menambah stok",
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
            headline += f" dan {rec.commit_later_units} unit menyusul"
        headline += f". Evaluasi ulang pukul {rec.reevaluate_at:%H:%M}."

        reasons = [
            f"- {decision.surge_persistence_48h * 100:.0f}% skenario menunjukkan demand "
            "kemungkinan masih di atas baseline dalam 48 jam.",
            f"- Perkiraan untung kotor Rp{rec.expected_contribution:,.0f} dengan "
            f"{rec.expected_fill_rate * 100:.0f}% permintaan terpenuhi.",
        ]
        for alt in decision.alternatives:
            alt_label = ACTION_LABELS.get(alt.action, alt.action)
            reasons.append(
                f"- Alternatif '{alt_label}': perkiraan untung kotor "
                f"Rp{alt.expected_contribution:,.0f}, penjualan berisiko hilang "
                f"{alt.expected_lost_units:.0f} unit, risiko stok tersisa "
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


class _HTTPLLMExplainer(LLMExplainer):
    """Shared prompt construction, retry loop and graceful fallback.

    Subclasses only implement `_call_once`, i.e. the provider-specific HTTP call.
    """

    is_llm: bool = True

    # HTTP statuses worth a short retry: rate limiting and transient overload.
    _RETRYABLE_STATUSES = {429, 500, 502, 503}

    def __init__(self, api_key: str, model_name: str, timeout_sec: int = 15,
                 max_retries: int = 2):
        self.api_key = api_key
        self.model_name = model_name
        self.timeout_sec = timeout_sec
        self.max_retries = max_retries
        self.fallback = DummyLLMExplainer()

    @abstractmethod
    def _call_once(self, prompt: str) -> str | None:
        """Single request attempt. Returns text, or None if no usable text came back."""

    def _post_json(self, url: str, payload: dict, headers: dict) -> dict | None:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **headers},
        )
        with urllib.request.urlopen(req, timeout=self.timeout_sec) as response:
            if response.status != 200:
                return None
            return json.loads(response.read().decode("utf-8"))

    def _build_prompt(self, packet: ExplanationPacket) -> str:
        rec = packet.decision.recommended
        action_label = ACTION_LABELS.get(rec.action, rec.action)

        alternatives_text = "\n".join(
            f"- Opsi '{ACTION_LABELS.get(alt.action, alt.action)}': Untung Bersih Rp{alt.expected_contribution:,.0f}, "
            f"Penjualan Berisiko Hilang {alt.expected_lost_units:.0f} unit, Risiko Barang Sisa {alt.residual_stock_risk_units:.0f} unit, "
            f"Modal Dibutuhkan Rp{alt.required_capital:,.0f}"
            for alt in packet.decision.alternatives
        )

        assumptions_text = "\n".join(f"- {a}" for a in packet.decision.assumptions)
        warnings_text = (
            "\n".join(f"- {w}" for w in packet.warnings) if packet.warnings else "Tidak ada catatan peringatan."
        )

        return f"""
Anda adalah RamAI, asisten AI analitik bisnis untuk merchant TikTok dan e-commerce.
Tugas Anda adalah menjelaskan hasil rekomendasi keputusan secara terstruktur, percaya diri, dan mudah dipahami oleh pemilik toko dalam Bahasa Indonesia yang profesional.

DATA KEPUTUSAN TERVERIFIKASI:
- SKU / Produk: {packet.sku_id}
- Rekomendasi Terpilih: {action_label.upper()}
- Disiapkan sekarang: {rec.commit_now_units} unit
- Tambahan menyusul: {rec.commit_later_units} unit
- Batas waktu evaluasi ulang: pukul {rec.reevaluate_at:%H:%M} WIB
- Modal kerja yang dibutuhkan: Rp{rec.required_capital:,.0f}
- Estimasi keuntungan bersih: Rp{rec.expected_contribution:,.0f}
- Permintaan yang terpenuhi: {rec.expected_fill_rate * 100:.0f}%
- Penjualan berisiko hilang: {rec.expected_lost_units:.0f} unit
- Risiko barang sisa: {rec.residual_stock_risk_units:.0f} unit
- Kemungkinan permintaan tetap ramai 48 jam ke depan: {packet.decision.surge_persistence_48h * 100:.0f}%
- Tingkat keyakinan sinyal data: {packet.decision.confidence}

ALTERNATIF YANG TELAH DIUJI:
{alternatives_text}

ASUMSI & BATASAN OPERASIONAL:
{assumptions_text}

CATATAN DATA / PERINGATAN:
{warnings_text}

ATURAN KETAT PENULISAN (COMPLIANCE):
1. HANYA gunakan angka dan data di atas. DILARANG membuat angka perkiraan atau asumsi biaya baru.
1b. Tulis untuk pemilik toko, bukan analis. DILARANG memakai istilah teknis seperti "commit", "fill rate", "lost sales", "SKU", "forecast horizon", atau "decision engine" -- pakai padanan sehari-hari (siapkan stok, permintaan terpenuhi, penjualan berisiko hilang, produk, jangka waktu, RamAI).
2. Tuliskan dengan format yang rapi:
   - Kalimat pembuka rekomendasi yang tegas.
   - Poin "Mengapa Rekomendasi Ini Paling Tepat" (sorot persistensi lonjakan dan perbandingan keuntungan vs risiko barang nyisa).
   - Ringkasan komparasi singkat terhadap opsi alternatif.
   - Poin penting waktu evaluasi ulang berikutnya sebelum batas deadline.
"""

    def explain(self, packet: ExplanationPacket) -> str:
        prompt = self._build_prompt(packet)
        provider = type(self).__name__

        attempt = 0
        while attempt <= self.max_retries:
            attempt += 1
            try:
                text = self._call_once(prompt)
                if text:
                    return text
                break  # 200 OK but no usable text -- retrying won't help
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8")[:500]
                except Exception:
                    pass
                if e.code in self._RETRYABLE_STATUSES and attempt <= self.max_retries:
                    logger.warning(
                        "%s transient error (HTTP %s), retrying (%d/%d). Body: %s",
                        provider, e.code, attempt, self.max_retries, body,
                    )
                    time.sleep(0.5 * attempt)
                    continue
                logger.warning(
                    "%s call failed (HTTP %s), falling back to template. Body: %s",
                    provider, e.code, body,
                )
                break
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt <= self.max_retries:
                    logger.warning(
                        "%s network error (%s), retrying (%d/%d).",
                        provider, e, attempt, self.max_retries,
                    )
                    time.sleep(0.5 * attempt)
                    continue
                logger.warning("%s call failed (%s: %s), falling back to template.",
                               provider, type(e).__name__, e)
                break
            except Exception as e:
                # Any unexpected error: gracefully fall back, no retry.
                logger.warning("%s call failed (%s: %s), falling back to template.",
                               provider, type(e).__name__, e)
                break

        return self.fallback.explain(packet)


class GroqLLMExplainer(_HTTPLLMExplainer):
    """Groq-hosted open-weight model via the OpenAI-compatible chat endpoint.

    Default model is `openai/gpt-oss-120b`: on Groq's free tier that allows
    30 req/min, 1,000 req/day and 200k tokens/day -- comfortably more headroom
    than Gemini's flash free tier for a per-SKU explanation workload.
    Set GROQ_MODEL to switch (e.g. `llama-3.1-8b-instant` for 14.4k req/day).
    """

    ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"

    def __init__(self, api_key: str, model_name: str = "openai/gpt-oss-120b",
                 timeout_sec: int = 20, max_retries: int = 2):
        super().__init__(api_key, model_name, timeout_sec, max_retries)

    def _call_once(self, prompt: str) -> str | None:
        payload = {
            "model": self.model_name,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_completion_tokens": 1500,
            # gpt-oss models expose a reasoning budget; keep it minimal so the
            # whole completion budget goes to the visible answer.
            "reasoning_effort": "low",
        }
        data = self._post_json(
            self.ENDPOINT, payload, {"Authorization": f"Bearer {self.api_key}"}
        )
        if not data:
            return None
        choices = data.get("choices", [])
        if not choices:
            return None
        text = (choices[0].get("message", {}).get("content") or "").strip()
        if not text:
            logger.warning(
                "Groq returned no text (finish_reason=%s).",
                choices[0].get("finish_reason"),
            )
        return text or None


class GeminiLLMExplainer(_HTTPLLMExplainer):
    """Alternative provider: Google Gemini (flash-tier)."""

    def __init__(self, api_key: str, model_name: str = "gemini-3.6-flash",
                 timeout_sec: int = 15, max_retries: int = 2):
        super().__init__(api_key, model_name, timeout_sec, max_retries)

    def _call_once(self, prompt: str) -> str | None:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model_name}:generateContent"
            f"?key={self.api_key}"
        )
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 1500,
                # Flash-tier "thinking" models otherwise spend part of the
                # output budget on hidden reasoning tokens before writing
                # the visible answer, which truncates short responses.
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        data = self._post_json(url, payload, {})
        if not data:
            return None
        candidates = data.get("candidates", [])
        if not candidates:
            return None
        parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts).strip()
        if not text:
            logger.warning(
                "Gemini returned no text (finishReason=%s).",
                candidates[0].get("finishReason"),
            )
        return text or None


def get_explainer() -> LLMExplainer:
    """Factory: pick a provider from the environment, else the offline template.

    `LLM_PROVIDER` (groq | gemini | dummy) forces a choice. Without it, Groq wins
    when `GROQ_API_KEY` is set, then Gemini via `GEMINI_API_KEY`.
    """
    _load_env_file()
    provider = os.environ.get("LLM_PROVIDER", "").strip().lower()
    groq_key = os.environ.get("GROQ_API_KEY")
    gemini_key = os.environ.get("GEMINI_API_KEY")

    if provider == "dummy":
        return DummyLLMExplainer()

    if provider == "gemini" or (not provider and not groq_key and gemini_key):
        if gemini_key:
            return GeminiLLMExplainer(
                api_key=gemini_key,
                model_name=os.environ.get("GEMINI_MODEL", "gemini-3.6-flash"),
            )
        logger.warning("LLM_PROVIDER=gemini but GEMINI_API_KEY is unset; using template fallback.")
        return DummyLLMExplainer()

    if groq_key:
        return GroqLLMExplainer(
            api_key=groq_key,
            model_name=os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"),
        )

    if provider == "groq":
        logger.warning("LLM_PROVIDER=groq but GROQ_API_KEY is unset; using template fallback.")
    return DummyLLMExplainer()
