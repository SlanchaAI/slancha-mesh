"""Eval-results loader + time-series panel computers.

Consumes `dashboard/eval_results.jsonl` — one row per eval-pass that
re-routes the held-out set through whatever the router currently is
and scores responses via the oracle judge.

The headline number this answers: *"Is the router actually getting
better over time?"*

Expected row schema (one per eval pass):

    {"ts":               "2026-05-17T03:00:00Z",
     "router_version":   "fast_head_v3 + overrides_v17",
     "fast_head_version": 3,
     "overrides_version": 17,
     "holdout_version":   1,
     "n_eval":            500,
     "judge_model":       "qwen3-coder-30b-a3b-fp8",
     "mean_score":        4.12,
     "median_score":      4.0,
     "pct_acceptable":    0.84,
     "pct_failure":       0.06,
     "per_domain_mean":   {"code": 4.5, "general": 4.1, ...},
     "per_model_mean":    {"qwen3-coder-30b": 4.3, "qwen3-8b": 3.9, "cloud": 4.5},
     "elapsed_seconds":   142.3}

Spark writes this file from their eval-runner (one row per pass).
Mac dashboard reads tail + renders the time-series + per-domain
breakdown + per-router-version comparison.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeAlias

EvalRecord: TypeAlias = dict[str, Any]


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


def load_eval_results(path: Path | str) -> list[EvalRecord]:
    """Parse eval_results.jsonl. Sorted ascending by ts (so the time-series
    panel can render directly without re-sorting). Blank lines skipped.
    Invalid lines raise ValueError with line number."""
    path = Path(path)
    out: list[EvalRecord] = []
    if not path.exists():
        return out
    with path.open("r", encoding="utf-8") as f:
        for i, raw in enumerate(f, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError as exc:
                raise ValueError(f"eval JSONL line {i}: invalid JSON ({exc})") from exc
    return sorted(out, key=lambda r: r.get("ts") or "")


# ---------------------------------------------------------------------------
# Time-series: mean score over eval passes
# ---------------------------------------------------------------------------


def mean_score_over_time(records: list[EvalRecord]) -> list[tuple[datetime, float, int]]:
    """`(ts, mean_score, n_eval)` per pass, sorted ascending.

    Empty input → empty list. Records missing ts or mean_score are skipped.
    """
    out: list[tuple[datetime, float, int]] = []
    for r in records:
        ts_str = r.get("ts")
        score = r.get("mean_score")
        if not ts_str or not isinstance(score, (int, float)):
            continue
        try:
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        out.append((dt, float(score), int(r.get("n_eval", 0))))
    return out


# ---------------------------------------------------------------------------
# Per-domain trajectory
# ---------------------------------------------------------------------------


def per_domain_score_over_time(
    records: list[EvalRecord],
) -> dict[str, list[tuple[datetime, float, int]]]:
    """{domain: [(ts, mean_score_in_domain, n_in_domain), ...]} per pass.

    Each domain gets its own time series so dashboard can chart e.g. how
    code-domain quality evolves separately from creative-domain quality.
    """
    out: dict[str, list[tuple[datetime, float, int]]] = defaultdict(list)
    for r in records:
        ts_str = r.get("ts")
        per_dom = r.get("per_domain_mean") or {}
        if not ts_str or not isinstance(per_dom, dict):
            continue
        try:
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        # Approximate per-domain n: n_eval × pct(domain) if known; else 0
        n_eval = int(r.get("n_eval", 0))
        for d, score in per_dom.items():
            if isinstance(score, (int, float)):
                out[d].append((dt, float(score), n_eval))
    return dict(out)


# ---------------------------------------------------------------------------
# Per-router-version comparison (latest pass per version)
# ---------------------------------------------------------------------------


def per_version_summary(records: list[EvalRecord]) -> list[dict[str, Any]]:
    """One row per distinct router_version, taking the LATEST eval pass.

    Useful for a "which router version performed how" table — shows the
    classifier's progress across deployed snapshots.

    Returns sorted by ts ascending so the table reads as a timeline.
    """
    latest_by_version: dict[str, EvalRecord] = {}
    for r in records:
        v = r.get("router_version") or "?"
        prev = latest_by_version.get(v)
        if prev is None or (r.get("ts") or "") > (prev.get("ts") or ""):
            latest_by_version[v] = r
    rows: list[dict[str, Any]] = []
    for v, r in latest_by_version.items():
        rows.append({
            "router_version":   v,
            "ts":               r.get("ts"),
            "fast_head":        r.get("fast_head_version"),
            "overrides":        r.get("overrides_version"),
            "n_eval":           r.get("n_eval"),
            "mean_score":       r.get("mean_score"),
            "median_score":     r.get("median_score"),
            "pct_acceptable":   r.get("pct_acceptable"),
            "pct_failure":      r.get("pct_failure"),
            "elapsed_seconds":  r.get("elapsed_seconds"),
        })
    rows.sort(key=lambda x: x.get("ts") or "")
    return rows


# ---------------------------------------------------------------------------
# Top-card summary — latest pass + delta vs first pass
# ---------------------------------------------------------------------------


def eval_summary(records: list[EvalRecord]) -> dict[str, Any]:
    """Latest eval pass + headline numbers for the dashboard top-card.

    Returns:
      - n_passes: int
      - latest_score: float
      - latest_ts: ISO
      - latest_router_version: str
      - first_score: float
      - improvement_pp: float  (delta in percentage-points, * 100)
      - improvement_pct: float (relative %, e.g. +10.5%)
      - distinct_router_versions: int
      - total_held_out_routings: int (sum of n_eval across passes)
    """
    if not records:
        return {
            "n_passes": 0,
            "latest_score": 0.0,
            "latest_ts": None,
            "latest_router_version": None,
            "first_score": 0.0,
            "improvement_pp": 0.0,
            "improvement_pct": 0.0,
            "distinct_router_versions": 0,
            "total_held_out_routings": 0,
        }
    sorted_recs = sorted(records, key=lambda r: r.get("ts") or "")
    first = sorted_recs[0]
    latest = sorted_recs[-1]
    first_score = float(first.get("mean_score") or 0.0)
    latest_score = float(latest.get("mean_score") or 0.0)
    improvement_pp = latest_score - first_score
    improvement_pct = (
        100.0 * improvement_pp / first_score if first_score > 0 else 0.0
    )
    return {
        "n_passes":                 len(sorted_recs),
        "latest_score":             round(latest_score, 3),
        "latest_ts":                latest.get("ts"),
        "latest_router_version":    latest.get("router_version"),
        "first_score":              round(first_score, 3),
        "improvement_pp":           round(improvement_pp, 3),
        "improvement_pct":          round(improvement_pct, 2),
        "distinct_router_versions": len({r.get("router_version") for r in records if r.get("router_version")}),
        "total_held_out_routings":  sum(int(r.get("n_eval") or 0) for r in records),
    }
