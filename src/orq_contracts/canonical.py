"""Canonicalização e digest dos contratos JSON."""

from __future__ import annotations

import hashlib
import math
from typing import Any

import rfc8785


class CanonicalizationError(ValueError):
    """Valor fora do perfil I-JSON/JCS aceito pelo ORQ."""


def _validate_numbers(value: Any, path: str = "$") -> None:
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, int):
        if abs(value) > 9_007_199_254_740_991:
            raise CanonicalizationError(f"{path}: integer_outside_ieee754_safe_range")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalizationError(f"{path}: non_finite_number")
        if value == 0.0 and math.copysign(1.0, value) < 0:
            raise CanonicalizationError(f"{path}: negative_zero")
        return
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise CanonicalizationError(f"{path}: non_string_object_key")
            _validate_numbers(child, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _validate_numbers(child, f"{path}[{index}]")


def canonicalize(value: Any) -> bytes:
    """Retorna bytes JCS, rejeitando ambiguidades numéricas conhecidas."""

    _validate_numbers(value)
    try:
        return rfc8785.dumps(value)
    except (rfc8785.CanonicalizationError, UnicodeError, TypeError) as error:
        raise CanonicalizationError(str(error)) from error


def canonical_digest(value: Any) -> dict[str, str]:
    """Calcula SHA-256 sobre os bytes JCS."""

    return {
        "algorithm": "sha256",
        "value": hashlib.sha256(canonicalize(value)).hexdigest(),
    }
