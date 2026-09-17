# RamAI

Django MVP scaffold. Full product spec: [RamAI_PRD.md](RamAI_PRD.md).

## Quickstart

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows; use `source .venv/bin/activate` on macOS/Linux
pip install -r requirements.txt

python manage.py migrate
python manage.py seed_dummy_data   # generates 4 dummy SKUs + 90 days hourly history
python manage.py runserver
```

Open http://127.0.0.1:8000/ and pick a SKU.

## Module map

Each concern is a separate Django app so pieces can be developed and swapped independently:

| App | Responsibility | Status |
| --- | --- | --- |
| `apps/skus` | SKU, decision constraints, hourly historical data models + `seed_dummy_data` command | Real (dummy data) |
| `apps/forecasting` | `ForecastProvider` interface + `DummyForecastProvider` | **Placeholder** |
| `apps/decisionengine` | Turns a forecast into 3 candidate actions (commit now / staged / wait), scored with the contribution formula from PRD section 11 | Real logic |
| `apps/agent` | Orchestrates validate → forecast → decide → explain; `LLMExplainer` interface + `DummyLLMExplainer` | Orchestration real, explainer is **placeholder** |
| `apps/dashboard` | Views + templates (SKU selector, forecast table, decision cards, what-if form, approval) | Real, minimal UI |

## Where to plug in the real components

- **Regression/forecast model**: implement a new `ForecastProvider` in `apps/forecasting/services.py` (see `DummyForecastProvider` for the exact input/output contract: `ForecastOutput` in `apps/forecasting/dataclasses.py`), then swap it in `get_forecast_provider()`. Nothing else needs to change — `apps.decisionengine` and `apps.agent` only depend on `ForecastOutput`.
- **LLM explanation**: implement a new `LLMExplainer` in `apps/agent/explainer.py` (contract: takes an `ExplanationPacket`, returns a string — must not invent numbers, only narrate what's already in the packet), then swap it in `get_explainer()`.

## Known MVP simplifications (see PRD for full spec)

- `seed_dummy_data` is a simplified synthetic generator, not the full reproducible event-seed simulator from PRD section 10.2.
- The `generate_scenarios` tool (Monte Carlo demand trajectories) is not implemented; the decision engine consumes forecast quantiles (P10/P50/P90) directly.
- Decision engine evaluates one SKU at a time (no cross-SKU capacity allocation, PRD FR-O07, marked P1).
- No TikTok/marketplace integration — all data is synthetic, no real transactions.
