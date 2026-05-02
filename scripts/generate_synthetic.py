from __future__ import annotations

import argparse
import json
from pathlib import Path

from system2.data import default_roles, generate_soldiers


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate deterministic synthetic System 2 candidate data.")
    parser.add_argument("--count", "--n", dest="count", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", type=Path, default=Path("artifacts/synthetic-cohort.jsonl"))
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    roles = [role.model_dump(mode="json") for role in default_roles()]
    with args.output.open("w", encoding="utf-8") as handle:
        header = {"kind": "metadata", "count": args.count, "seed": args.seed, "roles": roles}
        handle.write(json.dumps(header, sort_keys=True) + "\n")
        for soldier in generate_soldiers(args.count, args.seed):
            handle.write(json.dumps(soldier.model_dump(mode="json"), sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
