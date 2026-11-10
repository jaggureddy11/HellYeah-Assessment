"""Clairvoyant oracle baseline allowed to read ground-truth world_config.json."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sim.env import expected_revenue, expected_spend


class Clairvoyant:
    """Myopic per-day oracle maximizing today's expected revenue using ground-truth config.

    Note: This policy is myopic per day (maximizes day-t expected revenue using
    sim.env.expected_revenue and sim.env.expected_spend without dynamic multi-day
    fatigue planning), serving as a simple upper bound rather than the exact 30-day
    dynamic optimum.
    """

    def __init__(
        self,
        creatives: list[dict[str, Any]],
        daily_budget: float,
        seed: int,
        world_config: dict[str, Any] | str | Path = "sim/world_config.json",
        step_dollars: float = 5.0,
    ) -> None:
        _ = (creatives, seed)
        if isinstance(world_config, dict):
            self.config = world_config
        else:
            self.config = json.loads(Path(world_config).read_text(encoding="utf-8"))

        self.daily_budget = float(daily_budget)
        self.step_dollars = float(step_dollars)
        self.creative_cfgs: dict[str, dict[str, Any]] = {
            c["creative_id"]: c for c in self.config["creatives"]
        }
        self.creative_ids: list[str] = sorted(self.creative_cfgs.keys())
        self.base_launch_day: dict[str, int | None] = {
            cid: (int(c["launch_day"]) if c.get("launch_day") is not None else None)
            for cid, c in self.creative_cfgs.items()
        }

    def _infer_ages(self, day: int, history: list[dict[str, Any]]) -> dict[str, int]:
        first_seen: dict[str, int] = {}
        for r in history:
            cid = str(r["creative_id"])
            if float(r["budget"]) > 0.0 or float(r["spend"]) > 0.0:
                d = int(r["day"])
                if cid not in first_seen or d < first_seen[cid]:
                    first_seen[cid] = d

        ages: dict[str, int] = {}
        for cid in self.creative_ids:
            ld = self.base_launch_day[cid]
            if ld is not None:
                ages[cid] = max(0, day - ld)
            elif cid in first_seen:
                ages[cid] = max(0, day - first_seen[cid])
            else:
                # Unserved creative starts at age=0 if funded today
                ages[cid] = 0
        return ages

    def allocate(self, day: int, history: list[dict[str, Any]]) -> dict[str, float]:
        ages = self._infer_ages(day, history)
        alloc: dict[str, float] = {cid: 0.0 for cid in self.creative_ids}
        curr_rev: dict[str, float] = {cid: 0.0 for cid in self.creative_ids}

        n_steps = int(round(self.daily_budget / self.step_dollars))
        remaining = self.daily_budget

        for _ in range(n_steps):
            inc = min(self.step_dollars, remaining)
            if inc <= 0.0:
                break
            best_cid = None
            best_gain = -1.0
            for cid in self.creative_ids:
                c_cfg = self.creative_cfgs[cid]
                new_b = alloc[cid] + inc
                new_sp = expected_spend(new_b, c_cfg, self.config)
                new_rev = expected_revenue(new_sp, ages[cid], day, c_cfg, self.config)
                gain = new_rev - curr_rev[cid]
                if gain > best_gain:
                    best_gain = gain
                    best_cid = cid

            if best_cid is None:
                break
            alloc[best_cid] += inc
            c_cfg = self.creative_cfgs[best_cid]
            curr_rev[best_cid] = expected_revenue(
                expected_spend(alloc[best_cid], c_cfg, self.config),
                ages[best_cid],
                day,
                c_cfg,
                self.config,
            )
            remaining -= inc

        if remaining > 1e-6 and self.creative_ids:
            alloc[self.creative_ids[0]] += remaining

        total = sum(alloc.values())
        if total <= 0.0:
            return {cid: 1.0 / len(self.creative_ids) for cid in self.creative_ids}
        return {cid: val / total for cid, val in alloc.items()}

