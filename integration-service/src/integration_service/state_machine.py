"""The request status rules for one operation (ARCHITECTURE.md, "Request status").

Pure: no database, no I/O. services/operations.py calls transition() before every status
change and records the move in operation_events."""

from __future__ import annotations

RECEIVED = "RECEIVED"
FINERACT_PENDING = "FINERACT_PENDING"
FINERACT_DONE = "FINERACT_DONE"
ERP_PENDING = "ERP_PENDING"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"

STATES = (
    RECEIVED,
    FINERACT_PENDING,
    FINERACT_DONE,
    ERP_PENDING,
    COMPLETED,
    FAILED,
    NEEDS_RECONCILIATION,
)

# (from, to): why the move happens.
ALLOWED: dict[tuple[str, str], str] = {
    (RECEIVED, FINERACT_PENDING): "saved and claimed",
    (FINERACT_PENDING, FINERACT_DONE): "Fineract said yes, or a lookup found it",
    (FINERACT_PENDING, FAILED): "Fineract refused, was not reachable, or a lookup found nothing",
    (FINERACT_PENDING, NEEDS_RECONCILIATION): "too many attempts",
    (FAILED, FINERACT_PENDING): "retried with the same reference",
    (FINERACT_DONE, ERP_PENDING): "journal entry waiting for the ERP",
    (FINERACT_DONE, COMPLETED): "nothing to send to the ERP",
    (ERP_PENDING, COMPLETED): "the ERP saved it, or already had it",
    (ERP_PENDING, NEEDS_RECONCILIATION): "too many attempts, or the ERP said 409",
    (NEEDS_RECONCILIATION, COMPLETED): "fixed by an explicit repair",
}

# The Fineract part is done. A repeat of the request gets the saved answer.
FINISHED = (FINERACT_DONE, ERP_PENDING, COMPLETED, NEEDS_RECONCILIATION)


class InvalidTransition(ValueError):  # noqa: N818 (the name the plan uses)
    """The move is not in the ALLOWED table. Nothing was changed."""


def transition(current: str, target: str) -> str:
    """Returns target when current -> target is allowed, else raises InvalidTransition."""
    if (current, target) not in ALLOWED:
        raise InvalidTransition(f"{current} -> {target} is not allowed")
    return target
