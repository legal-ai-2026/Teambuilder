# System 2 - Talent / Soldier Selection Engine

FastAPI demo service for ranking Ranger candidates into mission rosters with model disagreement, fairness checks, second choices, career forecasts, and an operator kill switch surfaced in the API.

The repository is intentionally runnable offline. The scoring layer uses deterministic surrogate TabPFN and hierarchical-Bayes adapters so the demo can run without GPU, MCMC, or LLM credentials. The adapter boundaries are isolated for replacing them with `tabpfn`, `pymc`, and Anthropic structured outputs.

## Quick Start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
uvicorn system2.api:app --reload
```

Open `http://127.0.0.1:8000/docs`.

## Demo Endpoints

- `POST /score` - ranks candidates into a 14-slot direct-action roster by default.
- `GET /health` - service health and kill-switch state.
- `POST /admin/disable` - disables scoring.
- `POST /admin/enable` - re-enables scoring for local demos.

## Example

```bash
curl -X POST http://127.0.0.1:8000/score \
  -H 'content-type: application/json' \
  -d '{"mission_id":"raid-tonight","candidate_count":80}'
```

The response includes assigned slots, second choices, TabPFN/Bayes probabilities, confidence, risk factors, fairness audit metrics, trace metadata, and one five-year career forecast.

