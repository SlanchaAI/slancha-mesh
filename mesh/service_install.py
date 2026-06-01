"""Cross-OS boot-persistent service install for a slancha-mesh node.

`mesh/deploy/` only shipped systemd units + `install_*.sh` — Linux-only. This
module adds the macOS (launchd) and Windows (Scheduled Task) paths so a node
can run boot-persistent everywhere, surfaced as `slancha-mesh service install`.

It is deliberately THIN: every OS renders a unit/plist/task whose payload is
just `slancha-mesh up <args>` (or `serve`). It does NOT reimplement serving.

The rendering functions are pure — they turn `(exec_path, args, role, ...)`
into the unit/plist/task *text* — so they unit-test without touching the real
system. The CLI handler (`mesh.cli.cmd_service`) calls them and does the
filesystem + `launchctl`/`schtasks`/`systemctl` side effects, guarded so the
test suite never installs anything.

Windows mechanism choice — `schtasks` ONSTART scheduled task:
  We pick a Scheduled Task (`schtasks /Create /SC ONSTART`) over nssm or
  `sc.exe`. Rationale: it ships with every Windows install (no extra
  dependency to download like nssm), and an ONSTART task does not require an
  interactive login the way a per-user `Run` key does. Tradeoff: a Scheduled
  Task is not a true Windows *service* — no SCM integration, no automatic
  crash-restart semantics (we add `/RL HIGHEST` + a restart-on-fail via task
  settings where available, but it is best-effort). For a hard service with
  SCM restart, install nssm and wrap `slancha-mesh up` — documented in
  NODE_SETUP.md, not the default because it needs an extra download.
"""

from __future__ import annotations

import platform
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

# Reverse-DNS prefix for launchd labels and the Windows task path.
LABEL_PREFIX = "ai.slancha.mesh"
DEFAULT_ROLE = "node"


@dataclass(frozen=True)
class ServicePlan:
    """The fully-resolved, OS-specific install description.

    `text` is the unit/plist/task-command payload. `path` is where it would be
    written (None for mechanisms that don't write a file, e.g. schtasks). The
    CLI handler turns this into real side effects; tests inspect it directly.
    """

    os_name: str
    label: str
    text: str
    path: Path | None = None
    # Shell/CLI commands the handler runs to register/unregister/query.
    install_cmds: list[list[str]] = field(default_factory=list)
    uninstall_cmds: list[list[str]] = field(default_factory=list)
    status_cmds: list[list[str]] = field(default_factory=list)


class UnsupportedOSError(RuntimeError):
    """Raised for an OS with no service-install path. Caught by the CLI so the
    operator gets a clear message instead of a traceback."""


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def service_label(role: str = DEFAULT_ROLE) -> str:
    """Reverse-DNS label, e.g. `ai.slancha.mesh.node`. Used for the launchd
    plist filename + label and the Windows task name."""
    role = (role or DEFAULT_ROLE).strip() or DEFAULT_ROLE
    return f"{LABEL_PREFIX}.{role}"


def up_argv(up_args: list[str] | None) -> list[str]:
    """The mesh subcommand the service runs. Defaults to `up --auto` (the
    same happy-path NODE_SETUP.md documents). A non-empty `up_args` replaces
    the trailing args verbatim, e.g. `["up", "--specialist", "x"]`."""
    if up_args:
        return list(up_args)
    return ["up", "--auto"]


# ---------------------------------------------------------------------------
# Linux — systemd --user unit
# ---------------------------------------------------------------------------


def render_systemd_unit(exec_path: str, up_args: list[str] | None, role: str = DEFAULT_ROLE) -> str:
    """Render a systemd --user unit running `<exec_path> up <args>`.

    Mirrors the deploy/ pattern (Restart=on-failure, network-online ordering)
    but points ExecStart at the resolved `slancha-mesh` binary so it works
    from a normal `pip install -e .` without a hardcoded source checkout.
    """
    argv = up_argv(up_args)
    exec_start = " ".join([shlex.quote(exec_path), *(shlex.quote(a) for a in argv)])
    return (
        "[Unit]\n"
        f"Description=slancha-mesh node ({role})\n"
        "Documentation=https://github.com/SlanchaAi/slancha-mesh\n"
        "After=network-online.target tailscaled.service\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={exec_start}\n"
        "Restart=on-failure\n"
        "RestartSec=15\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _plan_linux(exec_path: str, up_args: list[str] | None, role: str, home: Path) -> ServicePlan:
    label = service_label(role)
    unit_name = f"{label}.service"
    unit_dir = home / ".config" / "systemd" / "user"
    path = unit_dir / unit_name
    return ServicePlan(
        os_name="Linux",
        label=label,
        text=render_systemd_unit(exec_path, up_args, role),
        path=path,
        install_cmds=[
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "--now", unit_name],
        ],
        uninstall_cmds=[
            ["systemctl", "--user", "disable", "--now", unit_name],
            ["systemctl", "--user", "daemon-reload"],
        ],
        status_cmds=[["systemctl", "--user", "--no-pager", "status", unit_name]],
    )


# ---------------------------------------------------------------------------
# macOS — launchd LaunchAgent plist
# ---------------------------------------------------------------------------


def render_launchd_plist(exec_path: str, up_args: list[str] | None, role: str = DEFAULT_ROLE) -> str:
    """Render a launchd LaunchAgent plist running `<exec_path> up <args>`.

    `RunAtLoad` + `KeepAlive` give boot-persistence + crash-restart (the
    launchd analog of systemd `Restart=on-failure`). ProgramArguments is the
    argv vector — no shell, so paths with spaces are safe.
    """
    label = service_label(role)
    argv = [exec_path, *up_argv(up_args)]
    args_xml = "\n".join(f"        <string>{_xml_escape(a)}</string>" for a in argv)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "    <key>Label</key>\n"
        f"    <string>{_xml_escape(label)}</string>\n"
        "    <key>ProgramArguments</key>\n"
        "    <array>\n"
        f"{args_xml}\n"
        "    </array>\n"
        "    <key>RunAtLoad</key>\n"
        "    <true/>\n"
        "    <key>KeepAlive</key>\n"
        "    <true/>\n"
        "    <key>ProcessType</key>\n"
        "    <string>Background</string>\n"
        "</dict>\n"
        "</plist>\n"
    )


def _plan_macos(exec_path: str, up_args: list[str] | None, role: str, home: Path) -> ServicePlan:
    label = service_label(role)
    path = home / "Library" / "LaunchAgents" / f"{label}.plist"
    return ServicePlan(
        os_name="Darwin",
        label=label,
        text=render_launchd_plist(exec_path, up_args, role),
        path=path,
        # `launchctl unload` first makes install idempotent (load fails if
        # already loaded). The handler tolerates the unload failing on a
        # fresh install.
        install_cmds=[
            ["launchctl", "unload", str(path)],
            ["launchctl", "load", str(path)],
        ],
        uninstall_cmds=[["launchctl", "unload", str(path)]],
        status_cmds=[["launchctl", "list", label]],
    )


# ---------------------------------------------------------------------------
# Windows — schtasks ONSTART scheduled task
# ---------------------------------------------------------------------------


def render_windows_task_command(exec_path: str, up_args: list[str] | None, role: str = DEFAULT_ROLE) -> str:
    """Render the `schtasks /Create` command line that registers an ONSTART
    task running `<exec_path> up <args>`.

    Returned as a single human-readable string (what the operator would see /
    what we document); the handler runs the argv form from `_plan_windows`.
    The task runs at system start with highest privileges so it doesn't need
    an interactive login. See the module docstring for the nssm tradeoff.
    """
    label = service_label(role)
    tr = " ".join([_win_quote(exec_path), *(_win_quote(a) for a in up_argv(up_args))])
    return (
        f'schtasks /Create /TN "{label}" /SC ONSTART /RL HIGHEST /F '
        f'/TR "{tr}"'
    )


def _win_quote(s: str) -> str:
    """Wrap an arg in double quotes for the /TR command string if it contains
    whitespace. (schtasks /TR is a single string the OS re-parses.)"""
    if s and not any(c.isspace() for c in s):
        return s
    return '"' + s.replace('"', '\\"') + '"'


def _plan_windows(exec_path: str, up_args: list[str] | None, role: str, home: Path) -> ServicePlan:
    label = service_label(role)
    tr = " ".join([_win_quote(exec_path), *(_win_quote(a) for a in up_argv(up_args))])
    return ServicePlan(
        os_name="Windows",
        label=label,
        text=render_windows_task_command(exec_path, up_args, role),
        path=None,  # schtasks registers in the OS scheduler, no file we write.
        install_cmds=[
            ["schtasks", "/Create", "/TN", label, "/SC", "ONSTART",
             "/RL", "HIGHEST", "/F", "/TR", tr],
        ],
        uninstall_cmds=[["schtasks", "/Delete", "/TN", label, "/F"]],
        status_cmds=[["schtasks", "/Query", "/TN", label]],
    )


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def build_service_plan(
    os_name: str,
    exec_path: str,
    up_args: list[str] | None = None,
    role: str = DEFAULT_ROLE,
    home: Path | None = None,
) -> ServicePlan:
    """Pure dispatcher: pick the renderer for `os_name` and return a ServicePlan.

    `os_name` is a `platform.system()` value (`Linux`/`Darwin`/`Windows`).
    `home` defaults to the real home dir but is injectable for tests.
    Unknown OS → UnsupportedOSError (the CLI turns this into a clean message).
    """
    home = home or Path.home()
    key = (os_name or "").strip().lower()
    if key == "linux":
        return _plan_linux(exec_path, up_args, role, home)
    if key == "darwin":
        return _plan_macos(exec_path, up_args, role, home)
    if key == "windows":
        return _plan_windows(exec_path, up_args, role, home)
    raise UnsupportedOSError(
        f"no service-install path for OS {os_name!r}. "
        "Supported: Linux (systemd), Darwin (launchd), Windows (schtasks). "
        "Run `slancha-mesh up` directly, or wrap it in your platform's "
        "init system manually."
    )


def current_os() -> str:
    """`platform.system()` — seam so tests can monkeypatch the detected OS."""
    return platform.system()
