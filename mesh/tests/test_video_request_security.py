"""Fail-closed video request controls for optional media runtimes."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from mesh.tests.test_multimodal_router import (
    SPECIALIST_ID,
    VIDEO_JOB_CAPABILITY,
    _multipart_parts,
    _snapshot,
    _video_app,
)


@pytest.mark.parametrize(
    "field",
    [
        "image_reference",
        "video_reference",
        "audio_reference",
        "frame_interpolation_model_path",
        "lora",
        "extra_params",
    ],
)
def test_video_rejects_runtime_and_remote_reference_fields_before_upstream(
    tmp_path,
    field: str,
) -> None:
    client = TestClient(
        _video_app(
            _snapshot(capabilities=[VIDEO_JOB_CAPABILITY]),
            lambda request: pytest.fail(f"unsafe {field} reached upstream"),
            tmp_path / "video-jobs.sqlite3",
        )
    )

    response = client.post(
        "/v1/videos",
        files=[
            ("model", (None, SPECIALIST_ID)),
            ("prompt", (None, "safe prompt")),
            (field, (None, "attacker-controlled")),
        ],
    )

    assert response.status_code == 400
    assert "unsupported multipart field" in response.json()["detail"]


def test_video_uploaded_reference_remains_bounded_and_forwarded(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        parts = {part["name"]: part for part in _multipart_parts(request)}
        assert parts["input_reference"]["filename"] == "reference.png"
        assert parts["input_reference"]["content_type"] == "image/png"
        assert parts["input_reference"]["content"] == b"bounded-image"
        return httpx.Response(
            200,
            json={"id": "upstream-job", "status": "queued", "created_at": 1},
            headers={"content-type": "application/json"},
        )

    response = TestClient(
        _video_app(
            _snapshot(capabilities=[VIDEO_JOB_CAPABILITY]),
            handler,
            tmp_path / "video-jobs.sqlite3",
        )
    ).post(
        "/v1/videos",
        files=[
            ("model", (None, SPECIALIST_ID)),
            ("prompt", (None, "safe prompt")),
            ("input_reference", ("reference.png", b"bounded-image", "image/png")),
        ],
    )

    assert response.status_code == 200
    assert response.json()["id"].startswith("video_")


def test_video_rejects_duplicate_scalar_fields_before_upstream(tmp_path) -> None:
    response = TestClient(
        _video_app(
            _snapshot(capabilities=[VIDEO_JOB_CAPABILITY]),
            lambda request: pytest.fail("duplicate scalar reached upstream"),
            tmp_path / "video-jobs.sqlite3",
        )
    ).post(
        "/v1/videos",
        files=[
            ("model", (None, SPECIALIST_ID)),
            ("prompt", (None, "safe prompt")),
            ("seed", (None, "1")),
            ("seed", (None, "2")),
        ],
    )

    assert response.status_code == 400
    assert "duplicate" in response.json()["detail"]
