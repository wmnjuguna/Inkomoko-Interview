from __future__ import annotations

from mock_erp.hashing import canonical_json, request_hash


def test_key_order_does_not_change_the_hash() -> None:
    assert request_hash({"a": 1, "b": "x"}) == request_hash({"b": "x", "a": 1})


def test_any_value_change_changes_the_hash() -> None:
    assert request_hash({"a": 1}) != request_hash({"a": 2})


def test_canonical_json_is_compact_and_sorted() -> None:
    assert canonical_json({"b": 1, "a": [2, 1]}) == '{"a":[2,1],"b":1}'
