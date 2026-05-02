from __future__ import annotations

import hashlib


MODEL_VERSIONS = {
    "tabpfn_adapter": "offline-surrogate-0.2; target tabpfn>=2.5",
    "bayes_adapter": "offline-hierarchical-surrogate-0.2; target pymc>=5.16",
    "assignment": "scipy.linear_sum_assignment@1",
    "narrative_adapter": "offline-template-0.2; target structured LLM JSON",
    "prompt": "system2-roster-recommendation-v1",
    "fairness_metrics": "counterfactual-mi-group-metrics-0.1",
}

DOD_AI_PRINCIPLES = {
    "Responsible": "Advisory recommendations only; command authority retains final decision.",
    "Equitable": "Counterfactual, proxy, demographic parity, and equalized-odds audits are returned.",
    "Traceable": "Feature hash, prompt hash, model versions, seed, and audit records are included.",
    "Reliable": "TabPFN/Bayes disagreement changes confidence and can demote candidates.",
    "Governable": "Administrative disable endpoint blocks scoring.",
}


def prompt_hash() -> str:
    return hashlib.sha256(MODEL_VERSIONS["prompt"].encode()).hexdigest()[:16]
