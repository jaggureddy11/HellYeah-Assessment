"""Generate policy_priors.json solely from data/*.csv (no sim/ imports or config reads).

Run from repository root:
    uv run python -m analysis.make_priors
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np

DATA_DIR = Path("data")
OUT_PATH = Path("policy_priors.json")


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def build_priors(data_dir: Path = DATA_DIR) -> dict[str, Any]:
    creatives = _read_csv(data_dir / "creatives.csv")
    reported = _read_csv(data_dir / "daily_reported.csv")
    final = _read_csv(data_dir / "daily_final.csv")

    # 1. Lag completion by row_age = day - row_day at day 61 (i.e. export pulled end of day 60).
    # row_day=60 -> row_age=1, row_day=59 -> row_age=2, ..., row_day=54 -> row_age=7.
    rep_by_day: dict[int, float] = defaultdict(float)
    fin_by_day: dict[int, float] = defaultdict(float)
    for r in reported:
        rep_by_day[int(r["day"])] += float(r["revenue"])
    for r in final:
        fin_by_day[int(r["day"])] += float(r["revenue"])

    lag_completion_by_age: dict[str, float] = {}
    for row_age in range(1, 8):
        d = 61 - row_age
        ratio = rep_by_day[d] / fin_by_day[d] if fin_by_day[d] > 0 else 1.0
        lag_completion_by_age[str(row_age)] = round(float(min(1.0, max(0.05, ratio))), 4)

    # 2. Creative-level ROAS & format-level mean ROAS (from daily_final.csv).
    # Intentionally excludes hook_type and cta because LOCO OOS R^2 was negative.
    fmt_by_id = {c["creative_id"]: c["format"] for c in creatives}
    spend_by_c: dict[str, float] = defaultdict(float)
    rev_by_c: dict[str, float] = defaultdict(float)
    for r in final:
        cid = r["creative_id"]
        spend_by_c[cid] += float(r["spend"])
        rev_by_c[cid] += float(r["revenue"])

    roas_list: list[float] = []
    roas_by_fmt: dict[str, list[float]] = defaultdict(list)
    for cid, sp in sorted(spend_by_c.items()):
        if sp > 0.0:
            r_val = rev_by_c[cid] / sp
            roas_list.append(r_val)
            roas_by_fmt[fmt_by_id[cid]].append(r_val)

    global_mean_roas = float(np.mean(roas_list)) if roas_list else 1.8
    pooled_roas = sum(rev_by_c.values()) / sum(spend_by_c.values()) if sum(spend_by_c.values()) > 0 else global_mean_roas
    format_mean_roas = {
        fmt: round(float(np.mean(vals)), 4)
        for fmt, vals in sorted(roas_by_fmt.items())
        if vals
    }

    return {
        "schema_version": 1,
        "source": "Computed strictly from data/creatives.csv, data/daily_reported.csv, data/daily_final.csv",
        "global_mean_roas": round(global_mean_roas, 4),
        "pooled_roas": round(pooled_roas, 4),
        "format_mean_roas": format_mean_roas,
        "lag_completion_by_age": lag_completion_by_age,
    }


def main() -> None:
    priors = build_priors(DATA_DIR)
    OUT_PATH.write_text(json.dumps(priors, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {OUT_PATH}:")
    print(json.dumps(priors, indent=2))


if __name__ == "__main__":
    main()

