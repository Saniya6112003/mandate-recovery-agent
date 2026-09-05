"""NPCI/UPI response code reference, as a real recovery system would have it.

Descriptions are the banks' own wording, verified against Axis Bank's
published "UPI Response Codes for H2H/API" list.

Why this exists
---------------
Without it the model has to recall the NPCI code book from training, which it
cannot do reliably — measured behaviour was confident hallucination (`IE` read
as "Invalid Entity" when it means funds blocked against another mandate). That
produces wrong diagnoses for an uninteresting reason, and no production
recovery system withholds its own reference table.

What this deliberately does NOT contain
---------------------------------------
The cause label. Each entry is the bank's raw English description; mapping a
description onto one of the six `likely_cause` values — and then choosing a
bounded intervention from retry history, amount, notice window and mandate
type — is still the model's judgement, not a lookup.

The table also lists codes that never appear in the synthetic batch, so it is
a genuine reference rather than an answer key fitted to the dataset.
"""

from __future__ import annotations

# code -> the bank's own description, verbatim in meaning
CODE_BOOK: dict[str, str] = {
    # Funds
    "Z9": "Insufficient funds in customer (remitter) account",
    "IE": "Adequate funds not available in the account because funds have been blocked for a mandate",
    # Bank / technical
    "U67": "Debit timeout",
    "U68": "Credit timeout",
    "UT": "Remitter/issuer unavailable (timeout)",
    "BT": "Acquirer/beneficiary unavailable (timeout)",
    "XY": "Remitter CBS offline",
    "Y1": "Beneficiary CBS offline",
    "IR": "Unable to process due to internal exception at server/CBS on the remitter side",
    "U28": "PSP not available",
    "U27": "No response from PSP",
    "HS": "Bank's HSM is down (remitter)",
    "B7": "Bank card management system is down",
    "91": "Timeout",
    # Limits
    "Z8": "Per-transaction limit exceeded as set by the remitting member",
    "Z7": "Transaction frequency limit exceeded as set by the remitting member",
    "ZU": "Limit exceeded for remitting/issuing bank",
    "M2": "Amount limit exceeded for customer",
    "M6": "Limit exceeded for member bank",
    "FL": "First transaction limit exceeded",
    # Mandate state
    "VA": "Mandate has been revoked",
    "VT": "Mandate is paused",
    "QA": "Mandate is paused by user",
    "QB": "Mandate is already honoured",
    "MA0": "Mandate not present",
    "IB": "Revoke mandate after the remitter unblocked the amount",
    "VB": "Incorrect recurrence pattern",
    # Authentication
    "ZM": "Invalid / incorrect MPIN",
    "Z6": "Number of PIN tries exceeded",
    "AM": "MPIN not set by customer",
    "ZR": "Invalid / incorrect OTP",
    "ZS": "OTP time expired",
    "75": "Excessive PIN tries",
    # Declines with no stated reason
    "ZA": "Transaction declined",
    "U30": "Debit has been failed",
    "B3": "Transaction not permitted to the account",
    "U16": "Risk threshold exceeded",
    "04": "Technical decline",
}


def render_reference() -> str:
    """The code book as prompt text, sorted for a stable cacheable prefix."""
    lines = [f"  {code} = {desc}" for code, desc in sorted(CODE_BOOK.items())]
    return "\n".join(lines)


def describe(code: str) -> str:
    return CODE_BOOK.get(code, "no published description for this code")
