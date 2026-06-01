"""Tests for mesh.feedback (issue #79 — non-weight edits as gated challengers).

The contract under test: a bounded config proposal can be PROPOSED, routed
through the SAME gate as an adapter, and promoted-or-rejected through a
pointer-rollback registry — never applied ungated.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mesh.feedback import (
    ALLOWED_FIELDS,
    REJECT_NOOP,
    ConfigChampionRegistry,
    ConfigProposal,
    ConfigProposalError,
    apply_proposal,
    gate_config_proposal,
    promote_if_accepted,
    propose_config_edit,
    record_proposal,
)
from mesh.training import ImprovementRationale


def _row(
    mean_score: float,
    per_domain: dict[str, float] | None = None,
    *,
    n_eval: int = 500,
    judge_model: str = "qwen3-coder-30b",
    pct_failure: float = 0.0,
    n_dispatch_failures: int = 0,
) -> dict:
    return {
        "router_version": "v",
        "n_eval": n_eval,
        "judge_model": judge_model,
        "mean_score": mean_score,
        "per_domain_mean": per_domain or {},
        "pct_failure": pct_failure,
        "n_dispatch_failures": n_dispatch_failures,
    }


# ───────────────────────── bounded edit surface ──────────────────────────────


def test_proposal_rejects_field_outside_surface():
    """A diff key not in ALLOWED_FIELDS is rejected at construction — the
    not-free-form guarantee."""
    with pytest.raises(ConfigProposalError, match="not an editable field"):
        ConfigProposal(target="c_0427", diff={"pref.arbitrary_code": "rm -rf /"})


def test_proposal_rejects_out_of_range_value():
    with pytest.raises(ConfigProposalError, match="above maximum"):
        ConfigProposal(target="c_0427", diff={"pref.quality_weight": 1.5})


def test_proposal_rejects_bool_where_float_expected():
    """bool is a subclass of int; quality_weight=True must not slip through."""
    with pytest.raises(ConfigProposalError, match="expected float"):
        ConfigProposal(target="c_0427", diff={"pref.quality_weight": True})


def test_proposal_rejects_out_of_choices():
    with pytest.raises(ConfigProposalError, match="not in allowed choices"):
        ConfigProposal(target="c_0427", diff={"card.coverage_tier": 4})


def test_proposal_rejects_overlong_preamble():
    with pytest.raises(ConfigProposalError, match="exceeds max_len"):
        ConfigProposal(
            target="c_0427",
            diff={"prompt.system_preamble": "x" * 2001},
        )


def test_proposal_accepts_valid_diff():
    p = ConfigProposal(
        target="c_0427",
        diff={"pref.quality_weight": 0.4, "card.coverage_tier": 2},
    )
    assert p.diff["pref.quality_weight"] == 0.4
    assert set(p.diff) <= set(ALLOWED_FIELDS)


# ─────────────────────────── proposal mechanics ──────────────────────────────


def test_config_hash_deterministic_and_order_invariant():
    a = ConfigProposal(target="t", diff={"pref.quality_weight": 0.4, "card.coverage_tier": 2})
    b = ConfigProposal(target="t", diff={"card.coverage_tier": 2, "pref.quality_weight": 0.4})
    assert a.config_hash() == b.config_hash()
    c = ConfigProposal(target="t", diff={"pref.quality_weight": 0.3})
    assert c.config_hash() != a.config_hash()


def test_is_noop_for_empty_diff():
    assert ConfigProposal(target="t", diff={}).is_noop()
    assert not ConfigProposal(target="t", diff={"pref.allow_fallbacks": True}).is_noop()


def test_apply_proposal_is_pure():
    cfg = {"pref.quality_weight": 0.5}
    p = ConfigProposal(target="t", diff={"pref.quality_weight": 0.4})
    out = apply_proposal(cfg, p)
    assert out["pref.quality_weight"] == 0.4
    assert cfg["pref.quality_weight"] == 0.5  # input untouched


def test_to_row_carries_rationale_and_provenance():
    p = ConfigProposal(
        target="c_0427",
        diff={"pref.allow_fallbacks": True},
        rationale=ImprovementRationale("h", "c", "e"),
        n_traces=480,
    )
    row = p.to_row()
    assert row["config_hash"].startswith("sha256:")
    assert row["rationale"] == {"hypothesis": "h", "change_summary": "c", "expected_effect": "e"}
    assert row["n_traces"] == 480


# ───────────────────────── the reference feedback step ───────────────────────


def test_proposer_enables_fallbacks_on_dispatch_failure():
    row = _row(4.2, {"code": 4.2}, n_dispatch_failures=3)
    p = propose_config_edit(target="c_0427", champion_row=row, current_config={})
    assert p is not None
    assert p.diff == {"pref.allow_fallbacks": True}
    assert p.rationale is not None and "dispatch" in p.rationale.hypothesis


def test_proposer_lowers_quality_weight_on_latency_bound_failures():
    row = _row(4.0, {"code": 4.0}, pct_failure=0.2)
    p = propose_config_edit(
        target="c_0427", champion_row=row, current_config={"pref.quality_weight": 0.6}
    )
    assert p is not None
    assert p.diff == {"pref.quality_weight": 0.5}


def test_proposer_returns_none_when_healthy():
    """No clear signal → no proposal. Never invent a change."""
    row = _row(4.5, {"code": 4.5}, pct_failure=0.0)
    assert propose_config_edit(target="c_0427", champion_row=row, current_config={}) is None


def test_proposer_returns_none_at_quality_weight_floor():
    row = _row(4.0, {"code": 4.0}, pct_failure=0.2)
    p = propose_config_edit(
        target="c", champion_row=row, current_config={"pref.quality_weight": 0.0}
    )
    assert p is None


# ─────────────────────── routing through the SAME gate ───────────────────────


def test_gate_rejects_noop_proposal_without_consulting_gate():
    """Empty diff → REJECT_NOOP (config analog of stub-rejection)."""
    p = ConfigProposal(target="t", diff={})
    v = gate_config_proposal(p, champion_row=_row(3.5), challenger_row=_row(3.9))
    assert not v.accept
    assert v.reject_reasons == (REJECT_NOOP,)


def test_gate_accepts_clean_config_improvement_and_carries_rationale():
    p = ConfigProposal(
        target="c_0427",
        diff={"pref.allow_fallbacks": True},
        rationale=ImprovementRationale("h", "enable fallbacks", "fewer drops"),
    )
    champ = _row(3.50, {"code": 3.5}, pct_failure=0.2)
    chall = _row(3.80, {"code": 3.8}, pct_failure=0.05)  # mean up, no regression
    v = gate_config_proposal(p, champion_row=champ, challenger_row=chall)
    assert v.accept
    # The verdict explains the config change in plain language (#80 tie-in).
    assert v.rationale == {"hypothesis": "h", "change_summary": "enable fallbacks",
                           "expected_effect": "fewer drops"}
    # And it identifies the artifact by the config-pointer hash.
    assert v.artifact_sha256_challenger == p.config_hash()


def test_gate_rejects_config_change_that_regresses_a_domain():
    """Same gate, same per-domain non-regression rule as an adapter."""
    p = ConfigProposal(target="c", diff={"pref.quality_weight": 0.3})
    champ = _row(3.50, {"code": 3.5, "general": 3.5})
    chall = _row(3.80, {"code": 2.9, "general": 4.2})  # mean up, code collapses
    v = gate_config_proposal(p, champion_row=champ, challenger_row=chall)
    assert not v.accept
    assert any("per-domain regression" in r for r in v.reject_reasons)


# ───────────────────── config-pointer registry + rollback ────────────────────


def test_registry_promote_current_previous_rollback(tmp_path: Path):
    reg = ConfigChampionRegistry(tmp_path)
    assert reg.current() is None
    reg.promote({"pref.quality_weight": 0.5}, target="c")
    assert reg.current() == {"pref.quality_weight": 0.5}
    reg.promote({"pref.quality_weight": 0.4}, target="c")
    assert reg.current() == {"pref.quality_weight": 0.4}
    assert reg.previous() == {"pref.quality_weight": 0.5}
    # Rollback restores the prior champion.
    assert reg.rollback() == {"pref.quality_weight": 0.5}
    assert reg.current() == {"pref.quality_weight": 0.5}


def test_registry_rollback_after_first_promote_falls_back_to_base(tmp_path: Path):
    """First-ever promote then rollback → None (routing falls back to defaults),
    mirroring adapters-as-pointers → base."""
    reg = ConfigChampionRegistry(tmp_path)
    reg.promote({"pref.allow_fallbacks": True}, target="c")
    assert reg.rollback() is None
    assert reg.current() is None


# ─────────────────── never applied ungated (the core guarantee) ──────────────


def test_promote_if_accepted_applies_only_on_accept(tmp_path: Path):
    reg = ConfigChampionRegistry(tmp_path)
    p = ConfigProposal(target="c", diff={"pref.quality_weight": 0.4})
    champ = _row(3.50, {"code": 3.5})
    chall = _row(3.80, {"code": 3.8})
    v = gate_config_proposal(p, champion_row=champ, challenger_row=chall)
    assert v.accept
    active = promote_if_accepted(p, v, reg, current_config={"pref.quality_weight": 0.5})
    assert active == {"pref.quality_weight": 0.4}
    assert reg.current() == {"pref.quality_weight": 0.4}


def test_promote_if_accepted_leaves_champion_on_reject(tmp_path: Path):
    reg = ConfigChampionRegistry(tmp_path)
    p = ConfigProposal(target="c", diff={"pref.quality_weight": 0.3})
    champ = _row(3.50, {"code": 3.5, "general": 3.5})
    chall = _row(3.80, {"code": 2.9, "general": 4.2})  # regresses code
    v = gate_config_proposal(p, champion_row=champ, challenger_row=chall)
    assert not v.accept
    base = {"pref.quality_weight": 0.5}
    active = promote_if_accepted(p, v, reg, current_config=base)
    assert active == base            # unchanged
    assert reg.current() is None     # nothing promoted


# ───────────────────────── human-gated proposal store ────────────────────────


def test_record_proposal_writes_status_proposed_and_does_not_apply(tmp_path: Path):
    reg = ConfigChampionRegistry(tmp_path / "reg")
    out = tmp_path / "config_proposals.jsonl"
    p = ConfigProposal(
        target="c_0427",
        diff={"pref.allow_fallbacks": True},
        rationale=ImprovementRationale("h", "c", "e"),
    )
    record_proposal(out, p)
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["status"] == "proposed"
    assert rows[0]["diff"] == {"pref.allow_fallbacks": True}
    # Recording is apply-free: no champion was set.
    assert reg.current() is None
