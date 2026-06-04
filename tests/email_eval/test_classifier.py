"""Email classifier evaluation harness.

Runs the live classifier against a labeled corpus and asserts overall
accuracy + per-label precision floors. Skipped when OPENAI_API_KEY is
not set (so CI without the key still passes); enable explicitly via:

  OPENAI_API_KEY=... pytest tests/email_eval -v

The corpus lives in `cases.json` and is meant to grow over time —
every new misfire the user reports should land here as a regression
case.

Accuracy expectations:
  * overall label match    >= 0.90
  * noise precision        >= 0.95 (false positives leak into the
                                   Pipeline; this is the most
                                   expensive class of mistake)
  * applied/offer/rejection precision >= 0.90 (high-stakes labels
                                              we don't want hallucinated)

When the gold corpus is small, individual misses can shift the
percentages a lot. Prefer adding cases over relaxing thresholds.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


CASES_FILE = Path(__file__).parent / "cases.json"


def _load_cases() -> list[dict]:
    with open(CASES_FILE) as f:
        return json.load(f)


pytestmark = pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set — classifier eval requires live LLM",
)


@pytest.mark.asyncio
async def test_overall_label_accuracy():
    from app.email_classifier import classify_batch, is_obvious_noise

    cases = _load_cases()
    inputs = []
    expected = {}
    for c in cases:
        # Mirror the orchestrator: blocklist short-circuits to noise.
        if is_obvious_noise(c["from"], c["subject"]):
            # Treat as deterministic noise — no LLM call.
            expected[c["id"]] = ("noise", c["expected_label"])
            continue
        inputs.append({
            "id": c["id"],
            "from": c["from"],
            "subject": c["subject"],
            "body": c["body"],
        })
        expected[c["id"]] = (None, c["expected_label"])

    results = await classify_batch(inputs) if inputs else {}

    correct = 0
    per_label_correct: dict[str, int] = {}
    per_label_total: dict[str, int] = {}
    misses: list[str] = []

    for cid, (deterministic, gold) in expected.items():
        per_label_total[gold] = per_label_total.get(gold, 0) + 1
        if deterministic is not None:
            actual = deterministic
        else:
            v = results.get(cid)
            actual = v.label if v is not None else "follow_up"
        if actual == gold:
            correct += 1
            per_label_correct[gold] = per_label_correct.get(gold, 0) + 1
        else:
            misses.append(f"{cid}: expected={gold} actual={actual}")

    total = len(expected)
    overall = correct / total if total else 0.0
    print("\n=== email classifier eval ===")
    print(f"overall: {correct}/{total} = {overall:.1%}")
    for label, n in sorted(per_label_total.items()):
        c = per_label_correct.get(label, 0)
        print(f"  {label:24s} {c}/{n}")
    if misses:
        print("misses:")
        for m in misses:
            print(f"  - {m}")

    assert overall >= 0.90, f"overall accuracy {overall:.1%} below 90% floor"

    # Per-label floors for the labels we MOST care about precision on.
    for label, floor in [("noise", 0.95), ("applied", 0.90), ("rejection", 0.90), ("offer", 0.90)]:
        n = per_label_total.get(label, 0)
        if n == 0:
            continue
        precision = per_label_correct.get(label, 0) / n
        assert precision >= floor, (
            f"{label} precision {precision:.1%} below floor {floor:.0%} ({per_label_correct.get(label, 0)}/{n})"
        )
