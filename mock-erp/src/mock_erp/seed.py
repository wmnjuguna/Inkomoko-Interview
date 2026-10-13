"""The starting chart of accounts. Codes follow 1xxx assets, 2xxx liabilities, 4xxx income,
5xxx expenses. The first migration inserts these rows."""

from __future__ import annotations

from typing import TypedDict


class SeedAccount(TypedDict):
    code: str
    external_reference: str
    name: str
    type: str
    usage: str
    parent_code: str | None


def _a(code: str, name: str, type_: str, parent: str | None = None) -> SeedAccount:
    return {
        "code": code,
        "external_reference": f"GL-{code}",
        "name": name,
        "type": type_,
        "usage": "HEADER" if parent is None else "DETAIL",
        "parent_code": parent,
    }


SEED_ACCOUNTS: list[SeedAccount] = [
    _a("1000", "Assets", "ASSET"),
    _a("1100", "Bank", "ASSET", "1000"),
    _a("1200", "Loan Receivable", "ASSET", "1000"),
    _a("1300", "Transfers in Suspense", "ASSET", "1000"),
    _a("2000", "Liabilities", "LIABILITY"),
    _a("2100", "Overpayment Liability", "LIABILITY", "2000"),
    _a("4000", "Income", "INCOME"),
    _a("4100", "Interest Income", "INCOME", "4000"),
    _a("4200", "Fee Income", "INCOME", "4000"),
    _a("4300", "Penalty Income", "INCOME", "4000"),
    _a("4400", "Recovery Income", "INCOME", "4000"),
    _a("5000", "Expenses", "EXPENSE"),
    _a("5100", "Loan Write-off", "EXPENSE", "5000"),
]
