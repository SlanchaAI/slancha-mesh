"""Missing output-mode contracts on the declared transcription protocol."""

import httpx
import pytest

from mesh.protocols import MULTIPART_PROTOCOLS_BY_PATH
from mesh.tests.test_multimodal_router import SPECIALIST_ID, _client, _snapshot


@pytest.mark.parametrize(
    ("response_format", "accepted", "media_type", "body", "status_code"),
    [
        (None, ("application/json",), "application/json", b'{"text":"hello"}', 200),
        (
            "srt",
            ("application/x-subrip", "text/plain"),
            "application/x-subrip",
            b"1\n00:00:00,000 --> 00:00:01,000\nhello\n",
            200,
        ),
        ("unsupported", (), "application/json", b"{}", 400),
    ],
    ids=["absent-defaults-to-json", "supported-srt", "unsupported-rejected"],
)
def test_declared_transcription_output_modes(
    response_format: str | None,
    accepted: tuple[str, ...],
    media_type: str,
    body: bytes,
    status_code: int,
) -> None:
    protocol = MULTIPART_PROTOCOLS_BY_PATH["/v1/audio/transcriptions"]
    assert protocol.success_media_types_for(response_format) == accepted
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=body, headers={"content-type": media_type})

    files = [
        ("model", (None, SPECIALIST_ID)),
        ("file", ("sample.wav", b"RIFFsynthetic", "audio/wav")),
    ]
    if response_format is not None:
        files.append(("response_format", (None, response_format)))
    with _client(_snapshot(capabilities=[protocol.capability]), handler) as client:
        response = client.post(protocol.public_path, files=files)

    assert response.status_code == status_code, response.text
    if status_code == 200:
        assert len(requests) == 1
        assert requests[0].url.path == protocol.upstream_path
        assert response.content == body
        assert response.headers["content-type"].split(";")[0] == media_type
    else:
        assert requests == []
        assert response.json()["detail"] == "unsupported response_format 'unsupported'"
