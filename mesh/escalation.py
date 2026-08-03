"""Fail-closed handoff when the mesh cannot serve a request locally."""

from __future__ import annotations

from enum import Enum

from fastapi.responses import JSONResponse


class PuntCode(str, Enum):
    """Stable reason codes understood by outer routing/spend policy."""

    NO_SUITABLE_LOCAL_ROUTE = "no_suitable_local_route"
    LOCAL_ROUTE_UNAVAILABLE = "local_route_unavailable"


def _bounded_text(value: str, limit: int) -> str:
    return value.replace("\r", " ").replace("\n", " ")[:limit]


def punt_response(
    *,
    code: PuntCode,
    message: str,
    reason: str,
    local_attempts: int,
    retryable: bool,
) -> JSONResponse:
    """Return the typed 503 contract; execution remains the caller's choice."""

    safe_reason = _bounded_text(reason, 256)
    return JSONResponse(
        status_code=503,
        content={
            "error": {
                "type": "slancha_punt",
                "code": code.value,
                "message": _bounded_text(message, 512),
                "details": {
                    "local_attempts": max(0, local_attempts),
                    "suggested_class": "cloud",
                    "retryable": retryable,
                },
            }
        },
        headers={
            "X-Slancha-Outcome": "punt",
            "X-Slancha-Reason": safe_reason,
        },
    )


__all__ = ["PuntCode", "punt_response"]
