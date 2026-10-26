"""Probe tests — run on the current machine.

We can't assert exact hardware values (CI runs on x86_64, Spark runs on
GB10 aarch64), but we can assert the probe doesn't crash, fills required
fields, and that warnings are non-empty where we know fields are
unknowable.
"""

from __future__ import annotations

import ctypes
import json
import runpy
import subprocess
import sys
import time
import types
import uuid
from pathlib import Path

import pytest

import mesh.probe as probe_mod
from mesh.models import NodeProbe
from mesh.probe import (
    _FP4_TOPS_BY_CHIP,
    _MEMORY_BANDWIDTH_GBS,
    _detect_amd_gpu,
    _detect_arch,
    _detect_backends,
    _detect_chip,
    _detect_cuda_capability,
    _detect_disk_free_gb,
    _detect_lan_interfaces,
    _detect_memory_gb,
    _detect_node_id,
    _detect_public_ipv4,
    _detect_thunderbolt5,
    _detect_vram_gb,
    _lookup_chip_table,
    _measure_memory_bandwidth_gbs,
    _run,
    _try_import,
    main,
    probe_node,
)


def test_detect_memory_is_positive_cross_os():
    """RAM probe must return real values on every OS (psutil-backed).

    Regression: on Windows the OS-specific paths (/proc/meminfo, sysctl) both
    missed, so RAM reported 0.0 (found on a real Win10 box, 2026-05-26). psutil
    is a hard dep and works on Windows/Linux/macOS, so total + available are
    always > 0 on a real host.
    """
    total, available = _detect_memory_gb([])
    assert total > 0, "ram_total_gb should be > 0 (psutil cross-OS path)"
    assert available > 0, "ram_available_gb should be > 0 (the Windows 0.0 bug)"
    assert available <= total


def test_probe_node_returns_node_probe():
    p = probe_node(friendly_name="test-host")
    assert isinstance(p, NodeProbe)
    assert p.friendly_name == "test-host"
    assert p.node_id  # non-empty
    assert p.chip
    assert p.arch in ("aarch64", "x86_64", "apple-silicon")
    assert p.ram_total_gb > 0  # any real machine has RAM


def test_probe_node_json_roundtrip():
    p = probe_node()
    js = p.model_dump_json()
    parsed = json.loads(js)
    # Restore via Pydantic
    p2 = NodeProbe.model_validate(parsed)
    assert p2.node_id == p.node_id
    assert p2.chip == p.chip
    assert p2.ram_total_gb == p.ram_total_gb


def test_probe_warnings_are_a_list():
    p = probe_node()
    assert isinstance(p.probe_warnings, list)
    # On any node lacking nvidia-smi or with no bandwidth table entry,
    # we expect at least one warning.


# ---------------------------------------------------------------------------
# Windows + AMD GPU detection (issue #63). Hermetic: fake platform.system and
# subprocess via mesh.probe._run — no real wmic/nvidia-smi is invoked.
# ---------------------------------------------------------------------------

_WMIC_AMD_OUTPUT = "Name\nAMD Radeon RX 7900 XTX\n"


def test_detect_amd_gpu_parses_wmic_on_windows(monkeypatch):
    monkeypatch.setattr(probe_mod.platform, "system", lambda: "Windows")
    monkeypatch.setattr(probe_mod, "_run", lambda *a, **k: _WMIC_AMD_OUTPUT)
    name = _detect_amd_gpu([])
    assert name == "AMD Radeon RX 7900 XTX"


def test_detect_amd_gpu_none_off_windows(monkeypatch):
    # Off Windows we never shell out — short-circuit to None.
    monkeypatch.setattr(probe_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(probe_mod, "_run", lambda *a, **k: _WMIC_AMD_OUTPUT)
    assert _detect_amd_gpu([]) is None


def test_detect_amd_gpu_none_when_no_amd_adapter(monkeypatch):
    monkeypatch.setattr(probe_mod.platform, "system", lambda: "Windows")
    monkeypatch.setattr(probe_mod, "_run", lambda *a, **k: "Name\nIntel UHD Graphics\n")
    warnings: list[str] = []
    assert _detect_amd_gpu(warnings) is None
    assert any("AMD" in w for w in warnings)


def test_probe_node_surfaces_amd_gpu_on_windows(monkeypatch):
    """Windows + AMD adapter + no nvidia-smi → chip is the AMD name, gpu_vendor=amd.

    Fakes a Windows host: nvidia-smi returns nothing (no CUDA GPU), wmic returns
    an AMD adapter. cuda_capability must stay None (AMD is not CUDA).
    """
    monkeypatch.setattr(probe_mod.platform, "system", lambda: "Windows")

    def fake_run(cmd, *a, **k):
        if cmd and cmd[0] == "nvidia-smi":
            return ""  # no NVIDIA GPU
        if cmd and cmd[0] == "wmic":
            return _WMIC_AMD_OUTPUT
        return ""  # everything else (hostname -I, lspci, ...) empty

    monkeypatch.setattr(probe_mod, "_run", fake_run)

    p = probe_node(friendly_name="win-amd-box")
    assert p.gpu_vendor == "amd"
    assert p.chip == "AMD Radeon RX 7900 XTX"
    assert p.cuda_capability is None  # do NOT fake a cuda_capability for AMD


# ---------------------------------------------------------------------------
# Bandwidth table lookup (§2 name-match fix) + guarded micro-bench (§4)
# ---------------------------------------------------------------------------

_RTX_PRO_6000_FULL = "NVIDIA RTX PRO 6000 Blackwell Workstation Edition"


def test_lookup_exact_match():
    assert _lookup_chip_table(_MEMORY_BANDWIDTH_GBS, "NVIDIA GB10") == 273.0


def test_lookup_prefix_match_rtx_pro_6000():
    # nvidia-smi reports the full marketing name; the table is keyed by family.
    # Before the prefix fix this returned None for BOTH bandwidth and fp4 tops.
    assert _lookup_chip_table(_MEMORY_BANDWIDTH_GBS, _RTX_PRO_6000_FULL) == 1467.0
    assert _lookup_chip_table(_FP4_TOPS_BY_CHIP, _RTX_PRO_6000_FULL) == 4000.0


def test_lookup_longest_prefix_wins():
    table = {"NVIDIA A": 1.0, "NVIDIA A100": 2.0}
    assert _lookup_chip_table(table, "NVIDIA A100 80GB") == 2.0


def test_lookup_no_match_returns_none():
    assert _lookup_chip_table(_MEMORY_BANDWIDTH_GBS, "Totally Unknown Chip") is None


def test_measure_bandwidth_never_raises():
    # No libcuda (CI / Mac) → None; a CUDA box → positive float. Never raises,
    # never blocks past the wall-clock box, never OOMs (free-mem guarded).
    result = _measure_memory_bandwidth_gbs([])
    assert result is None or (isinstance(result, float) and result > 0)


def test_bw_source_guessed_from_table(monkeypatch):
    monkeypatch.setattr(probe_mod, "_detect_chip", lambda w: _RTX_PRO_6000_FULL)
    p = probe_node(measure_bandwidth=False)
    assert p.memory_bandwidth_gbs == 1467.0
    assert p.bw_source == "guessed"


def test_bw_source_measured_when_bench_runs(monkeypatch):
    monkeypatch.setattr(probe_mod, "_detect_chip", lambda w: _RTX_PRO_6000_FULL)
    monkeypatch.setattr(probe_mod, "_measure_memory_bandwidth_gbs", lambda w: 1500.0)
    p = probe_node(measure_bandwidth=True)
    assert p.memory_bandwidth_gbs == 1500.0
    assert p.bw_source == "measured"


def test_bw_source_falls_back_to_table_when_bench_skips(monkeypatch):
    # Bench returns None (busy GPU / non-CUDA) → keep the table value, "guessed".
    monkeypatch.setattr(probe_mod, "_detect_chip", lambda w: _RTX_PRO_6000_FULL)
    monkeypatch.setattr(probe_mod, "_measure_memory_bandwidth_gbs", lambda w: None)
    p = probe_node(measure_bandwidth=True)
    assert p.memory_bandwidth_gbs == 1467.0
    assert p.bw_source == "guessed"


# ---------------------------------------------------------------------------
# Offline error-path matrix. Every test below patches the subprocess, platform,
# filesystem, psutil, ctypes and clock boundaries, so no real nvidia-smi, wmic,
# lspci, libcuda, benchmark or network lookup ever runs. Fixtures are synthetic.
# ---------------------------------------------------------------------------


def _fake_run(responses):
    """`_run` stand-in keyed on argv[0]; unknown commands behave like a missing binary."""

    def fake(cmd, *a, **k):
        return responses.get(cmd[0], "")

    return fake


def _fake_files(monkeypatch, files):
    """Serve the given absolute paths from `files`; an Exception value is raised.

    Paths outside `files` fall through to the real `Path.read_text` so pytest's
    own file reads keep working.
    """
    real = Path.read_text

    def fake(self, *a, **k):
        key = str(self)
        if key not in files:
            return real(self, *a, **k)
        value = files[key]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(probe_mod.Path, "read_text", fake)


def _fake_psutil(monkeypatch, total, available, raises=None):
    mod = types.ModuleType("psutil")

    def virtual_memory():
        if raises is not None:
            raise raises
        return types.SimpleNamespace(total=total, available=available)

    mod.virtual_memory = virtual_memory
    monkeypatch.setitem(sys.modules, "psutil", mod)


def _system(monkeypatch, name):
    monkeypatch.setattr(probe_mod.platform, "system", lambda: name)


# --- _run -------------------------------------------------------------------


def test_run_returns_stdout_and_passes_timeout(monkeypatch):
    seen = {}

    def fake_subprocess_run(cmd, **kwargs):
        seen["cmd"], seen["kwargs"] = cmd, kwargs
        return types.SimpleNamespace(stdout="hello\n")

    monkeypatch.setattr(probe_mod.subprocess, "run", fake_subprocess_run)
    assert _run(["echo", "hello"], timeout=1.5) == "hello\n"
    assert seen["cmd"] == ["echo", "hello"]
    assert seen["kwargs"]["timeout"] == 1.5
    assert seen["kwargs"]["check"] is False  # a non-zero exit must not raise


@pytest.mark.parametrize(
    "exc",
    [
        FileNotFoundError("no such binary"),
        PermissionError("not executable"),
        subprocess.TimeoutExpired(cmd="nvidia-smi", timeout=4.0),
        subprocess.SubprocessError("boom"),
    ],
)
def test_run_swallows_launch_failures(monkeypatch, exc):
    def boom(*a, **k):
        raise exc

    monkeypatch.setattr(probe_mod.subprocess, "run", boom)
    assert _run(["nvidia-smi"]) == ""


def test_run_none_stdout_becomes_empty_string(monkeypatch):
    monkeypatch.setattr(
        probe_mod.subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout=None)
    )
    assert _run(["true"]) == ""


# --- _detect_chip -----------------------------------------------------------


def test_detect_chip_prefers_first_nvidia_line(monkeypatch):
    monkeypatch.setattr(
        probe_mod, "_run", _fake_run({"nvidia-smi": "  NVIDIA L4 \nNVIDIA L40\n"})
    )
    assert _detect_chip([]) == "NVIDIA L4"


def test_detect_chip_darwin_uses_sysctl(monkeypatch):
    _system(monkeypatch, "Darwin")
    monkeypatch.setattr(
        probe_mod, "_run", _fake_run({"sysctl": "Apple M4 Max\n"})
    )
    warnings: list[str] = []
    assert _detect_chip(warnings) == "Apple M4 Max"
    assert warnings == []


def test_detect_chip_darwin_empty_sysctl_falls_through_to_cpuinfo(monkeypatch):
    _system(monkeypatch, "Darwin")
    monkeypatch.setattr(probe_mod, "_run", _fake_run({}))
    _fake_files(monkeypatch, {"/proc/cpuinfo": "model name\t: Synthetic CPU 9000\n"})
    assert _detect_chip([]) == "Synthetic CPU 9000"


def test_detect_chip_reads_cpuinfo_case_insensitively(monkeypatch):
    _system(monkeypatch, "Linux")
    monkeypatch.setattr(probe_mod, "_run", _fake_run({}))
    _fake_files(
        monkeypatch,
        {"/proc/cpuinfo": "processor\t: 0\nModel Name\t: Synthetic: Core i0\n"},
    )
    # split(":", 1) keeps any later colon in the brand string
    assert _detect_chip([]) == "Synthetic: Core i0"


def test_detect_chip_cpuinfo_without_model_name_uses_platform_processor(monkeypatch):
    _system(monkeypatch, "Linux")
    monkeypatch.setattr(probe_mod, "_run", _fake_run({}))
    _fake_files(monkeypatch, {"/proc/cpuinfo": "processor\t: 0\nflags\t: fpu\n"})
    monkeypatch.setattr(probe_mod.platform, "processor", lambda: "arm-cpu")
    warnings: list[str] = []
    assert _detect_chip(warnings) == "arm-cpu"
    assert warnings == []


def test_detect_chip_unreadable_cpuinfo_warns_then_falls_back(monkeypatch):
    _system(monkeypatch, "Linux")
    monkeypatch.setattr(probe_mod, "_run", _fake_run({}))
    _fake_files(monkeypatch, {"/proc/cpuinfo": FileNotFoundError("gone")})
    monkeypatch.setattr(probe_mod.platform, "processor", lambda: "")
    warnings: list[str] = []
    assert _detect_chip(warnings) == "unknown"
    assert warnings == ["could not read /proc/cpuinfo"]


# --- _detect_arch -----------------------------------------------------------


@pytest.mark.parametrize(
    ("machine", "system", "expected"),
    [
        ("arm64", "Darwin", "apple-silicon"),
        ("ARM64", "Windows", "aarch64"),  # case-insensitive; non-Darwin arm
        ("aarch64", "Linux", "aarch64"),
        ("aarch64", "Darwin", "apple-silicon"),
        ("x86_64", "Linux", "x86_64"),
        ("AMD64", "Windows", "x86_64"),
        ("riscv64", "Linux", "x86_64"),  # unknown arch → documented default
        ("", "Linux", "x86_64"),
    ],
)
def test_detect_arch_matrix(monkeypatch, machine, system, expected):
    monkeypatch.setattr(probe_mod.platform, "machine", lambda: machine)
    _system(monkeypatch, system)
    assert _detect_arch() == expected


# --- _detect_cuda_capability ------------------------------------------------


def test_cuda_capability_valid_first_line(monkeypatch):
    monkeypatch.setattr(probe_mod, "_run", _fake_run({"nvidia-smi": "12.1\n8.9\n"}))
    warnings: list[str] = []
    assert _detect_cuda_capability(warnings) == "12.1"
    assert warnings == []


def test_cuda_capability_missing_nvidia_smi_warns(monkeypatch):
    monkeypatch.setattr(probe_mod, "_run", _fake_run({}))
    warnings: list[str] = []
    assert _detect_cuda_capability(warnings) is None
    assert warnings == ["no nvidia-smi or no CUDA GPU"]


@pytest.mark.parametrize("garbage", ["[N/A]\n", "8\n", "8.9.1\n", "abc\n"])
def test_cuda_capability_malformed_is_none_without_missing_gpu_warning(monkeypatch, garbage):
    # The tool answered, so "no nvidia-smi" would be a misleading warning.
    monkeypatch.setattr(probe_mod, "_run", _fake_run({"nvidia-smi": garbage}))
    warnings: list[str] = []
    assert _detect_cuda_capability(warnings) is None
    assert warnings == []


def test_cuda_capability_whitespace_only_output_warns(monkeypatch):
    # Truthy-but-blank output: cap stays "" and the output is not empty → no warning.
    monkeypatch.setattr(probe_mod, "_run", _fake_run({"nvidia-smi": "  \n"}))
    warnings: list[str] = []
    assert _detect_cuda_capability(warnings) is None
    assert warnings == []


# --- _detect_amd_gpu (extra OS fallbacks) -----------------------------------


def test_detect_amd_gpu_falls_back_to_powershell_when_wmic_missing(monkeypatch):
    _system(monkeypatch, "Windows")
    calls: list[str] = []

    def fake(cmd, *a, **k):
        calls.append(cmd[0])
        return "Radeon RX 6600\r\n" if cmd[0] == "powershell" else ""

    monkeypatch.setattr(probe_mod, "_run", fake)
    assert _detect_amd_gpu([]) == "Radeon RX 6600"
    assert calls == ["wmic", "powershell"]


def test_detect_amd_gpu_skips_header_and_blank_rows(monkeypatch):
    _system(monkeypatch, "Windows")
    out = "Name  \n\n   \nNVIDIA GeForce GTX 1070\nAMD Radeon Pro W7800\n"
    monkeypatch.setattr(probe_mod, "_run", _fake_run({"wmic": out}))
    assert _detect_amd_gpu([]) == "AMD Radeon Pro W7800"


def test_detect_amd_gpu_both_commands_empty_warns(monkeypatch):
    _system(monkeypatch, "Windows")
    monkeypatch.setattr(probe_mod, "_run", _fake_run({}))
    warnings: list[str] = []
    assert _detect_amd_gpu(warnings) is None
    assert warnings == ["no AMD/Radeon display adapter found via wmic/CIM"]


# --- _detect_memory_gb ------------------------------------------------------

_GIB_KB = 1024 * 1024  # kB per GiB, the unit /proc/meminfo uses


def test_memory_psutil_reports_gib(monkeypatch):
    _fake_psutil(monkeypatch, total=16 * 1024**3, available=4 * 1024**3)
    warnings: list[str] = []
    assert _detect_memory_gb(warnings) == (16.0, 4.0)
    assert warnings == []


def test_memory_psutil_failure_falls_back_to_proc_meminfo(monkeypatch):
    _fake_psutil(monkeypatch, 0, 0, raises=RuntimeError("psutil exploded"))
    meminfo = f"MemTotal: {8 * _GIB_KB} kB\nMemFree: 1 kB\nMemAvailable: {2 * _GIB_KB} kB\n"
    _fake_files(monkeypatch, {"/proc/meminfo": meminfo})
    warnings: list[str] = []
    assert _detect_memory_gb(warnings) == (8.0, 2.0)
    assert warnings == []


def test_memory_psutil_missing_module_falls_back(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)  # `import psutil` → ImportError
    _fake_files(monkeypatch, {"/proc/meminfo": f"MemTotal: {4 * _GIB_KB} kB\n"})
    # No MemAvailable line → documented 50% guess of total
    assert _detect_memory_gb([]) == (4.0, 2.0)


def test_memory_psutil_zero_total_is_not_trusted(monkeypatch):
    _fake_psutil(monkeypatch, total=0, available=0)
    meminfo = f"MemTotal: {2 * _GIB_KB} kB\nMemAvailable: {_GIB_KB} kB\n"
    _fake_files(monkeypatch, {"/proc/meminfo": meminfo})
    assert _detect_memory_gb([]) == (2.0, 1.0)


def test_memory_meminfo_without_total_falls_through_to_warning(monkeypatch):
    _fake_psutil(monkeypatch, 0, 0, raises=OSError("no psutil"))
    _fake_files(monkeypatch, {"/proc/meminfo": "MemAvailable: 100 kB\n"})
    _system(monkeypatch, "Linux")
    warnings: list[str] = []
    assert _detect_memory_gb(warnings) == (0.0, 0.0)
    assert warnings == ["memory probe failed; using 0.0"]


def test_memory_darwin_sysctl_fallback_halves_total(monkeypatch):
    _fake_psutil(monkeypatch, 0, 0, raises=RuntimeError("x"))
    _fake_files(monkeypatch, {"/proc/meminfo": FileNotFoundError("macOS has no /proc")})
    _system(monkeypatch, "Darwin")
    monkeypatch.setattr(probe_mod, "_run", _fake_run({"sysctl": f"{32 * 1024**3}\n"}))
    warnings: list[str] = []
    assert _detect_memory_gb(warnings) == (32.0, 16.0)
    assert warnings == []


@pytest.mark.parametrize("sysctl_out", ["", "not-a-number\n", "-5\n", "12.5\n"])
def test_memory_darwin_malformed_sysctl_warns_and_zeros(monkeypatch, sysctl_out):
    _fake_psutil(monkeypatch, 0, 0, raises=RuntimeError("x"))
    _fake_files(monkeypatch, {"/proc/meminfo": FileNotFoundError("none")})
    _system(monkeypatch, "Darwin")
    monkeypatch.setattr(probe_mod, "_run", _fake_run({"sysctl": sysctl_out}))
    warnings: list[str] = []
    assert _detect_memory_gb(warnings) == (0.0, 0.0)
    assert warnings == ["memory probe failed; using 0.0"]


def test_memory_windows_without_any_source_warns(monkeypatch):
    _fake_psutil(monkeypatch, 0, 0, raises=RuntimeError("x"))
    _fake_files(monkeypatch, {"/proc/meminfo": FileNotFoundError("none")})
    _system(monkeypatch, "Windows")
    warnings: list[str] = []
    assert _detect_memory_gb(warnings) == (0.0, 0.0)
    assert warnings == ["memory probe failed; using 0.0"]


# --- _detect_vram_gb --------------------------------------------------------


def _smi_mem(monkeypatch, out):
    monkeypatch.setattr(probe_mod, "_run", _fake_run({"nvidia-smi": out}))


def test_vram_parses_mib_to_gib(monkeypatch):
    _smi_mem(monkeypatch, "24576, 20480\n")
    warnings: list[str] = []
    assert _detect_vram_gb(warnings) == (24.0, 20.0, False)
    assert warnings == []


def test_vram_uses_first_gpu_line_only(monkeypatch):
    _smi_mem(monkeypatch, "8192, 1024\n16384, 16384\n")
    assert _detect_vram_gb([]) == (8.0, 1.0, False)


@pytest.mark.parametrize("line", ["[N/A], [N/A]", "N/A, N/A", " , ", "[n/a], 100"])
def test_vram_na_total_flags_unified_memory(monkeypatch, line):
    _smi_mem(monkeypatch, line + "\n")
    warnings: list[str] = []
    assert _detect_vram_gb(warnings) == (None, None, True)
    assert len(warnings) == 1
    assert "[N/A]" in warnings[0]
    assert "unified-mem" in warnings[0]


@pytest.mark.parametrize("free", ["[N/A]", "n/a"])
def test_vram_na_free_defaults_to_total(monkeypatch, free):
    _smi_mem(monkeypatch, f"4096, {free}\n")
    assert _detect_vram_gb([]) == (4.0, 4.0, False)


@pytest.mark.parametrize("line", ["lots, plenty", "1024, abc", "1,2,3"])
def test_vram_malformed_line_warns_and_reports_discrete_none(monkeypatch, line):
    _system(monkeypatch, "Linux")
    _smi_mem(monkeypatch, line + "\n")
    warnings: list[str] = []
    assert _detect_vram_gb(warnings) == (None, None, False)
    assert warnings == [f"could not parse nvidia-smi memory line: {line!r}"]


def test_vram_malformed_line_on_darwin_is_still_unified(monkeypatch):
    _system(monkeypatch, "Darwin")
    _smi_mem(monkeypatch, "x, y\n")
    warnings: list[str] = []
    assert _detect_vram_gb(warnings) == (None, None, True)
    assert len(warnings) == 1


def test_vram_no_comma_line_is_ignored_silently(monkeypatch):
    _system(monkeypatch, "Linux")
    _smi_mem(monkeypatch, "No devices were found\n")
    warnings: list[str] = []
    assert _detect_vram_gb(warnings) == (None, None, False)
    assert warnings == []


@pytest.mark.parametrize(
    ("system", "unified"), [("Darwin", True), ("Linux", False), ("Windows", False)]
)
def test_vram_without_nvidia_smi_depends_on_platform(monkeypatch, system, unified):
    _system(monkeypatch, system)
    monkeypatch.setattr(probe_mod, "_run", _fake_run({}))
    assert _detect_vram_gb([]) == (None, None, unified)


# --- LAN interfaces, public IPv4, Thunderbolt, disk, node id ----------------

_IP_LINK = (
    "1: lo: <LOOPBACK,UP> mtu 65536 qdisc noqueue state UNKNOWN\n"
    "2: eth0: <BROADCAST,UP> mtu 1500 qdisc mq state UP\n"
    "3: veth1a2b@if4: <BROADCAST,UP> mtu 1500 qdisc noqueue\n"
    "4: wlan0: <BROADCAST,UP> mtu 1500\n"
    "garbage line without an index\n"
)


def test_lan_interfaces_parse_ip_link_and_skip_loopback(monkeypatch):
    monkeypatch.setattr(probe_mod, "_run", _fake_run({"ip": _IP_LINK}))
    assert _detect_lan_interfaces() == ["eth0", "veth1a2b", "wlan0"]


def test_lan_interfaces_ifconfig_fallback_drops_lo0(monkeypatch):
    monkeypatch.setattr(probe_mod, "_run", _fake_run({"ifconfig": "lo0 en0 awdl0 \n"}))
    assert _detect_lan_interfaces() == ["en0", "awdl0"]


def test_lan_interfaces_empty_when_no_tool_answers(monkeypatch):
    monkeypatch.setattr(probe_mod, "_run", _fake_run({}))
    assert _detect_lan_interfaces() == []


def test_lan_interfaces_unparseable_ip_output_does_not_try_ifconfig(monkeypatch):
    # `ip` answered (non-empty) so its parse result wins even when it is empty.
    monkeypatch.setattr(
        probe_mod, "_run", _fake_run({"ip": "junk\n", "ifconfig": "en0\n"})
    )
    assert _detect_lan_interfaces() == []


@pytest.mark.parametrize(
    ("out", "expected"),
    [
        ("127.0.0.1 10.1.2.3 192.168.1.9\n", "10.1.2.3"),
        ("fe80::1 2001:db8::1 203.0.113.7\n", "203.0.113.7"),
        ("127.0.0.1 ::1\n", None),
        ("1.2.3 999.x.y.z\n", None),
        ("", None),
    ],
)
def test_public_ipv4_skips_loopback_and_non_v4(monkeypatch, out, expected):
    monkeypatch.setattr(probe_mod, "_run", _fake_run({"hostname": out}))
    assert _detect_public_ipv4() == expected


@pytest.mark.parametrize(
    ("lspci", "expected"),
    [
        ("00:0d.0 USB controller: Intel Thunderbolt 5 NHI\n", True),
        ("00:0d.0 Bridge: vendor TB5 host\n", True),
        ("00:0d.0 USB controller: Intel Thunderbolt 4 NHI\n", False),
        ("", False),  # lspci missing → False
    ],
)
def test_thunderbolt5_detection(monkeypatch, lspci, expected):
    monkeypatch.setattr(probe_mod, "_run", _fake_run({"lspci": lspci}))
    assert _detect_thunderbolt5() is expected


def test_disk_free_gb_converts_bytes(monkeypatch):
    seen = {}

    def fake_usage(path):
        seen["path"] = path
        return types.SimpleNamespace(free=5 * 1024**3)

    monkeypatch.setattr(probe_mod.shutil, "disk_usage", fake_usage)
    assert _detect_disk_free_gb("/synthetic") == 5.0
    assert seen["path"] == "/synthetic"


def test_disk_free_gb_defaults_to_cwd_and_zero_on_oserror(monkeypatch):
    seen = {}

    def boom(path):
        seen["path"] = path
        raise FileNotFoundError("vanished mount")

    monkeypatch.setattr(probe_mod.shutil, "disk_usage", boom)
    assert _detect_disk_free_gb() == 0.0
    assert seen["path"] == "."


def test_node_id_prefers_machine_id_stripped(monkeypatch):
    _fake_files(monkeypatch, {"/etc/machine-id": "abc123def\n"})
    warnings: list[str] = []
    assert _detect_node_id(warnings) == "abc123def"
    assert warnings == []


@pytest.mark.parametrize("machine_id", ["", "   \n", FileNotFoundError("none"), PermissionError("no")])
def test_node_id_falls_back_to_hostname_uuid(monkeypatch, machine_id):
    _fake_files(monkeypatch, {"/etc/machine-id": machine_id})
    monkeypatch.setattr(probe_mod.socket, "gethostname", lambda: "synthetic-host")
    warnings: list[str] = []
    expected = str(uuid.uuid5(uuid.NAMESPACE_DNS, "synthetic-host"))
    assert _detect_node_id(warnings) == expected
    assert warnings == ["no /etc/machine-id; using hostname+uuid mix"]


def test_node_id_fallback_is_stable_and_handles_empty_hostname(monkeypatch):
    _fake_files(monkeypatch, {"/etc/machine-id": OSError("none")})
    monkeypatch.setattr(probe_mod.socket, "gethostname", lambda: "")
    first = _detect_node_id([])
    assert first == str(uuid.uuid5(uuid.NAMESPACE_DNS, "unknown"))
    assert _detect_node_id([]) == first


# --- Backends ---------------------------------------------------------------


def _backends(monkeypatch, *, which=(), imports=(), system="Linux"):
    monkeypatch.setattr(
        probe_mod.shutil, "which", lambda name: f"/usr/bin/{name}" if name in which else None
    )
    monkeypatch.setattr(probe_mod, "_try_import", lambda mod: mod in imports)
    _system(monkeypatch, system)
    return _detect_backends()


def test_backends_none_installed(monkeypatch):
    assert _backends(monkeypatch) == []


def test_backends_vllm_by_binary_or_import(monkeypatch):
    assert _backends(monkeypatch, which={"vllm"}) == ["vllm"]
    assert _backends(monkeypatch, imports={"vllm"}) == ["vllm"]


@pytest.mark.parametrize(
    ("which", "imports"),
    [({"llama-cli"}, set()), ({"llama"}, set()), (set(), {"llama_cpp"})],
)
def test_backends_llamacpp_any_of_three_signals(monkeypatch, which, imports):
    assert _backends(monkeypatch, which=which, imports=imports) == ["llamacpp"]


def test_backends_ollama_binary_only(monkeypatch):
    assert _backends(monkeypatch, which={"ollama"}) == ["ollama"]
    assert _backends(monkeypatch, imports={"ollama"}) == []  # no import probe


def test_backends_mlx_only_on_darwin(monkeypatch):
    assert _backends(monkeypatch, imports={"mlx"}, system="Linux") == []
    assert _backends(monkeypatch, imports={"mlx"}, system="Darwin") == ["mlx"]


def test_backends_order_is_stable_when_everything_present(monkeypatch):
    got = _backends(
        monkeypatch,
        which={"vllm", "llama-cli", "ollama"},
        imports={"mlx"},
        system="Darwin",
    )
    assert got == ["vllm", "llamacpp", "ollama", "mlx"]


def test_try_import_finds_stdlib_and_misses_unknown():
    assert _try_import("json") is True
    assert _try_import("definitely_not_an_installed_module_xyz") is False


@pytest.mark.parametrize("exc", [ImportError("bad parent"), ValueError("__spec__ is None")])
def test_try_import_swallows_find_spec_errors(monkeypatch, exc):
    import importlib.util

    def boom(name):
        raise exc

    monkeypatch.setattr(importlib.util, "find_spec", boom)
    assert _try_import("anything") is False


# --- Bandwidth micro-bench with a fake libcuda ------------------------------

_GIB = 1 << 30
_BENCH_OK_FREE = 16 * _GIB  # needs 2*2GiB buffers + 4GiB headroom = 8GiB


def _handle(arg):
    """The ctypes object behind a `ctypes.byref(...)` argument."""
    return arg._obj


class _FakeCuda:
    """Scriptable libcuda: records calls, can fail the Nth call of any function."""

    def __init__(self, *, free=_BENCH_OK_FREE, ms=(10.0,), fail_on=None, raise_on=None):
        self.free = free
        self.ms = list(ms)
        self.fail_on = dict(fail_on or {})
        self.raise_on = dict(raise_on or {})
        self.calls: dict[str, int] = {}
        self._ms_index = 0

    def __getattr__(self, name):
        if not name.startswith("cu"):
            raise AttributeError(name)

        class _Fn:
            argtypes = None

            def __call__(inner, *args):
                return self._dispatch(name, args)

        fn = _Fn()
        object.__setattr__(self, name, fn)
        return fn

    def _dispatch(self, name, args):
        n = self.calls[name] = self.calls.get(name, 0) + 1
        if name in self.raise_on and self.raise_on[name] == n:
            raise RuntimeError(f"driver fault in {name}")
        if self.fail_on.get(name) == n:
            return 1
        if name == "cuMemGetInfo_v2":
            _handle(args[0]).value = self.free
            _handle(args[1]).value = self.free
        elif name == "cuMemAlloc_v2":
            _handle(args[0]).value = 0x1000 * n
        elif name == "cuEventCreate":
            _handle(args[0]).value = 0x2000 + n
        elif name == "cuEventElapsedTime":
            idx = min(self._ms_index, len(self.ms) - 1)
            _handle(args[0]).value = self.ms[idx]
            self._ms_index += 1
        return 0


def _install_cuda(monkeypatch, cuda, *, platform_name="linux"):
    loaded: list[str] = []

    def fake_cdll(name):
        loaded.append(name)
        return cuda

    monkeypatch.setattr(ctypes, "CDLL", fake_cdll)
    monkeypatch.setattr(sys, "platform", platform_name)
    return loaded


def test_bench_computes_gbs_from_best_copy_time(monkeypatch):
    cuda = _FakeCuda(ms=(12.0, 10.0, 11.0, 20.0))
    loaded = _install_cuda(monkeypatch, cuda)
    warnings: list[str] = []
    result = _measure_memory_bandwidth_gbs(warnings)
    # DtoD counts read+write: 2 * 2GiB / 10ms = 429.5 GB/s (best of the samples)
    assert result == 429.5
    assert warnings == []
    assert loaded == ["libcuda.so.1"]
    assert cuda.calls["cuMemcpyDtoD_v2"] == 3 + 50  # warmup + capped timed loop


def test_bench_releases_every_resource_on_success(monkeypatch):
    cuda = _FakeCuda()
    _install_cuda(monkeypatch, cuda)
    assert _measure_memory_bandwidth_gbs([]) is not None
    assert cuda.calls["cuMemFree_v2"] == 2
    assert cuda.calls["cuEventDestroy_v2"] == 2
    assert cuda.calls["cuCtxDestroy_v2"] == 1


def test_bench_uses_windows_driver_dll_name(monkeypatch):
    cuda = _FakeCuda()
    loaded = _install_cuda(monkeypatch, cuda, platform_name="win32")
    assert _measure_memory_bandwidth_gbs([]) is not None
    assert loaded == ["nvcuda"]


def test_bench_returns_none_without_libcuda(monkeypatch):
    attempts: list[str] = []

    def missing(name):
        attempts.append(name)
        raise OSError(f"{name}: cannot open shared object file")

    monkeypatch.setattr(ctypes, "CDLL", missing)
    monkeypatch.setattr(sys, "platform", "darwin")
    warnings: list[str] = []
    assert _measure_memory_bandwidth_gbs(warnings) is None
    assert attempts == ["libcuda.so.1"]
    assert warnings == []  # a non-CUDA box is normal, not a warning


def test_bench_skips_when_free_memory_is_too_tight(monkeypatch):
    cuda = _FakeCuda(free=3 * _GIB)
    _install_cuda(monkeypatch, cuda)
    warnings: list[str] = []
    assert _measure_memory_bandwidth_gbs(warnings) is None
    assert warnings == [
        "bandwidth bench skipped: GPU free 3GB < 8GB needed; keeping table value (guessed)"
    ]
    assert "cuMemAlloc_v2" not in cuda.calls  # never allocated on a tight GPU
    assert cuda.calls["cuCtxDestroy_v2"] == 1  # but the context was released


def test_bench_runs_at_exactly_the_required_free_memory(monkeypatch):
    cuda = _FakeCuda(free=8 * _GIB)
    _install_cuda(monkeypatch, cuda)
    warnings: list[str] = []
    assert _measure_memory_bandwidth_gbs(warnings) == 429.5
    assert warnings == []


@pytest.mark.parametrize(
    ("fail_on", "allocs_freed", "events_destroyed", "ctx_destroyed"),
    [
        ({"cuInit": 1}, 0, 0, 0),
        ({"cuDeviceGet": 1}, 0, 0, 0),
        ({"cuCtxCreate_v2": 1}, 0, 0, 0),  # context never made → not destroyed
        ({"cuMemGetInfo_v2": 1}, 0, 0, 1),
        ({"cuMemAlloc_v2": 1}, 0, 0, 1),
        ({"cuMemAlloc_v2": 2}, 1, 0, 1),  # only the first buffer is freed
        ({"cuEventCreate": 1}, 2, 0, 1),
        ({"cuEventCreate": 2}, 2, 1, 1),
        ({"cuMemcpyDtoD_v2": 1}, 2, 2, 1),  # warmup copy fails
        ({"cuCtxSynchronize": 1}, 2, 2, 1),
    ],
)
def test_bench_driver_error_returns_none_and_cleans_up(
    monkeypatch, fail_on, allocs_freed, events_destroyed, ctx_destroyed
):
    cuda = _FakeCuda(fail_on=fail_on)
    _install_cuda(monkeypatch, cuda)
    warnings: list[str] = []
    assert _measure_memory_bandwidth_gbs(warnings) is None
    assert warnings == []  # a non-zero return code is a quiet skip
    assert cuda.calls.get("cuMemFree_v2", 0) == allocs_freed
    assert cuda.calls.get("cuEventDestroy_v2", 0) == events_destroyed
    assert cuda.calls.get("cuCtxDestroy_v2", 0) == ctx_destroyed


@pytest.mark.parametrize("fail_on", [{"cuMemcpyDtoD_v2": 4}, {"cuEventSynchronize": 1}])
def test_bench_failure_in_timed_loop_yields_none(monkeypatch, fail_on):
    # Copy #4 is the first timed copy (3 warmups first). Breaking out before any
    # sample is recorded must not report a bogus bandwidth.
    cuda = _FakeCuda(fail_on=fail_on)
    _install_cuda(monkeypatch, cuda)
    assert _measure_memory_bandwidth_gbs([]) is None
    assert cuda.calls["cuMemFree_v2"] == 2
    assert cuda.calls["cuCtxDestroy_v2"] == 1


def test_bench_failure_after_some_samples_keeps_best_so_far(monkeypatch):
    cuda = _FakeCuda(ms=(8.0,), fail_on={"cuMemcpyDtoD_v2": 6})  # 2 timed copies ok
    _install_cuda(monkeypatch, cuda)
    result = _measure_memory_bandwidth_gbs([])
    assert result == round(2.0 * (1 << 31) / 0.008 / 1e9, 1)


@pytest.mark.parametrize("ms", [0.0, -1.0])
def test_bench_non_positive_elapsed_time_is_none(monkeypatch, ms):
    cuda = _FakeCuda(ms=(ms,))
    _install_cuda(monkeypatch, cuda)
    assert _measure_memory_bandwidth_gbs([]) is None


def test_bench_deadline_before_first_sample_is_none(monkeypatch):
    cuda = _FakeCuda()
    _install_cuda(monkeypatch, cuda)
    ticks = iter([100.0])  # deadline = 103.0

    def clock():
        return next(ticks, 1000.0)  # every later read is past the deadline

    monkeypatch.setattr(time, "monotonic", clock)
    assert _measure_memory_bandwidth_gbs([]) is None
    assert cuda.calls["cuMemcpyDtoD_v2"] == 3  # warmup only; no timed copies


def test_bench_unexpected_exception_becomes_warning_and_still_cleans_up(monkeypatch):
    cuda = _FakeCuda(raise_on={"cuMemcpyDtoD_v2": 2})
    _install_cuda(monkeypatch, cuda)
    warnings: list[str] = []
    assert _measure_memory_bandwidth_gbs(warnings) is None
    assert warnings == ["bandwidth bench errored (RuntimeError); keeping table value"]
    assert cuda.calls["cuMemFree_v2"] == 2
    assert cuda.calls["cuCtxDestroy_v2"] == 1


# --- probe_node orchestration with every detector stubbed -------------------


def _stub_detectors(monkeypatch, **over):
    values = {
        "chip": "Synthetic CPU",
        "arch": "x86_64",
        "cuda": None,
        "mem": (15.999, 7.001),
        "vram": (None, None, False),
        "amd": None,
        "node_id": "node-1",
        "ipv4": "10.0.0.5",
        "lans": ["eth0"],
        "tb5": False,
        "backends": ["ollama"],
        "disk": 12.3456,
        "bench": None,
    }
    values.update(over)
    monkeypatch.setattr(probe_mod, "_detect_chip", lambda w: values["chip"])
    monkeypatch.setattr(probe_mod, "_detect_arch", lambda: values["arch"])
    monkeypatch.setattr(probe_mod, "_detect_cuda_capability", lambda w: values["cuda"])
    monkeypatch.setattr(probe_mod, "_detect_memory_gb", lambda w: values["mem"])
    monkeypatch.setattr(probe_mod, "_detect_vram_gb", lambda w: values["vram"])
    monkeypatch.setattr(probe_mod, "_detect_amd_gpu", lambda w: values["amd"])
    monkeypatch.setattr(probe_mod, "_detect_node_id", lambda w: values["node_id"])
    monkeypatch.setattr(probe_mod, "_detect_public_ipv4", lambda: values["ipv4"])
    monkeypatch.setattr(probe_mod, "_detect_lan_interfaces", lambda: values["lans"])
    monkeypatch.setattr(probe_mod, "_detect_thunderbolt5", lambda: values["tb5"])
    monkeypatch.setattr(probe_mod, "_detect_backends", lambda: values["backends"])
    monkeypatch.setattr(probe_mod, "_detect_disk_free_gb", lambda path: values["disk"])
    monkeypatch.setattr(probe_mod, "_measure_memory_bandwidth_gbs", lambda w: values["bench"])
    monkeypatch.setattr(probe_mod.socket, "gethostname", lambda: "synthetic-host")


def test_probe_node_rounds_and_passes_through_stub_values(monkeypatch):
    _stub_detectors(
        monkeypatch,
        chip="NVIDIA L40",
        cuda="8.9",
        vram=(47.9999, 40.001, False),
        tb5=True,
        backends=["vllm", "ollama"],
    )
    p = probe_node()
    assert p.friendly_name == "synthetic-host"  # falls back to socket.gethostname()
    assert p.node_id == "node-1"
    assert p.chip == "NVIDIA L40"
    assert p.cuda_capability == "8.9"
    assert p.gpu_vendor is None  # CUDA found → AMD probe is skipped
    assert p.fp4_tops is None  # Ada has no FP4 row
    assert p.fp16_tops == 362.0
    assert p.ram_total_gb == 16.0
    assert p.ram_available_gb == 7.0
    assert p.vram_total_gb == 48.0
    assert p.vram_available_gb == 40.0
    assert p.memory_bandwidth_gbs == 864.0
    assert p.bw_source == "guessed"
    assert p.public_ipv4 == "10.0.0.5"
    assert p.lan_interfaces == ["eth0"]
    assert p.thunderbolt5 is True
    assert p.available_backends == ["vllm", "ollama"]
    assert p.disk_free_gb == 12.35
    assert p.bandwidth_to_master_mbps is None and p.rtt_to_master_ms is None
    assert p.probe_warnings == []


def test_probe_node_does_not_probe_amd_when_cuda_present(monkeypatch):
    _stub_detectors(monkeypatch, cuda="9.0", amd="AMD Radeon Should Not Win")
    p = probe_node()
    assert p.chip == "Synthetic CPU"
    assert p.gpu_vendor is None


def test_probe_node_amd_name_replaces_cpu_chip(monkeypatch):
    _stub_detectors(monkeypatch, cuda=None, amd="AMD Radeon RX 7900 XTX")
    p = probe_node()
    assert (p.chip, p.gpu_vendor, p.cuda_capability) == ("AMD Radeon RX 7900 XTX", "amd", None)


def test_probe_node_zero_vram_reported_as_none(monkeypatch):
    _stub_detectors(monkeypatch, vram=(0.0, 0.0, False))
    p = probe_node()
    assert p.vram_total_gb is None and p.vram_available_gb is None


def test_probe_node_gb10_without_vram_is_unified_and_warns(monkeypatch):
    _stub_detectors(monkeypatch, chip="NVIDIA GB10", cuda="12.1", vram=(None, None, False))
    p = probe_node()
    assert p.unified_memory is True
    assert p.vram_total_gb is None
    assert p.memory_bandwidth_gbs == 273.0
    assert p.fp4_tops == 3800.0
    assert p.probe_warnings == [
        "GB10 unified-mem: using ram_available_gb - 8 as effective VRAM"
    ]


def test_probe_node_gb10_with_vram_is_unified_without_warning(monkeypatch):
    _stub_detectors(monkeypatch, chip="NVIDIA GB10", vram=(96.0, 80.0, False))
    p = probe_node()
    assert p.unified_memory is True
    assert p.vram_total_gb == 96.0
    assert p.probe_warnings == []


def test_probe_node_unknown_chip_warns_about_missing_bandwidth(monkeypatch):
    _stub_detectors(monkeypatch, chip="  Mystery Accelerator  ")
    p = probe_node(friendly_name="named")
    assert p.friendly_name == "named"
    assert p.memory_bandwidth_gbs is None
    assert p.bw_source is None
    assert p.fp16_tops is None
    assert p.probe_warnings == [
        (
            "no memory_bandwidth for 'Mystery Accelerator' (table miss, bench unavailable); "
            "allocator falls back to estimated_tps_at"
        )
    ]


def test_probe_node_measured_bandwidth_on_unknown_chip_suppresses_warning(monkeypatch):
    _stub_detectors(monkeypatch, chip="Mystery Accelerator", bench=777.7)
    p = probe_node(measure_bandwidth=True)
    assert (p.memory_bandwidth_gbs, p.bw_source) == (777.7, "measured")
    assert p.probe_warnings == []


def test_probe_node_unknown_chip_bench_miss_keeps_none(monkeypatch):
    _stub_detectors(monkeypatch, chip="Mystery Accelerator", bench=None)
    p = probe_node(measure_bandwidth=True)
    assert (p.memory_bandwidth_gbs, p.bw_source) == (None, None)
    assert len(p.probe_warnings) == 1


def test_probe_node_skips_bench_unless_requested(monkeypatch):
    _stub_detectors(monkeypatch, chip="NVIDIA H100")

    def must_not_run(warnings):
        raise AssertionError("bench ran on the heartbeat path")

    monkeypatch.setattr(probe_mod, "_measure_memory_bandwidth_gbs", must_not_run)
    assert probe_node().bw_source == "guessed"


# --- CLI --------------------------------------------------------------------


def _cli(monkeypatch, capsys, argv, **stub):
    _stub_detectors(monkeypatch, chip="NVIDIA H100", cuda="9.0", **stub)
    monkeypatch.setattr(sys, "argv", ["mesh.probe", *argv])
    main()
    return capsys.readouterr()


def test_cli_emits_compact_json_by_default(monkeypatch, capsys):
    out = _cli(monkeypatch, capsys, [])
    assert out.err == ""
    assert out.out.count("\n") == 1  # one compact line
    data = json.loads(out.out)
    assert data["chip"] == "NVIDIA H100"
    assert data["friendly_name"] == "synthetic-host"
    assert data["memory_bandwidth_gbs"] == 3350.0
    assert data["bw_source"] == "guessed"
    assert data["available_backends"] == ["ollama"]
    NodeProbe.model_validate(data)  # the emitted contract round-trips


def test_cli_pretty_flag_indents(monkeypatch, capsys):
    out = _cli(monkeypatch, capsys, ["--pretty", "--friendly-name", "cli-node"])
    assert out.out.startswith("{\n  ")
    assert json.loads(out.out)["friendly_name"] == "cli-node"


def test_cli_json_flag_is_accepted_and_still_compact(monkeypatch, capsys):
    out = _cli(monkeypatch, capsys, ["--json"])
    assert out.out.count("\n") == 1
    assert json.loads(out.out)["node_id"] == "node-1"


@pytest.mark.parametrize(("flag", "expected"), [([], None), (["--measure-bandwidth"], 1234.5)])
def test_cli_measure_bandwidth_flag_reaches_the_bench(monkeypatch, capsys, flag, expected):
    out = _cli(monkeypatch, capsys, flag, bench=1234.5)
    data = json.loads(out.out)
    if expected is None:
        assert data["bw_source"] == "guessed"
    else:
        assert (data["memory_bandwidth_gbs"], data["bw_source"]) == (expected, "measured")


def test_cli_rejects_unknown_flag(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["mesh.probe", "--bogus"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


@pytest.mark.filterwarnings("ignore::RuntimeWarning")
def test_module_entrypoint_runs_main_offline(monkeypatch, capsys):
    """`python -m mesh.probe` path, with the OS boundaries (not mesh.probe) faked."""
    import importlib.util

    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout="")
    )
    real_find_spec = importlib.util.find_spec

    def find_spec(name, *a, **k):
        # runpy itself needs the real lookup for mesh.*; every backend probe misses.
        return real_find_spec(name, *a, **k) if name.startswith("mesh") else None

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)
    monkeypatch.setattr(probe_mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        probe_mod.shutil, "disk_usage", lambda p: types.SimpleNamespace(free=2 * 1024**3)
    )
    monkeypatch.setattr(probe_mod.socket, "gethostname", lambda: "entry-host")
    _system(monkeypatch, "Linux")
    _fake_psutil(monkeypatch, total=8 * 1024**3, available=3 * 1024**3)
    _fake_files(
        monkeypatch,
        {
            "/proc/cpuinfo": "model name\t: Synthetic CPU\n",
            "/etc/machine-id": "entry-machine-id\n",
        },
    )
    monkeypatch.setattr(sys, "argv", ["mesh.probe"])
    runpy.run_module("mesh.probe", run_name="__main__")
    data = json.loads(capsys.readouterr().out)
    assert data["node_id"] == "entry-machine-id"
    assert data["friendly_name"] == "entry-host"
    assert data["chip"] == "Synthetic CPU"
    assert data["ram_total_gb"] == 8.0
    assert data["disk_free_gb"] == 2.0
    assert data["available_backends"] == []
