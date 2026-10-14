"""Part K: reconciliation of Fineract loan transactions against ERP journal entries. Read only:
it never writes to Fineract, the ERP or our own tables, and the report is not stored.

Matching key: the op key (DSB:<ref>, RPY:<ref>). It is the Fineract transaction's externalId
and the ERP entry's external_reference, so no lookup table is needed. A transaction made
straight in Fineract has no externalId or a foreign one; it is matched on that value as is.

Sources:
- Fineract: every transaction of every loan in our loans table
  (GET /loans/external-id/{loan}?associations=transactions). Only DISBURSEMENT and REPAYMENT
  are compared; accruals and every other type are counted under not_compared.
- ERP: GET /api/journal-entries by date range or by reference, then by reference again for
  any in-scope Fineract key the first list did not hold (a different entry date).
- Our tables: the operation status and outbox status per op key, to tell IN_FLIGHT from
  missing.

Outcomes per key:
- MATCHED: the ERP entry equals the entry our Part H mapping (mapping/journal.build_entry)
  builds from Fineract's own transaction: same lines per account and side, same source,
  same entry date. Amounts are never computed apart from Fineract.
- AMOUNT_MISMATCH: both present but different. Each difference is one reason. A Fineract
  transaction the mapping refuses (fees, odd portions) is also here when the ERP has an
  entry, since that entry cannot be confirmed.
- MISSING_IN_ERP / MISSING_IN_FINERACT: present on one side only.
- IN_FLIGHT: missing in the ERP, but our operation has done its Fineract part and its outbox
  row has not been sent yet (status FINERACT_PENDING, FINERACT_DONE or ERP_PENDING with the
  outbox PENDING or not written). Not counted as missing: the outbox will send it.

Reversals (posting them is not built; the shapes are handled): a reversed Fineract
transaction (manuallyReversed) expects two ERP entries, <key> with the original lines and
<key>:REV with the opposite lines, source REVERSAL, reverses <key>, dated reversedOnDate.
Each is compared on its own. When the ERP has neither, both sides net to zero: one MATCHED
item with that reason. An ERP <key>:REV whose Fineract transaction is not reversed is
MISSING_IN_FINERACT."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import Engine, select

from integration_service.clients.erp import ErpClient
from integration_service.clients.errors import (
    ErpError,
    ErpTimeout,
    ErpUnavailable,
    FineractError,
    FineractNotFound,
)
from integration_service.errors import ApiError, ErrorCode
from integration_service.mapping.journal import (
    DISBURSEMENT_TYPE,
    REPAYMENT_TYPE,
    FineractTransaction,
    GlRoleAccounts,
    JournalLine,
    NeedsReconciliation,
    build_entry,
    parse_transaction,
)
from integration_service.models import loans, operations, outbox
from integration_service.schemas.reconciliation import MAX_RANGE_DAYS, ReconciliationRequest
from integration_service.services.loans import FineractApi, fineract_read_error
from integration_service.services.money import loan_path
from integration_service.services.templates import FieldProblem, validation_error

REV = ":REV"
OP_PREFIXES = ("DSB:", "RPY:")
_OP_KEY = re.compile(r"^(DSB|RPY):[A-Za-z0-9._:\-]{1,40}$")
COMPARED = {DISBURSEMENT_TYPE: ("DSB", "DISBURSEMENT"), REPAYMENT_TYPE: ("RPY", "REPAYMENT")}
COMPARED_TYPES = ["DISBURSEMENT", "REPAYMENT"]
IN_FLIGHT_STATUSES = frozenset({"FINERACT_PENDING", "FINERACT_DONE", "ERP_PENDING"})
ERP_PAGE = 1000
FINERACT_READERS = 8
_PLACEHOLDER_KEY = "NO-EXTERNAL-ID"


class Outcome(StrEnum):
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    MISSING_IN_ERP = "MISSING_IN_ERP"
    MISSING_IN_FINERACT = "MISSING_IN_FINERACT"
    IN_FLIGHT = "IN_FLIGHT"
    MATCHED = "MATCHED"


_RANK = {outcome: n for n, outcome in enumerate(Outcome)}


# --- scope ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date

    def covers(self, key: str | None, on: date) -> bool:
        return self.start <= on <= self.end


@dataclass(frozen=True)
class References:
    values: frozenset[str]

    def selects(self, key: str | None) -> bool:
        if key is None:
            return False
        base = key.removesuffix(REV)
        names = {key, base}
        if _OP_KEY.match(base):
            names.add(base[4:])
        return not names.isdisjoint(self.values)

    def covers(self, key: str | None, on: date) -> bool:
        return self.selects(key)


type Scope = DateRange | References


def scope_of(request: ReconciliationRequest) -> Scope:
    """The request's scope, or a 422 naming the field."""
    has_range = request.date_from is not None or request.date_to is not None
    if request.references is not None:
        if has_range:
            raise validation_error(
                [FieldProblem("references", "send either from and to, or references, not both")]
            )
        return References(frozenset(request.references))
    if not has_range:
        raise validation_error([FieldProblem("body", "send from and to, or references")])
    if request.date_from is None:
        raise validation_error([FieldProblem("from", "required with to")])
    if request.date_to is None:
        raise validation_error([FieldProblem("to", "required with from")])
    if request.date_to < request.date_from:
        raise validation_error([FieldProblem("to", "must be on or after from")])
    if (request.date_to - request.date_from).days + 1 > MAX_RANGE_DAYS:
        raise validation_error(
            [FieldProblem("to", f"the range can be at most {MAX_RANGE_DAYS} days")]
        )
    return DateRange(request.date_from, request.date_to)


def reference_candidates(values: Iterable[str]) -> list[str]:
    """The ERP references a reference list can select: each value as is, as DSB: and RPY:
    (unless it is an op key already), and each of those with :REV."""
    found: dict[str, None] = {}
    for value in values:
        bases = [value] if _OP_KEY.match(value) else [value, *(p + value for p in OP_PREFIXES)]
        for base in bases:
            found[base] = None
            found[base + REV] = None
    return list(found)


# --- the two sides --------------------------------------------------------------------------


@dataclass(frozen=True)
class LoanTransaction:
    """One Fineract transaction of a loan we know, as Fineract sent it."""

    loan_reference: str
    data: Mapping[str, Any]


@dataclass(frozen=True)
class OurState:
    status: str
    erp_status: str | None


@dataclass(frozen=True)
class Line:
    account_code: str
    debit: int
    credit: int


@dataclass(frozen=True)
class FineractFacts:
    """What Fineract shows, in whole RWF. No Fineract ids."""

    type: str
    transaction_date: date
    amount: int
    principal_portion: int
    interest_portion: int
    overpayment_portion: int
    fees_portion: int
    penalties_portion: int
    reversed: bool


@dataclass(frozen=True)
class ErpFacts:
    reference: str
    entry_date: date
    source: str
    reverses: str | None
    total: int
    lines: tuple[Line, ...]


@dataclass(frozen=True)
class Expected:
    """An ERP entry that Fineract's transaction calls for."""

    key: str | None
    loan_reference: str
    kind: str
    entry_date: date
    source: str
    reverses: str | None
    lines: tuple[Line, ...] | None
    problem: str | None
    fineract: FineractFacts


@dataclass(frozen=True)
class Item:
    key: str | None
    outcome: Outcome
    loan_reference: str | None
    kind: str
    fineract: FineractFacts | None
    erp: ErpFacts | None
    expected_lines: tuple[Line, ...] | None
    ours: OurState | None
    reasons: tuple[str, ...]


@dataclass
class Report:
    items: list[Item] = field(default_factory=list)
    not_compared_fineract: dict[str, int] = field(default_factory=dict)
    not_compared_erp: int = 0
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, int]:
        counts = Counter(item.outcome.value for item in self.items)
        return {
            **{outcome.value: counts.get(outcome.value, 0) for outcome in Outcome},
            "total": len(self.items),
        }


def type_name(code: str) -> str:
    """loanTransactionType.waiveInterest -> WAIVE_INTEREST."""
    word = code.rsplit(".", 1)[-1]
    return re.sub(r"(?<!^)(?=[A-Z])", "_", word).upper()


def _whole(value: Decimal) -> int:
    return int(value.to_integral_value())


def _facts(kind: str, t: FineractTransaction) -> FineractFacts:
    return FineractFacts(
        type=kind,
        transaction_date=t.transaction_date,
        amount=_whole(t.amount),
        principal_portion=_whole(t.principal_portion),
        interest_portion=_whole(t.interest_portion),
        overpayment_portion=_whole(t.overpayment_portion),
        fees_portion=_whole(t.fee_charges_portion),
        penalties_portion=_whole(t.penalty_charges_portion),
        reversed=t.reversed,
    )


def _lines(lines: Iterable[JournalLine]) -> tuple[Line, ...]:
    return tuple(Line(x.account_code, x.debit, x.credit) for x in lines)


def expectations(txn: LoanTransaction, accounts: GlRoleAccounts) -> list[Expected]:
    """The ERP entries one compared Fineract transaction calls for: one, or two when it is
    reversed. The lines come from build_entry() on Fineract's own data, as if it were live
    and carried its key; a refusal (fees, odd portions) gives lines None and the reason."""
    t = parse_transaction(txn.data)
    op_type, kind = COMPARED[t.type_code]
    key = t.external_id
    facts = _facts(kind, t)
    live = t.model_copy(
        update={
            "external_id": key or _PLACEHOLDER_KEY,
            "manually_reversed": False,
            "reversal_external_id": None,
            "reversed_on_date": None,
        }
    )
    lines: tuple[Line, ...] | None = None
    problem: str | None = None
    source = kind
    try:
        built = build_entry(op_type, key or _PLACEHOLDER_KEY, live, accounts, txn.loan_reference)
        lines, source = _lines(built.lines), built.source
    except NeedsReconciliation as exc:
        problem = exc.reason
    found = [
        Expected(
            key, txn.loan_reference, kind, t.transaction_date, source, None, lines, problem, facts
        )
    ]
    if t.reversed:
        opposite = tuple(Line(x.account_code, x.credit, x.debit) for x in lines) if lines else None
        found.append(
            Expected(
                key + REV if key else None,
                txn.loan_reference,
                "REVERSAL",
                t.reversed_on_date or t.transaction_date,
                "REVERSAL",
                key,
                opposite,
                problem,
                facts,
            )
        )
    return found


def erp_facts(entry: Mapping[str, Any]) -> ErpFacts:
    return ErpFacts(
        reference=str(entry["external_reference"]),
        entry_date=date.fromisoformat(str(entry["entry_date"])),
        source=str(entry["source"]),
        reverses=entry.get("reverses"),
        total=int(entry.get("total") or 0),
        lines=tuple(
            Line(str(x["account_code"]), int(x.get("debit") or 0), int(x.get("credit") or 0))
            for x in entry.get("lines") or []
        ),
    )


def _sides(lines: Iterable[Line]) -> Counter[tuple[str, str]]:
    sums: Counter[tuple[str, str]] = Counter()
    for x in lines:
        if x.debit:
            sums[(x.account_code, "debit")] += x.debit
        if x.credit:
            sums[(x.account_code, "credit")] += x.credit
    return sums


def compare(expected: Expected, erp: ErpFacts) -> list[str]:
    """Every difference between the ERP entry and the one built from Fineract."""
    if expected.lines is None:
        return [f"the mapping cannot build an entry from Fineract's data: {expected.problem}"]
    reasons: list[str] = []
    want_total = sum(x.debit for x in expected.lines)
    if erp.total != want_total:
        reasons.append(f"total: ERP {erp.total}, from Fineract {want_total}")
    want, got = _sides(expected.lines), _sides(erp.lines)
    for account, side in sorted(set(want) | set(got)):
        if want[(account, side)] != got[(account, side)]:
            reasons.append(
                f"account {account} {side}: ERP {got[(account, side)]}, "
                f"from Fineract {want[(account, side)]}"
            )
    if erp.source != expected.source:
        reasons.append(f"source: ERP {erp.source}, from Fineract {expected.source}")
    if erp.entry_date != expected.entry_date:
        reasons.append(
            f"entry_date: ERP {erp.entry_date.isoformat()}, "
            f"from Fineract {expected.entry_date.isoformat()}"
        )
    if expected.reverses is not None and erp.reverses != expected.reverses:
        reasons.append(f"reverses: ERP {erp.reverses}, from Fineract {expected.reverses}")
    return reasons


def is_in_flight(ours: OurState | None) -> bool:
    return (
        ours is not None
        and ours.status in IN_FLIGHT_STATUSES
        and ours.erp_status in (None, "PENDING")
    )


def _missing_reasons(e: Expected, ours: OurState | None) -> list[str]:
    if e.key is None:
        return [
            "the Fineract transaction has no externalId: it was not made through the "
            "Integration Service"
        ]
    reasons = [f"the ERP has no entry {e.key}"]
    base = e.key.removesuffix(REV)
    if not _OP_KEY.match(base):
        reasons.append(
            f"externalId {base} is not one of our operation keys: made outside the "
            "Integration Service"
        )
    elif e.kind == "REVERSAL":
        reasons.append("reversed in Fineract; the opposite entry was never posted")
    elif ours is None:
        reasons.append("no operation with this key in our records")
    else:
        reasons.append(f"our records: status {ours.status}, outbox {ours.erp_status}")
    if e.problem:
        reasons.append(f"the mapping cannot build an entry: {e.problem}")
    return reasons


def _keys_and_dates(txns: Iterable[LoanTransaction]) -> Iterable[tuple[str | None, date]]:
    for txn in txns:
        code = (txn.data.get("type") or {}).get("code")
        if code not in COMPARED:
            continue
        try:
            t = parse_transaction(txn.data)
        except NeedsReconciliation:
            continue
        yield t.external_id, t.transaction_date
        if t.reversed and t.external_id:
            yield t.external_id + REV, t.reversed_on_date or t.transaction_date


def erp_lookups(txns: Iterable[LoanTransaction], fetched: set[str], scope: Scope) -> list[str]:
    """In-scope Fineract keys the ERP list did not hold: look them up by reference, since
    the ERP may hold them under another date."""
    return [
        key
        for key, on in _keys_and_dates(txns)
        if key is not None and key not in fetched and scope.covers(key, on)
    ]


def reconcile(
    txns: Sequence[LoanTransaction],
    entries: Sequence[Mapping[str, Any]],
    ours: Mapping[str, OurState],
    accounts: GlRoleAccounts,
    scope: Scope,
) -> Report:
    """The report. Pure: the caller reads Fineract, the ERP and our tables."""
    report = Report()
    expected: list[Expected] = []
    skipped: Counter[str] = Counter()
    for txn in txns:
        code = str((txn.data.get("type") or {}).get("code") or "unknown")
        if code not in COMPARED:
            skipped[type_name(code)] += 1
            continue
        try:
            expected += expectations(txn, accounts)
        except NeedsReconciliation:
            report.warnings.append(f"a Fineract transaction of {txn.loan_reference} is unreadable")
    report.not_compared_fineract = dict(skipped)

    erp = {facts.reference: facts for facts in map(erp_facts, entries)}
    by_key = {e.key: e for e in expected if e.key is not None}
    seen: set[str] = set()
    net_zero: set[str] = set()

    for e in expected:
        found = erp.get(e.key) if e.key else None
        if e.key:
            seen.add(e.key)
        in_scope = scope.covers(e.key, e.entry_date) or (
            found is not None and scope.covers(found.reference, found.entry_date)
        )
        if not in_scope or (e.key is not None and e.key in net_zero):
            continue
        if e.key is None and e.kind == "REVERSAL":
            continue  # no key: the original's item already says it nets to zero
        state = ours.get(e.key.removesuffix(REV)) if e.key else None
        if (
            e.fineract.reversed
            and e.kind != "REVERSAL"
            and found is None
            and (e.key is None or e.key + REV not in erp)
        ):
            if e.key:
                net_zero.add(e.key + REV)
            report.items.append(
                _item(
                    e,
                    Outcome.MATCHED,
                    None,
                    state,
                    [
                        "reversed in Fineract; neither the "
                        "entry nor its reversal is in the ERP, so both sides net to zero"
                    ],
                )
            )
            continue
        if found is None:
            outcome = Outcome.IN_FLIGHT if is_in_flight(state) else Outcome.MISSING_IN_ERP
            reasons = (
                [
                    f"our outbox has not sent it yet (status {state.status}, outbox "
                    f"{state.erp_status}); not counted as missing"
                ]
                if outcome is Outcome.IN_FLIGHT and state is not None
                else _missing_reasons(e, state)
            )
            report.items.append(_item(e, outcome, None, state, reasons))
            continue
        reasons = compare(e, found)
        outcome = Outcome.AMOUNT_MISMATCH if reasons else Outcome.MATCHED
        report.items.append(_item(e, outcome, found, state, reasons))

    for reference, facts in erp.items():
        if reference in seen or not scope.covers(reference, facts.entry_date):
            continue
        base = reference.removesuffix(REV)
        if not _OP_KEY.match(base):
            report.not_compared_erp += 1
            continue
        reasons = [f"no Fineract transaction with externalId {reference} on any loan we know"]
        if reference.endswith(REV) and base in by_key:
            reasons = [f"the ERP reverses {base}, but Fineract shows it not reversed"]
        state = ours.get(base)
        if state is not None:
            reasons.append(f"our records: status {state.status}, outbox {state.erp_status}")
        report.items.append(
            Item(
                key=reference,
                outcome=Outcome.MISSING_IN_FINERACT,
                loan_reference=by_key[base].loan_reference if base in by_key else None,
                kind="REVERSAL" if facts.source == "REVERSAL" else facts.source,
                fineract=None,
                erp=facts,
                expected_lines=None,
                ours=state,
                reasons=tuple(reasons),
            )
        )

    report.items.sort(
        key=lambda i: (
            _RANK[i.outcome],
            i.loan_reference or "",
            i.fineract.transaction_date if i.fineract else i.erp.entry_date if i.erp else date.min,
            i.key or "",
        )
    )
    return report


def _item(
    e: Expected, outcome: Outcome, erp: ErpFacts | None, ours: OurState | None, reasons: list[str]
) -> Item:
    return Item(
        key=e.key,
        outcome=outcome,
        loan_reference=e.loan_reference,
        kind=e.kind,
        fineract=e.fineract,
        erp=erp,
        expected_lines=e.lines,
        ours=ours,
        reasons=tuple(reasons),
    )


# --- reads ----------------------------------------------------------------------------------


def erp_read_error(exc: ErpError) -> ApiError:
    if isinstance(exc, ErpTimeout):
        return ApiError(ErrorCode.ERP_TIMEOUT, "The ERP did not answer in time.", 504)
    if isinstance(exc, ErpUnavailable):
        return ApiError(ErrorCode.ERP_UNAVAILABLE, "The ERP is not reachable.", 503)
    return ApiError(ErrorCode.ERP_UNAVAILABLE, "The ERP answered with an error.", 502)


def _known_loans(engine: Engine) -> list[str]:
    with engine.connect() as conn:
        return list(
            conn.execute(select(loans.c.loan_reference).order_by(loans.c.loan_reference)).scalars()
        )


def _our_states(engine: Engine, keys: Iterable[str]) -> dict[str, OurState]:
    wanted = sorted({k.removesuffix(REV) for k in keys if _OP_KEY.match(k.removesuffix(REV))})
    if not wanted:
        return {}
    query = (
        select(operations.c.op_key, operations.c.status, outbox.c.status.label("outbox"))
        .select_from(operations.outerjoin(outbox, outbox.c.operation_id == operations.c.id))
        .where(operations.c.op_key.in_(wanted))
    )
    with engine.connect() as conn:
        return {r.op_key: OurState(r.status, r.outbox) for r in conn.execute(query)}


def read_fineract(
    fineract: FineractApi, loan_references: Sequence[str], correlation_id: str | None
) -> tuple[list[LoanTransaction], list[str]]:
    """Every transaction of every loan. A loan Fineract does not know is a warning; any other
    failure stops the report, since a partial read would show false MISSING items."""

    def one(ref: str) -> list[LoanTransaction] | None:
        try:
            data = fineract.get(
                loan_path(ref),
                params={"associations": "transactions"},
                correlation_id=correlation_id,
            )
        except FineractNotFound:
            return None
        return [LoanTransaction(ref, t) for t in (data or {}).get("transactions") or []]

    txns: list[LoanTransaction] = []
    warnings: list[str] = []
    try:
        with ThreadPoolExecutor(max_workers=FINERACT_READERS) as pool:
            for ref, found in zip(loan_references, pool.map(one, loan_references), strict=True):
                if found is None:
                    warnings.append(f"loan {ref} is in our records but not in Fineract")
                else:
                    txns += found
    except FineractError as exc:
        raise fineract_read_error(exc) from exc
    return txns, warnings


def _erp_range(
    erp: ErpClient, start: date, end: date, correlation_id: str | None
) -> list[dict[str, Any]]:
    """Entries dated start..end. The ERP list has no paging: a full page is split in two."""
    items = erp.search_journal_entries(
        date_from=start, date_to=end, limit=ERP_PAGE, correlation_id=correlation_id
    )
    if len(items) < ERP_PAGE:
        return items
    if start == end:
        raise validation_error(
            [
                FieldProblem(
                    "from",
                    f"the ERP has {ERP_PAGE} or more entries on {start}; send references instead",
                )
            ]
        )
    middle = start + timedelta(days=(end - start).days // 2)
    return _erp_range(erp, start, middle, correlation_id) + _erp_range(
        erp, middle + timedelta(days=1), end, correlation_id
    )


def _erp_by_reference(
    erp: ErpClient, references: Sequence[str], correlation_id: str | None
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for start in range(0, len(references), ERP_PAGE):
        chunk = list(references[start : start + ERP_PAGE])
        found += erp.search_journal_entries(
            references=chunk, limit=ERP_PAGE, correlation_id=correlation_id
        )
    return found


def run_reconciliation(
    engine: Engine,
    fineract: FineractApi,
    erp: ErpClient,
    scope: Scope,
    accounts: Callable[[], GlRoleAccounts],
    correlation_id: str | None,
) -> Report:
    """Reads the three sources and reconciles them. Writes nothing."""
    txns, warnings = read_fineract(fineract, _known_loans(engine), correlation_id)
    try:
        if isinstance(scope, DateRange):
            entries = _erp_range(erp, scope.start, scope.end, correlation_id)
        else:
            entries = _erp_by_reference(
                erp, reference_candidates(sorted(scope.values)), correlation_id
            )
        fetched = {str(e["external_reference"]) for e in entries}
        entries += _erp_by_reference(erp, erp_lookups(txns, fetched, scope), correlation_id)
    except ErpError as exc:
        raise erp_read_error(exc) from exc
    keys = [str(e["external_reference"]) for e in entries] + [
        str(t.data["externalId"]) for t in txns if t.data.get("externalId")
    ]
    report = reconcile(txns, entries, _our_states(engine, keys), accounts(), scope)
    report.warnings = warnings + report.warnings
    return report


def generated_at() -> datetime:
    return datetime.now(UTC)
