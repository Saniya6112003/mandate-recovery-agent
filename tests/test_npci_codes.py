"""The code reference must stay a reference, not an answer key.

Supplying the bank's own descriptions is realistic — a production recovery
system has the NPCI code book. Supplying the *cause label* would make
`likely_cause` a restatement of an input field, which is the "lookup table
wearing an LLM costume" the project brief warns against. These tests pin
that line.
"""

from __future__ import annotations

from agent.npci_codes import CODE_BOOK, describe, render_reference
from agent.schemas import LikelyCause
from data.generate_dataset import CAUSE_CODES

CAUSE_LABELS = {
    "insufficient_funds",
    "bank_timeout",
    "mandate_revoked",
    "daily_limit_exceeded",
    "authentication_failure",
}


def test_reference_never_contains_a_cause_label():
    """If a description ever reads 'insufficient_funds', the model is being
    handed the answer rather than the bank's wording.
    """
    reference = render_reference().lower()

    for label in CAUSE_LABELS:
        assert label not in reference, f"code book leaks the cause label {label!r}"


def test_every_dataset_code_is_documented():
    """A code the model sees but cannot look up sends it back to guessing."""
    for codes in CAUSE_CODES.values():
        for code in codes:
            assert code in CODE_BOOK, f"{code} appears in the dataset but not the code book"


def test_reference_is_broader_than_the_dataset():
    """A book containing only the dataset's codes would be an answer key
    fitted to the batch rather than a real reference table.
    """
    dataset_codes = {code for codes in CAUSE_CODES.values() for code in codes}

    assert len(CODE_BOOK) > len(dataset_codes) + 5


def test_reference_is_stable_across_calls():
    """Sorted output keeps the prompt prefix byte-stable, which matters for
    prompt caching and for reproducible runs.
    """
    assert render_reference() == render_reference()


def test_unknown_code_degrades_gracefully():
    assert "no published description" in describe("NOT_A_CODE")


def test_ambiguous_codes_have_no_diagnostic_description():
    """ZA / U30 are declines with no stated reason. Their descriptions must
    not accidentally explain a cause, or the `unknown` bucket becomes solvable
    by lookup and stops testing anything.
    """
    for code in ("ZA", "U30"):
        desc = describe(code).lower()
        assert "insufficient" not in desc
        assert "limit" not in desc
        assert "mandate" not in desc
        assert "mpin" not in desc
