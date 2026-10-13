"""Parallel requests against the running Fineract: a loan is paid out exactly once whatever
the partner sends at the same moment, and parallel repayments each land exactly once.
Every request is released by one barrier. Fixtures and helpers come from test_disburse_api."""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Callable
from typing import Any

from flask.testing import FlaskClient
from flask_openapi3.openapi import OpenAPI
from test_disburse_api import (  # noqa: F401 (fixtures are used by name)
    _clean,
    checked,
    disburse,
    fineract,
    fineract_transactions,
    new_loan,
    settings,
    tag,
)

from integration_service.clients.fineract import FineractClient
from integration_service.services.money import transaction_path

# Fixtures imported from test_disburse_api are used as test arguments of the same name.
# ruff: noqa: F811


def in_parallel(app: OpenAPI, calls: list[Callable[[FlaskClient], Any]]) -> list[Any]:
    """Runs each call on its own thread and test client, all released together."""
    barrier = threading.Barrier(len(calls))
    results: list[Any] = [None] * len(calls)

    def run(i: int) -> None:
        client = app.test_client()
        barrier.wait()
        try:
            results[i] = calls[i](client)
        except Exception as exc:  # report it in the results, not as a lost thread
            results[i] = exc

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(calls))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(120)
    return results


def statuses(results: list[Any]) -> Counter[int]:
    for result in results:
        assert not isinstance(result, Exception), result
    return Counter(r.status_code for r in results)


def test_ten_requests_with_one_reference_pay_out_once(
    app: OpenAPI, client: FlaskClient, auth: dict[str, str], fineract: FineractClient
) -> None:
    loan = new_loan(client, auth)
    ref = f"IT-DSB-{tag()}"
    results = in_parallel(app, [lambda c: disburse(c, auth, loan, ref) for _ in range(10)])
    counts = statuses(results)
    assert set(counts) <= {200, 202}, counts
    assert counts[200] >= 1
    bodies = [r.get_json() for r in results if r.status_code == 200]
    assert all(body == bodies[0] for body in bodies)
    assert len(fineract_transactions(fineract, loan, "disbursement")) == 1
    assert disburse(client, auth, loan, ref).get_json() == bodies[0]


def test_ten_references_for_one_loan_pay_out_once(
    app: OpenAPI, client: FlaskClient, auth: dict[str, str], fineract: FineractClient
) -> None:
    loan = new_loan(client, auth)
    refs = [f"IT-DSB-{tag()}" for _ in range(10)]
    results = in_parallel(app, [lambda c, ref=ref: disburse(c, auth, loan, ref) for ref in refs])
    counts = statuses(results)
    assert counts == Counter({200: 1, 409: 9}), counts
    codes = {r.get_json()["error"]["code"] for r in results if r.status_code == 409}
    assert codes == {"LOAN_ALREADY_DISBURSED"}
    txns = fineract_transactions(fineract, loan, "disbursement")
    assert len(txns) == 1
    winner = next(ref for ref, r in zip(refs, results, strict=True) if r.status_code == 200)
    assert txns[0]["externalId"] == f"DSB:{winner}"


def test_parallel_repayments_each_land_once(
    app: OpenAPI, client: FlaskClient, auth: dict[str, str], fineract: FineractClient
) -> None:
    loan = new_loan(client, auth)
    assert disburse(client, auth, loan, f"IT-DSB-{tag()}").status_code == 200
    before = client.get(f"/api/loans/{loan}", headers=auth).get_json()["balances"]
    refs = [f"IT-RPY-{tag()}" for _ in range(5)]
    amounts = {ref: 10000 * (i + 1) for i, ref in enumerate(refs)}

    def repay(c: FlaskClient, ref: str) -> Any:
        return checked(
            c.post(
                f"/api/loans/{loan}/repayments",
                json={"external_reference": ref, "amount": amounts[ref]},
                headers=auth,
            )
        )

    # The five references, plus five more copies of the first one.
    plan = refs + [refs[0]] * 5
    results = in_parallel(app, [lambda c, ref=ref: repay(c, ref) for ref in plan])
    counts = statuses(results)
    assert set(counts) <= {200, 202}, counts
    for ref, result in zip(plan[:5], results[:5], strict=True):
        assert result.status_code in (200, 202), (ref, result.get_json())

    txns = fineract_transactions(fineract, loan, "repayment")
    assert sorted(t["externalId"] for t in txns) == sorted(f"RPY:{ref}" for ref in refs)
    for ref in refs:
        found = fineract.get(transaction_path(loan, f"RPY:{ref}"))
        assert int(found["amount"]) == amounts[ref]

    principal_paid = sum(int(t["principalPortion"]) for t in txns)
    after = client.get(f"/api/loans/{loan}", headers=auth).get_json()["balances"]
    assert after["principal_outstanding"] == before["principal_outstanding"] - principal_paid
    assert after["total_repaid"] == sum(amounts.values())

    # Each reference now replays its own saved answer, with Fineract's split.
    for ref in refs:
        replay = repay(client, ref)
        assert replay.status_code == 200
        found = fineract.get(transaction_path(loan, f"RPY:{ref}"))
        assert replay.get_json()["principal_portion"] == int(found["principalPortion"])
