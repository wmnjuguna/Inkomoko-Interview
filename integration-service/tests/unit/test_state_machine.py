"""The request status rules (ARCHITECTURE.md, "Request status"). Every pair of states is
checked: the allowed ones pass, every other one fails."""

from __future__ import annotations

import itertools

import pytest

from integration_service.state_machine import (
    ALLOWED,
    COMPLETED,
    ERP_PENDING,
    FAILED,
    FINERACT_DONE,
    FINERACT_PENDING,
    FINISHED,
    NEEDS_RECONCILIATION,
    RECEIVED,
    STATES,
    InvalidTransition,
    transition,
)

EXPECTED = {
    (RECEIVED, FINERACT_PENDING),
    (FINERACT_PENDING, FINERACT_DONE),
    (FINERACT_PENDING, FAILED),
    (FINERACT_PENDING, NEEDS_RECONCILIATION),
    (FAILED, FINERACT_PENDING),
    (FINERACT_DONE, ERP_PENDING),
    (FINERACT_DONE, COMPLETED),
    (ERP_PENDING, COMPLETED),
    (ERP_PENDING, NEEDS_RECONCILIATION),
    (NEEDS_RECONCILIATION, COMPLETED),
}


def test_states_are_the_seven_documented_ones() -> None:
    assert set(STATES) == {
        "RECEIVED",
        "FINERACT_PENDING",
        "FINERACT_DONE",
        "ERP_PENDING",
        "COMPLETED",
        "FAILED",
        "NEEDS_RECONCILIATION",
    }


def test_allowed_table_is_exactly_the_documented_one() -> None:
    assert set(ALLOWED) == EXPECTED


@pytest.mark.parametrize(("current", "target"), sorted(EXPECTED))
def test_allowed_moves_return_the_target(current: str, target: str) -> None:
    assert transition(current, target) == target


@pytest.mark.parametrize(
    ("current", "target"),
    sorted(set(itertools.product(STATES, STATES)) - EXPECTED),
)
def test_every_other_move_is_refused(current: str, target: str) -> None:
    with pytest.raises(InvalidTransition) as info:
        transition(current, target)
    assert current in str(info.value)
    assert target in str(info.value)


@pytest.mark.parametrize(("current", "target"), [("NOPE", COMPLETED), (RECEIVED, "NOPE")])
def test_unknown_states_are_refused(current: str, target: str) -> None:
    with pytest.raises(InvalidTransition):
        transition(current, target)


def test_completed_is_final() -> None:
    assert not any(current == COMPLETED for current, _ in ALLOWED)


def test_finished_means_the_fineract_part_is_done() -> None:
    assert set(FINISHED) == {FINERACT_DONE, ERP_PENDING, COMPLETED, NEEDS_RECONCILIATION}
