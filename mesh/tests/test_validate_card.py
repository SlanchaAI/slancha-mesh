"""Tests for the SpecialistCard TOML linter."""

from __future__ import annotations

from pathlib import Path

import pytest

from mesh.validate_card import (
    _ADVISORY_CODES,
    _Report,
    KNOWN_CAPABILITIES,
    KNOWN_DOMAINS,
    main,
    validate_paths,
)


def _write(path: Path, content: str) -> Path:
    path.write_text(content)
    return path


# Minimal valid TOML — every other test extends/breaks this.
_GOOD_TOML = """
model_id = "vendor/model"
specialist_id = "{stem}"
domain = "general"
difficulty_tiers = ["medium"]
languages = ["en"]
required_backend = "vllm"
storage_gb = 10.0
runtime_gb = 12.0
min_vram_gb = 8.0
context_window = 8192
n_layers = 32
capabilities = ["streaming"]
revision = "abc123def456"
"""


def test_clean_card_no_findings(tmp_path):
    f = _write(tmp_path / "clean-card.toml", _GOOD_TOML.format(stem="clean-card"))
    report = validate_paths([f])
    assert report.findings == [], report.findings


def test_runtime_lt_storage_errors(tmp_path):
    bad = _GOOD_TOML.format(stem="bad").replace(
        "runtime_gb = 12.0", "runtime_gb = 8.0"
    )
    f = _write(tmp_path / "bad.toml", bad)
    report = validate_paths([f])
    codes = [x.code for x in report.findings]
    assert "RUNTIME_LT_STORAGE" in codes
    assert report.has_errors


def test_unknown_capability_warns(tmp_path):
    bad = _GOOD_TOML.format(stem="bad-cap").replace(
        'capabilities = ["streaming"]', 'capabilities = ["streming"]'  # typo
    )
    f = _write(tmp_path / "bad-cap.toml", bad)
    report = validate_paths([f])
    assert any(x.code == "UNKNOWN_CAPABILITY" for x in report.findings)
    # warning, not error
    assert not report.has_errors


def test_unknown_domain_warns(tmp_path):
    bad = _GOOD_TOML.format(stem="d").replace(
        'domain = "general"', 'domain = "wrtiing"'
    )
    f = _write(tmp_path / "d.toml", bad)
    report = validate_paths([f])
    assert any(x.code == "UNKNOWN_DOMAIN" for x in report.findings)


def test_unknown_language_warns(tmp_path):
    bad = _GOOD_TOML.format(stem="lang").replace(
        'languages = ["en"]', 'languages = ["engish"]'
    )
    f = _write(tmp_path / "lang.toml", bad)
    report = validate_paths([f])
    assert any(x.code == "UNKNOWN_LANGUAGE_TAG" for x in report.findings)


def test_filename_id_mismatch_warns(tmp_path):
    """File 'a.toml' declaring specialist_id='b' surfaces the mismatch."""
    bad = _GOOD_TOML.format(stem="b")  # specialist_id=b
    f = _write(tmp_path / "a.toml", bad)
    report = validate_paths([f])
    assert any(x.code == "FILENAME_ID_MISMATCH" for x in report.findings)


def test_quality_router_observed_hardcoded_errors(tmp_path):
    """Operator setting router_observed directly is a hard error."""
    bad = _GOOD_TOML.format(stem="q") + 'quality_router_observed = 4.5\n'
    f = _write(tmp_path / "q.toml", bad)
    report = validate_paths([f])
    assert any(x.code == "QUALITY_OBSERVED_HARDCODED" for x in report.findings)
    assert report.has_errors


def test_kv_arch_without_geometry_warns(tmp_path):
    """A standard-attention card missing n_kv_heads/head_dim warns (not errors)."""
    bad = _GOOD_TOML.format(stem="kv") + 'kv_arch = "gqa"\n'
    f = _write(tmp_path / "kv.toml", bad)
    report = validate_paths([f])
    assert any(x.code == "KV_GEOMETRY_INCOMPLETE" for x in report.findings)
    assert not report.has_errors


def test_kv_arch_with_full_geometry_clean(tmp_path):
    good = _GOOD_TOML.format(stem="kv-ok") + (
        'kv_arch = "gqa"\nn_kv_heads = 8\nhead_dim = 128\n'
    )
    f = _write(tmp_path / "kv-ok.toml", good)
    report = validate_paths([f])
    assert not any(x.code == "KV_GEOMETRY_INCOMPLETE" for x in report.findings)


def test_active_params_ge_runtime_warns(tmp_path):
    # active_params_gb must be SMALLER than total runtime_gb (12.0 in _GOOD_TOML).
    bad = _GOOD_TOML.format(stem="moe") + "active_params_gb = 20.0\n"
    f = _write(tmp_path / "moe.toml", bad)
    report = validate_paths([f])
    assert any(x.code == "ACTIVE_PARAMS_NOT_LESS_THAN_TOTAL" for x in report.findings)
    assert not report.has_errors  # warning, not error


def test_tier1_vllm_card_without_revision_warns_and_strict_fails(tmp_path):
    """Tier-1 vllm card lacking `revision` is a WARNING (#148) that `--strict` fails.

    _GOOD_TOML sets no coverage_tier, so it defaults to tier-1. Tier-1
    essentials are what a default deploy runs; an unpinned HF ref is a live
    repo-squat/rename RCE surface (#142), so it must gate CI.
    """
    bad = _GOOD_TOML.format(stem="norev-t1").replace('revision = "abc123def456"\n', "")
    f = _write(tmp_path / "norev-t1.toml", bad)
    report = validate_paths([f])
    assert any(x.code == "REVISION_UNPINNED" for x in report.findings)
    assert report.has_warnings
    assert not report.has_errors            # warning, not a hard error…
    assert main([str(f)]) == 0              # …normal mode still passes
    assert main([str(f), "--strict"]) == 1  # …but --strict gates it


def test_tier2_vllm_card_without_revision_advises_and_strict_clean(tmp_path):
    """Tier-2+ vllm card without `revision` stays a soft advisory (migration in flight).

    Advisory is its own severity, never promoted by --strict — the nudge
    without the fleet-wide break for the not-yet-migrated cards.
    """
    bad = (
        _GOOD_TOML.format(stem="norev-t2").replace('revision = "abc123def456"\n', "")
        + "coverage_tier = 2\n"
    )
    f = _write(tmp_path / "norev-t2.toml", bad)
    report = validate_paths([f])
    assert any(x.code == "REVISION_UNPINNED" for x in report.findings)
    assert not report.has_warnings  # advisory, not warning
    assert not report.has_errors
    assert main([str(f)]) == 0
    assert main([str(f), "--strict"]) == 0


def test_trust_remote_code_without_revision_errors_at_any_tier(tmp_path):
    """trust_remote_code=true + no revision is the #142 RCE precondition (#148 review).

    It's a hard ERROR regardless of coverage_tier — fails even without
    `--strict` — because executing repo modeling code from a mutable ref is
    never safe, tier notwithstanding.
    """
    bad = (
        _GOOD_TOML.format(stem="trc").replace('revision = "abc123def456"\n', "")
        + "coverage_tier = 2\ntrust_remote_code = true\n"
    )
    f = _write(tmp_path / "trc.toml", bad)
    report = validate_paths([f])
    assert any(x.code == "TRUST_REMOTE_CODE_UNPINNED" for x in report.findings)
    assert report.has_errors            # hard error, tier-2 notwithstanding…
    assert main([str(f)]) == 1          # …fails even in non-strict mode
    # Pinning a revision clears it — remote code is fine once the ref is immutable.
    good = _GOOD_TOML.format(stem="trc-ok") + "coverage_tier = 2\ntrust_remote_code = true\n"
    g = _write(tmp_path / "trc-ok.toml", good)
    rep2 = validate_paths([g])
    assert not any(x.code == "TRUST_REMOTE_CODE_UNPINNED" for x in rep2.findings)


def test_advise_rejects_non_allowlisted_code():
    """advise() refuses a code outside _ADVISORY_CODES (#148 design note).

    Soft advisories dodge --strict, so the allowlist is the membrane a
    security/correctness finding cannot cross silently: routing one through
    advise() raises at author time instead of never failing CI.
    """
    rep = _Report()
    with pytest.raises(ValueError, match="non-allowlisted"):
        rep.advise(Path("x.toml"), "NEW_SECURITY_CODE", "should raise")
    # A known migration-nudge code is accepted.
    assert "REVISION_UNPINNED" in _ADVISORY_CODES
    rep.advise(Path("x.toml"), "REVISION_UNPINNED", "ok")
    assert any(f.code == "REVISION_UNPINNED" for f in rep.findings)


def test_ollama_card_without_revision_no_advisory(tmp_path):
    """The revision-pin nudge is vllm-specific; other backends don't need HF `--revision`."""
    bad = (
        _GOOD_TOML.format(stem="norev-ollama")
        .replace('revision = "abc123def456"\n', "")
        .replace('required_backend = "vllm"', 'required_backend = "ollama"')
    )
    f = _write(tmp_path / "norev-ollama.toml", bad)
    report = validate_paths([f])
    assert not any(x.code == "REVISION_UNPINNED" for x in report.findings)


def test_pydantic_validation_error_surfaces(tmp_path):
    """Missing required field → Pydantic validation error → reported."""
    bad = _GOOD_TOML.format(stem="missing").replace(
        'required_backend = "vllm"\n', ""
    )
    f = _write(tmp_path / "missing.toml", bad)
    report = validate_paths([f])
    assert any(x.code == "PYDANTIC_VALIDATION" for x in report.findings)
    assert report.has_errors


def test_toml_parse_error_surfaces(tmp_path):
    f = _write(tmp_path / "garbage.toml", "this is = not [ valid")
    report = validate_paths([f])
    assert any(x.code == "TOML_PARSE" for x in report.findings)
    assert report.has_errors


def test_duplicate_specialist_id_across_files(tmp_path):
    a = _write(tmp_path / "a.toml", _GOOD_TOML.format(stem="a"))
    b_text = _GOOD_TOML.format(stem="b").replace(
        'specialist_id = "b"', 'specialist_id = "a"'  # collide with file a
    )
    b = _write(tmp_path / "b.toml", b_text)
    report = validate_paths([a, b])
    assert any(x.code == "DUPLICATE_SPECIALIST_ID" for x in report.findings)
    assert report.has_errors


def test_file_not_found_errors(tmp_path):
    report = validate_paths([tmp_path / "does-not-exist.toml"])
    assert any(x.code == "FILE_NOT_FOUND" for x in report.findings)


def test_main_returns_0_for_clean_catalog(tmp_path, capsys):
    _write(tmp_path / "ok.toml", _GOOD_TOML.format(stem="ok"))
    rc = main([str(tmp_path / "ok.toml")])
    assert rc == 0
    assert "OK" in capsys.readouterr().out


def test_main_returns_1_on_error(tmp_path, capsys):
    bad = _GOOD_TOML.format(stem="bad").replace(
        "runtime_gb = 12.0", "runtime_gb = 8.0"
    )
    _write(tmp_path / "bad.toml", bad)
    rc = main([str(tmp_path / "bad.toml")])
    assert rc == 1


def test_main_strict_promotes_warnings_to_errors(tmp_path):
    """Warning-only TOML returns 0 in normal mode, 1 in --strict."""
    bad = _GOOD_TOML.format(stem="warn").replace(
        'capabilities = ["streaming"]', 'capabilities = ["typo-only"]'
    )
    _write(tmp_path / "warn.toml", bad)
    assert main([str(tmp_path / "warn.toml")]) == 0
    assert main([str(tmp_path / "warn.toml"), "--strict"]) == 1


# ── Run against the actual on-disk catalog ─────────────────────────────────


def test_real_catalog_passes_strict():
    """The real on-disk catalog must exit 0 under `--strict` — CI's exact gate.

    Post-#148 every tier-1 vllm card carries a revision pin, so the tier-1
    REVISION_UNPINNED *warning* never fires on the real catalog; any tier-2+
    card still unpinned emits only a strict-immune advisory. If this ever
    fails, a tier-1 vllm card shipped without a pin (or a new strict warning
    landed) — fix the card, don't relax the gate.
    """
    assert main(["--all"]) == 0
    assert main(["--all", "--strict"]) == 0


def test_real_catalog_has_known_capabilities_and_domains():
    """Sanity check: KNOWN_* sets cover the real catalog's authored values.

    If this test fails, EITHER the catalog is using a new capability /
    domain that should be added to the canonical set (extend KNOWN_*),
    OR it's a typo bug that the linter just caught.
    """
    from mesh.catalog import load_catalog

    cards = load_catalog()
    unknown_caps = set()
    unknown_domains = set()
    for c in cards:
        for cap in c.capabilities:
            if cap not in KNOWN_CAPABILITIES:
                unknown_caps.add(cap)
        if c.domain not in KNOWN_DOMAINS:
            unknown_domains.add(c.domain)

    # If anything surfaces, fail loudly with a hint.
    assert not unknown_caps, (
        f"catalog uses capability values not in KNOWN_CAPABILITIES: {unknown_caps}. "
        f"Either extend the set in mesh/validate_card.py or fix the catalog typos."
    )
    assert not unknown_domains, (
        f"catalog uses domain values not in KNOWN_DOMAINS: {unknown_domains}. "
        f"Either extend the set or fix the catalog typos."
    )
