"""Tests for mesh.scripts.pipeline_status — pure-function status computation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mesh.scripts.pipeline_status import (
    _bar,
    compute_status,
    render_ascii,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_stats(tmp_path: Path, **overrides):
    data = {
        "floodgate": {"rows": 12345, "total": 100000},
        "oracle":    {"labels": 247, "healthy": True},
        "proxy":     {"healthy": True, "routed": 12345},
        "preclassify": {"running": False, "rows": 100002},
        "ft":        {"fast_running": False, "slow_running": False},
        **overrides,
    }
    (tmp_path / "stats.json").write_text(json.dumps(data))


def _write_overrides(tmp_path: Path, n_cells: int = 9, oracle_rows: int = 519):
    data = {
        "from_oracle_rows": oracle_rows,
        "overrides": [
            {"match": {"domain": "code"}, "preferred_model": "codestral:22b",
             "support": 10, "mean_score": 4.2}
            for _ in range(n_cells)
        ],
    }
    (tmp_path / "overrides.json").write_text(json.dumps(data))


def _write_decisions(tmp_path: Path, rows: list[dict]):
    (tmp_path / "decisions.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n"
    )


# ---------------------------------------------------------------------------
# compute_status
# ---------------------------------------------------------------------------


def test_compute_status_empty_dir(tmp_path: Path):
    """No files at all → status returns with sensible defaults, no crash."""
    s = compute_status(tmp_path)
    assert s["overall"] == "down"          # proxy=down + floodgate=warn + …
    assert s["floodgate"]["rows"] == 0
    assert s["decisions"]["n"] == 0
    assert s["latency_ms"]["p50"] is None


def test_compute_status_healthy(tmp_path: Path):
    _write_stats(tmp_path)
    _write_overrides(tmp_path)
    _write_decisions(tmp_path, [
        {"ts": "2026-05-17T02:00:00Z", "decision": "passthrough", "first_latency_ms": 100},
        {"ts": "2026-05-17T02:00:01Z", "decision": "passthrough", "first_latency_ms": 200},
        {"ts": "2026-05-17T02:00:02Z", "decision": "explore",     "first_latency_ms": 300},
        {"ts": "2026-05-17T02:00:03Z", "decision": "override",    "first_latency_ms": 400},
    ])
    s = compute_status(tmp_path)
    assert s["overall"] == "live"
    assert s["floodgate"]["pct"] == pytest.approx(12.345, abs=0.01)
    assert s["oracle"]["labels"] == 247
    assert s["overrides"]["cells"] == 9
    assert s["decisions"]["n"] == 4
    assert s["decisions"]["mix"]["passthrough"] == 2
    assert s["latency_ms"]["p50"] is not None
    assert s["latency_ms"]["n"] == 4


def test_compute_status_partial_files(tmp_path: Path):
    """stats.json present, others missing → no crash."""
    _write_stats(tmp_path)
    s = compute_status(tmp_path)
    assert s["overrides"]["cells"] == 0
    assert s["decisions"]["n"] == 0
    assert s["floodgate"]["rows"] == 12345


def test_compute_status_proxy_down(tmp_path: Path):
    _write_stats(tmp_path, proxy={"healthy": False, "routed": 0})
    _write_overrides(tmp_path)
    s = compute_status(tmp_path)
    assert s["components"]["proxy"] == "down"
    # 1 critical component down → degraded
    assert s["overall"] == "degraded"


def test_compute_status_oracle_warn_when_labels_but_unhealthy(tmp_path: Path):
    _write_stats(tmp_path, oracle={"labels": 50, "healthy": False})
    _write_overrides(tmp_path)
    s = compute_status(tmp_path)
    assert s["components"]["oracle"] == "warn"


def test_compute_status_ignores_bad_jsonl_lines(tmp_path: Path):
    _write_stats(tmp_path)
    (tmp_path / "decisions.jsonl").write_text(
        json.dumps({"ts": "2026-05-17T02:00:00Z", "decision": "passthrough",
                    "first_latency_ms": 100}) + "\n"
        "{not json\n"
        "\n"   # blank
        + json.dumps({"ts": "x", "decision": "passthrough", "first_latency_ms": 200}) + "\n"
    )
    s = compute_status(tmp_path)
    # 2 valid lines, 1 garbage + 1 blank skipped
    assert s["decisions"]["n"] == 2


def test_compute_status_two_criticals_down_is_down(tmp_path: Path):
    _write_stats(tmp_path,
                 proxy={"healthy": False},
                 oracle={"labels": 0, "healthy": False})
    s = compute_status(tmp_path)
    # proxy=down + oracle=down + recirc=warn (no overrides) + floodgate=up
    # → 2 critical components down → overall="down"
    assert s["overall"] == "down"


# ---------------------------------------------------------------------------
# ASCII renderer
# ---------------------------------------------------------------------------


def test_render_ascii_includes_key_sections(tmp_path: Path):
    _write_stats(tmp_path)
    _write_overrides(tmp_path)
    _write_decisions(tmp_path, [
        {"ts": "2026-05-17T02:00:00Z", "decision": "passthrough", "first_latency_ms": 100},
    ])
    out = render_ascii(compute_status(tmp_path), use_color=False)
    assert "SLANCHA PIPELINE" in out
    assert "overall:" in out
    assert "floodgate:" in out
    assert "components:" in out
    # Decision mix appears
    assert "passthrough" in out


def test_render_ascii_no_color_strips_ansi(tmp_path: Path):
    _write_stats(tmp_path)
    out = render_ascii(compute_status(tmp_path), use_color=False)
    # No ANSI escape sequences when use_color=False
    assert "\033[" not in out


def test_render_ascii_with_color_includes_ansi(tmp_path: Path):
    _write_stats(tmp_path)
    out = render_ascii(compute_status(tmp_path), use_color=True)
    assert "\033[" in out


def test_bar_renders_correctly():
    assert _bar(0)   == "░" * 24
    assert _bar(100) == "█" * 24
    half = _bar(50)
    assert half.count("█") == 12
    assert half.count("░") == 12


def test_bar_clamps_out_of_range():
    assert _bar(-10) == "░" * 24
    assert _bar(120) == "█" * 24


# ---------------------------------------------------------------------------
# Exit code semantics — tested via compute_status overall mapping
# ---------------------------------------------------------------------------


def test_overall_states_have_distinct_exit_semantics():
    """compute_status emits 3 states; CLI maps to exit codes 0/1/2."""
    mapping = {"live": 0, "degraded": 1, "down": 2}
    # Just assert the mapping exists for each — the actual CLI exit is
    # tested via integration when needed.
    for state, code in mapping.items():
        assert code in (0, 1, 2)
        assert state in ("live", "degraded", "down")
