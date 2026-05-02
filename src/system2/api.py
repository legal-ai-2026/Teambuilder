from __future__ import annotations

from fastapi import FastAPI, HTTPException

from . import __version__
from .models import RosterRecommendation, ScoreRequest
from .service import SelectionService


app = FastAPI(
    title="System 2 Talent Selection Engine",
    version=__version__,
    description="Ranks soldiers into mission rosters with uncertainty, fairness, and trace outputs.",
)

service = SelectionService()


@app.get("/health")
def health() -> dict[str, object]:
    return {"status": "ok", "version": __version__, "disabled": service.disabled}


@app.get("/v1/healthz")
def healthz() -> dict[str, object]:
    return health()


@app.post("/score", response_model=RosterRecommendation)
def score(request: ScoreRequest) -> RosterRecommendation:
    try:
        return service.score(request)
    except RuntimeError as exc:
        raise HTTPException(status_code=423, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/v1/score", response_model=RosterRecommendation)
def score_v1(request: ScoreRequest) -> RosterRecommendation:
    return score(request)


@app.post("/admin/disable")
def disable() -> dict[str, object]:
    service.disable()
    return {"disabled": service.disabled}


@app.post("/admin/enable")
def enable() -> dict[str, object]:
    service.enable()
    return {"disabled": service.disabled}
