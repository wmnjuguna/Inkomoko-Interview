"""C-1: create a client, find it by client_uid, check it, then send the same request again.

Run from the repo root, after setup (make up):

    make demo-identity
    # or: uv run --project integration-service python scripts/demo_identity.py

It talks to the Integration Service only, with the partner key from .env, like a partner
would. To print what each system holds it reads Fineract with the admin user (GET only),
the Mock ERP (GET only) and the databases with psql in a read-only transaction.

Every run uses fresh references (a run nonce) and a new client_uid. Nothing is deleted.
The data is fake: the client is "Test Client One".

Steps:
  1. POST /api/clients                      -> 201, ACTIVE, the name we sent
  2. GET /api/clients/{client_uid}?name=... -> exists, active, name matches (and a wrong
                                               name is reported as not matching)
  3. the same POST again                    -> 200 with the exact same body, nothing new
  4. same external_reference, other name    -> 409 IDEMPOTENCY_CONFLICT, nothing changed
  5. new external_reference, same client_uid -> 409 CLIENT_UID_TAKEN

Exit code 0 when every step matched what should happen, 1 otherwise."""

from __future__ import annotations

import json
import secrets
import sys
import uuid
from typing import Any

import httpx
from demo_gl_sync import ENV, FINERACT_URL, SERVICE_URL, evidence, partner_key, sql

INTEGRATION_DB = ENV.get("DEMO_INTEGRATION_DB", "integration_db")
RUN = secrets.token_hex(3).upper()  # run nonce, part of every reference this run sends


# --- partner calls (shared with demo_lifecycle.py and simulate_duplicates.py) --------------


class Partner:
    """The Integration Service API as a partner sees it. Every call gets its own
    correlation id that starts with the run nonce, so its log lines can be found."""

    def __init__(self, run: str = RUN) -> None:
        self.run = run
        self.calls = 0
        self.http = httpx.Client(
            base_url=SERVICE_URL, headers={"X-API-Key": partner_key()}, timeout=90.0
        )

    def correlation_prefix(self) -> str:
        return f"c-demo-{self.run.lower()}-"

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Reply:
        self.calls += 1
        cid = f"{self.correlation_prefix()}{self.calls:03d}"
        response = self.http.request(method, path, json=body, headers={"X-Correlation-ID": cid})
        try:
            data = response.json()
        except ValueError:
            data = {"raw": response.text[:200]}
        return Reply(response.status_code, data, cid)

    def close(self) -> None:
        self.http.close()


class Reply:
    def __init__(self, status: int, body: Any, correlation_id: str) -> None:
        self.status = status
        self.body = body
        self.correlation_id = correlation_id

    def code(self) -> str | None:
        """Our error code, for an error answer."""
        if isinstance(self.body, dict):
            error = self.body.get("error")
            if isinstance(error, dict):
                return str(error.get("code"))
            if "code" in self.body:
                return str(self.body["code"])
        return None

    def __str__(self) -> str:
        return f"HTTP {self.status} {short(self.body)}"


def short(data: Any, limit: int = 600) -> str:
    text = json.dumps(data, sort_keys=True, default=str)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def reference(kind: str) -> str:
    """A fresh external_reference for this run, e.g. C1-CLI-A1B2C3-1f2e."""
    return f"{kind}-{RUN}-{uuid.uuid4().hex[:4]}"


def admin_get(path: str, params: dict[str, str] | None = None) -> tuple[int, Any]:
    """A read-only GET on Fineract as the admin user, only to print what Fineract holds."""
    response = httpx.get(
        f"{FINERACT_URL}{path}",
        params=params,
        headers={"Fineract-Platform-TenantId": "default"},
        auth=(ENV.get("FINERACT_ADMIN_USER", "mifos"), ENV.get("FINERACT_ADMIN_PASSWORD", "")),
        timeout=30.0,
    )
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, None


def history(partner: Partner, external_reference: str) -> str:
    """What the Integration Service shows for a reference: type, status, erp_status and the
    status moves, from GET /api/transactions/{external_reference}."""
    reply = partner.call("GET", f"/api/transactions/{external_reference}")
    if reply.status != 200:
        return str(reply)
    parts = []
    for op in reply.body["operations"]:
        moves = " -> ".join([op["history"][0]["from"] or "", *(h["to"] for h in op["history"])])
        parts.append(
            f"{op['type']} status {op['status']}, erp_status {op['erp_status']}, "
            f"attempts {op['attempts']}, history {moves.strip(' ->')}"
        )
    return "; ".join(parts)


OPERATION_COLUMNS = {"external_reference", "client_uid", "loan_reference", "op_type"}


def operations_count(**where: str) -> str:
    """How many operations rows the Integration Service holds that match, read with psql."""
    unknown = set(where) - OPERATION_COLUMNS
    if unknown:
        raise ValueError(f"not an operations filter column: {unknown}")
    clauses = " AND ".join(f"{column} = :'{column}'" for column in where)
    query = f"SELECT count(*) FROM operations WHERE {clauses}"  # noqa: S608 (fixed columns)
    return sql(INTEGRATION_DB, query, **where)


def free_client_uid(partner: Partner) -> str:
    """A client_uid Fineract does not know yet (fake: DEMO-NNNNNN)."""
    for _ in range(10):
        uid = f"DEMO-{secrets.randbelow(10**6):06d}"
        if partner.call("GET", f"/api/clients/{uid}").status == 404:
            return uid
    sys.exit("could not find a free client_uid after 10 tries")


def fineract_client(uid: str) -> str:
    status, data = admin_get(f"/clients/external-id/{uid}")
    count = sql("fineract_default", "SELECT count(*) FROM m_client WHERE external_id = :'u'", u=uid)
    if status != 200:
        return f"GET /clients/external-id/{uid}: HTTP {status}; m_client rows: {count}"
    return (
        f"'{data.get('displayName')}', status {data['status']['value']}, active "
        f"{data.get('active')}, office {data.get('officeName')}; m_client rows with "
        f"externalId {uid}: {count}"
    )


def erp_for(text: str) -> str:
    """ERP journal entries that mention this text (a client_uid or loan_reference)."""
    response = httpx.get(
        f"{ENV.get('DEMO_ERP_URL', 'http://localhost:8090')}/api/journal-entries",
        params={"q": text},
        headers={"X-API-Key": ENV.get("ERP_API_KEY", "change-me-erp-key")},
        timeout=10.0,
    )
    if response.status_code != 200:
        return f"HTTP {response.status_code}"
    return f"{len(response.json()['items'])} journal entries mention {text}"


# --- C-1 ----------------------------------------------------------------------------------


def main() -> int:
    partner = Partner()
    uid = free_client_uid(partner)
    ref = reference("C1-CLI")
    body = {
        "external_reference": ref,
        "client_uid": uid,
        "first_name": "Test",
        "last_name": "Client One",
    }
    print(f"C-1 run {RUN}: client_uid {uid}, external_reference {ref}")
    results = []

    # 1. Create.
    before = (
        f"{fineract_client(uid)}; operations for {ref}: {operations_count(external_reference=ref)}"
    )
    created = partner.call("POST", "/api/clients", body)
    ok = (
        created.status == 201
        and created.body.get("status") == "ACTIVE"
        and created.body.get("name") == "Test Client One"
        and created.body.get("client_uid") == uid
    )
    results.append(
        evidence(
            "C-1.1 Create a client",
            ok,
            before,
            f"POST /api/clients {short(body)}",
            str(created),
            fineract_client(uid),
            erp_for(uid) + " (a client moves no money, so none are expected)",
            f"{history(partner, ref)}; operations rows: {operations_count(external_reference=ref)}",
            "201, status ACTIVE, name 'Test Client One'; one active client in Fineract",
            f"HTTP {created.status}, status {created.body.get('status')}, "
            f"name '{created.body.get('name')}'",
            "the service checks the request, saves the operation under CLI:<reference>, "
            "creates and activates the client in one Fineract call with externalId = client_uid",
        )
    )

    # 2. Find it and check it.
    found = partner.call("GET", f"/api/clients/{uid}?name=Test%20Client%20One")
    wrong = partner.call("GET", f"/api/clients/{uid}?name=Someone%20Else")
    ok = (
        found.status == 200
        and found.body.get("active") is True
        and found.body.get("status") == "ACTIVE"
        and found.body.get("name") == "Test Client One"
        and found.body.get("name_matches") is True
        and found.body.get("matches_our_record") is True
        and wrong.status == 200
        and wrong.body.get("name_matches") is False
    )
    results.append(
        evidence(
            "C-1.2 Find the client by client_uid and check it",
            ok,
            "the client from step 1",
            f"GET /api/clients/{uid}?name=Test Client One, then ?name=Someone Else",
            f"{found}; with the wrong name: name_matches {wrong.body.get('name_matches')}",
            fineract_client(uid),
            erp_for(uid),
            f"operations rows for {uid}: {operations_count(client_uid=uid)} (a read saves nothing)",
            "exists, active, name 'Test Client One' and name_matches true; the wrong name "
            "gives name_matches false",
            f"active {found.body.get('active')}, status {found.body.get('status')}, name "
            f"'{found.body.get('name')}', name_matches {found.body.get('name_matches')} / "
            f"{wrong.body.get('name_matches')}",
            "the service reads the client live from Fineract by externalId and compares the "
            "name; it answers with our client_uid only",
        )
    )

    # 3. Same request again.
    replay = partner.call("POST", "/api/clients", body)
    ok = replay.status == 200 and replay.body == created.body
    count = sql("fineract_default", "SELECT count(*) FROM m_client WHERE external_id = :'u'", u=uid)
    ok = ok and count == "1" and operations_count(external_reference=ref) == "1"
    results.append(
        evidence(
            "C-1.3 The same request again",
            ok,
            f"one client, one operation for {ref}",
            "POST /api/clients, the same body as step 1",
            str(replay),
            fineract_client(uid),
            erp_for(uid),
            f"{history(partner, ref)}; operations rows: {operations_count(external_reference=ref)}",
            "200 with exactly the body of step 1; still one client in Fineract",
            f"HTTP {replay.status}, same body: {replay.body == created.body}, "
            f"m_client rows {count}",
            "the saved answer for (CLI, reference) is returned; Fineract is not called again",
        )
    )

    # 4. Same reference, different data.
    changed = body | {"last_name": "Client Two"}
    conflict = partner.call("POST", "/api/clients", changed)
    after = partner.call("GET", f"/api/clients/{uid}")
    ok = (
        conflict.status == 409
        and conflict.code() == "IDEMPOTENCY_CONFLICT"
        and after.body.get("name") == "Test Client One"
        and operations_count(external_reference=ref) == "1"
    )
    results.append(
        evidence(
            "C-1.4 Same reference, different data",
            ok,
            "client 'Test Client One'",
            f"POST /api/clients {short(changed)}",
            str(conflict),
            fineract_client(uid),
            erp_for(uid),
            f"{history(partner, ref)}; operations rows: {operations_count(external_reference=ref)}",
            "409 IDEMPOTENCY_CONFLICT; the name in Fineract stays 'Test Client One'",
            f"HTTP {conflict.status} {conflict.code()}; name now '{after.body.get('name')}'",
            "the request hash differs from the one saved for this reference, so it is refused "
            "before anything is sent",
        )
    )

    # 5. Another reference trying to reuse the client_uid.
    other_ref = reference("C1-CLI")
    taken = partner.call("POST", "/api/clients", body | {"external_reference": other_ref})
    ok = (
        taken.status == 409
        and taken.code() == "CLIENT_UID_TAKEN"
        and sql("fineract_default", "SELECT count(*) FROM m_client WHERE external_id = :'u'", u=uid)
        == "1"
    )
    results.append(
        evidence(
            "C-1.5 A new reference with a client_uid that is taken",
            ok,
            f"{uid} belongs to the client from step 1",
            f"POST /api/clients with external_reference {other_ref}, same client_uid",
            str(taken),
            fineract_client(uid),
            erp_for(uid),
            f"operations rows for {uid}: {operations_count(client_uid=uid)}; "
            f"{other_ref}: {history(partner, other_ref)}",
            "409 CLIENT_UID_TAKEN; still one client",
            f"HTTP {taken.status} {taken.code()}",
            "client_uid is unique in our clients table and in Fineract's client externalId",
        )
    )

    partner.close()
    print(f"\n{sum(results)} of {len(results)} steps matched")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
