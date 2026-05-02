from __future__ import annotations

from .models import CandidateAssessment


def calibration_bins(
    predictions: list[float],
    outcomes: list[int],
    *,
    bin_count: int = 10,
) -> list[dict[str, float | int]]:
    bins: list[dict[str, float | int]] = []
    for idx in range(bin_count):
        lower = idx / bin_count
        upper = (idx + 1) / bin_count
        if idx == bin_count - 1:
            selected = [i for i, p in enumerate(predictions) if lower <= p <= upper]
        else:
            selected = [i for i, p in enumerate(predictions) if lower <= p < upper]
        count = len(selected)
        mean_predicted = sum(predictions[i] for i in selected) / count if count else 0.0
        observed_rate = sum(outcomes[i] for i in selected) / count if count else 0.0
        bins.append(
            {
                "bin": idx,
                "lower": lower,
                "upper": upper,
                "count": count,
                "mean_predicted": float(mean_predicted),
                "observed_rate": float(observed_rate),
            }
        )
    return bins


def disagreement_histogram(
    assessments: list[CandidateAssessment],
    *,
    bucket_size: float = 0.05,
    max_value: float = 0.5,
) -> list[dict[str, float | int]]:
    bucket_count = int(max_value / bucket_size)
    histogram: list[dict[str, float | int]] = []
    for idx in range(bucket_count):
        lower = idx * bucket_size
        upper = lower + bucket_size
        if idx == bucket_count - 1:
            count = sum(lower <= item.model_disagreement <= upper for item in assessments)
        else:
            count = sum(lower <= item.model_disagreement < upper for item in assessments)
        histogram.append({"lower": round(lower, 4), "upper": round(upper, 4), "count": int(count)})
    return histogram
