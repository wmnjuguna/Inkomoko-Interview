"""POST /api/loans/{loan_reference}/disburse against the running Fineract (as the
least-privilege API user) and the running Mock ERP.

The app runs with the outbox sender off, so each test can look at the outbox row and then
send it with send_due() itself. Fineract is shared and keeps its data: every test makes its
own client, loan and references, and nothing is deleted. Every answer is checked for
Fineract ids."""

from __future__ import annotations

import os
import re
import secrets
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest
from flask.testing import FlaskClient
from sqlalchemy import Engine, select, text

from integration_service.clients.erp import ErpClient
from integration_service.clients.errors import (
    FineractNotFound,
    FineractTimeout,
    FineractUnavailable,
)
from integration_service.clients.fineract import FineractClient
from integration_service.config import Settings
from integration_service.errors import ApiError, ErrorCode
from integration_service.mapping.journal import load_gl_role_accounts
from integration_service.models import outbox
from integration_service.schemas.money import DisbursementRequest
from integration_service.services import money
from integration_service.services.loan_locks import loan_lock
from integration_service.services.loans import fineract_from_settings, kigali_today
from integration_service.services.money import (
    disburse_loan,
    recover_money_operation,
    transaction_path,
)
from integration_service.services.operations import get_operation
from integration_service.services.outbox import send_due
from integration_service.services.recovery import recover_once

PARTNER_A = "partner-a"
ID_KEY = re.compile(r"(^id$|_id$|Id$)")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GL_FILE = os.path.join(os.path.dirname(ROOT), "fineract/configuration/loan-product.json")


def accounts() -> Any:
    return load_gl_role_accounts(GL_FILE)


# --- fixtures and helpers --------------------------------------------------------------------


@pytest.fixture
def settings(settings: Settings) -> Settings:
    password = os.environ.get("FINERACT_API_PASSWORD", "")
    if not password:
        pytest.skip("FINERACT_API_PASSWORD is not set (the API user's password from .env)")
    return replace(
        settings,
        fineract_username=os.environ.get("FINERACT_API_USERNAME", "integration-api"),
        fineract_password=password,
        gl_roles_config_path=GL_FILE,
        outbox_enabled=False,
    )


@pytest.fixture(autouse=True)
def _clean(db: Engine) -> None:
    start = 100000 + secrets.randbelow(890000)
    with db.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE outbox, operation_events, loans, clients, operations "
                "RESTART IDENTITY CASCADE"
            )
        )
        conn.execute(text(f"ALTER SEQUENCE loan_reference_seq RESTART WITH {start}"))


@pytest.fixture
def fineract(settings: Settings) -> Iterator[FineractClient]:
    client = fineract_from_settings(settings)
    yield client
    client.close()


@pytest.fixture
def admin(settings: Settings) -> Iterator[FineractClient]:
    client = FineractClient(
        base_url=settings.fineract_base_url,
        tenant=settings.fineract_tenant,
        username=os.environ.get("FINERACT_USERNAME", "mifos"),
        password=os.environ.get("FINERACT_PASSWORD", "password"),
        timeout_seconds=30,
    )
    yield client
    client.close()


@pytest.fixture
def erp(settings: Settings) -> Iterator[ErpClient]:
    client = ErpClient(settings.erp_base_url, settings.erp_api_key, timeout_seconds=10.0)
    yield client
    client.close()


def tag() -> str:
    return f"{secrets.token_hex(3)}-{secrets.randbelow(10**6)}"


def id_keys(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, inner in value.items():
            if ID_KEY.search(key) and key != "correlation_id":
                found.append(key)
            found += id_keys(inner)
    elif isinstance(value, list):
        for inner in value:
            found += id_keys(inner)
    return found


def checked(response: Any) -> Any:
    """Every answer: no Fineract id anywhere."""
    assert id_keys(response.get_json()) == []
    return response


def new_loan(
    client: FlaskClient, auth: dict[str, str], principal: int = 1200000, approve: bool = True
) -> str:
    uid = f"ITM-{secrets.randbelow(10**6):06d}"
    made = client.post(
        "/api/clients",
        json={
            "external_reference": f"IT-MCLI-{tag()}",
            "client_uid": uid,
            "first_name": "Monica",
            "last_name": "Moneytest",
        },
        headers=auth,
    )
    assert made.status_code == 201, made.get_json()
    applied = client.post(
        "/api/loans",
        json={"external_reference": f"IT-MAPP-{tag()}", "client_uid": uid, "principal": principal},
        headers=auth,
    )
    assert applied.status_code == 201, applied.get_json()
    loan = str(applied.get_json()["loan_reference"])
    if approve:
        approved = client.post(
            f"/api/loans/{loan}/approve",
            json={"external_reference": f"IT-MAPR-{tag()}"},
            headers=auth,
        )
        assert approved.status_code == 200, approved.get_json()
    return loan


def disburse(client: FlaskClient, auth: dict[str, str], loan: str, ref: str, **extra: Any) -> Any:
    return checked(
        client.post(
            f"/api/loans/{loan}/disburse", json={"external_reference": ref, **extra}, headers=auth
        )
    )


def fineract_transactions(fineract: FineractClient, loan: str, kind: str) -> list[dict[str, Any]]:
    """The loan's transactions of one kind (disbursement, repayment), not reversed."""
    data = fineract.get(f"/loans/external-id/{loan}", params={"associations": "transactions"})
    return [
        t
        for t in data.get("transactions") or []
        if t["type"]["code"] == f"loanTransactionType.{kind}" and not t.get("manuallyReversed")
    ]


def outbox_entry(db: Engine, op_key: str) -> dict[str, Any]:
    op = get_operation(db, op_key[:3], op_key[4:])
    assert op is not None
    with db.connect() as conn:
        row = conn.execute(select(outbox).where(outbox.c.operation_id == op.id)).mappings().one()
    return dict(row)


def history(client: FlaskClient, auth: dict[str, str], ref: str) -> dict[str, Any]:
    response = checked(client.get(f"/api/transactions/{ref}", headers=auth))
    assert response.status_code == 200
    ops = response.get_json()["operations"]
    assert len(ops) == 1
    return dict(ops[0])


class Flaky:
    """The real Fineract, with chosen failures: the first `fail_posts` writes raise
    `post_error` (after_send=True: the write reaches Fineract, then its answer is lost), the
    first `fail_gets` reads raise FineractTimeout, and the first `fail_gets_after_post` reads
    after a clean write raise FineractTimeout too."""

    def __init__(
        self,
        real: FineractClient,
        post_error: Exception | None = None,
        fail_posts: int = 0,
        after_send: bool = False,
        fail_gets: int = 0,
        fail_gets_after_post: int = 0,
    ) -> None:
        self.real = real
        self.post_error = post_error
        self.fail_posts = fail_posts
        self.after_send = after_send
        self.fail_gets = fail_gets
        self.fail_gets_after_post = fail_gets_after_post
        self.posted = False
        self.post_keys: list[str | None] = []

    def get(self, path: str, params: Any = None, correlation_id: str | None = None) -> Any:
        if self.fail_gets:
            self.fail_gets -= 1
            raise FineractTimeout("no answer (test)")
        if self.posted and self.fail_gets_after_post:
            self.fail_gets_after_post -= 1
            raise FineractTimeout("no answer (test)")
        return self.real.get(path, params=params, correlation_id=correlation_id)

    def post(
        self,
        path: str,
        body: Any,
        idempotency_key: str | None = None,
        params: Any = None,
        correlation_id: str | None = None,
    ) -> Any:
        self.post_keys.append(idempotency_key)
        if self.fail_posts and self.post_error is not None:
            self.fail_posts -= 1
            if self.after_send:
                self.real.post(path, body, idempotency_key, params, correlation_id)
            raise self.post_error
        answer = self.real.post(path, body, idempotency_key, params, correlation_id)
        self.posted = True
        return answer


class LandsLate:
    """The race the rule "look it up before deciding" guards against. Attempt 1 is not
    sent (FineractUnavailable). When the same reference is sent again, attempt 1 lands in
    Fineract after all, just before attempt 2: on the write itself (when="post") or on the
    loan read that comes before a repayment (when="loan_read"). The lookup made before
    attempt 2 comes too early to see it."""

    def __init__(self, real: FineractClient, when: str = "post") -> None:
        self.real = real
        self.when = when
        self.first: tuple[str, Any, Any] | None = None
        self.landed = False
        self.post_keys: list[str | None] = []

    def _land(self, correlation_id: str | None) -> None:
        if self.first is not None and not self.landed:
            self.landed = True
            path, body, params = self.first
            self.real.post(path, body, f"{self.post_keys[0]}-late", params, correlation_id)

    def get(self, path: str, params: Any = None, correlation_id: str | None = None) -> Any:
        if self.first is not None and not self.landed:
            if "/transactions/external-id/" in path:
                raise FineractNotFound(404, {})  # too early to see attempt 1
            if self.when == "loan_read" and "/transactions" not in path and params is None:
                self._land(correlation_id)
        return self.real.get(path, params=params, correlation_id=correlation_id)

    def post(
        self,
        path: str,
        body: Any,
        idempotency_key: str | None = None,
        params: Any = None,
        correlation_id: str | None = None,
    ) -> Any:
        self.post_keys.append(idempotency_key)
        if self.first is None:
            self.first = (path, body, params)
            raise FineractUnavailable("down (test)")
        self._land(correlation_id)
        return self.real.post(path, body, idempotency_key, params, correlation_id)


# --- tests -------------------------------------------------------------------------------------


def test_disburse_pays_out_once_and_the_erp_gets_fineracts_entry(
    client: FlaskClient,
    auth: dict[str, str],
    db: Engine,
    fineract: FineractClient,
    erp: ErpClient,
) -> None:
    loan = new_loan(client, auth)
    ref = f"IT-DSB-{tag()}"
    response = disburse(client, auth, loan, ref)
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body == {
        "loan_reference": loan,
        "client_uid": body["client_uid"],
        "external_reference": ref,
        "type": "DISBURSEMENT",
        "status": "ERP_PENDING",
        "amount": 1200000,
        "transaction_date": kigali_today().isoformat(),
        "erp_status": "PENDING",
        "loan_status": "ACTIVE",
    }

    # Fineract's own transaction, found by our op key.
    txn = fineract.get(transaction_path(loan, f"DSB:{ref}"))
    assert txn["type"]["code"] == "loanTransactionType.disbursement"
    amount = int(txn["amount"])
    assert amount == body["amount"]

    row = outbox_entry(db, f"DSB:{ref}")
    assert row["status"] == "PENDING"
    assert row["entry"]["external_reference"] == f"DSB:{ref}"
    assert row["entry"]["source"] == "DISBURSEMENT"
    assert row["entry"]["entry_date"] == kigali_today().isoformat()
    assert row["entry"]["lines"] == [
        {"account_code": "1200", "debit": amount},
        {"account_code": "1100", "credit": amount},
    ]

    report = send_due(db, erp)
    assert report.sent == 1
    saved = erp.get_journal_entry(f"DSB:{ref}")
    assert saved is not None
    assert saved["total"] == amount
    assert [(line["account_code"], line["debit"], line["credit"]) for line in saved["lines"]] == [
        ("1200", amount, 0),
        ("1100", 0, amount),
    ]
    op = history(client, auth, ref)
    assert (op["status"], op["erp_status"]) == ("COMPLETED", "SENT")
    assert [h["to"] for h in op["history"]] == [
        "RECEIVED",
        "FINERACT_PENDING",
        "FINERACT_DONE",
        "ERP_PENDING",
        "COMPLETED",
    ]


def test_replay_gives_the_same_body_and_fineract_has_one_disbursement(
    client: FlaskClient, auth: dict[str, str], fineract: FineractClient
) -> None:
    loan = new_loan(client, auth)
    ref = f"IT-DSB-{tag()}"
    first = disburse(client, auth, loan, ref)
    assert first.status_code == 200
    again = disburse(client, auth, loan, ref)
    assert again.status_code == 200
    assert again.get_json() == first.get_json()
    assert len(fineract_transactions(fineract, loan, "disbursement")) == 1

    changed = disburse(client, auth, loan, ref, transaction_date="2026-01-01")
    assert changed.status_code == 409
    assert changed.get_json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_another_reference_for_the_same_loan_is_409(
    client: FlaskClient, auth: dict[str, str], fineract: FineractClient
) -> None:
    loan = new_loan(client, auth)
    assert disburse(client, auth, loan, f"IT-DSB-{tag()}").status_code == 200
    other_ref = f"IT-DSB-{tag()}"
    other = disburse(client, auth, loan, other_ref)
    assert other.status_code == 409
    assert other.get_json()["error"]["code"] == "LOAN_ALREADY_DISBURSED"
    assert len(fineract_transactions(fineract, loan, "disbursement")) == 1
    assert client.get(f"/api/transactions/{other_ref}", headers=auth).status_code == 404


def test_fineract_refuses_a_loan_paid_out_elsewhere_and_we_say_409(
    client: FlaskClient,
    auth: dict[str, str],
    admin: FineractClient,
    fineract: FineractClient,
) -> None:
    """L5: our tables know of no disbursement, but Fineract's loan state does."""
    loan = new_loan(client, auth)
    admin.post(
        f"/loans/external-id/{loan}",
        {
            "actualDisbursementDate": kigali_today().isoformat(),
            "transactionAmount": 1200000,
            "externalId": f"OUT-{tag()}",
            "dateFormat": "yyyy-MM-dd",
            "locale": "en",
        },
        params={"command": "disburse"},
    )
    ref = f"IT-DSB-{tag()}"
    response = disburse(client, auth, loan, ref)
    assert response.status_code == 409, response.get_json()
    error = response.get_json()["error"]
    assert error["code"] == "LOAN_ALREADY_DISBURSED"
    assert "identifier" not in error["message"]
    assert history(client, auth, ref)["status"] == "FAILED"
    assert len(fineract_transactions(fineract, loan, "disbursement")) == 1


def test_a_loan_not_approved_yet_is_422_and_failed(
    client: FlaskClient, auth: dict[str, str]
) -> None:
    loan = new_loan(client, auth, approve=False)
    ref = f"IT-DSB-{tag()}"
    response = disburse(client, auth, loan, ref)
    assert response.status_code == 422, response.get_json()
    assert "not approved" in response.get_json()["error"]["message"]
    assert history(client, auth, ref)["status"] == "FAILED"


def test_unknown_loan_is_404(client: FlaskClient, auth: dict[str, str]) -> None:
    response = disburse(client, auth, "LN-2026-999999", f"IT-DSB-{tag()}")
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "LOAN_NOT_FOUND"


@pytest.mark.parametrize(
    "extra", [{"transaction_date": "2999-01-01"}, {"amount": 5}, {"transaction_date": "x"}]
)
def test_bad_requests_are_422_and_save_nothing(
    client: FlaskClient, auth: dict[str, str], extra: dict[str, Any]
) -> None:
    ref = f"IT-DSB-{tag()}"
    response = disburse(client, auth, "LN-2026-999999", ref, **extra)
    assert response.status_code == 422
    assert client.get(f"/api/transactions/{ref}", headers=auth).status_code == 404


# --- failures, with the real Fineract behind a flaky stand-in ---------------------------------


def run_disburse(db: Engine, fake: Any, loan: str, ref: str) -> tuple[int, dict[str, Any]]:
    try:
        answer = disburse_loan(
            db, fake, loan, DisbursementRequest(external_reference=ref), PARTNER_A, None, accounts
        )
    except ApiError as exc:
        return exc.status, {"code": exc.code, "message": exc.message, "details": exc.details}
    return answer.status, answer.body


def test_not_sent_is_failed_and_the_same_reference_then_works_with_attempt_two(
    client: FlaskClient, auth: dict[str, str], db: Engine, fineract: FineractClient
) -> None:
    loan = new_loan(client, auth)
    ref = f"IT-DSB-{tag()}"
    fake = Flaky(fineract, FineractUnavailable("down (test)"), fail_posts=1)

    status, body = run_disburse(db, fake, loan, ref)
    assert status == 503
    assert body["code"] == ErrorCode.FINERACT_UNAVAILABLE
    op = get_operation(db, "DSB", ref)
    assert op is not None and op.status == "FAILED"
    assert fineract_transactions(fineract, loan, "disbursement") == []

    status, body = run_disburse(db, fake, loan, ref)
    assert status == 200, body
    assert fake.post_keys == [f"DSB:{ref}:1", f"DSB:{ref}:2"]
    assert len(fineract_transactions(fineract, loan, "disbursement")) == 1


def test_lost_answer_is_looked_up_and_not_sent_again(
    client: FlaskClient, auth: dict[str, str], db: Engine, fineract: FineractClient
) -> None:
    loan = new_loan(client, auth)
    ref = f"IT-DSB-{tag()}"
    fake = Flaky(fineract, FineractTimeout("answer lost (test)"), fail_posts=1, after_send=True)

    status, body = run_disburse(db, fake, loan, ref)
    assert status == 200, body
    assert body["amount"] == 1200000
    assert fake.post_keys == [f"DSB:{ref}:1"]
    assert len(fineract_transactions(fineract, loan, "disbursement")) == 1
    events = history(client, auth, ref)["history"]
    assert [e["to"] for e in events][-2:] == ["FINERACT_DONE", "ERP_PENDING"]


def test_unclear_with_a_failed_lookup_is_504_then_202_then_recovery_finishes_it(
    client: FlaskClient, auth: dict[str, str], db: Engine, fineract: FineractClient
) -> None:
    loan = new_loan(client, auth)
    ref = f"IT-DSB-{tag()}"
    fake = Flaky(
        fineract, FineractTimeout("answer lost (test)"), fail_posts=1, after_send=True, fail_gets=1
    )

    status, body = run_disburse(db, fake, loan, ref)
    assert status == 504
    assert body["code"] == ErrorCode.FINERACT_TIMEOUT
    assert "identifier" not in body["message"]
    op = get_operation(db, "DSB", ref)
    assert op is not None and op.status == "FINERACT_PENDING"

    repeat = disburse(client, auth, loan, ref)
    assert repeat.status_code == 202
    assert repeat.get_json()["status"] == "FINERACT_PENDING"
    assert fake.post_keys == [f"DSB:{ref}:1"]

    assert recover_money_operation(db, fineract, op, accounts) == "done"
    replay = disburse(client, auth, loan, ref)
    assert replay.status_code == 200
    assert replay.get_json()["amount"] == 1200000
    assert replay.get_json()["loan_status"] == "ACTIVE"
    assert replay.get_json()["erp_status"] == "PENDING"
    assert len(fineract_transactions(fineract, loan, "disbursement")) == 1


def test_recovery_of_a_disbursement_fineract_never_got_is_failed(
    client: FlaskClient, auth: dict[str, str], db: Engine, fineract: FineractClient
) -> None:
    loan = new_loan(client, auth)
    ref = f"IT-DSB-{tag()}"
    fake = Flaky(fineract, FineractTimeout("no answer (test)"), fail_posts=1, fail_gets=1)
    status, _ = run_disburse(db, fake, loan, ref)
    assert status == 504
    op = get_operation(db, "DSB", ref)
    assert op is not None
    assert recover_money_operation(db, fineract, op, accounts) == "failed"
    status, body = run_disburse(db, fineract, loan, ref)
    assert status == 200, body
    assert len(fineract_transactions(fineract, loan, "disbursement")) == 1


def test_recovery_pass_finishes_it_with_the_routes_answer(
    client: FlaskClient,
    auth: dict[str, str],
    db: Engine,
    fineract: FineractClient,
    erp: ErpClient,
) -> None:
    """The production recovery pass (default recoverers, as the loop and POST /admin/recover
    run it) saves the route's own answer."""
    loan = new_loan(client, auth)
    ref = f"IT-DSB-{tag()}"
    fake = Flaky(
        fineract, FineractTimeout("answer lost (test)"), fail_posts=1, after_send=True, fail_gets=1
    )
    assert run_disburse(db, fake, loan, ref)[0] == 504

    report = recover_once(
        db,
        fineract,
        erp,
        accounts=accounts,
        stale_after_seconds=0,
        send_outbox=False,
    )
    assert (report.operations.checked, report.operations.completed) == (1, 1)
    replay = disburse(client, auth, loan, ref)
    assert replay.status_code == 200
    assert set(replay.get_json()) == {
        "loan_reference",
        "client_uid",
        "external_reference",
        "type",
        "status",
        "amount",
        "transaction_date",
        "erp_status",
        "loan_status",
    }


def test_unclear_and_not_found_yet_stays_pending_until_recovery_decides(
    client: FlaskClient,
    auth: dict[str, str],
    db: Engine,
    fineract: FineractClient,
    erp: ErpClient,
) -> None:
    """A timeout and an empty lookup right after it prove nothing: Fineract may still be
    working on it. The partner is not told "nothing was done"; recovery decides later."""
    loan = new_loan(client, auth)
    ref = f"IT-DSB-{tag()}"
    fake = Flaky(fineract, FineractTimeout("no answer (test)"), fail_posts=1)

    status, body = run_disburse(db, fake, loan, ref)
    assert status == 504, body
    op = get_operation(db, "DSB", ref)
    assert op is not None and op.status == "FINERACT_PENDING"
    assert disburse(client, auth, loan, ref).status_code == 202
    # The loan stays held by this reference: another one cannot slip in meanwhile.
    assert disburse(client, auth, loan, f"IT-DSB-{tag()}").status_code == 409

    report = recover_once(
        db, fineract, erp, accounts=accounts, stale_after_seconds=0, send_outbox=False
    )
    assert report.operations.failed == 1
    status, body = run_disburse(db, fineract, loan, ref)
    assert status == 200, body
    assert len(fineract_transactions(fineract, loan, "disbursement")) == 1


def test_an_earlier_attempt_that_lands_late_is_ours_not_a_conflict(
    client: FlaskClient, auth: dict[str, str], db: Engine, fineract: FineractClient
) -> None:
    """Attempt 2 is refused because attempt 1 got there first. The refused write carries
    our own externalId, so it is looked up and finished, never reported as a conflict."""
    loan = new_loan(client, auth)
    ref = f"IT-DSB-{tag()}"
    fake = LandsLate(fineract)
    assert run_disburse(db, fake, loan, ref)[0] == 503
    status, body = run_disburse(db, fake, loan, ref)
    assert status == 200, body
    assert body["amount"] == 1200000
    op = get_operation(db, "DSB", ref)
    assert op is not None and op.status == "ERP_PENDING"
    assert len(fineract_transactions(fineract, loan, "disbursement")) == 1
    assert outbox_entry(db, f"DSB:{ref}")["status"] == "PENDING"


def test_fineract_took_it_but_the_read_back_failed_is_202_then_recovery_finishes(
    client: FlaskClient,
    auth: dict[str, str],
    db: Engine,
    fineract: FineractClient,
    erp: ErpClient,
) -> None:
    loan = new_loan(client, auth)
    ref = f"IT-DSB-{tag()}"
    fake = Flaky(fineract, fail_gets_after_post=1)
    status, body = run_disburse(db, fake, loan, ref)
    assert status == 202, body
    assert body["status"] == "FINERACT_PENDING"
    op = get_operation(db, "DSB", ref)
    assert op is not None and op.status == "FINERACT_PENDING"
    assert op.next_attempt_at is not None

    report = recover_once(db, fineract, erp, accounts=accounts, send_outbox=False)
    assert report.operations.completed == 1
    replay = disburse(client, auth, loan, ref)
    assert replay.status_code == 200
    assert replay.get_json()["amount"] == 1200000
    assert fake.post_keys == [f"DSB:{ref}:1"]
    assert len(fineract_transactions(fineract, loan, "disbursement")) == 1


def test_a_busy_loan_is_503_failed_and_nothing_is_sent(
    client: FlaskClient,
    auth: dict[str, str],
    db: Engine,
    fineract: FineractClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loan = new_loan(client, auth)
    ref = f"IT-DSB-{tag()}"
    monkeypatch.setattr(
        money, "loan_lock", lambda engine, ref: loan_lock(engine, ref, timeout_seconds=0.3)
    )
    fake = Flaky(fineract)
    with loan_lock(db, loan):
        status, body = run_disburse(db, fake, loan, ref)
    assert status == 503, body
    assert body["code"] == ErrorCode.FINERACT_UNAVAILABLE
    assert "Send the same request again" in body["message"]
    op = get_operation(db, "DSB", ref)
    assert op is not None and op.status == "FAILED"
    assert fake.post_keys == []
    assert fineract_transactions(fineract, loan, "disbursement") == []
