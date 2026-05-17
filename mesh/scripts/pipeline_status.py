"""One-shot terminal status report for the slancha recirculating pipeline.

Reads the same dashboard/ directory the operator console + live HTML
consume (stats.json + overrides.json + decisions.jsonl) and prints a
compact ASCII summary. Useful from SSH sessions, ops scripts, or
just-to-glance-at-state from anywhere with read access to the
dashboard directory.

Usage:

    # Local — reads from a slancha-test/dashboard/ checkout
    python -m mesh.scripts.pipeline_status --dashboard slancha-test/dashboard

    # Remote — over SSH (read-only, no daemons spun)
    ssh admin@spark python -m mesh.scripts.pipeline_status \\
        --dashboard /home/admin/Source/slancha-test/dashboard

    # JSON output (for ops scripts / monitoring scrapers)
    python -m mesh.scripts.pipeline_status --dashboard ... --json

The script is read-only and never mutates anything. Subcommand-style
(start/stop/restart) is intentionally out of scope — the daemons live
on the spark host(s) and SSH-orchestrated control is beyond what one
small CLI should do safely.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _load_jsonl_tail(path: Path, n: int = 1000) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        for line in lines[-n:]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return out


# ---------------------------------------------------------------------------
# Stat computation (pure)
# ---------------------------------------------------------------------------


def compute_status(dashboard_dir: Path) -> dict[str, Any]:
    """Read dashboard files and return a structured status dict.

    The dict is consumed by both the ASCII renderer and the JSON output.
    Keep new fields backward-compatible (additive) — downstream monitoring
    scripts will key on these.
    """
    stats = _load_json(dashboard_dir / "stats.json") or {}
    overrides = _load_json(dashboard_dir / "overrides.json") or {}
    decisions = _load_jsonl_tail(dashboard_dir / "decisions.jsonl", n=1000)

    # Floodgate
    fg_rows = stats.get("floodgate", {}).get("rows", 0)
    fg_total = stats.get("floodgate", {}).get("total", 100000)
    fg_pct = (100 * fg_rows / fg_total) if fg_total > 0 else 0.0

    # Override table
    override_count = len(overrides.get("overrides", []))
    from_oracle_rows = overrides.get("from_oracle_rows", 0)

    # Decision mix + latency percentiles from rolling buffer
    decision_mix = Counter()
    latencies: list[int] = []
    for d in decisions:
        decision_mix[d.get("decision", "unknown")] += 1
        lat = d.get("first_latency_ms")
        if isinstance(lat, (int, float)) and lat > 0:
            latencies.append(int(lat))
    latencies.sort()

    def _pct(p: float) -> int | None:
        if not latencies:
            return None
        return latencies[min(len(latencies) - 1, int(len(latencies) * p))]

    # Component health (heuristic — mirrors pipeline.html updateHealthPanel)
    proxy_healthy = stats.get("proxy", {}).get("healthy", False)
    oracle_healthy = stats.get("oracle", {}).get("healthy", False)
    oracle_labels = stats.get("oracle", {}).get("labels", 0)
    preclassify = stats.get("preclassify", {})
    ft = stats.get("ft", {})
    components = {
        "proxy":     "up" if proxy_healthy else "down",
        "recirc":    "up" if override_count > 0 else "warn",
        "oracle":    "up" if oracle_healthy else ("warn" if oracle_labels > 0 else "down"),
        "floodgate": "up" if fg_rows > 0 else "warn",
        "mmbert":    ("up" if preclassify.get("rows", 0) >= 100000
                      else "warn" if preclassify.get("running") else "down"),
        "ft":        "up" if (ft.get("fast_running") or ft.get("slow_running")) else "warn",
    }
    down_critical = sum(
        1 for k in ("proxy", "recirc", "oracle", "floodgate")
        if components[k] == "down"
    )
    if down_critical >= 2:
        overall = "down"
    elif down_critical >= 1:
        overall = "degraded"
    else:
        overall = "live"

    return {
        "checked_at":          datetime.now(timezone.utc).isoformat(),
        "dashboard_dir":       str(dashboard_dir),
        "overall":             overall,
        "floodgate": {
            "rows":  fg_rows,
            "total": fg_total,
            "pct":   round(fg_pct, 2),
        },
        "oracle": {
            "labels":  oracle_labels,
            "healthy": oracle_healthy,
        },
        "overrides": {
            "cells":            override_count,
            "from_oracle_rows": from_oracle_rows,
        },
        "latency_ms": {
            "p50": _pct(0.5),
            "p95": _pct(0.95),
            "p99": _pct(0.99),
            "n":   len(latencies),
        },
        "decisions": {
            "n":   sum(decision_mix.values()),
            "mix": dict(decision_mix),
        },
        "components": components,
    }


# ---------------------------------------------------------------------------
# ASCII rendering
# ---------------------------------------------------------------------------


_OVERALL_GLYPHS = {
    "live":     ("\033[32m", "● LIVE",     "\033[0m"),
    "degraded": ("\033[33m", "▲ DEGRADED", "\033[0m"),
    "down":     ("\033[31m", "✖ DOWN",     "\033[0m"),
}


def _color_overall(s: str, use_color: bool) -> str:
    pre, label, post = _OVERALL_GLYPHS.get(s, ("", s.upper(), ""))
    return f"{pre}{label}{post}" if use_color else label


def _bar(pct: float, width: int = 24) -> str:
    pct = max(0.0, min(100.0, pct))
    fill = int(round(pct * width / 100))
    return "█" * fill + "░" * (width - fill)


def render_ascii(status: dict[str, Any], use_color: bool = True) -> str:
    fg = status["floodgate"]
    oracle = status["oracle"]
    ovr = status["overrides"]
    lat = status["latency_ms"]
    dec = status["decisions"]
    comps = status["components"]

    lines: list[str] = []
    lines.append("=" * 62)
    lines.append(f"SLANCHA PIPELINE — {status['checked_at']}")
    lines.append(f"dashboard: {status['dashboard_dir']}")
    lines.append("=" * 62)
    lines.append("")
    lines.append(f"overall:    {_color_overall(status['overall'], use_color)}")
    lines.append("")
    lines.append(f"floodgate:  {fg['rows']:>8,} / {fg['total']:,} ({fg['pct']:>5.1f}%)  {_bar(fg['pct'])}")
    lines.append(f"oracle:     {oracle['labels']:>8,} labels  (healthy={oracle['healthy']})")
    lines.append(f"overrides:  {ovr['cells']:>8} cells, {ovr['from_oracle_rows']:,} from oracle rows")
    lines.append("")
    if lat["n"]:
        lines.append(f"latency:    p50={lat['p50']:>4} ms   p95={lat['p95']:>5} ms   p99={lat['p99']:>5} ms   (n={lat['n']})")
    else:
        lines.append("latency:    — (no decisions yet)")
    if dec["n"]:
        mix = dec["mix"]
        parts = []
        for k in ("passthrough", "explore", "override"):
            v = mix.get(k, 0)
            pct = 100 * v / dec["n"] if dec["n"] else 0
            parts.append(f"{k}={v} ({pct:.0f}%)")
        lines.append(f"decisions:  {dec['n']:>5} total · {'  '.join(parts)}")
    else:
        lines.append("decisions:  — (no decisions yet)")
    lines.append("")
    lines.append("components:")
    for name, state in comps.items():
        glyph = {"up": "●", "warn": "▲", "down": "✖"}.get(state, "?")
        if use_color:
            color = {"up": "\033[32m", "warn": "\033[33m", "down": "\033[31m"}.get(state, "")
            reset = "\033[0m" if color else ""
            lines.append(f"  {color}{glyph}{reset} {name:<12} {state.upper()}")
        else:
            lines.append(f"  {glyph} {name:<12} {state.upper()}")
    lines.append("=" * 62)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--dashboard",
        type=Path,
        required=True,
        help="Path to the dashboard/ dir with stats.json, overrides.json, decisions.jsonl.",
    )
    ap.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of ASCII.")
    ap.add_argument("--no-color", action="store_true", help="Disable ANSI color in ASCII output.")
    args = ap.parse_args(argv)

    if not args.dashboard.exists():
        print(f"dashboard dir not found: {args.dashboard}", file=sys.stderr)
        return 2

    status = compute_status(args.dashboard)
    if args.json:
        print(json.dumps(status, indent=2))
    else:
        use_color = not args.no_color and sys.stdout.isatty()
        print(render_ascii(status, use_color=use_color))

    # Exit code semantics for monitoring: 0=live, 1=degraded, 2=down
    return {"live": 0, "degraded": 1, "down": 2}.get(status["overall"], 0)


if __name__ == "__main__":
    raise SystemExit(main())
