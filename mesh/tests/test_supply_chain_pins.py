"""Release-facing external executables stay immutable between reviews."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).parents[2]
SHA256_IMAGE = re.compile(r"@sha256:[0-9a-f]{64}(?:\s|$)")
ACTION_SHA = re.compile(r"^\s*(?:-\s*)?uses:\s*[^@\s]+@[0-9a-f]{40}(?:\s+#.*)?$")


def test_optional_localai_image_is_digest_pinned() -> None:
    compose = (ROOT / "examples/multimodal/localai/docker-compose.yml").read_text()
    image_line = next(line for line in compose.splitlines() if "image:" in line)
    assert SHA256_IMAGE.search(image_line), image_line


def test_github_actions_are_commit_pinned() -> None:
    action_lines = [
        line
        for workflow in (ROOT / ".github/workflows").glob("*.yml")
        for line in workflow.read_text().splitlines()
        if "uses:" in line
    ]
    assert action_lines
    assert all(ACTION_SHA.match(line) for line in action_lines), action_lines
