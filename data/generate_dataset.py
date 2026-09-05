from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.schemas import MandateEvent  # noqa: E402

OUTPUT_PATH = Path(__file__).parent / "synthetic_failures.json"

# Every code below is a real NPCI/UPI response code, verified against Axis
# Bank's published "UPI Response Codes for H2H/API" list. The agent sees only
# the raw code — never the English cause — so it has to infer the diagnosis
# the way a real recovery system would.
#
# Two things worth noting about this mapping:
#  - `IE`, `VA`, `MA0` and `QA` are mandate-specific codes with no card-rail
#    equivalent. They're the concrete reason a generic "failed payment"
#    recovery tool can't reason about UPI Autopay properly.
#  - The `unknown` bucket is deliberately genuinely ambiguous: `ZA`
#    ("transaction declined") and `U30` ("debit has been failed") are codes a
#    bank returns without saying why, so these cases can't be solved by any
#    lookup table — the agent has to reason about the surrounding signals.
CAUSE_CODES: dict[str, list[str]] = {
    # Z9: insufficient funds in remitter account.
    # IE: funds inadequate because they're blocked against another mandate.
    "insufficient_funds": ["Z9", "IE"],
    # U67 debit timeout, UT remitter/issuer unavailable, XY remitter CBS
    # offline, IR internal exception on remitter side, U28 PSP not available.
    "bank_timeout": ["U67", "UT", "XY", "IR", "U28"],
    # Z8 per-transaction cap, Z7 transaction frequency cap, ZU issuing-bank
    # limit, M2 per-customer amount limit.
    "daily_limit_exceeded": ["Z8", "Z7", "ZU", "M2"],
    # VA revoked, QA paused by user, MA0 mandate not present. All three mean
    # the same thing to the guardrail: this mandate cannot be debited at all.
    "mandate_revoked": ["VA", "QA", "MA0"],
    # ZM invalid MPIN, Z6 PIN tries exceeded, AM MPIN not set by customer.
    "authentication_failure": ["ZM", "Z6", "AM"],
    # ZA declined with no reason given, U30 debit failed with no reason given,
    # B3 transaction not permitted to the account.
    "unknown": ["ZA", "U30", "B3"],
}

DISTRIBUTION = {
    "insufficient_funds": 0.40,
    "bank_timeout": 0.20,
    "daily_limit_exceeded": 0.15,
    "mandate_revoked": 0.10,
    "authentication_failure": 0.10,
    "unknown": 0.05,
}

AMOUNT_TIERS = [99, 199, 299, 499, 999, 1999, 2999, 4999, 9999, 14999]


def _counts(total: int) -> dict[str, int]:
    counts = {cause: round(total * share) for cause, share in DISTRIBUTION.items()}
    drift = total - sum(counts.values())
    counts["insufficient_funds"] += drift  # absorb rounding drift in the largest bucket
    return counts


def _random_amount(cause: str, rng: random.Random) -> float:
    if cause == "daily_limit_exceeded":
        return float(rng.choice(AMOUNT_TIERS[-4:]))
    return float(rng.choice(AMOUNT_TIERS))


def _generate_event(cause: str, is_hard_case: bool, rng: random.Random, now: datetime) -> MandateEvent:
    code = rng.choice(CAUSE_CODES[cause])
    mandate_created_at = now - timedelta(days=rng.randint(30, 540))
    failure_timestamp = now - timedelta(days=rng.randint(0, 30), hours=rng.randint(0, 23))
    retry_count = 3 if is_hard_case else rng.randint(0, 2)

    # Most cases satisfy the 24h pre-debit notice window; hard cases sometimes
    # sit right at or under that boundary, which is exactly what the Day-2
    # guardrail's notice-window check needs to have something real to catch.
    notice_lag_hours = rng.choice([2, 6, 20, 24]) if is_hard_case else rng.choice([26, 30, 36, 48])
    notice_sent_at = failure_timestamp - timedelta(hours=notice_lag_hours)

    return MandateEvent(
        # Drawn from the seeded RNG, not uuid4 — otherwise the same seed
        # produces different mandate ids on every run, which would break
        # reproducibility, `run_batch.py --resume`, and any attempt to
        # correlate an audit log with the batch that produced it.
        mandate_id=f"MID{rng.getrandbits(40):010X}",
        customer_id=f"CUST{rng.randint(100000, 999999)}",
        amount=_random_amount(cause, rng),
        bank_response_code=code,
        retry_count=retry_count,
        # A customer can't easily revoke a non-revocable mandate, so keep
        # mandate_revoked cases realistic by forcing them revocable.
        mandate_type="revocable" if cause == "mandate_revoked" else (
            "non-revocable" if rng.random() < 0.2 else "revocable"
        ),
        mandate_created_at=mandate_created_at,
        failure_timestamp=failure_timestamp,
        notice_sent_at=notice_sent_at,
        # What an authoritative mandate-status lookup would return. QA is
        # literally "mandate is paused by user"; VA and MA0 mean it's gone.
        mandate_status=("paused" if code == "QA" else "revoked")
        if cause == "mandate_revoked"
        else "active",
        true_cause=cause,
        is_hard_case=is_hard_case,
    )


def generate(
    total: int = 60,
    hard_case_share: float = 0.1,
    seed: int = 42,
    now: Optional[datetime] = None,
) -> list[MandateEvent]:
    """Build a reproducible batch of synthetic mandate failures.

    Timestamps are relative to `now`, which defaults to the wall clock so a
    fresh batch looks current. Pass an explicit `now` to make the output
    byte-identical for a given seed — which is what makes the batch provably
    reproducible rather than merely similarly distributed.
    """
    rng = random.Random(seed)
    now = now or datetime.now(timezone.utc)
    counts = _counts(total)

    events: list[MandateEvent] = []
    for cause, count in counts.items():
        n_hard = round(count * hard_case_share)
        for i in range(count):
            events.append(_generate_event(cause, is_hard_case=i < n_hard, rng=rng, now=now))

    rng.shuffle(events)
    return events


def save(events: list[MandateEvent], path: Path = OUTPUT_PATH) -> None:
    path.write_text(json.dumps([e.model_dump(mode="json") for e in events], indent=2))


def load_dataset(path: Path = OUTPUT_PATH) -> list[MandateEvent]:
    raw = json.loads(path.read_text())
    return [MandateEvent.model_validate(item) for item in raw]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate synthetic UPI Autopay mandate failures.")
    parser.add_argument("--count", type=int, default=60)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--hard-case-share", type=float, default=0.1)
    args = parser.parse_args()

    events = generate(total=args.count, hard_case_share=args.hard_case_share, seed=args.seed)
    save(events)

    print(f"Generated {len(events)} synthetic mandate failures -> {OUTPUT_PATH}")
    for cause in DISTRIBUTION:
        n = sum(1 for e in events if e.true_cause == cause)
        print(f"  {cause:<24} {n:>3} ({n / len(events):.0%})")
    print(f"  hard cases: {sum(1 for e in events if e.is_hard_case)}")
