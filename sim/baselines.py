"""Reference baseline policies (B1 Uniform and B2 Windowed Greedy with Exploration)."""

from __future__ import annotations

from collections import defaultdict
from typing import Any


def _extract_creative_ids(creatives: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for row in creatives:
        cid = str(row["creative_id"]).strip()
        if cid and cid not in seen:
            seen.add(cid)
            ids.append(cid)
    return ids


class B1Uniform:
    """B1: Equal share to all creatives every day."""

    def __init__(self, creatives: list[dict[str, Any]], daily_budget: float, seed: int) -> None:
        self.creative_ids = _extract_creative_ids(creatives)
        self.daily_budget = float(daily_budget)
        self.seed = int(seed)

    def allocate(self, day: int, history: list[dict[str, Any]]) -> dict[str, float]:
        _ = (day, history)
        n = len(self.creative_ids)
        if n == 0:
            return {}
        share = 1.0 / n
        return {cid: share for cid in self.creative_ids}


class B2WindowedGreedy:
    """B2: Trailing 7-day windowed greedy with exploration (75/25 split).

    Assumption for population mean rate:
        Computed as pooled revenue divided by pooled spend across all creatives in the
        trailing 7-day window ([day - 7, day - 1]) using reported history as received.
        If pooled trailing spend is <= 0, the population mean rate defaults to 0.0.
    """

    def __init__(self, creatives: list[dict[str, Any]], daily_budget: float, seed: int) -> None:
        self.creative_ids = _extract_creative_ids(creatives)
        self.daily_budget = float(daily_budget)
        self.seed = int(seed)

    def allocate(self, day: int, history: list[dict[str, Any]]) -> dict[str, float]:
        n = len(self.creative_ids)
        if n == 0:
            return {}

        win_start = day - 7
        spend_by_c: dict[str, float] = defaultdict(float)
        rev_by_c: dict[str, float] = defaultdict(float)
        impr_by_c: dict[str, int] = defaultdict(int)

        pooled_spend = 0.0
        pooled_rev = 0.0

        for row in history:
            r_day = int(row["day"])
            if not (win_start <= r_day < day):
                continue
            cid = str(row["creative_id"])
            sp = float(row["spend"])
            rv = float(row["revenue"])
            imp = int(row["impressions"])
            spend_by_c[cid] += sp
            rev_by_c[cid] += rv
            impr_by_c[cid] += imp
            pooled_spend += sp
            pooled_rev += rv

        pop_mean_rate = (pooled_rev / pooled_spend) if pooled_spend > 0.0 else 0.0

        rates: dict[str, float] = {}
        for cid in self.creative_ids:
            if impr_by_c[cid] < 2000 or spend_by_c[cid] <= 0.0:
                rates[cid] = pop_mean_rate
            else:
                rates[cid] = rev_by_c[cid] / spend_by_c[cid]

        # Rank descending by rate; break ties deterministically by creative_id ascending.
        ranked = sorted(self.creative_ids, key=lambda cid: (-rates[cid], cid))

        if n <= 5:
            share = 1.0 / n
            return {cid: share for cid in self.creative_ids}

        top_share = 0.75 / 5.0
        rest_share = 0.25 / (n - 5)
        out: dict[str, float] = {}
        for idx, cid in enumerate(ranked):
            out[cid] = top_share if idx < 5 else rest_share
        return out

