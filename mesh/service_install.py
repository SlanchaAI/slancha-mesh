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
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

# Reverse-DNS prefix for launchd labels and the Windows task path.
LABEL_PREFIX = "ai.slancha.mesh"
DEFAULT_ROLE = "node"
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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


def service_argv(kind: str, service_args: list[str] | None) -> list[str]:
    """Build the exact mesh or semantic-router command for a service."""

    command = {
        "node": "up",
        "router": "router",
        "semantic-router": "semantic-router",
    }.get(kind)
    if command is None:
        raise ValueError(
            f"unknown service kind {kind!r}; expected node, router, or semantic-router"
        )
    if not service_args:
        if kind == "node":
            return ["up", "--auto"]
        if kind == "semantic-router":
            return ["semantic-router", "serve"]
        return ["router"]
    args = list(service_args)
    if args[0] in {"up", "router", "semantic-router"}:
        if args[0] != command:
            raise ValueError(
                f"service kind {kind!r} cannot run {args[0]!r}; expected {command!r}"
            )
        return args
    return [command, *args]


def parse_service_environment(assignments: list[str] | None) -> dict[str, str]:
    """Parse repeatable non-secret ``NAME=VALUE`` service settings."""

    environment: dict[str, str] = {}
    for assignment in assignments or []:
        name, separator, value = assignment.partition("=")
        if not separator:
            raise ValueError(f"service environment must use NAME=VALUE: {assignment!r}")
        if not _ENV_NAME.fullmatch(name):
            raise ValueError(f"invalid environment name {name!r}")
        if "\x00" in value or "\n" in value or "\r" in value:
            raise ValueError(
                f"service environment value for {name!r} contains a newline or NUL"
            )
        environment[name] = value
    return environment


def up_argv(up_args: list[str] | None) -> list[str]:
    """The mesh subcommand the service runs. Defaults to `up --auto` (the
    same happy-path NODE_SETUP.md documents). A non-empty `up_args` replaces
    the trailing args verbatim, e.g. `["up", "--specialist", "x"]`."""
    return service_argv("node", up_args)


# ---------------------------------------------------------------------------
# Linux — systemd --user unit
# ---------------------------------------------------------------------------


def render_systemd_unit(
    exec_path: str,
    up_args: list[str] | None,
    role: str = DEFAULT_ROLE,
    *,
    kind: str = "node",
    environment: dict[str, str] | None = None,
) -> str:
    """Render a systemd --user unit running `<exec_path> up <args>`.

    Mirrors the deploy/ pattern (Restart=on-failure, network-online ordering)
    but points ExecStart at the resolved `slancha-mesh` binary so it works
    from a normal `pip install -e .` without a hardcoded source checkout.
    """
    argv = service_argv(kind, up_args)
    exec_start = " ".join([shlex.quote(exec_path), *(shlex.quote(a) for a in argv)])
    environment_lines = ""
    for name, value in sorted((environment or {}).items()):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
        environment_lines += f'Environment="{name}={escaped}"\n'
    return (
        "[Unit]\n"
        f"Description=slancha-mesh {kind} ({role})\n"
        "Documentation=https://github.com/SlanchaAi/slancha-mesh\n"
        "After=network-online.target tailscaled.service\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"{environment_lines}"
        f"ExecStart={exec_start}\n"
        "Restart=on-failure\n"
        "RestartSec=15\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _plan_linux(
    exec_path: str,
    up_args: list[str] | None,
    role: str,
    home: Path,
    kind: str,
    environment: dict[str, str],
) -> ServicePlan:
    label = service_label(role)
    unit_name = f"{label}.service"
    unit_dir = home / ".config" / "systemd" / "user"
    path = unit_dir / unit_name
    return ServicePlan(
        os_name="Linux",
        label=label,
        text=render_systemd_unit(
            exec_path, up_args, role, kind=kind, environment=environment
        ),
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


def render_launchd_plist(
    exec_path: str,
    up_args: list[str] | None,
    role: str = DEFAULT_ROLE,
    *,
    kind: str = "node",
    environment: dict[str, str] | None = None,
) -> str:
    """Render a launchd LaunchAgent plist running `<exec_path> up <args>`.

    `RunAtLoad` + `KeepAlive` give boot-persistence + crash-restart (the
    launchd analog of systemd `Restart=on-failure`). ProgramArguments is the
    argv vector — no shell, so paths with spaces are safe.
    """
    label = service_label(role)
    argv = [exec_path, *service_argv(kind, up_args)]
    args_xml = "\n".join(f"        <string>{_xml_escape(a)}</string>" for a in argv)
    environment_xml = ""
    if environment:
        pairs = "\n".join(
            f"        <key>{_xml_escape(name)}</key>\n"
            f"        <string>{_xml_escape(value)}</string>"
            for name, value in sorted(environment.items())
        )
        environment_xml = (
            "    <key>EnvironmentVariables</key>\n"
            "    <dict>\n"
            f"{pairs}\n"
            "    </dict>\n"
        )
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
        f"{environment_xml}"
        "    <key>RunAtLoad</key>\n"
        "    <true/>\n"
        "    <key>KeepAlive</key>\n"
        "    <true/>\n"
        "    <key>ProcessType</key>\n"
        "    <string>Background</string>\n"
        "</dict>\n"
        "</plist>\n"
    )


def _plan_macos(
    exec_path: str,
    up_args: list[str] | None,
    role: str,
    home: Path,
    kind: str,
    environment: dict[str, str],
) -> ServicePlan:
    label = service_label(role)
    path = home / "Library" / "LaunchAgents" / f"{label}.plist"
    return ServicePlan(
        os_name="Darwin",
        label=label,
        text=render_launchd_plist(
            exec_path, up_args, role, kind=kind, environment=environment
        ),
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


def render_windows_task_command(
    exec_path: str,
    up_args: list[str] | None,
    role: str = DEFAULT_ROLE,
    *,
    kind: str = "node",
) -> str:
    """Render the `schtasks /Create` command line that registers an ONSTART
    task running `<exec_path> up <args>`.

    Returned as a single human-readable string (what the operator would see /
    what we document); the handler runs the argv form from `_plan_windows`.
    The task runs at system start with highest privileges so it doesn't need
    an interactive login. See the module docstring for the nssm tradeoff.
    """
    label = service_label(role)
    tr = " ".join(
        [_win_quote(exec_path), *(_win_quote(a) for a in service_argv(kind, up_args))]
    )
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


def _plan_windows(
    exec_path: str,
    up_args: list[str] | None,
    role: str,
    home: Path,
    kind: str,
    environment: dict[str, str],
) -> ServicePlan:
    if environment:
        raise ValueError(
            "--env is not supported for Windows Scheduled Tasks; configure "
            "machine environment variables before installing the task"
        )
    label = service_label(role)
    tr = " ".join(
        [_win_quote(exec_path), *(_win_quote(a) for a in service_argv(kind, up_args))]
    )
    return ServicePlan(
        os_name="Windows",
        label=label,
        text=render_windows_task_command(exec_path, up_args, role, kind=kind),
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
    kind: str = "node",
    environment: dict[str, str] | None = None,
) -> ServicePlan:
    """Pure dispatcher: pick the renderer for `os_name` and return a ServicePlan.

    `os_name` is a `platform.system()` value (`Linux`/`Darwin`/`Windows`).
    `home` defaults to the real home dir but is injectable for tests.
    Unknown OS → UnsupportedOSError (the CLI turns this into a clean message).
    """
    home = home or Path.home()
    resolved_environment = environment or {}
    key = (os_name or "").strip().lower()
    if key == "linux":
        return _plan_linux(exec_path, up_args, role, home, kind, resolved_environment)
    if key == "darwin":
        return _plan_macos(exec_path, up_args, role, home, kind, resolved_environment)
    if key == "windows":
        return _plan_windows(exec_path, up_args, role, home, kind, resolved_environment)
    raise UnsupportedOSError(
        f"no service-install path for OS {os_name!r}. "
        "Supported: Linux (systemd), Darwin (launchd), Windows (schtasks). "
        "Run `slancha-mesh up` directly, or wrap it in your platform's "
        "init system manually."
    )


def current_os() -> str:
    """`platform.system()` — seam so tests can monkeypatch the detected OS."""
    return platform.system()
