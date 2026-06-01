"""Non-weight "harness" edits as gated challengers (issue #79).

The self-improving loop has two improvement levers:

  * **weights** — a LoRA adapter per cluster (`mesh.training`, #65), and
  * **the non-weight routing surface** — `PrefVector` defaults, a per-cluster
    system preamble, and a few tunable `SpecialistCard` fields.

[hexo-ai/sia](https://github.com/hexo-ai/sia)'s feedback-agent rewrites the
harness with **no gate** (its orchestrator hill-climbs and keeps every edit).
We do the opposite, and that is the whole point: a proposed config edit is a
**challenger** that must clear the *same* holdout gate (`mesh.eval.gate.decide`)
and ride the *same* pointer-rollback path (mirroring `ChampionRegistry`) as a
retrained adapter — or it is rejected and **never applied**. Promotion
semantics are identical; only the artifact differs (a config pointer instead
of an adapter pointer). See `docs/GATE-CONTRACT.md` invariant #5.

**Bounded by construction.** A proposal may only touch the whitelisted keys in
:data:`ALLOWED_FIELDS`, each value type/range-checked at construction. There is
no free-form code, prompt-exec, or arbitrary-key surface — the worst a bad
proposal can do is fail the gate. This is the line the issue draws against
copying SIA: not "rewrite the harness", but "propose a bounded diff and let the
gate decide".

**Human-gated.** The feedback step only *proposes*; :func:`record_proposal`
writes it for review. Applying a proposal is a separate, explicit call
(:func:`gate_config_proposal` → :func:`promote_if_accepted`), and only ever
after the gate accepts. Auto-promotion behind the gate is a later step.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from mesh.eval.gate import GateThresholds, PromotionVerdict, decide
from mesh.training import ImprovementRationale

# ─────────────────────────── the bounded edit surface ────────────────────────


@dataclass(frozen=True)
class FieldSpec:
    """Validation contract for one editable config key.

    `kind` is the Python type the value must be. `choices` (if set) restricts
    to a fixed set; `lo`/`hi` (if set) bound a numeric value inclusively;
    `max_len` (if set) bounds a string's length. A key absent from
    :data:`ALLOWED_FIELDS` is rejected outright — that is the not-free-form
    guarantee.
    """

    kind: type
    lo: float | None = None
    hi: float | None = None
    choices: tuple[Any, ...] | None = None
    max_len: int | None = None

    def validate(self, key: str, value: Any) -> None:
        if not isinstance(value, self.kind) or (
            self.kind is not bool and isinstance(value, bool)
        ):
            # `bool` is a subclass of `int`; reject a bool where a number is
            # expected (and vice-versa) so `quality_weight=True` can't sneak in.
            raise ConfigProposalError(
                f"{key!r}: expected {self.kind.__name__}, got "
                f"{type(value).__name__} ({value!r})"
            )
        if self.choices is not None and value not in self.choices:
            raise ConfigProposalError(
                f"{key!r}: {value!r} not in allowed choices {self.choices}"
            )
        if self.lo is not None and value < self.lo:
            raise ConfigProposalError(f"{key!r}: {value!r} below minimum {self.lo}")
        if self.hi is not None and value > self.hi:
            raise ConfigProposalError(f"{key!r}: {value!r} above maximum {self.hi}")
        if self.max_len is not None and len(value) > self.max_len:
            raise ConfigProposalError(
                f"{key!r}: length {len(value)} exceeds max_len {self.max_len}"
            )


# The complete non-weight surface a feedback proposal may touch. Dotted keys
# namespace the three sub-surfaces the issue calls out: routing prefs, the
# per-cluster preamble, and tunable catalog-card fields. Anything not listed
# here cannot be proposed — extend this dict (with a spec) to widen the surface.
ALLOWED_FIELDS: dict[str, FieldSpec] = {
    # PrefVector routing defaults (see mesh.select.PrefVector).
    "pref.quality_weight": FieldSpec(float, lo=0.0, hi=1.0),
    "pref.allow_fallbacks": FieldSpec(bool),
    "pref.require_streaming": FieldSpec(bool),
    # Per-cluster system preamble (prompt surface). Bounded length so a
    # proposal can't smuggle a megabyte of instructions past the gate.
    "prompt.system_preamble": FieldSpec(str, max_len=2000),
    # Tunable SpecialistCard field (see mesh.models.SpecialistCard). Hardware
    # facts (vram, layers, ...) are NOT editable — only the routing-policy tier.
    "card.coverage_tier": FieldSpec(int, choices=(1, 2, 3)),
}


class ConfigProposalError(ValueError):
    """A proposed diff touched a key outside :data:`ALLOWED_FIELDS` or carried
    an out-of-contract value. Raised at construction — a malformed proposal
    never exists as an object."""


# ─────────────────────────────── the proposal ────────────────────────────────


@dataclass(frozen=True)
class ConfigProposal:
    """A bounded, structured, non-weight challenger (issue #79).

    `target` keys the champion registry (a cluster/specialist id), exactly as
    an adapter promotion keys off its cluster. `diff` is the proposed change —
    a mapping of :data:`ALLOWED_FIELDS` keys to new values, validated on
    construction. `rationale` is the #80 human-readable WHY; `eval_delta` /
    `n_traces` / `source` are provenance for the audit trail.
    """

    target: str
    diff: dict[str, Any]
    rationale: ImprovementRationale | None = None
    eval_delta: float | None = None
    n_traces: int | None = None
    source: str = "feedback"

    def __post_init__(self) -> None:
        if not self.target:
            raise ConfigProposalError("proposal target must be non-empty")
        if not isinstance(self.diff, dict):
            raise ConfigProposalError("proposal diff must be a dict")
        for key, value in self.diff.items():
            spec = ALLOWED_FIELDS.get(key)
            if spec is None:
                raise ConfigProposalError(
                    f"{key!r} is not an editable field. Allowed: "
                    f"{sorted(ALLOWED_FIELDS)}"
                )
            spec.validate(key, value)

    def is_noop(self) -> bool:
        """An empty diff proposes nothing — the config analog of a stub
        artifact, and rejected by the gate the same way (#55 / invariant #4)."""
        return not self.diff

    def config_hash(self) -> str:
        """Content hash of (target + diff) — the config-pointer identity, the
        analog of an adapter checkpoint's `artifact_sha256`. Deterministic:
        keys are sorted so equal diffs hash equal regardless of insertion
        order."""
        payload = json.dumps(
            {"target": self.target, "diff": self.diff},
            sort_keys=True,
            ensure_ascii=False,
        )
        return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_row(self) -> dict[str, Any]:
        """JSON-serializable proposal record for the review store."""
        return {
            "target": self.target,
            "diff": dict(self.diff),
            "config_hash": self.config_hash(),
            "rationale": asdict(self.rationale) if self.rationale else None,
            "eval_delta": self.eval_delta,
            "n_traces": self.n_traces,
            "source": self.source,
        }


def apply_proposal(config: dict[str, Any], proposal: ConfigProposal) -> dict[str, Any]:
    """Return a NEW config dict with the proposal's diff applied.

    Pure — never mutates `config` (so a rejected proposal leaves the champion
    config untouched, and the caller can build the challenger config to eval
    without disturbing the live one). Only whitelisted keys are written,
    enforced again here as defense-in-depth (the proposal validated them at
    construction, but `apply_proposal` is also a public entry point)."""
    out = dict(config)
    for key, value in proposal.diff.items():
        if key not in ALLOWED_FIELDS:  # unreachable for a valid proposal
            raise ConfigProposalError(f"{key!r} is not an editable field")
        out[key] = value
    return out


# ───────────────────────────── the feedback step ─────────────────────────────


class ConfigProposer(Protocol):
    """A feedback step: (cluster context) → a bounded proposal or None.

    The reference implementation (:func:`propose_config_edit`) is rule-based
    and deterministic. An LLM-backed proposer can drop in behind this same
    interface — what matters for the contract is that it emits a
    :class:`ConfigProposal` (bounded) and that the gate, not the proposer,
    decides whether it ships."""

    def __call__(
        self,
        *,
        target: str,
        champion_row: dict[str, Any],
        current_config: dict[str, Any],
    ) -> ConfigProposal | None: ...


# Heuristic thresholds for the reference proposer. Deliberately conservative;
# the gate is the real guard, so these only decide *whether to bother asking*.
_FAILURE_RATE_TRIGGER = 0.10  # ≥10% failing outputs is worth a routing nudge
_HEALTHY_MEAN_TRIGGER = 3.5   # …but only if quality itself is still healthy
_QUALITY_WEIGHT_STEP = 0.1    # how far to back off quality_weight per proposal


def propose_config_edit(
    *,
    target: str,
    champion_row: dict[str, Any],
    current_config: dict[str, Any],
) -> ConfigProposal | None:
    """Reference (rule-based) feedback step.

    Reads the champion's eval row for `target` and proposes ONE bounded
    non-weight edit when a clear, honest signal is present; otherwise returns
    None (no proposal is the common case — never invent a change). The rules:

    1. **Failures look latency/dispatch-bound, not quality-bound.** If the
       failure rate is high while the mean score is healthy, the cluster is
       probably losing requests to slow/over-picky routing, not bad answers —
       so back `pref.quality_weight` off one step (route to faster/cheaper
       endpoints). Hypothesis the gate will then test on the holdout.
    2. **Dispatch failures with fallbacks disabled.** If any request failed to
       dispatch and `pref.allow_fallbacks` is off, propose turning it on.

    At most one edit per call (smaller diffs are easier for the gate to
    attribute and for a human to review).
    """
    mean = float(champion_row.get("mean_score") or 0.0)
    pct_failure = float(champion_row.get("pct_failure") or 0.0)
    n_dispatch_failures = int(champion_row.get("n_dispatch_failures") or 0)
    n_traces = int(champion_row.get("n_eval") or 0)

    # Rule 2 first: a hard dispatch failure with no fallback is the clearest fix.
    if n_dispatch_failures > 0 and not current_config.get("pref.allow_fallbacks"):
        return ConfigProposal(
            target=target,
            diff={"pref.allow_fallbacks": True},
            rationale=ImprovementRationale(
                hypothesis=(
                    f"{n_dispatch_failures} dispatch failure(s) on {target} with "
                    "fallbacks disabled — requests are being dropped, not "
                    "mis-answered."
                ),
                change_summary="enable pref.allow_fallbacks for this cluster",
                expected_effect=(
                    "fewer dropped requests (lower pct_failure) with no drop in "
                    "mean_score or any per-domain axis"
                ),
            ),
            eval_delta=None,
            n_traces=n_traces,
        )

    # Rule 1: latency-bound failures while quality is fine → route faster.
    if pct_failure >= _FAILURE_RATE_TRIGGER and mean >= _HEALTHY_MEAN_TRIGGER:
        current_qw = current_config.get("pref.quality_weight")
        base = 0.5 if not isinstance(current_qw, (int, float)) else float(current_qw)
        new_qw = round(max(0.0, base - _QUALITY_WEIGHT_STEP), 4)
        if new_qw == base:  # already at the floor — nothing to propose
            return None
        return ConfigProposal(
            target=target,
            diff={"pref.quality_weight": new_qw},
            rationale=ImprovementRationale(
                hypothesis=(
                    f"{target}: pct_failure={pct_failure:.0%} while mean={mean:.2f} "
                    "is healthy — failures look latency-bound, not quality-bound."
                ),
                change_summary=(
                    f"lower pref.quality_weight {base} → {new_qw} (route to "
                    "faster/cheaper endpoints)"
                ),
                expected_effect=(
                    "lower pct_failure with mean_score and per-domain axes held "
                    "within the gate's non-regression tolerance"
                ),
            ),
            eval_delta=None,
            n_traces=n_traces,
        )

    return None


# ──────────────────────── routing the proposal through the gate ───────────────

# Extra verdict reasons specific to the config plane (the gate's own reasons
# cover the rest, unchanged).
REJECT_NOOP = "config proposal is a no-op (empty diff) — nothing to promote"
REJECT_OUT_OF_SURFACE = "config proposal touches a non-editable field"


def gate_config_proposal(
    proposal: ConfigProposal,
    *,
    champion_row: dict[str, Any],
    challenger_row: dict[str, Any],
    thresholds: GateThresholds = GateThresholds(),
    gate_decide: Callable[..., PromotionVerdict] = decide,
) -> PromotionVerdict:
    """Decide a config proposal through the SAME gate as an adapter.

    `champion_row` and `challenger_row` are eval rows over the curated holdout —
    the challenger row is produced by applying the proposal's config and running
    the holdout eval, exactly as an adapter's challenger row is produced by
    evaluating the retrained adapter. This function does NOT run the eval (that
    is the runner's job, identical for both artifact types); it gates the rows.

    Before delegating, it stamps the proposal's #80 rationale onto the
    challenger row so the resulting `PromotionVerdict.rationale` explains the
    config change in plain language — same audit surface as a retrained adapter.

    A no-op proposal (empty diff) is rejected here without consulting the gate,
    the config analog of stub-rejection (#55 / invariant #4): a degenerate
    artifact can never promote.
    """
    if proposal.is_noop():
        return PromotionVerdict(
            accept=False,
            reject_reasons=(REJECT_NOOP,),
            mean_delta=0.0,
            rationale=asdict(proposal.rationale) if proposal.rationale else None,
        )
    # Carry the proposal's provenance onto the challenger row so the gate's
    # verdict is self-explaining (mirrors how an adapter row carries its meta).
    challenger_row = dict(challenger_row)
    if proposal.rationale is not None:
        challenger_row.setdefault("rationale", asdict(proposal.rationale))
    challenger_row.setdefault("artifact_sha256", proposal.config_hash())
    return gate_decide(champion_row, challenger_row, thresholds)


# ───────────────────── config-pointer champion registry ──────────────────────


class ConfigChampionRegistry:
    """Tracks the current champion config + supports rollback (issue #79).

    The exact analog of `mesh.training.ChampionRegistry`, but the artifact is a
    small config dict rather than an adapter directory, so the config is stored
    inline in the pointer file (no separate weights to point at):

      registry_dir/
        config_champion.json       → {"config": {...}, "config_hash": ...,
                                       "target": ..., "promoted_at": ...}
        config_champion.prev.json  → snapshot of the prior champion (rollback)

    Promotion swaps the pointer; rollback restores the prior — the same cheap,
    atomic "drop the pointer to revert" property the adapter path relies on.
    """

    def __init__(self, registry_dir: Path) -> None:
        self.registry_dir = Path(registry_dir)
        self.registry_dir.mkdir(parents=True, exist_ok=True)
        self._current = self.registry_dir / "config_champion.json"
        self._prev = self.registry_dir / "config_champion.prev.json"

    def current(self) -> dict[str, Any] | None:
        """The current champion config dict, or None if unset."""
        if not self._current.exists():
            return None
        return json.loads(self._current.read_text())["config"]

    def previous(self) -> dict[str, Any] | None:
        """The prior champion config dict, or None if none kept."""
        if not self._prev.exists():
            return None
        return json.loads(self._prev.read_text())["config"]

    def _write_pointer(
        self, path: Path, config: dict[str, Any], target: str, config_hash: str
    ) -> None:
        path.write_text(
            json.dumps(
                {
                    "config": config,
                    "config_hash": config_hash,
                    "target": target,
                    "promoted_at": datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
                ensure_ascii=False,
            )
        )

    def promote(self, config: dict[str, Any], *, target: str) -> dict[str, Any]:
        """Make `config` the champion; keep the prior for rollback."""
        config_hash = "sha256:" + hashlib.sha256(
            json.dumps({"target": target, "config": config}, sort_keys=True).encode()
        ).hexdigest()
        if self._current.exists():
            self._prev.write_text(self._current.read_text())
        elif self._prev.exists():
            # No current champion to back up → clear stale prev so a rollback
            # after a first-ever promote is a clean no-op (mirrors ChampionRegistry).
            self._prev.unlink()
        self._write_pointer(self._current, config, target, config_hash)
        return config

    def rollback(self) -> dict[str, Any] | None:
        """Restore the prior champion config. Returns it, or None.

        With no prior (first-ever promote just happened) this drops the current
        pointer and returns None — the routing layer falls back to its built-in
        defaults, the config analog of adapters-as-pointers → base."""
        if self._prev.exists():
            self._current.write_text(self._prev.read_text())
            self._prev.unlink()
            return self.current()
        if self._current.exists():
            self._current.unlink()
        return None


def promote_if_accepted(
    proposal: ConfigProposal,
    verdict: PromotionVerdict,
    registry: ConfigChampionRegistry,
    *,
    current_config: dict[str, Any],
) -> dict[str, Any]:
    """Apply a proposal IFF the gate accepted it; otherwise leave the champion.

    This is the only path that mutates the live config, and it never mutates on
    a reject — the "never applied ungated" guarantee. On accept, the proposal's
    diff is applied to `current_config` and promoted (prior kept for rollback);
    on reject, `current_config` is returned unchanged (champion stays). Returns
    the now-active config either way."""
    if not verdict.accept:
        return current_config
    new_config = apply_proposal(current_config, proposal)
    return registry.promote(new_config, target=proposal.target)


# ─────────────────────────── human-gated proposal store ──────────────────────


def record_proposal(output: Path, proposal: ConfigProposal) -> None:
    """Append a proposal to the review store (default convention:
    `dashboard/config_proposals.jsonl`) with status "proposed".

    Append-only and apply-free: writing a proposal here NEVER applies it. A
    human (or, later, an auto-promoter gated on the holdout) reads this log,
    and application goes through :func:`gate_config_proposal` →
    :func:`promote_if_accepted`. This is the human-gated posture the issue
    asks for."""
    output.parent.mkdir(parents=True, exist_ok=True)
    row = {**proposal.to_row(), "status": "proposed"}
    with output.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


__all__ = [
    "ALLOWED_FIELDS",
    "ConfigChampionRegistry",
    "ConfigProposal",
    "ConfigProposalError",
    "ConfigProposer",
    "FieldSpec",
    "REJECT_NOOP",
    "apply_proposal",
    "gate_config_proposal",
    "promote_if_accepted",
    "propose_config_edit",
    "record_proposal",
]
