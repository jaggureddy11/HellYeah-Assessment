"""Smoke and contract tests for the root Policy class."""

from __future__ import annotations

import csv
import math
from pathlib import Path

import pytest

from policy import Policy

CREATIVES_CSV = Path(__file__).resolve().parent.parent / "data" / "creatives.csv"


def _load_creatives() -> list[dict[str, str]]:
    with CREATIVES_CSV.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_uniform_split_contract_and_idempotency() -> None:
    creatives = _load_creatives()
    assert len(creatives) == 50

    pol = Policy(creatives=creatives, daily_budget=2000.0, seed=7)
    first = pol.allocate(day=61, history=[])

    assert len(first) == 50
    assert set(first.keys()) == {row["creative_id"] for row in creatives}
    assert all(math.isfinite(v) and v >= 0.0 for v in first.values())
    assert sum(first.values()) == pytest.approx(1.0, abs=1e-9)

    repeated = pol.allocate(day=61, history=[])
    assert repeated == first


def test_malformed_and_empty_history_do_not_crash() -> None:
    creatives = _load_creatives()
    pol = Policy(creatives=creatives, daily_budget=2000.0, seed=11)

    malformed_histories = [
        None,
        "not-a-list",
        [None, 123, {"bad": "row"}, {"creative_id": "CR001", "spend": float("nan"), "revenue": -50}],
    ]
    for idx, hist in enumerate(malformed_histories):
        out = pol.allocate(day=61 + idx, history=hist)  # type: ignore[arg-type]
        assert len(out) == 50
        assert all(math.isfinite(v) and v >= 0.0 for v in out.values())
        assert sum(out.values()) == pytest.approx(1.0, abs=1e-9)

