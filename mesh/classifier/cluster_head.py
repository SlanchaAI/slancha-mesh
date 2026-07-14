"""Cluster-head selector — the 7th-head READ path. Ported from slancha-local.

A trained cluster head (`mmbert_tl_cluster.bin` + its
``cluster_id_to_route.json`` sidecar, produced by the offline
embed → HDBSCAN → promote pipeline) emits a **confidence-gated** route
hint. `mesh.classifier.heads.SignalClassifier` consumes the hint as a
domain override before `mesh.select.select_mesh_route` runs.

Three guardrails (unchanged from the slancha-local port source):

1. **SAFE BY DEFAULT** — no artifact directory configured, or anything
   in it fails to load ⇒ :func:`load_from_dir` returns ``None`` and the
   selector is fully inert. The 6-head classifier behaves exactly as
   without a cluster head.

2. **CONFIDENCE GATED** — ``predict()`` returns ``None`` whenever the
   head's top-class probability is below
   ``SLANCHA_CLUSTER_HEAD_CONF_THRESHOLD`` (default ``0.7``, env-tunable).
   Under-firing is safe (falls through to the 6 heads); over-firing
   could route a prompt to the wrong capability.

3. **CLUSTER→CAP MAPPING** — the head emits an integer cluster id,
   meaningless without a mapping to a capability. The mapping is a JSON
   sidecar (:data:`SIDECAR_FILENAME`) co-located with the ``.bin`` in
   the SAME version directory, so a rollback restores the head and its
   matching mapping together.

Sidecar schema (``cluster_id_to_route.json`` v1)::

    {
      "schema_version": "v1",
      "routes": {
        "<cluster_id_int_as_str>": "<cap>",
        ...
      }
    }

``<cap>`` is one of ``"coding"``, ``"math"``, ``"general"`` (the sidecar
vocabulary shared with slancha-local). Any non-``v1`` schema makes the
selector inert (logged at WARNING) rather than crash.

Port note: slancha-local resolves the ACTIVE artifact through its
pointer-store; the mesh has no pointer-store, so :func:`load_from_dir`
takes the version directory directly (wired from
``SLANCHA_CLUSTER_HEAD_DIR`` in `mesh.classifier.auto`).

Trust: ``treelite.Model.deserialize`` parses the ``.bin`` in native
code — point ``SLANCHA_CLUSTER_HEAD_DIR`` only at artifacts you
produced or verified.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np

logger = logging.getLogger(__name__)

#: Sidecar JSON filename. Lives in the SAME version directory as the
#: cluster-head ``.bin`` so both resolve atomically.
SIDECAR_FILENAME = "cluster_id_to_route.json"

#: Cluster-head artifact filename inside the version directory.
HEAD_FILENAME = "mmbert_tl_cluster.bin"

#: Sidecar schema version this module understands.
SCHEMA_VERSION = "v1"

#: Default confidence threshold below which ``predict()`` returns ``None``.
#: Override via the ``SLANCHA_CLUSTER_HEAD_CONF_THRESHOLD`` env var.
DEFAULT_CONFIDENCE_THRESHOLD = 0.7

_ENV_THRESHOLD = "SLANCHA_CLUSTER_HEAD_CONF_THRESHOLD"


class ClusterHead(Protocol):
    """Minimum surface a cluster-head implementation must satisfy.

    Implementations return ``(cluster_id, confidence)`` for a single
    embedding row. ``confidence`` is the top-class probability in
    ``[0, 1]``; ``cluster_id`` is the argmax class index. Tests can
    inject a fake :class:`ClusterHead` to exercise the selector without
    the optional ``treelite`` dep.
    """

    def predict(self, x: np.ndarray) -> tuple[int, float]:  # pragma: no cover - protocol
        ...


@dataclass(frozen=True)
class ClusterRouteHint:
    """A confidence-gated, mapping-resolved routing hint.

    Emitted only when the cluster head exceeded the threshold AND the
    sidecar mapping had an entry for the predicted cluster id.
    """

    cluster_id: int
    cap: str
    confidence: float
    head_version: str

    def reason(self) -> str:
        """Human-readable reason string for the decision trace."""
        v = self.head_version or "unknown"
        return f"cluster-head v={v} cid={self.cluster_id} conf={self.confidence:.2f} → cap={self.cap}"


def _resolve_threshold() -> float:
    """Read the active confidence threshold from the env, falling back to
    :data:`DEFAULT_CONFIDENCE_THRESHOLD` on missing/invalid values.

    Invalid values are logged at WARNING so an operator typo doesn't
    silently disable the gate.
    """
    raw = os.environ.get(_ENV_THRESHOLD)
    if raw is None:
        return DEFAULT_CONFIDENCE_THRESHOLD
    try:
        v = float(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not a float; using default %.2f",
            _ENV_THRESHOLD,
            raw,
            DEFAULT_CONFIDENCE_THRESHOLD,
        )
        return DEFAULT_CONFIDENCE_THRESHOLD
    if not (0.0 <= v <= 1.0):
        logger.warning(
            "%s=%r is out of [0,1]; using default %.2f",
            _ENV_THRESHOLD,
            raw,
            DEFAULT_CONFIDENCE_THRESHOLD,
        )
        return DEFAULT_CONFIDENCE_THRESHOLD
    return v


class ClusterHeadSelector:
    """Confidence-gated wrapper around a :class:`ClusterHead` + mapping.

    The selector itself is dependency-free; the ``treelite``-backed
    :class:`ClusterHead` impl is constructed in :func:`load_from_dir`
    via the optional ``[classifier]`` extra. Tests can build a selector
    directly with a fake head.
    """

    def __init__(
        self,
        head: ClusterHead,
        mapping: dict[int, str],
        *,
        head_version: str = "",
    ) -> None:
        self._head = head
        self._mapping = dict(mapping)
        self._head_version = head_version

    @property
    def head_version(self) -> str:
        return self._head_version

    @property
    def mapping(self) -> dict[int, str]:
        return dict(self._mapping)

    def predict(self, x: np.ndarray) -> ClusterRouteHint | None:
        """Return a route hint, or ``None`` if the head is uncertain or
        the predicted cluster has no mapping entry.

        ``None`` always means "fall through to the 6-head signals";
        the caller never sees a hint it should override on.
        """
        threshold = _resolve_threshold()
        try:
            cid, conf = self._head.predict(x)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("cluster-head predict() raised %s; falling through", e)
            return None
        if conf < threshold:
            return None
        cap = self._mapping.get(int(cid))
        if cap is None:
            # Predicted cluster has no mapping entry — could be a freshly
            # spawned cluster the promote pipeline hasn't catalogued yet.
            # Fall through silently (expected during the warm-up window
            # between cluster appearance and the next promotion).
            return None
        return ClusterRouteHint(
            cluster_id=int(cid),
            cap=str(cap),
            confidence=float(conf),
            head_version=self._head_version,
        )


# ---------------------------------------------------------------------------
# Artifact loading
# ---------------------------------------------------------------------------


class _ClusterHeadLoadError(RuntimeError):
    """Internal: sidecar/artifact failed to load. The selector is always
    inert when this fires — the cluster head must never crash the
    classifier on startup."""


def _load_mapping(sidecar_path: Path) -> dict[int, str]:
    """Parse ``cluster_id_to_route.json``. Raises :class:`_ClusterHeadLoadError`
    on any structural problem; the caller logs at WARNING and falls back
    to an inert selector."""
    try:
        raw = sidecar_path.read_text(encoding="utf-8")
    except OSError as e:
        raise _ClusterHeadLoadError(f"sidecar read failed: {e}") from e
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise _ClusterHeadLoadError(f"sidecar invalid JSON: {e}") from e
    if not isinstance(data, dict):
        raise _ClusterHeadLoadError(f"sidecar root must be object, got {type(data).__name__}")
    schema = data.get("schema_version")
    if schema != SCHEMA_VERSION:
        raise _ClusterHeadLoadError(f"sidecar schema_version={schema!r} (want {SCHEMA_VERSION!r})")
    routes = data.get("routes")
    if not isinstance(routes, dict):
        raise _ClusterHeadLoadError(f"sidecar 'routes' must be object, got {type(routes).__name__}")
    mapping: dict[int, str] = {}
    for k, v in routes.items():
        try:
            cid = int(k)
        except (TypeError, ValueError) as e:
            raise _ClusterHeadLoadError(f"sidecar route key {k!r} not int-coercible: {e}") from e
        if not isinstance(v, str) or not v:
            raise _ClusterHeadLoadError(
                f"sidecar route value for cid={cid} must be non-empty str, got {v!r}"
            )
        mapping[cid] = v
    return mapping


class _TreeliteClusterHead:
    """Treelite-backed :class:`ClusterHead`. Loaded lazily so the import
    cost is only paid by callers who actually invoke the cluster head."""

    def __init__(self, path: Path) -> None:
        import treelite  # local import — optional dep

        self._model: Any = treelite.Model.deserialize(str(path))

    def predict(self, x: np.ndarray) -> tuple[int, float]:
        from treelite import gtil  # local import — optional dep

        raw = gtil.predict(self._model, x).squeeze().flatten()
        # Mirror SignalClassifier._predict_multiclass: if the output looks
        # like raw scores (negative or sums < 0.5), softmax it. Otherwise
        # assume it's already a probability distribution.
        probs = raw
        if probs.min() < 0 or probs.sum() < 0.5:
            exp = np.exp(probs - probs.max())
            probs = exp / exp.sum()
        idx = int(np.argmax(probs))
        return idx, float(probs[idx])


def load_from_dir(version_dir: str | Path | None) -> ClusterHeadSelector | None:
    """Load a cluster head + sidecar from a version directory, or return
    ``None`` for "no selector" (inert behavior).

    Returns ``None`` (caller runs 6-head-only) when:

    * ``version_dir`` is ``None``/empty — nothing configured, the default.
    * The ``.bin`` is missing, the sidecar JSON is missing, malformed,
      schema-mismatched, or doesn't parse to ``{int: str}`` (logged WARNING).
    * The treelite import or model deserialization fails (logged WARNING).

    Never raises — cluster-head failures must NEVER block the classifier
    from starting up.
    """
    if not version_dir:
        return None
    root = Path(version_dir)
    bin_path = root / HEAD_FILENAME
    if not bin_path.exists():
        logger.warning("cluster-head selector inert: %s not found", bin_path)
        return None

    sidecar_path = root / SIDECAR_FILENAME
    try:
        mapping = _load_mapping(sidecar_path)
    except _ClusterHeadLoadError as e:
        logger.warning(
            "cluster-head selector inert: %s (sidecar=%s); 6-head signals will handle all prompts",
            e,
            sidecar_path,
        )
        return None

    try:
        head: ClusterHead = _TreeliteClusterHead(bin_path)
    except ImportError as e:
        logger.warning(
            "cluster-head selector inert: treelite not installed (%s); "
            "install slancha-mesh[classifier] to activate the 7th head",
            e,
        )
        return None
    except Exception as e:
        logger.warning(
            "cluster-head selector inert: treelite failed to load %s: %s",
            bin_path,
            e,
        )
        return None

    head_version = root.name
    return ClusterHeadSelector(head, mapping, head_version=head_version)
