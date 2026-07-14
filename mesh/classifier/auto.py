"""AutoRouter — resolves `model: "auto"` to a specialist via the classifier.

The router hands this the request body + current snapshot; it extracts
the prompt, classifies it (`mesh.classifier.heads`), and runs the
existing selector (`mesh.select.select_mesh_route`). The result carries
the picked specialist plus a human-readable reason for the decision
trace.

Env:

* ``SLANCHA_CLUSTER_HEAD_DIR`` — version directory holding
  ``mmbert_tl_cluster.bin`` + ``cluster_id_to_route.json`` to activate
  the 7th head. Unset (the default) → 6-head signals only.
* ``SLANCHA_CLUSTER_HEAD_CONF_THRESHOLD`` — see
  `mesh.classifier.cluster_head`.
"""

from __future__ import annotations

import logging
import os

from mesh.models import MeshSelectionResult, RegistrySnapshot
from mesh.select import DEFAULT_MAX_QUEUE_MS, ClassifierSignals, select_mesh_route

logger = logging.getLogger(__name__)

_ENV_CLUSTER_HEAD_DIR = "SLANCHA_CLUSTER_HEAD_DIR"


def _content_text(content: object) -> str:
    """Text of one OpenAI message `content` (plain string or parts list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(p.get("text", ""))
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return ""


def prompt_text(body: dict) -> str:
    """The text to classify: the last user message in the request.

    The classifier heads were trained on user prompts, so the latest
    user turn — not the whole transcript — is the signal.
    """
    messages = body.get("messages")
    if not isinstance(messages, list):
        return ""
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            return _content_text(m.get("content"))
    return ""


class AutoRouter:
    """Classifier-backed `model:"auto"` resolver.

    `select()` is synchronous CPU work (embed + 6-7 treelite heads,
    ms-scale after warmup); the router calls it via a threadpool so the
    event loop never blocks on it.
    """

    def __init__(self, classifier, *, max_queue_ms: int = DEFAULT_MAX_QUEUE_MS) -> None:
        self._classifier = classifier
        self._max_queue_ms = max_queue_ms

    def warmup(self) -> None:
        self._classifier.warmup()

    def select(self, body: dict, snapshot: RegistrySnapshot) -> MeshSelectionResult:
        text = prompt_text(body)
        if not text:
            # No user text (image-only parts, system-only transcript):
            # classifying "" yields an arbitrary head vote — route
            # deterministically to the general/medium pool instead.
            signals = ClassifierSignals(domain="general", difficulty="medium")
            result = select_mesh_route(signals, snapshot, self._max_queue_ms)
            logger.info("[auto] empty prompt → general/medium → %s",
                        result.specialist_id or result.reason)
            return result
        ps = self._classifier.signals_for(text)
        result = select_mesh_route(ps.signals, snapshot, self._max_queue_ms)
        logger.info(
            "[auto] domain=%s difficulty=%s lang=%s tools=%s jailbreak=%s pii=%s "
            "cluster=%s (%.1fms) → %s",
            ps.signals.domain,
            ps.signals.difficulty,
            ps.signals.language,
            ps.signals.needs_tools,
            ps.jailbreak,
            ps.pii,
            ps.cluster_hint or "-",
            ps.classifier_ms,
            result.specialist_id or result.reason,
        )
        return result


def build_auto_router() -> AutoRouter:
    """Construct the production AutoRouter, failing loud when the
    `classifier` extra is missing.

    Activates the cluster head iff ``SLANCHA_CLUSTER_HEAD_DIR`` points at
    a loadable artifact (safe-by-default otherwise).
    """
    try:
        from mesh.classifier.heads import SignalClassifier
    except ImportError as e:
        raise RuntimeError(
            "auto-routing needs the classifier extra: pip install 'slancha-mesh[classifier]'"
        ) from e

    selector = None
    head_dir = os.environ.get(_ENV_CLUSTER_HEAD_DIR)
    if head_dir:
        from mesh.classifier.cluster_head import load_from_dir

        selector = load_from_dir(head_dir)
        if selector is not None:
            logger.info("[auto] cluster head active: version=%s", selector.head_version)

    return AutoRouter(SignalClassifier(cluster_head_selector=selector))
