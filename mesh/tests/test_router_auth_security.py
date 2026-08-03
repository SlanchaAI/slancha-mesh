"""Regression tests for router listener authentication boundaries."""

from __future__ import annotations

import pytest

from mesh.cli import main


def test_peer_fetch_token_does_not_authenticate_nonloopback_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSnapshotHolder:
        def __init__(self, *args, **kwargs):
            pass

        def start(self) -> None:
            pass

        def get(self):
            return object()

        def stop(self) -> None:
            pass

    monkeypatch.delenv("SLANCHA_NODE_TOKEN", raising=False)
    monkeypatch.delenv("SLANCHA_AUTH_REQUIRED", raising=False)
    monkeypatch.setattr("mesh.cli._RefreshingSnapshot", FakeSnapshotHolder)
    monkeypatch.setattr("mesh.cli.create_router_app", lambda **kwargs: object())
    monkeypatch.setattr(
        "uvicorn.run",
        lambda *args, **kwargs: pytest.fail("unsafe router reached uvicorn"),
    )

    with pytest.raises(SystemExit, match="SLANCHA_NODE_TOKEN"):
        main(
            [
                "router",
                "--peer",
                "127.0.0.1",
                "--bind",
                "0.0.0.0",
                "--token",
                "peer-fetch-only",
            ]
        )
