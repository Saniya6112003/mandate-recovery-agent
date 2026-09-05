"""The dataset must be reproducible from its seed.

Project guide §7 requires the batch to be regenerable on demand so the
distribution is provably not cherry-picked. `mandate_id` originally came from
uuid4, which ignored the seed — the same seed produced different ids every
run, silently breaking reproducibility, `run_batch.py --resume`, and any
correlation between an audit log and the batch that produced it.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from data.generate_dataset import DISTRIBUTION, generate

FIXED_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_same_seed_and_clock_produces_byte_identical_batch():
    first = generate(total=60, seed=42, now=FIXED_NOW)
    second = generate(total=60, seed=42, now=FIXED_NOW)

    assert [e.model_dump(mode="json") for e in first] == [
        e.model_dump(mode="json") for e in second
    ]


def test_mandate_ids_are_stable_across_runs_regardless_of_clock():
    """Ids must not drift with wall-clock time, or an audit log can never be
    correlated with the batch that produced it and --resume breaks silently.
    """
    first = generate(total=60, seed=42)
    second = generate(total=60, seed=42)

    assert [e.mandate_id for e in first] == [e.mandate_id for e in second]


def test_different_seeds_produce_different_batches():
    a = generate(total=60, seed=42)
    b = generate(total=60, seed=7)

    assert [e.mandate_id for e in a] != [e.mandate_id for e in b]


def test_mandate_ids_are_unique():
    events = generate(total=60, seed=42)

    ids = [e.mandate_id for e in events]
    assert len(set(ids)) == len(ids)


def test_distribution_matches_spec():
    events = generate(total=60, seed=42)
    counts = Counter(e.true_cause for e in events)

    for cause, share in DISTRIBUTION.items():
        assert abs(counts[cause] / len(events) - share) < 0.02, cause


def test_revoked_cases_carry_non_active_mandate_status():
    """The guardrail keys off mandate_status, so a revoked case that still
    reads `active` would never be caught.
    """
    events = generate(total=60, seed=42)

    for event in events:
        if event.true_cause == "mandate_revoked":
            assert event.mandate_status in ("revoked", "paused")
        else:
            assert event.mandate_status == "active"


def test_ground_truth_is_excluded_from_agent_input():
    events = generate(total=60, seed=42)
    payload = events[0].agent_input()

    assert "true_cause" not in payload
    assert "mandate_status" not in payload
    assert "is_hard_case" not in payload
