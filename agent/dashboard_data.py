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
        "by_action": _counted(rows, "action"),
        "by_outcome": _counted(rows, "outcome"),
        "by_code": _counted(rows, "code", limit=8),
        "overrides_by_rule": _override_rules(rows),
        "confidence_points": [
            {"c": r["confidence"], "ok": r["match"]} for r in scored
        ],
        "money": {
            "recovered": amount_recovered,
            "in_flight": sum(r["amount"] or 0 for r in rows if r["outcome"] == "pending"),
            "escalated": sum(r["amount"] or 0 for r in rows if r["outcome"] == "escalated"),
        },
        "rows": list(reversed(rows)),
    }


def _counted(rows: list[dict], field: str, limit: int | None = None) -> list[dict]:
    counts: dict[str, int] = {}
    amounts: dict[str, float] = {}
    for row in rows:
        key = row.get(field)
        if key is None:
            continue
        counts[key] = counts.get(key, 0) + 1
        amounts[key] = amounts.get(key, 0) + (row.get("amount") or 0)

    out = [
        {"key": k, "n": n, "amount": amounts[k], "share": n / len(rows) if rows else 0}
        for k, n in counts.items()
    ]
    out.sort(key=lambda r: -r["n"])
    return out[:limit] if limit else out


def _override_rules(rows: list[dict]) -> list[dict]:
    """Group overrides by which rule fired — the reason string is prefixed
    with the rule name, e.g. `retry_cap_exceeded: ...`.
    """
    counts: dict[str, int] = {}
    for row in rows:
        if not row["overridden"] or not row["override_reason"]:
            continue
        rule = row["override_reason"].split(":")[0].strip()
        counts[rule] = counts.get(rule, 0) + 1
    return sorted(
        ({"rule": k, "n": v} for k, v in counts.items()), key=lambda r: -r["n"]
    )


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
