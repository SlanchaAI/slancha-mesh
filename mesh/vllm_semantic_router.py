"""Pinned vLLM Semantic Router front-door lifecycle.

The semantic router owns caller-facing policy on port 8888. Slancha Mesh stays
on port 8080 as its private, request-ready fleet backend. This module only
installs and supervises the upstream CLI; it does not embed or fork the router.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

VLLM_SR_VERSION = "0.3.0"
VLLM_SR_IMAGE = "ghcr.io/vllm-project/semantic-router/vllm-sr:v0.3.0"
VLLM_SR_ENVOY_IMAGE = (
    "envoyproxy/envoy@sha256:"
    "cfc0678bc03cca19cbb031688acb31d510bff501ff97e163026a375fe0515d69"
)
VLLM_SR_SIM_IMAGE = (
    "ghcr.io/vllm-project/semantic-router/vllm-sr-sim@sha256:"
    "14d391ab633b4a471f7cf9775094eca414c6d1769b14b27289c19fe3fd932807"
)
VLLM_SR_STACK_NAME = "slancha-mesh"
VLLM_SR_PORT_OFFSET = "100"
VLLM_SR_NETWORK = f"{VLLM_SR_STACK_NAME}-vllm-sr-network"
LOOPBACK_WRAPPER = '''\
"""Pinned vLLM SR shim: publish every container port on host loopback."""

import os
import subprocess

from cli.main import main


_real_run = subprocess.run
_real_popen = subprocess.Popen


def loopback_mapping(mapping):
    parts = mapping.rsplit(":", 2)
    if len(parts) == 1:
        return f"127.0.0.1::{mapping}"
    if len(parts) == 2:
        return f"127.0.0.1:{mapping}"
    return f"127.0.0.1:{parts[-2]}:{parts[-1]}"


def rewrite_command(command):
    rewritten = list(command) if isinstance(command, (list, tuple)) else command
    if isinstance(rewritten, list):
        runtime_index = next(
            (
                index
                for index, token in enumerate(rewritten)
                if isinstance(token, str)
                and os.path.basename(token) in {"docker", "podman"}
            ),
            None,
        )
        action_index = runtime_index + 1 if runtime_index is not None else None
        if (
            action_index is not None
            and action_index < len(rewritten)
            and rewritten[action_index] == "container"
        ):
            action_index += 1
        if (
            action_index is None
            or action_index >= len(rewritten)
            or rewritten[action_index] not in {"run", "create"}
        ):
            return rewritten
        for index, token in enumerate(rewritten[action_index + 1 :], action_index + 1):
            if token in {"-p", "--publish"}:
                if index + 1 >= len(rewritten):
                    continue
                mapping = rewritten[index + 1]
                if isinstance(mapping, str):
                    rewritten[index + 1] = loopback_mapping(mapping)
            elif isinstance(token, str) and token.startswith(("-p=", "--publish=")):
                option, mapping = token.split("=", 1)
                rewritten[index] = f"{option}={loopback_mapping(mapping)}"
            elif isinstance(token, str) and token.startswith("-p") and len(token) > 2:
                rewritten[index] = f"-p{loopback_mapping(token[2:])}"
    return rewritten


def run_with_loopback_ports(command, *args, **kwargs):
    return _real_run(rewrite_command(command), *args, **kwargs)


def popen_with_loopback_ports(command, *args, **kwargs):
    return _real_popen(rewrite_command(command), *args, **kwargs)


subprocess.run = run_with_loopback_ports
subprocess.Popen = popen_with_loopback_ports
main(prog_name="vllm-sr")
'''
DEFAULT_STATE_ROOT = Path.home() / ".local/state/slancha-mesh/vllm-semantic-router"

DEFAULT_CONFIG = """\
version: v0.3

listeners:
  - name: slancha-semantic-router
    address: 0.0.0.0
    port: 8788
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
      algorithm:
        type: static

global:
  router:
    model_selection:
      enabled: false
  stores:
    semantic_cache:
      enabled: false
  integrations:
    tools:
      enabled: false
"""


@dataclass(frozen=True)
class VllmSemanticRouterPaths:
    """All mutable vLLM Semantic Router files beneath one state root."""

    root: Path
    venv: Path
    python: Path
    executable: Path
    config: Path
    wrapper: Path

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
            wrapper=resolved / "vllm-sr-loopback.py",
        )


def build_vllm_sr_commands(
    paths: VllmSemanticRouterPaths,
) -> dict[str, list[list[str]]]:
    """Return shell-free argv for every supported lifecycle action."""

    executable = str(paths.executable)
    return {
        "install": [
            ["uv", "venv", "--clear", "--python", "3.11", str(paths.venv)],
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(paths.python),
                f"vllm-sr=={VLLM_SR_VERSION}",
            ],
        ],
        "validate": [[executable, "validate", "--config", str(paths.config)]],
        "serve": [
            [
                str(paths.python),
                str(paths.wrapper),
                "serve",
                "--config",
                str(paths.config),
                "--image",
                VLLM_SR_IMAGE,
                "--envoy-image",
                VLLM_SR_ENVOY_IMAGE,
                "--sim-image",
                VLLM_SR_SIM_IMAGE,
                "--image-pull-policy",
                "ifnotpresent",
                "--minimal",
            ]
        ],
        "status": [[executable, "status"]],
        "stop": [[executable, "stop"]],
    }


def build_vllm_sr_environment(
    base: dict[str, str] | None = None,
    *,
    os_name: str | None = None,
) -> dict[str, str]:
    """Return the isolated upstream stack identity used for every action."""

    environment = dict(os.environ if base is None else base)
    environment["VLLM_SR_STACK_NAME"] = VLLM_SR_STACK_NAME
    environment["VLLM_SR_PORT_OFFSET"] = VLLM_SR_PORT_OFFSET
    if (os_name or platform.system()) == "Darwin":
        environment["DOCKER_CONTEXT"] = "desktop-linux"
        paths = environment.get("PATH", "").split(os.pathsep)
        for binary_dir in ("/usr/local/bin", "/opt/homebrew/bin"):
            if binary_dir not in paths:
                paths.append(binary_dir)
        environment["PATH"] = os.pathsep.join(path for path in paths if path)
    return environment


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
    paths.wrapper.write_text(LOOPBACK_WRAPPER)
    return paths.config


def start_runtime(
    paths: VllmSemanticRouterPaths,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> int:
    """Start every command in the pinned, loopback-wrapped upstream action."""

    environment = build_vllm_sr_environment()
    for command in build_vllm_sr_commands(paths)["serve"]:
        started = runner(command, check=False, env=environment)
        if started.returncode != 0:
            return started.returncode
    return 0 if verify_loopback_bindings(runner=runner, environment=environment) else 1


def verify_loopback_bindings(
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    environment: dict[str, str] | None = None,
) -> bool:
    """Fail closed if a running upstream container publishes off loopback."""

    if environment is None:
        environment = build_vllm_sr_environment()
    listed = runner(
        [
            "docker",
            "network",
            "inspect",
            "--format",
            "{{json .Containers}}",
            VLLM_SR_NETWORK,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    try:
        decoded = json.loads(listed.stdout) if listed.returncode == 0 else {}
    except json.JSONDecodeError:
        decoded = {}
    members = decoded if isinstance(decoded, dict) else {}
    names = sorted(
        member["Name"]
        for member in members.values()
        if isinstance(member, dict) and member.get("Name")
    )
    if not names:
        print("[semantic-router] no running stack containers found after serve")
        return False

    inspected = runner(
        [
            "docker",
            "inspect",
            "--format",
            "{{json .NetworkSettings.Ports}}",
            *names,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    violations: list[str] = []
    try:
        port_maps = [json.loads(line) for line in inspected.stdout.splitlines()]
    except json.JSONDecodeError:
        port_maps = []
    if inspected.returncode != 0 or len(port_maps) != len(names):
        violations.append("unable to inspect published port bindings")
    else:
        for name, port_map in zip(names, port_maps, strict=True):
            if port_map is None:
                continue
            if not isinstance(port_map, dict):
                violations.append(f"{name} returned no port map")
                continue
            for container_port, bindings in port_map.items():
                for binding in bindings or []:
                    host_ip = binding.get("HostIp")
                    if host_ip not in {"127.0.0.1", "::1"}:
                        violations.append(f"{name} {container_port} on {host_ip}")

    if not violations:
        return True

    print("[semantic-router] unsafe published ports: " + ", ".join(violations))
    runner(["docker", "stop", *names], check=False, env=environment)
    return False


def supervise_runtime(
    paths: VllmSemanticRouterPaths,
    *,
    poll_seconds: float = 15,
    max_cycles: int | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    """Keep the upstream runtime present without restarting healthy containers."""

    containers = [
        f"{VLLM_SR_STACK_NAME}-vllm-sr-router-container",
        f"{VLLM_SR_STACK_NAME}-vllm-sr-envoy-container",
    ]
    environment = build_vllm_sr_environment()
    cycles = 0
    try:
        while max_cycles is None or cycles < max_cycles:
            inspected = runner(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{.State.Running}}",
                    *containers,
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            states = inspected.stdout.splitlines() if inspected.returncode == 0 else []
            if states != ["true", "true"]:
                if start_runtime(paths, runner=runner) != 0:
                    print(
                        "[semantic-router] runtime unavailable; "
                        f"retrying in {poll_seconds:g}s"
                    )
            cycles += 1
            if max_cycles is None or cycles < max_cycles:
                sleeper(poll_seconds)
    except KeyboardInterrupt:
        pass
    return 0


def run_action(
    action: str,
    paths: VllmSemanticRouterPaths,
    *,
    source_config: Path | None = None,
    dry_run: bool = False,
) -> int:
    """Execute one lifecycle action, returning its process exit code."""

    commands = build_vllm_sr_commands(paths)
    if action not in {*commands, "supervise"}:
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
    elif action in {"validate", "serve", "supervise"} and not paths.config.exists():
        raise RuntimeError(
            f"semantic-router config is missing at {paths.config}; run install first"
        )

    if dry_run:
        if action == "supervise":
            print("[semantic-router] would supervise the pinned runtime")
            return 0
        for command in commands[action]:
            print("[semantic-router] would run: " + " ".join(command))
        return 0

    if action == "supervise":
        return supervise_runtime(paths)
    if action == "serve":
        return start_runtime(paths)

    environment = build_vllm_sr_environment()
    for command in commands[action]:
        result = subprocess.run(command, check=False, env=environment)
        if result.returncode != 0:
            return result.returncode
    return 0


__all__ = [
    "DEFAULT_CONFIG",
    "DEFAULT_STATE_ROOT",
    "LOOPBACK_WRAPPER",
    "VLLM_SR_IMAGE",
    "VLLM_SR_ENVOY_IMAGE",
    "VLLM_SR_NETWORK",
    "VLLM_SR_PORT_OFFSET",
    "VLLM_SR_SIM_IMAGE",
    "VLLM_SR_STACK_NAME",
    "VLLM_SR_VERSION",
    "VllmSemanticRouterPaths",
    "build_vllm_sr_commands",
    "build_vllm_sr_environment",
    "check_prerequisites",
    "run_action",
    "start_runtime",
    "supervise_runtime",
    "verify_loopback_bindings",
    "write_runtime_config",
]
