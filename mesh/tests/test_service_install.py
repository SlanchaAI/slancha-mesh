"""Cross-OS service install (issue #62).

The rendering functions in `mesh.service_install` are pure — they turn
`(exec_path, args, role)` into the systemd unit / launchd plist / Windows
task text. These lock that the right ExecStart/program+args land per-OS, that
an unknown OS is handled (not a crash), and that the `slancha-mesh service`
subcommand is wired into the CLI parser + handler without performing any real
install (everything routes through `--dry-run` or pure functions).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mesh.cli import build_parser, cmd_service, main
from mesh.service_install import (
    UnsupportedOSError,
    build_service_plan,
    render_launchd_plist,
    render_systemd_unit,
    render_windows_task_command,
    service_label,
    up_argv,
)

EXEC = "/home/u/.local/bin/slancha-mesh"


# ---------------------------------------------------------------------------
# pure renderers — per-OS ExecStart / program+args
# ---------------------------------------------------------------------------


def test_up_argv_defaults_to_auto_and_passes_through():
    assert up_argv(None) == ["up", "--auto"]
    assert up_argv(["up", "--specialist", "x"]) == ["up", "--specialist", "x"]


def test_service_label_reverse_dns():
    assert service_label("node") == "ai.slancha.mesh.node"
    assert service_label("gb10") == "ai.slancha.mesh.gb10"
    assert service_label("") == "ai.slancha.mesh.node"  # falls back to default


def test_systemd_unit_has_execstart_with_exec_and_args():
    unit = render_systemd_unit(EXEC, ["up", "--specialist", "code-7b"], role="node")
    assert f"ExecStart={EXEC} up --specialist code-7b" in unit
    assert "Restart=on-failure" in unit
    assert "[Install]" in unit and "WantedBy=default.target" in unit


def test_systemd_unit_defaults_to_up_auto():
    unit = render_systemd_unit(EXEC, None)
    assert f"ExecStart={EXEC} up --auto" in unit


def test_launchd_plist_has_program_arguments_vector():
    plist = render_launchd_plist(EXEC, ["up", "--specialist", "code-7b"], role="node")
    assert "<key>Label</key>" in plist
    assert "<string>ai.slancha.mesh.node</string>" in plist
    # argv is a string array, one element per token (no shell).
    assert f"<string>{EXEC}</string>" in plist
    assert "<string>up</string>" in plist
    assert "<string>--specialist</string>" in plist
    assert "<string>code-7b</string>" in plist
    # boot-persist + crash-restart.
    assert "<key>RunAtLoad</key>" in plist and "<true/>" in plist
    assert "<key>KeepAlive</key>" in plist


def test_launchd_plist_is_xml_parseable():
    import xml.dom.minidom as minidom

    plist = render_launchd_plist(EXEC, None, role="node")
    minidom.parseString(plist)  # raises on malformed XML


def test_windows_task_command_has_onstart_and_tr_with_args():
    cmd = render_windows_task_command(EXEC, ["up", "--specialist", "code-7b"], role="node")
    assert "schtasks /Create" in cmd
    assert '/TN "ai.slancha.mesh.node"' in cmd
    assert "/SC ONSTART" in cmd
    assert EXEC in cmd
    assert "up --specialist code-7b" in cmd


# ---------------------------------------------------------------------------
# dispatcher — plan shape per OS, injectable home, unknown OS
# ---------------------------------------------------------------------------


def test_plan_linux_writes_systemd_user_unit(tmp_path):
    plan = build_service_plan("Linux", EXEC, role="node", home=tmp_path)
    assert plan.os_name == "Linux"
    assert plan.path == tmp_path / ".config/systemd/user/ai.slancha.mesh.node.service"
    assert any("enable" in c for c in (" ".join(x) for x in plan.install_cmds))


def test_plan_macos_writes_launchagent_plist_at_expected_path(tmp_path):
    plan = build_service_plan("Darwin", EXEC, role="gb10", home=tmp_path)
    assert plan.os_name == "Darwin"
    assert plan.path == tmp_path / "Library/LaunchAgents/ai.slancha.mesh.gb10.plist"
    cmds = [" ".join(c) for c in plan.install_cmds]
    assert any("launchctl load" in c for c in cmds)


def test_plan_windows_has_no_file_and_uses_schtasks(tmp_path):
    plan = build_service_plan("Windows", EXEC, role="node", home=tmp_path)
    assert plan.os_name == "Windows"
    assert plan.path is None  # schtasks registers in the scheduler, no file
    install = [" ".join(c) for c in plan.install_cmds]
    assert any("schtasks /Create" in c for c in install)
    uninstall = [" ".join(c) for c in plan.uninstall_cmds]
    assert any("schtasks /Delete" in c for c in uninstall)


def test_unknown_os_raises_unsupported_not_crash():
    with pytest.raises(UnsupportedOSError):
        build_service_plan("Plan9", EXEC, role="node", home=Path("/tmp"))


# ---------------------------------------------------------------------------
# CLI wiring — subcommand parses, handler runs without installing anything
# ---------------------------------------------------------------------------


def test_service_subcommand_parses_action_and_flags():
    args = build_parser().parse_args(["service", "install", "--role", "gb10", "--dry-run"])
    assert args.action == "install"
    assert args.role == "gb10"
    assert args.dry_run is True
    assert args.func is cmd_service


def test_service_passthrough_after_double_dash_routes_to_up_args(monkeypatch):
    """`service install -- --specialist x` forwards the post-`--` tokens to
    `slancha-mesh up` without colliding with `service`'s own flags. main()
    does the split; we capture the ServicePlan inputs to confirm."""
    monkeypatch.setattr("mesh.service_install.platform.system", lambda: "Linux")
    monkeypatch.setattr("mesh.cli._resolve_exec_path", lambda: EXEC)
    seen = {}

    def fake_build(os_name, exec_path, up_args=None, role="node", home=None):
        seen["up_args"] = up_args
        seen["role"] = role
        raise UnsupportedOSError("stop before side effects")

    monkeypatch.setattr("mesh.cli.build_service_plan", fake_build, raising=False)
    # build_service_plan is imported inside cmd_service from mesh.service_install,
    # so patch there too.
    monkeypatch.setattr("mesh.service_install.build_service_plan", fake_build)

    rc = main(["service", "install", "--role", "gb10", "--", "--specialist", "code-7b"])
    assert rc == 2  # UnsupportedOSError → clean exit
    assert seen["role"] == "gb10"
    assert seen["up_args"] == ["up", "--specialist", "code-7b"]


def test_service_install_dry_run_renders_but_does_not_install(monkeypatch, capsys, tmp_path):
    # Force the detected OS + home so the rendered artifact is deterministic
    # and never escapes the tmp dir.
    monkeypatch.setattr("mesh.service_install.platform.system", lambda: "Darwin")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr("mesh.cli._resolve_exec_path", lambda: EXEC)

    rc = main(["service", "install", "--dry-run", "--", "--specialist", "code-7b"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "would write" in out
    assert "ai.slancha.mesh.node.plist" in out
    assert "<string>--specialist</string>" in out
    # No plist actually written.
    assert not (tmp_path / "Library/LaunchAgents/ai.slancha.mesh.node.plist").exists()


def test_service_unsupported_os_prints_message_and_exits_clean(monkeypatch, capsys):
    monkeypatch.setattr("mesh.service_install.platform.system", lambda: "Plan9")
    monkeypatch.setattr("mesh.cli._resolve_exec_path", lambda: EXEC)
    rc = main(["service", "install", "--dry-run"])
    assert rc == 2
    assert "no service-install path" in capsys.readouterr().out.lower()


def test_service_appears_in_help(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--help"])
    assert "service" in capsys.readouterr().out
