from __future__ import annotations

import hashlib


MODEL_VERSIONS = {
    "tabpfn_adapter": "deterministic-tabpfn-compatible-0.2",
    "bayes_adapter": "deterministic-hierarchical-pooling-0.2",
    "assignment": "scipy.linear_sum_assignment@1",
    "narrative_adapter": "deterministic-template-0.2",
    "prompt": "system2-roster-recommendation-v1",
    "fairness_metrics": "counterfactual-mi-group-metrics-0.1",
    "cognitive_state_estimator": "deterministic-cognitive-state-0.1",
    "scenario_director": "deterministic-scenario-director-0.1",
    "safety_doctrine_auditor": "deterministic-safety-doctrine-auditor-0.1",
    "operational_twin_perception": "deterministic-twin-perception-0.1",
    "operational_twin_state_estimator": "deterministic-twin-state-estimator-0.1",
    "operational_twin_scenario_director": "deterministic-twin-scenario-director-0.1",
    "operational_twin_critic": "deterministic-twin-critic-0.1",
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
