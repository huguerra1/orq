from __future__ import annotations

import pytest

from orq_contracts.canonical import CanonicalizationError, canonical_digest, canonicalize


def test_object_order_does_not_change_bytes_or_digest() -> None:
    left = {"z": 1, "a": {"b": True, "a": "ç"}}
    right = {"a": {"a": "ç", "b": True}, "z": 1}

    assert canonicalize(left) == canonicalize(right)
    assert canonical_digest(left) == canonical_digest(right)
    assert canonical_digest(left)["algorithm"] == "sha256"
    assert len(canonical_digest(left)["value"]) == 64


@pytest.mark.parametrize("value", [-0.0, float("nan"), float("inf")])
def test_rejects_ambiguous_or_non_finite_numbers(value: float) -> None:
    with pytest.raises(CanonicalizationError):
        canonicalize({"value": value})


def test_rejects_integer_outside_interoperable_range() -> None:
    with pytest.raises(CanonicalizationError, match="integer_outside_ieee754_safe_range"):
        canonicalize({"value": 9_007_199_254_740_992})


def test_rejects_non_string_object_key() -> None:
    with pytest.raises(CanonicalizationError, match="non_string_object_key"):
        canonicalize({1: "invalid"})
