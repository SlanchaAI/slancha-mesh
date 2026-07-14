"""Six-head signal classifier — prompt → `mesh.select.ClassifierSignals`.

Ported from slancha-local (`slancha_local.classifier.local.LocalClassifier`),
originally vendored from slancha-api `app/classifier/`. Perception only:
the port keeps the treelite heads and drops slancha-local's rule-based
target selector — `mesh.select.select_mesh_route` already ranks
specialists by (domain, difficulty) and porting a second selector would
have duplicated it.

Signals produced per prompt:

* domain (14 MMLU-Pro categories) → `select_mesh_route` normalizes to
  catalog domains (code / math / multilingual / reasoning / general).
* difficulty (easy / medium / hard) — drives tier lookup + fall-through.
* language (en / es / fr / de / zh / ja) — non-en steers multilingual.
* needs_tools — carried on the signals; the v0 selector doesn't rank on
  it yet.
* jailbreak / pii — reported on :class:`PromptSignals` for the decision
  trace. Policy mirrors slancha-local: surface, don't auto-reject (the
  v1 jailbreak head has known false positives; routing-time rejection
  belongs to the operator).

The optional cluster head (`mesh.classifier.cluster_head`, the 7th head)
overrides the domain signal when it fires: its sidecar cap ("coding" /
"math" / "general") maps to a catalog domain before selection runs.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Callable

import numpy as np

from mesh.classifier.cluster_head import ClusterHeadSelector
from mesh.select import ClassifierSignals

logger = logging.getLogger(__name__)

_ASSET_ROOT = Path(str(files("mesh.classifier.assets") / "classifier_v1"))

_HEAD_NAMES = ["domain", "jailbreak", "pii", "difficulty", "tool_calling", "language"]

#: Cluster-head sidecar ``cap`` → catalog domain override. The sidecar
#: vocabulary is shared with slancha-local ("coding" | "math" | "general");
#: the catalog uses `select_mesh_route`'s canonical short forms.
_CLUSTER_CAP_TO_DOMAIN = {"coding": "code", "math": "math", "general": "general"}


@dataclass(frozen=True)
class PromptSignals:
    """Everything the classifier learned about one prompt."""

    signals: ClassifierSignals  # what select_mesh_route consumes
    jailbreak: bool
    pii: bool
    classifier_ms: float
    cluster_hint: str | None = None  # reason string when the 7th head fired


class SignalClassifier:
    """Loads the 6 treelite heads + optional cluster head; classifies in-process.

    ``embed_fn`` is a seam: production defaults to the vendored ONNX
    embedder (lazy import so constructing with a fake keeps onnxruntime
    out of tests); tests inject a stub returning a fixed vector.
    """

    def __init__(
        self,
        asset_root: Path | None = None,
        *,
        cluster_head_selector: ClusterHeadSelector | None = None,
        embed_fn: Callable[[str], np.ndarray] | None = None,
    ) -> None:
        root = asset_root or _ASSET_ROOT
        with open(root / "labels.json") as f:
            self._labels = json.load(f)
        self._heads = self._load_heads(root)
        self._cluster_head_selector = cluster_head_selector
        self._embed = embed_fn

    def _load_heads(self, root: Path) -> dict[str, Any]:
        try:
            import treelite
        except ImportError as e:
            raise RuntimeError(
                "treelite not installed — auto-routing needs the classifier extra: "
                "pip install 'slancha-mesh[classifier]'"
            ) from e
        except OSError as e:
            # macOS treelite wheels link libomp via a build-machine rpath;
            # without brew's libomp the dylib fails to load.
            raise RuntimeError(
                "treelite failed to load its native library — on macOS run "
                "`brew install libomp`, then if it still fails: "
                "install_name_tool -add_rpath /opt/homebrew/opt/libomp/lib "
                f"<venv>/site-packages/treelite/lib/libtreelite.dylib ({e})"
            ) from e

        heads: dict[str, Any] = {}
        missing = []
        for name in _HEAD_NAMES:
            path = root / f"mmbert_tl_{name}.bin"
            if path.exists():
                heads[name] = treelite.Model.deserialize(str(path))
            else:
                missing.append(str(path))
        if missing:
            # All six heads are hard requirements — warn-and-continue here
            # would boot fine and then 500 every "auto" request instead.
            raise RuntimeError(f"classifier heads missing: {missing}")
        return heads

    @staticmethod
    def _predict_multiclass(model: Any, x: np.ndarray, labels: list[str]) -> tuple[str, float]:
        from treelite import gtil

        raw = gtil.predict(model, x).squeeze().flatten()
        if raw.ndim == 0:
            return labels[0], float(raw)
        probs = raw
        if probs.min() < 0 or probs.sum() < 0.5:
            exp = np.exp(probs - probs.max())
            probs = exp / exp.sum()
        idx = int(np.argmax(probs))
        return labels[idx], float(probs[idx])

    @staticmethod
    def _predict_binary(model: Any, x: np.ndarray) -> float:
        from treelite import gtil

        raw = gtil.predict(model, x).squeeze()
        return float(raw.flat[0]) if raw.size == 1 else float(raw.flat[-1])

    def warmup(self) -> None:
        """Pay the lazy ONNX-session cost now instead of on the first request."""
        self.signals_for("warmup")

    def signals_for(self, text: str) -> PromptSignals:
        """Classify one prompt. ~ms-scale on CPU after warmup."""
        if self._embed is None:
            from mesh.classifier.embedder import embed_single

            self._embed = embed_single
        # Amplification bound (#101 pattern): the tokenizer walks the FULL
        # text before truncating to 512 tokens — an 8MB prompt costs ~3s of
        # CPU untrimmed vs ~0.1s trimmed, with identical signals. 512
        # tokens never span 16k chars.
        text = text[:16384]
        t0 = time.perf_counter()
        x = np.asarray(self._embed(text), dtype=np.float32).reshape(1, -1)

        domain, _ = self._predict_multiclass(
            self._heads["domain"], x, self._labels["domain"]["labels"]
        )
        difficulty, _ = self._predict_multiclass(
            self._heads["difficulty"], x, self._labels["difficulty"]["labels"]
        )
        language, _ = self._predict_multiclass(
            self._heads["language"], x, self._labels["language"]["labels"]
        )
        jailbreak = self._predict_binary(self._heads["jailbreak"], x) >= 0.5
        pii = self._predict_binary(self._heads["pii"], x) >= 0.5
        needs_tools = self._predict_binary(self._heads["tool_calling"], x) >= 0.5

        domain, cluster_hint = self._apply_cluster_override(x, domain)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        return PromptSignals(
            signals=ClassifierSignals(
                domain=domain,
                difficulty=difficulty if difficulty in ("easy", "medium", "hard") else "medium",
                language=language,
                needs_tools=needs_tools,
            ),
            jailbreak=jailbreak,
            pii=pii,
            classifier_ms=elapsed_ms,
            cluster_hint=cluster_hint,
        )

    def _apply_cluster_override(self, x: np.ndarray, domain: str) -> tuple[str, str | None]:
        """7th head: a confident, mapped cluster hint overrides the domain.

        Safe by default — every "no" path (no selector, low confidence,
        unmapped cluster, unknown cap) returns the 6-head domain untouched.
        """
        selector = self._cluster_head_selector
        if selector is None:
            return domain, None
        hint = selector.predict(x)
        if hint is None:
            return domain, None
        override = _CLUSTER_CAP_TO_DOMAIN.get(hint.cap)
        if override is None:
            logger.warning(
                "cluster-head sidecar produced unknown cap=%r (known: %s); ignoring this hint",
                hint.cap,
                sorted(_CLUSTER_CAP_TO_DOMAIN),
            )
            return domain, None
        return override, hint.reason()
