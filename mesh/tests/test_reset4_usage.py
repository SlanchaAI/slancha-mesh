"""Offline regression cases for incomplete or unusable stream usage frames."""

import pytest

from mesh.usage import build_usage_event, parse_stream_usage


@pytest.mark.parametrize(
    "tail,expected",
    [
        pytest.param(
            b'"completion_tokens":99}}\n\n'
            b'data: {"usage":{"prompt_tokens":7,"completion_tokens":3}}\n\n'
            b'data: {"usage":{"prompt_tokens":99,"completion_tokens":',
            {"prompt_tokens": 7, "completion_tokens": 3},
            id="truncated-boundaries-preserve-complete-usage",
        ),
        pytest.param(
            b'\n\ndata:\n\ndata:   \r\n\r\ndata: [DONE]\n\n',
            None,
            id="empty-frames-do-not-invent-counts",
        ),
        pytest.param(
            b'data: {"usage":{"prompt_tokens":7,"completion_tokens":3,}}\n\n'
            b'data: [DONE]\n\n',
            None,
            id="malformed-usage-json-does-not-invent-counts",
        ),
    ],
)
def test_stream_usage_boundaries(tail, expected):
    usage = parse_stream_usage(tail)
    assert usage == expected
    event = build_usage_event(
        specialist_id="synthetic-specialist",
        user_field="synthetic-user",
        status_code=200,
        latency_ms=100,
        usage=usage,
        response_id="synthetic-request",
    )
    if expected is None:
        assert event is None
    else:
        assert event is not None
        assert (event["tokens_in"], event["tokens_out"]) == (7, 3)
        assert event["request_id"] == "synthetic-request"
