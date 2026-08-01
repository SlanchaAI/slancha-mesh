"""Pinned vLLM Semantic Router front-door lifecycle.

The semantic router owns caller-facing policy on port 8888. Slancha Mesh stays
on port 8080 as its private, request-ready fleet backend. This module only
installs and supervises the upstream CLI; it does not embed or fork the router.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

VLLM_SR_VERSION = "0.3.0"
VLLM_SR_IMAGE = (
    "ghcr.io/vllm-project/semantic-router/vllm-sr:v0.3.0"
)
DEFAULT_STATE_ROOT = (
    Path.home() / ".local/state/slancha-mesh/vllm-semantic-router"
)

DEFAULT_CONFIG = """\
version: v0.3

listeners:
  - name: slancha-semantic-router
    address: 127.0.0.1
    port: 8888
    timeout: 300s

providers:
  defaults:
    default_model: slancha-auto
  models:
    - name: slancha-auto
      provider_model_id: auto
      api_format: openai
      backend_refs:
        - name: slancha-local-mesh
          endpoint: host.docker.internal:8080
          protocol: http
          weight: 100

routing:
  modelCards:
    - name: slancha-auto
      description: Request-ready models discovered across the local Slancha mesh.
      capabilities: [chat, reasoning, tools]
      tags: [local, private-fleet]
  decisions:
    - name: local-mesh
      description: Route through the request-ready local mesh.
      priority: 100
      rules:
        operator: AND
        conditions: []
      modelRefs:
        - model: slancha-auto
          use_reasoning: false
"""


@dataclass(frozen=True)
class VllmSemanticRouterPaths:
    """All mutable vLLM Semantic Router files beneath one state root."""

    root: Path
    venv: Path
    python: Path
    executable: Path
    config: Path

    @classmethod
    def from_root(cls, root: Path | str) -> "VllmSemanticRouterPaths":
        resolved = Path(root).expanduser()
        venv = resolved / "venv"
        return cls(
            root=resolved,
            venv=venv,
            python=venv / "bin/python",
            executable=venv / "bin/vllm-sr",
            config=resolved / "config.yaml",
        )


def build_vllm_sr_commands(
    paths: VllmSemanticRouterPaths,
) -> dict[str, list[list[str]]]:
    """Return shell-free argv for every supported lifecycle action."""

    executable = str(paths.executable)
    return {
        "install": [
            ["uv", "venv", "--python", "3.11", str(paths.venv)],
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(paths.python),
                f"vllm-sr=={VLLM_SR_VERSION}",
            ],
        ],
        "validate": [
            [executable, "validate", "--config", str(paths.config)]
        ],
        "serve": [
            [
                executable,
                "serve",
                "--config",
                str(paths.config),
                "--image",
                VLLM_SR_IMAGE,
                "--image-pull-policy",
                "ifnotpresent",
                "--minimal",
            ]
        ],
        "status": [[executable, "status"]],
        "stop": [[executable, "stop"]],
    }


def check_prerequisites(
    which: Callable[[str], str | None] = shutil.which,
) -> None:
    """Fail before installation if the two external runtimes are absent."""

    if which("uv") is None:
        raise RuntimeError("uv is required to install the pinned vLLM SR CLI")
    if which("docker") is None:
        raise RuntimeError("Docker is required by the vLLM SR v0.3 local runtime")


def write_runtime_config(
    paths: VllmSemanticRouterPaths,
    source: Path | None = None,
) -> Path:
    """Write the canonical config or copy one explicit operator config."""

    paths.root.mkdir(parents=True, exist_ok=True)
    content = source.read_text() if source is not None else DEFAULT_CONFIG
    paths.config.write_text(content)
    return paths.config


def run_action(
    action: str,
    paths: VllmSemanticRouterPaths,
    *,
    source_config: Path | None = None,
    dry_run: bool = False,
) -> int:
    """Execute one lifecycle action, returning its process exit code."""

    commands = build_vllm_sr_commands(paths)
    if action not in commands:
        raise ValueError(f"unknown semantic-router action {action!r}")

    if action == "install":
        if dry_run:
            for command in commands[action]:
                print("[semantic-router] would run: " + " ".join(command))
            print(f"[semantic-router] would write: {paths.config}")
            return 0
        check_prerequisites()
        write_runtime_config(paths, source_config)
    elif not paths.executable.exists():
        raise RuntimeError(
            f"vLLM Semantic Router is not installed at {paths.executable}; "
            "run `slancha-mesh semantic-router install` first"
        )
    elif action in {"validate", "serve"} and not paths.config.exists():
        raise RuntimeError(
            f"semantic-router config is missing at {paths.config}; run install first"
        )

    if dry_run:
        for command in commands[action]:
            print("[semantic-router] would run: " + " ".join(command))
        return 0

    for command in commands[action]:
        result = subprocess.run(command, check=False)
        if result.returncode != 0:
            return result.returncode
    return 0


__all__ = [
    "DEFAULT_CONFIG",
    "DEFAULT_STATE_ROOT",
    "VLLM_SR_IMAGE",
    "VLLM_SR_VERSION",
    "VllmSemanticRouterPaths",
    "build_vllm_sr_commands",
    "check_prerequisites",
    "run_action",
    "write_runtime_config",
]
