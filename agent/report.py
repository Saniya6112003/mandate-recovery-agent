"""Metrics report for a batch run (project guide §7).

Deliberately reports the unflattering numbers alongside the good ones:
diagnosis accuracy per cause, how the agent handled the hard cases
specifically, false-positive cost, and how many cases a human had to look at.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from agent.schemas import AuditLogEntry, MandateEvent

CONFIDENCE_ESCALATION_THRESHOLD = 0.6

CSV_PATH = Path(__file__).resolve().parent.parent / "logs" / "batch_report.csv"


def build_dataframe(
    events: list[MandateEvent], entries: list[AuditLogEntry]
) -> pd.DataFrame:
    rows = []
    for event, entry in zip(events, entries):
        rows.append(
            {
                "mandate_id": event.mandate_id,
                "customer_id": event.customer_id,
                "amount": event.amount,
                "bank_response_code": event.bank_response_code,
                "mandate_type": event.mandate_type,
                "retry_count": event.retry_count,
                "is_hard_case": event.is_hard_case,
                "true_cause": event.true_cause,
                "predicted_cause": entry.ai_output.likely_cause,
                "cause_match": entry.ai_output.likely_cause == event.true_cause,
                "confidence": round(entry.ai_output.confidence, 3),
                "recommended_action": entry.ai_output.recommended_action,
                "action_taken": entry.action_taken,
                "guardrail_allowed": entry.guardrail_verdict.allowed,
                "overridden_reason": entry.guardrail_verdict.overridden_reason,
                "outcome": entry.outcome,
                "reasoning": entry.ai_output.reasoning,
            }
        )
    return pd.DataFrame(rows)


def summarize(df: pd.DataFrame) -> dict:
    confident = df[df["confidence"] >= CONFIDENCE_ESCALATION_THRESHOLD]
    confident_wrong = confident[~confident["cause_match"]]
    recovered = df[df["outcome"] == "recovered"]

    return {
        "attempted": len(df),
        "cause_accuracy": df["cause_match"].mean() if len(df) else 0.0,
        "amount_at_risk": df["amount"].sum(),
        "amount_recovered": recovered["amount"].sum(),
        "recovered_count": len(recovered),
        "escalated_count": int((df["action_taken"] == "escalate_human").sum()),
        "guardrail_override_count": int((~df["guardrail_allowed"]).sum()),
        "false_positive_count": len(confident_wrong),
        "false_positive_cost": confident_wrong["amount"].sum(),
    }


def per_cause_table(df: pd.DataFrame) -> pd.DataFrame:
    return (
        df.groupby("true_cause")
        .agg(
            cases=("mandate_id", "count"),
            accuracy=("cause_match", "mean"),
            mean_confidence=("confidence", "mean"),
            amount=("amount", "sum"),
        )
        .sort_values("cases", ascending=False)
    )


def hard_case_table(df: pd.DataFrame) -> pd.DataFrame:
    """Guide §7 asks for hard cases to be reported explicitly rather than
    averaged away into the headline number.
    """
    hard = df[df["is_hard_case"]]
    return hard[
        [
            "mandate_id",
            "bank_response_code",
            "true_cause",
            "predicted_cause",
            "cause_match",
            "confidence",
            "action_taken",
            "outcome",
        ]
    ]


def export_csv(df: pd.DataFrame, path: Path = CSV_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8")
    return path
