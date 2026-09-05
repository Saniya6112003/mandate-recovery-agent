"""Demo entry point (project guide §8, Day 3): run the full recovery loop
over the synthetic batch and print the honest metrics report — no
cherry-picking, and confidence is called out explicitly as LLM self-report,
not a calibrated probability.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

# Windows terminals often default stdout to cp1252, which can't encode the
# rupee sign used throughout this report — force utf-8 so the demo doesn't
# crash mid-run on `python run_batch.py`.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent.audit_log import load_log
from agent.executor import kill_switch_engaged
from agent.pipeline import CONFIDENCE_ESCALATION_THRESHOLD, process_batch
from agent.reasoning_agent import _provider
from agent.report import (
    build_dataframe,
    export_csv,
    hard_case_table,
    per_cause_table,
    summarize,
)
from data.generate_dataset import load_dataset


def _print_report(events, entries) -> None:
    df = build_dataframe(events, entries)
    stats = summarize(df)

    print("=" * 78)
    print("UPI AUTOPAY MANDATE RECOVERY — BATCH REPORT")
    print("=" * 78)
    if _provider() == "stub":
        print("!! OFFLINE STUB MODE — decisions come from a deterministic fixture,")
        print("!! not the LLM. These numbers are NOT valid pitch metrics.")
        print("!! Set REASONING_PROVIDER=anthropic for real results.")
        print("-" * 78)
    print(f"kill switch engaged:       {kill_switch_engaged()}")
    print(f"attempted (batch size):    {stats['attempted']}")
    print(f"diagnosis accuracy:        {stats['cause_accuracy']:.0%} vs ground-truth cause")
    print()

    print("accuracy by true cause:")
    print(per_cause_table(df).to_string())
    print()

    print("actions taken:")
    for action, count in Counter(e.action_taken for e in entries).most_common():
        print(f"  {action:<18} {count:>3}")
    print()

    print("outcomes (`recovered` only flips on a webhook-verified payment,")
    print("so cases read `pending` until that webhook actually arrives):")
    for outcome, count in Counter(e.outcome for e in entries).most_common():
        print(f"  {outcome:<18} {count:>3}")
    print()

    overrides = [e for e in entries if not e.guardrail_verdict.allowed]
    print(f"guardrail overrides:       {stats['guardrail_override_count']}")
    for e in overrides:
        print(f"  {e.mandate_id}: {e.guardrail_verdict.overridden_reason}")
    print()

    hard = hard_case_table(df)
    print(f"hard cases ({len(hard)}) — reported explicitly, not averaged away:")
    print(hard.to_string(index=False) if len(hard) else "  none in this batch")
    print()

    print(f"₹ at risk (batch total):   ₹{stats['amount_at_risk']:,.0f}")
    print(f"₹ recovered (confirmed):   ₹{stats['amount_recovered']:,.0f}")
    print(f"escalated to human:        {stats['escalated_count']}")
    print(
        f"false-positive cost:       ₹{stats['false_positive_cost']:,.0f} "
        f"({stats['false_positive_count']} cases — AI confident "
        f"(>={CONFIDENCE_ESCALATION_THRESHOLD:.0%}) but wrong)"
    )
    print()
    print("note: `confidence` is the model's self-reported confidence, not a")
    print("calibrated probability — used here only as a routing signal.")
    print(f"\nper-case detail written to {export_csv(df)}")
    print("=" * 78)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the mandate recovery batch demo.")
    parser.add_argument("--limit", type=int, default=None, help="only process the first N events")
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "skip mandates already present in the audit log. Free-tier LLM quotas "
            "and API rate limits can interrupt a long batch; this continues it "
            "without re-spending on cases already decided."
        ),
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=None,
        help="path to a synthetic_failures.json to use instead of the default",
    )
    args = parser.parse_args()

    events = load_dataset(args.dataset) if args.dataset else load_dataset()
    if args.limit:
        events = events[: args.limit]

    if args.resume:
        already_done = {entry["mandate_id"] for entry in load_log()}
        remaining = [e for e in events if e.mandate_id not in already_done]
        skipped = len(events) - len(remaining)
        if skipped:
            print(f"resuming: {skipped} already in the audit log, {len(remaining)} to process\n")
        events = remaining
        if not events:
            print("nothing left to process — every mandate is already in the audit log")
            return

    entries = process_batch(events)
    _print_report(events, entries)


if __name__ == "__main__":
    main()
