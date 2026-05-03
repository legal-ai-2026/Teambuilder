# System 2 Feature Specification

This dictionary is the operational contract for scoring, fairness evidence, and adapter validation. Protected attributes are fairness-only and must not enter the success scorer or assignment objective.

| Feature | Type | Range | Source | Model usage | Fairness-only | Proxy risk | BFOQ rationale |
| --- | --- | --- | --- | --- | --- | --- | --- |
| soldier_id | string | unique | roster system | trace and audit linkage | no | low | None |
| unit_id | string | unit code | personnel system | Bayes pooled unit effect, redacted in audit | no | medium | None |
| mos | string | MOS code | personnel system | required role qualification and Bayes MOS effect | no | medium | Some mission slots require role-specific MOS certification, such as medic or comms. |
| age_years | integer | 18-45 | personnel system | fairness/proxy audit; not directly scored | no | medium | Age is not a selection target. Physical readiness and injury-risk indicators are used only where mission safety is job-related. |
| two_mile_run_sec | integer | 600-1500 | fitness record | physical readiness signal | no | high | Time-under-load endurance is job-related for dismounted mission demands and casualty movement. |
| self_efficacy_score | float | 1-5 | psychometric instrument | individual readiness signal | no | low | None |
| peer_rating_z | float | unbounded z-score | peer review | cohesion and leadership signal | no | medium | None |
| home_unit_ranger_density | float | 0-1 | training history | experience/context signal and Bayes pooling | no | high | None; monitored as a potential protected-class proxy. |
| acft_score | integer | 300-600 | fitness record | qualification gate and readiness signal | no | high | Physical capacity is job-related for load carriage, casualty movement, and sustained tactical movement. |
| operational_readiness | float | 0-1 | readiness system | success and calibration signal | no | low | None |
| prior_missions | integer | 0+ | deployment history | experience and uncertainty signal | no | medium | None |
| medical_risk | float | 0-1 | medical readiness review | injury-risk penalty and fairness label proxy | no | high | Used only for safety-sensitive assignment review where injury risk affects mission and soldier welfare. |
| landing_asymmetry_score | float | 0-1 | biomechanics screen | injury-risk penalty | no | high | Used as a safety-sensitive physical-risk indicator for impact and load-bearing duties. |
| hip_extension_power_w | float | >0 | biomechanics screen | documented for adapter expansion; not currently scored | no | medium | Lower-body power can be job-related for load carriage and casualty movement. |
| change_of_direction_index | float | 0-1 | movement screen | TabPFN surrogate local pattern | no | medium | Agility under load is job-related for close terrain movement. |
| fatigue_index | float | 0-1 | readiness screen | physical resilience penalty and TabPFN local pattern | no | high | Fatigue tolerance is job-related for sustained missions and safety review. |
| sandbox_score | float | 0-1 | simulation exercise | scenario performance signal | no | low | None |
| protected_race | string/null | declared groups | voluntary demographic record | fairness audit only | yes | protected | Never used for scoring or assignment. |
| protected_gender | string/null | declared groups | voluntary demographic record | fairness audit only | yes | protected | Never used for scoring or assignment. |
| milestones | object[str,int] | 1-5 values | training system | milestone readiness term | no | medium | None |
| competencies | object[str,int] | 1-5 values | training system | role-specific competency weights | no | medium | None |

Model adapters must reject feature bundles that lack this mapping or that route `protected_race` or `protected_gender` into a success estimator.

## Deployment Recommendation Inputs

The deployment recommendation lane is not a personnel success scorer. It wraps
processed System 1 evidence and mission context into the operational twin, then
returns advisory deployment posture and required controls.

| Input | Type | Source | Usage | Notes |
| --- | --- | --- | --- | --- |
| mission_id | string | mission system | trace and source refs | canonical ID |
| requester_id | string | frontend/BFF auth context | audit actor | must identify the requesting reviewer or service |
| team_id | string | mission/unit projection | platoon recommendation identity | canonical ID |
| scope | `individual`/`platoon` | caller | response shape and decision context | defaults to `platoon` |
| target_soldier_ids | list[string] | mission/team projection | individual recommendation wrappers | no soldier scoring is performed here |
| mission_context | string | System 1/shared mission context | operational twin mission artifact | required |
| terrain | string/null | System 1/shared terrain context | operational twin terrain artifact | optional but recommended |
| weather | object | weather/shared context | operational twin weather artifact and environment | optional |
| readiness | object | readiness/training projection | operational twin sleep/readiness artifact | optional |
| processed_observations | list[ArtifactInput] | System 1 processed outputs | operational twin evidence | raw audio/images are not accepted |
| constraints | list[string] | commander/policy context | decision context and mission artifact metadata | advisory controls |

Deployment response posture values are `deploy`, `deploy_with_controls`, `hold`,
and `escalate_review`. All deployment recommendations remain advisory and
human-gated unless the request explicitly sets `require_human_approval` to
`false`. The persisted lifecycle is recommendation -> approve/reject/escalate
decision -> outcome/AAR capture -> lesson draft.
