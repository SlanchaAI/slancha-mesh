"""Typed escalation contract for callers that own cloud/spend policy."""

from __future__ import annotations

import json

from mesh.escalation import PuntCode, punt_response


def test_punt_response_is_machine_readable_and_bounded():
    response = punt_response(
        code=PuntCode.NO_SUITABLE_LOCAL_ROUTE,
        message="No healthy local specialist satisfies this request.",
        reason="classifier selected cloud",
        local_attempts=0,
        retryable=True,
    )

    assert response.status_code == 503
    assert response.headers["X-Slancha-Outcome"] == "punt"
    assert response.headers["X-Slancha-Reason"] == "classifier selected cloud"
    assert json.loads(response.body) == {
        "error": {
            "type": "slancha_punt",
            "code": "no_suitable_local_route",
            "message": "No healthy local specialist satisfies this request.",
            "details": {
                "local_attempts": 0,
                "suggested_class": "cloud",
                "retryable": True,
            },
        }
    }


def test_punt_response_does_not_emit_unbounded_reason_or_message():
    response = punt_response(
        code=PuntCode.LOCAL_ROUTE_UNAVAILABLE,
        message="m" * 2_000,
        reason="r" * 2_000,
        local_attempts=3,
        retryable=True,
    )

    payload = json.loads(response.body)
    assert len(response.headers["X-Slancha-Reason"]) == 256
    assert len(payload["error"]["message"]) == 512
