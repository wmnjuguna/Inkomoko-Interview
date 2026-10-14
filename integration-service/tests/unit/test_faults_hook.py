"""The J-5 crash hook: off unless FAULT_CRASH_AFTER_FINERACT_DONE names a reference prefix,
and then only for references that start with it."""

from __future__ import annotations

import pytest

from integration_service import faults


class Died(BaseException):
    pass


@pytest.fixture
def deaths(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    def die() -> None:
        seen.append("died")
        raise Died

    monkeypatch.setattr(faults, "_die", die)
    return seen


def test_off_when_the_variable_is_not_set(
    monkeypatch: pytest.MonkeyPatch, deaths: list[str]
) -> None:
    monkeypatch.delenv(faults.CRASH_ENV, raising=False)
    faults.crash_after_fineract_done("J5-ABC")
    assert deaths == []


@pytest.mark.parametrize("value", ["", "   "])
def test_off_when_the_variable_is_blank(
    monkeypatch: pytest.MonkeyPatch, deaths: list[str], value: str
) -> None:
    monkeypatch.setenv(faults.CRASH_ENV, value)
    faults.crash_after_fineract_done("J5-ABC")
    assert deaths == []


def test_other_references_are_never_hit(monkeypatch: pytest.MonkeyPatch, deaths: list[str]) -> None:
    monkeypatch.setenv(faults.CRASH_ENV, "J5-")
    faults.crash_after_fineract_done("I1-DSB-123")
    assert deaths == []


def test_crashes_for_a_matching_reference(
    monkeypatch: pytest.MonkeyPatch, deaths: list[str]
) -> None:
    monkeypatch.setenv(faults.CRASH_ENV, "J5-")
    with pytest.raises(Died):
        faults.crash_after_fineract_done("J5-DSB-1")
    assert deaths == ["died"]


def test_crash_prefix_reads_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(faults.CRASH_ENV, " J5- ")
    assert faults.crash_prefix() == "J5-"
    monkeypatch.delenv(faults.CRASH_ENV)
    assert faults.crash_prefix() is None
