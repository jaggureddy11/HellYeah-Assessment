"""Creative Rotation Brain budget-allocation policy."""

from __future__ import annotations

import math
from typing import Any

import numpy as np


class Policy:
    """Baseline uniform policy skeleton with defensive input handling."""

    def __init__(self, creatives: list[dict[str, Any]], daily_budget: float, seed: int) -> None:
        """Initialize policy state from creative metadata, daily budget, and RNG seed."""
        self.creative_ids: list[str] = []
        seen: set[str] = set()
        if isinstance(creatives, (list, tuple)):
            for row in creatives:
                if not isinstance(row, dict):
                    continue
                cid = row.get("creative_id")
                if cid is None:
                    continue
                cid_str = str(cid).strip()
                if cid_str and cid_str not in seen:
                    seen.add(cid_str)
                    self.creative_ids.append(cid_str)
        self._creative_id_set: set[str] = set(self.creative_ids)

        try:
            budget_val = float(daily_budget)
        except (TypeError, ValueError):
            budget_val = 0.0
        self.daily_budget: float = budget_val if math.isfinite(budget_val) and budget_val > 0.0 else 0.0

        try:
            self.seed = int(seed)
        except (TypeError, ValueError):
            self.seed = 0
        self._rng = np.random.default_rng(self.seed)
        self._day_cache: dict[int, dict[str, float]] = {}

    def _normalize_shares(self, raw_shares: dict[str, float]) -> dict[str, float]:
        """Sanitize and normalize shares to finite, non-negative values summing to 1.0."""
        if not self.creative_ids:
            return {}
        cleaned = []
        for cid in self.creative_ids:
            try:
                val = float(raw_shares.get(cid, 0.0))
            except (TypeError, ValueError):
                val = 0.0
            cleaned.append(val if math.isfinite(val) and val > 0.0 else 0.0)
        arr = np.array(cleaned, dtype=float)
        total = float(arr.sum())
        if not math.isfinite(total) or total <= 0.0:
            arr = np.full(len(self.creative_ids), 1.0 / len(self.creative_ids), dtype=float)
        else:
            arr = arr / total
        return {cid: float(val) for cid, val in zip(self.creative_ids, arr)}

    def _validate_history(self, history: Any) -> list[dict[str, Any]]:
        """Filter malformed history entries without failing."""
        if not isinstance(history, (list, tuple)):
            return []
        valid: list[dict[str, Any]] = []
        for row in history:
            if not isinstance(row, dict):
                continue
            cid = row.get("creative_id")
            if cid is None or str(cid) not in self._creative_id_set:
                continue
            valid.append(row)
        return valid

    def allocate(self, day: int, history: list[dict[str, Any]]) -> dict[str, float]:
        """Return a normalized {creative_id: share} split for the given day."""
        try:
            day_key = int(day)
        except (TypeError, ValueError):
            day_key = -1

        if day_key >= 0 and day_key in self._day_cache:
            return dict(self._day_cache[day_key])

        _ = self._validate_history(history)
        n = len(self.creative_ids)
        if n == 0:
            return {}

        uniform = {cid: 1.0 / n for cid in self.creative_ids}
        shares = self._normalize_shares(uniform)
        if day_key >= 0:
            self._day_cache[day_key] = dict(shares)
        return shares
