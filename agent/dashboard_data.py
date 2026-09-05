"""Builds the payload the dashboard renders.

Joins the audit log against the synthetic batch so the UI can show both what
the agent decided and whether it was right — the ground-truth label lives
only here and in the report, never in anything the model sees.
"""

from __future__ import annotations

from typing import Any

from agent.audit_log import load_log
from agent.pipeline import CONFIDENCE_ESCALATION_THRESHOLD
from data.generate_dataset import load_dataset


def _safe_truth() -> dict:
    try:
        return {e.mandate_id: e for e in load_dataset()}
    except Exception:
        return {}


def build_payload() -> dict[str, Any]:
    entries = load_log()
    truth = _safe_truth()

    rows: list[dict[str, Any]] = []
    for entry in entries:
        event = truth.get(entry["mandate_id"])
        ai = entry["ai_output"]
        verdict = entry["guardrail_verdict"]
        execution = entry.get("execution_result") or {}

        rows.append(
            {
                "mandate_id": entry["mandate_id"],
                "timestamp": entry["timestamp"],
                "code": entry["input_signal"].get("bank_response_code"),
                "amount": entry["input_signal"].get("amount_inr"),
                "retry_count": entry["input_signal"].get("retry_count"),
                "predicted": ai["likely_cause"],
                "true_cause": event.true_cause if event else None,
                "match": (event.true_cause == ai["likely_cause"]) if event else None,
                "confidence": round(ai["confidence"], 2),
                "reasoning": ai["reasoning"],
                "recommended": ai["recommended_action"],
                "action": entry["action_taken"],
                "overridden": not verdict["allowed"],
                "override_reason": verdict["overridden_reason"],
                "outcome": entry["outcome"],
                "payment_url": execution.get("short_url"),
                "payment_id": execution.get("id"),
                "is_hard_case": bool(event.is_hard_case) if event else False,
            }
        )

    scored = [r for r in rows if r["match"] is not None]
    correct = sum(1 for r in scored if r["match"])
    confident = [r for r in scored if r["confidence"] >= CONFIDENCE_ESCALATION_THRESHOLD]
    confident_wrong = [r for r in confident if not r["match"]]

    amount_at_risk = sum(r["amount"] or 0 for r in rows)
    amount_recovered = sum(r["amount"] or 0 for r in rows if r["outcome"] == "recovered")

    right = [r["confidence"] for r in scored if r["match"]]
    wrong = [r["confidence"] for r in scored if not r["match"]]

    return {
        "summary": {
            "cases": len(rows),
            "accuracy": (correct / len(scored)) if scored else None,
            "correct": correct,
            "scored": len(scored),
            "amount_at_risk": amount_at_risk,
            "amount_recovered": amount_recovered,
            "overrides": sum(1 for r in rows if r["overridden"]),
            "escalated": sum(1 for r in rows if r["action"] == "escalate_human"),
            "recovered": sum(1 for r in rows if r["outcome"] == "recovered"),
            "pending": sum(1 for r in rows if r["outcome"] == "pending"),
            "false_positive_count": len(confident_wrong),
            "false_positive_cost": sum(r["amount"] or 0 for r in confident_wrong),
            # The headline calibration result: near-identical means that
            # confidence carries almost no information about correctness.
            "confidence_when_right": (sum(right) / len(right)) if right else None,
            "confidence_when_wrong": (sum(wrong) / len(wrong)) if wrong else None,
            "threshold": CONFIDENCE_ESCALATION_THRESHOLD,
        },
        "by_cause": _by_cause(scored),
        "rows": list(reversed(rows)),
    }


def _by_cause(scored: list[dict]) -> list[dict]:
    buckets: dict[str, list[dict]] = {}
    for row in scored:
        buckets.setdefault(row["true_cause"], []).append(row)

    out = []
    for cause, items in buckets.items():
        out.append(
            {
                "cause": cause,
                "n": len(items),
                "accuracy": sum(1 for i in items if i["match"]) / len(items),
                "amount": sum(i["amount"] or 0 for i in items),
            }
        )
    return sorted(out, key=lambda r: -r["n"])
