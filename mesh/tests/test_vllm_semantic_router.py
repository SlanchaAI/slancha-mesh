"""Supported vLLM Semantic Router front-door tooling."""

from __future__ import annotations

import json
from pathlib import Path
from subprocess import CompletedProcess
from types import ModuleType

import pytest


def test_paths_keep_all_runtime_state_under_selected_root(tmp_path: Path) -> None:
    from mesh.vllm_semantic_router import VllmSemanticRouterPaths

    paths = VllmSemanticRouterPaths.from_root(tmp_path)

    assert paths.root == tmp_path
    assert paths.venv == tmp_path / "venv"
    assert paths.executable == tmp_path / "venv/bin/vllm-sr"
    assert paths.config == tmp_path / "config.yaml"
    assert paths.wrapper == tmp_path / "vllm-sr-loopback.py"


def test_commands_pin_release_image_and_keep_mesh_as_backend(tmp_path: Path) -> None:
    from mesh.vllm_semantic_router import (
        VLLM_SR_PORT_OFFSET,
        VLLM_SR_STACK_NAME,
        VLLM_SR_ENVOY_IMAGE,
        VLLM_SR_IMAGE,
        VLLM_SR_SIM_IMAGE,
        VLLM_SR_VERSION,
        VllmSemanticRouterPaths,
        build_vllm_sr_environment,
        build_vllm_sr_commands,
    )

    paths = VllmSemanticRouterPaths.from_root(tmp_path)

    commands = build_vllm_sr_commands(paths)

    assert VLLM_SR_VERSION == "0.3.0"
    assert VLLM_SR_IMAGE == (
        "ghcr.io/vllm-project/semantic-router/vllm-sr:v0.3.0@"
        "sha256:667c4d45e03fcee84d33792e6901fa3ac0e6a1f53e6a2674ecb1174e1decea64"
    )
    assert VLLM_SR_STACK_NAME == "slancha-mesh"
    assert VLLM_SR_PORT_OFFSET == "100"
    assert "@sha256:" in VLLM_SR_ENVOY_IMAGE
    assert "@sha256:" in VLLM_SR_SIM_IMAGE
    assert build_vllm_sr_environment({"PATH": "/bin"}, os_name="Darwin") == {
        "DOCKER_CONTEXT": "desktop-linux",
        "PATH": "/bin:/usr/local/bin:/opt/homebrew/bin",
        "VLLM_SR_STACK_NAME": "slancha-mesh",
        "VLLM_SR_PORT_OFFSET": "100",
    }
    assert commands["install"] == [
        ["uv", "venv", "--clear", "--python", "3.11", str(paths.venv)],
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(paths.python),
            "vllm-sr==0.3.0",
        ],
    ]
    assert commands["validate"] == [
        [str(paths.executable), "validate", "--config", str(paths.config)]
    ]
    assert commands["serve"] == [
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
    ]
    assert commands["status"] == [[str(paths.executable), "status"]]
    assert commands["stop"] == [[str(paths.executable), "stop"]]


def test_runtime_config_is_container_reachable(
    tmp_path: Path,
) -> None:
    from mesh.vllm_semantic_router import (
        VllmSemanticRouterPaths,
        write_runtime_config,
    )

    paths = VllmSemanticRouterPaths.from_root(tmp_path)

    written = write_runtime_config(paths)

    assert written == paths.config
    config = paths.config.read_text()
    assert "address: 0.0.0.0" in config
    assert "port: 8788" in config
    assert "endpoint: host.docker.internal:8080" in config
    assert "provider_model_id: auto" in config
    assert "default_model: slancha-auto" in config
    assert "type: static" in config
    assert config.count("enabled: false") == 3
    assert 'return f"127.0.0.1::{mapping}"' in paths.wrapper.read_text()


def test_prerequisites_fail_before_side_effects_when_runtime_is_missing() -> None:
    from mesh.vllm_semantic_router import check_prerequisites

    with pytest.raises(RuntimeError, match="Docker"):
        check_prerequisites(lambda name: "/usr/bin/uv" if name == "uv" else None)
    with pytest.raises(RuntimeError, match="uv"):
        check_prerequisites(
            lambda name: "/usr/bin/docker" if name == "docker" else None
        )


def test_semantic_router_subcommand_parses_without_touching_runtime() -> None:
    from mesh.cli import build_parser, cmd_semantic_router

    args = build_parser().parse_args(
        ["semantic-router", "serve", "--state-dir", "/tmp/vllm-sr"]
    )

    assert args.action == "serve"
    assert args.state_dir == "/tmp/vllm-sr"
    assert args.func is cmd_semantic_router


def test_supervisor_starts_missing_runtime_then_only_monitors(tmp_path: Path) -> None:
    from mesh.vllm_semantic_router import (
        VllmSemanticRouterPaths,
        build_vllm_sr_commands,
        supervise_runtime,
    )

    paths = VllmSemanticRouterPaths.from_root(tmp_path)
    calls: list[list[str]] = []
    responses = iter(
        [
            CompletedProcess([], 1, stdout="", stderr="not found"),
            CompletedProcess([], 0, stdout="", stderr=""),
            CompletedProcess(
                [],
                0,
                stdout='{"one":{"Name":"slancha-mesh-vllm-sr-envoy-container"}}',
                stderr="",
            ),
            CompletedProcess(
                [],
                0,
                stdout='{"8788/tcp":[{"HostIp":"127.0.0.1","HostPort":"8888"}]}\n',
                stderr="",
            ),
            CompletedProcess([], 0, stdout="true\ntrue\n", stderr=""),
        ]
    )

    def fake_run(command: list[str], **_kwargs: object) -> CompletedProcess[str]:
        calls.append(command)
        return next(responses)

    assert (
        supervise_runtime(
            paths,
            poll_seconds=0,
            max_cycles=2,
            runner=fake_run,
            sleeper=lambda _seconds: None,
        )
        == 0
    )
    assert calls[0][:3] == ["docker", "inspect", "--format"]
    assert calls[1] == build_vllm_sr_commands(paths)["serve"][0]
    assert calls[2][:3] == ["docker", "network", "inspect"]
    assert calls[3][:3] == ["docker", "inspect", "--format"]
    assert calls[4][:3] == ["docker", "inspect", "--format"]


def test_loopback_wrapper_rewrites_every_upstream_docker_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mesh.vllm_semantic_router import LOOPBACK_WRAPPER

    import subprocess
    import sys

    calls: list[list[str]] = []
    popen_calls: list[list[str]] = []

    def fake_run(command: list[str], *_args: object, **_kwargs: object) -> None:
        calls.append(command)

    def fake_popen(command: list[str], *_args: object, **_kwargs: object) -> None:
        popen_calls.append(command)

    fake_cli = ModuleType("cli")
    fake_main_module = ModuleType("cli.main")

    def fake_main(**_kwargs: object) -> None:
        subprocess.run(
            [
                "/usr/local/bin/docker",
                "run",
                "-p",
                "8888:8788",
                "--publish",
                "8180:8080",
                "-p",
                "0.0.0.0:9290:9190",
                "--publish=8910:8000",
                "-p8088",
                "image",
            ]
        )
        subprocess.Popen(
            [
                "sudo",
                "docker",
                "container",
                "create",
                "--publish",
                "0.0.0.0:5532:5432",
                "image",
            ]
        )

    fake_main_module.main = fake_main  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "cli", fake_cli)
    monkeypatch.setitem(sys.modules, "cli.main", fake_main_module)
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    exec(LOOPBACK_WRAPPER, {"__name__": "__main__"})

    assert calls == [
        [
            "/usr/local/bin/docker",
            "run",
            "-p",
            "127.0.0.1:8888:8788",
            "--publish",
            "127.0.0.1:8180:8080",
            "-p",
            "127.0.0.1:9290:9190",
            "--publish=127.0.0.1:8910:8000",
            "-p127.0.0.1::8088",
            "image",
        ]
    ]
    assert popen_calls == [
        [
            "sudo",
            "docker",
            "container",
            "create",
            "--publish",
            "127.0.0.1:5532:5432",
            "image",
        ]
    ]


def test_binding_verification_stops_stack_on_non_loopback_publish() -> None:
    from mesh.vllm_semantic_router import (
        VLLM_SR_NETWORK,
        verify_loopback_bindings,
    )

    calls: list[list[str]] = []
    names = sorted(
        [
            "slancha-mesh-vllm-sr-envoy-container",
            "slancha-mesh-vllm-sr-router-container",
            "slancha-mesh-vllm-sr-sim",
            "slancha-mesh-vllm-sr-postgres",
            "slancha-mesh-vllm-sr-redis",
        ]
    )
    port_maps = {
        names[0]: {"8788/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8888"}]},
        names[1]: {"5432/tcp": [{"HostIp": "0.0.0.0", "HostPort": "5532"}]},
        names[2]: None,
        names[3]: {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8180"}]},
        names[4]: {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8910"}]},
    }
    responses = iter(
        [
            CompletedProcess(
                [],
                0,
                stdout=json.dumps(
                    {str(index): {"Name": name} for index, name in enumerate(names)}
                ),
                stderr="",
            ),
            CompletedProcess(
                [],
                0,
                stdout="\n".join(json.dumps(port_maps[name]) for name in names) + "\n",
                stderr="",
            ),
            CompletedProcess([], 0, stdout="", stderr=""),
        ]
    )

    def fake_run(command: list[str], **_kwargs: object) -> CompletedProcess[str]:
        calls.append(command)
        return next(responses)

    assert not verify_loopback_bindings(runner=fake_run, environment={})
    assert calls[0][-1] == VLLM_SR_NETWORK
    assert calls[-1] == ["docker", "stop", *names]


def test_semantic_router_install_dry_run_prints_pinned_commands(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    from mesh.cli import main

    rc = main(
        [
            "semantic-router",
            "install",
            "--state-dir",
            str(tmp_path),
            "--dry-run",
        ]
    )

    assert rc == 0
    output = capsys.readouterr().out
    assert "vllm-sr==0.3.0" in output
    assert str(tmp_path / "config.yaml") in output
    assert not (tmp_path / "config.yaml").exists()
    assert not (tmp_path / "venv").exists()
