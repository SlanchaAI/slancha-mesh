"""Mesh router — spec §6.

Extends slancha-api `select_model_lmarena()` with `(specialist, node_url)`
selection. v0.0.1 implementation is standalone (doesn't import from
slancha-api directly so this package can be used in isolation), but
mirrors the pareto-mode logic: rank by composite score, prefer
mesh-local before cloud fallback.

Inputs:  classifier signals (domain, difficulty, language, needs_tools)
Output:  MeshSelectionResult with chosen node_url + fallback chain.

If no mesh route matches: returns a MeshSelectionResult with
`node_id=None, cluster_coverage_used=False, reason="cloud fallback ..."`
so the caller knows to defer to slancha-cloud.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from mesh.models import (
    MeshSelectionResult,
    ModelId,
    NodeId,
    RegistrySnapshot,
    Route,
    SpecialistId,
)
from mesh.registry import build_ranked_routes

# Queue budget per spec §6 — drop routes with > this much queue time
# unless the caller explicitly relaxes it (e.g., batch class).
DEFAULT_MAX_QUEUE_MS = 2000

# p95 budget for hot-interactive routes (per spec §6 latency budget table).
HOT_INTERACTIVE_P95_MS = 1500


@dataclass(frozen=True)
class ClassifierSignals:
    """The signals the slancha-api classifier produces. Mirrors the
    fields `select_model_lmarena` consumes; we keep it self-contained
    so the mesh package doesn't depend on slancha-api importable."""

    domain: str
    difficulty: str  # "easy" | "medium" | "hard"
    language: str = "en"
    needs_tools: bool = False
    route_class: str = "standard"  # "hot_interactive" | "standard" | "batch"


def _route_score(route: Route, route_class: str) -> float:
    """Composite quality score. Lower queue + lower p95 = better.

    For hot-interactive, p95 dominates; for batch, we mostly care about
    queue depth. Tunable; current weights are a starting point and will
    be re-tuned once we have live mesh traffic in Langfuse.
    """
    queue_pen = route.estimated_queue_ms / 1000.0  # seconds
    p95 = route.p95_latency_ms or 500.0
    if route_class == "hot_interactive":
        return -(queue_pen * 2.0 + p95 / 100.0)
    if route_class == "batch":
        return -(queue_pen * 0.5 + p95 / 500.0)
    # standard
    return -(queue_pen + p95 / 250.0)


def _domain_for_signals(signals: ClassifierSignals) -> str:
    """Map classifier domain → catalog domain.

    The classifier emits LMArena-aligned categories ("math", "code",
    "computer science", ...); the catalog uses canonical short forms.
    We normalize a few common synonyms here so the mesh side doesn't
    need a perfect mirror of the classifier's taxonomy.
    """
    d = signals.domain.lower().strip()
    if d in {"code", "computer science", "engineering", "programming", "coding"}:
        return "code"
    if d in {"math", "physics", "chemistry", "mathematics"}:
        return "math"
    if d in {"multilingual"} or signals.language not in ("en", ""):
        return "multilingual"
    if d in {"reasoning", "analysis"}:
        return "reasoning"
    return "general"


def _filter_routes(
    routes: list[Route],
    route_class: str,
    max_queue_ms: int,
) -> list[Route]:
    """Apply per-spec §6 health + queue + p95 budget filters."""
    out: list[Route] = []
    for r in routes:
        if r.estimated_queue_ms > max_queue_ms:
            continue
        if route_class == "hot_interactive":
            if r.p95_latency_ms is not None and r.p95_latency_ms > HOT_INTERACTIVE_P95_MS:
                continue
        out.append(r)
    return out


def select_mesh_route(
    signals: ClassifierSignals,
    registry_snapshot: RegistrySnapshot,
    max_queue_ms: int = DEFAULT_MAX_QUEUE_MS,
    cloud_fallback_model: str = "claude-sonnet-4-7",
) -> MeshSelectionResult:
    """Pick (specialist, node) for this request, or fall through to cloud.

    Flow (spec §6):
      1. Map classifier signals → catalog domain.
      2. Lookup `(domain, difficulty)` candidates from snapshot.
      3. Filter unhealthy / over-queue / over-p95.
      4. Rank by `_route_score` (route-class aware).
      5. Top = primary; rest = fallback chain.
      6. None survive → cloud fallback.

    Snapshot's `ranked_routes` may already be populated; if empty we
    build on the fly so callers that pass raw snapshots still work.
    """
    domain = _domain_for_signals(signals)
    difficulty = signals.difficulty.lower().strip()
    key = f"{domain}|{difficulty}"

    ranked = registry_snapshot.ranked_routes
    if not ranked:
        ranked = build_ranked_routes(registry_snapshot)

    candidates = list(ranked.get(key, []))

    # Difficulty fall-through: if no routes at this tier, try harder tiers
    # (a specialist that handles 'hard' can usually handle 'medium').
    if not candidates and difficulty == "easy":
        candidates = list(ranked.get(f"{domain}|medium", []))
    if not candidates and difficulty in ("easy", "medium"):
        candidates = list(ranked.get(f"{domain}|hard", []))

    # Domain fall-through: try `general` if domain-specific had nothing.
    if not candidates and domain != "general":
        candidates = list(ranked.get(f"general|{difficulty}", []))

    filtered = _filter_routes(candidates, signals.route_class, max_queue_ms)
    if not filtered:
        return MeshSelectionResult(
            model=cloud_fallback_model,
            specialist_id=None,
            node_id=None,
            node_url=None,
            reason=(
                f"no mesh route for {domain}/{difficulty}/{signals.language} "
                f"(candidates={len(candidates)} filtered_out={len(candidates) - len(filtered)}); "
                f"falling through to cloud"
            ),
            queue_ms_estimated=0,
            cluster_coverage_used=False,
            fallback_chain=[(cloud_fallback_model, None)],
        )

    ranked_filtered = sorted(filtered, key=lambda r: -_route_score(r, signals.route_class))
    primary = ranked_filtered[0]
    fallback_chain: list[tuple[ModelId, NodeId | None]] = [
        (_model_id_for(r, registry_snapshot), r.node_id) for r in ranked_filtered[1:]
    ]
    # Always end with cloud
    fallback_chain.append((cloud_fallback_model, None))

    return MeshSelectionResult(
        model=_model_id_for(primary, registry_snapshot),
        specialist_id=primary.specialist_id,
        node_id=primary.node_id,
        node_url=primary.node_url,
        reason=(
            f"mesh: {primary.specialist_id} @ {primary.node_id} "
            f"queue={primary.estimated_queue_ms}ms "
            f"p95={primary.p95_latency_ms}ms route_class={signals.route_class}"
        ),
        queue_ms_estimated=primary.estimated_queue_ms,
        cluster_coverage_used=True,
        fallback_chain=fallback_chain,
    )


def _model_id_for(route: Route, snap: RegistrySnapshot) -> ModelId:
    """Resolve specialist_id → upstream model_id via catalog."""
    card = snap.catalog.get(route.specialist_id)
    return card.model_id if card else route.specialist_id


__all__ = [
    "ClassifierSignals",
    "DEFAULT_MAX_QUEUE_MS",
    "HOT_INTERACTIVE_P95_MS",
    "select_mesh_route",
]


# Silence unused-import lint of math (kept for parity with slancha-api
# _pareto_score which uses log2; if we add pareto-mode cost weighting
# in v0.0.2 we'll need it).
_ = math
