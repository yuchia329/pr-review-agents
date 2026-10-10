import pytest

from pr_verification.report import GateDecision, Verdict, compute_gate

CONFIRMED, REFUTED, INCONCLUSIVE = Verdict.CONFIRMED, Verdict.REFUTED, Verdict.INCONCLUSIVE


@pytest.mark.parametrize(
    "verdicts",
    [
        [REFUTED],
        [CONFIRMED, REFUTED],
        [INCONCLUSIVE, CONFIRMED, REFUTED, INCONCLUSIVE],
    ],
)
def test_gate_blocks_when_at_least_one_claim_is_refuted(verdicts: list[Verdict]) -> None:
    assert compute_gate(verdicts).decision is GateDecision.BLOCK


@pytest.mark.parametrize(
    "verdicts",
    [
        [],
        [CONFIRMED],
        [INCONCLUSIVE],
        [CONFIRMED, INCONCLUSIVE, INCONCLUSIVE],
    ],
)
def test_gate_passes_when_no_claim_is_refuted(verdicts: list[Verdict]) -> None:
    assert compute_gate(verdicts).decision is GateDecision.PASS


def test_gate_note_says_nothing_to_verify_only_when_there_are_no_claims() -> None:
    assert "nothing to verify" in compute_gate([]).note
    assert "nothing to verify" not in compute_gate([INCONCLUSIVE]).note


def test_gate_note_counts_claims_by_verdict() -> None:
    note = compute_gate([CONFIRMED, INCONCLUSIVE, INCONCLUSIVE]).note

    assert note == "Claims: 1 Confirmed, 0 Refuted, 2 Inconclusive."

# weakened
