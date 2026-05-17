"""Tests for mesh.eval.holdout."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mesh.eval.holdout import (
    DEFAULT_HOLDOUT_DISTRIBUTION,
    iter_jsonl,
    sample_holdout,
    stratify_by_domain,
    write_holdout,
)


def _records_domain(domain: str, n: int, prefix: str = "p") -> list[dict]:
    return [
        {"prompt_id": f"{prefix}-{domain}-{i}", "prompt_text": f"text{i}",
         "signals": {"domain": domain}}
        for i in range(n)
    ]


def test_default_distribution_sums_to_one():
    assert sum(DEFAULT_HOLDOUT_DISTRIBUTION.values()) == pytest.approx(1.0)


def test_iter_jsonl_skips_blanks(tmp_path: Path):
    p = tmp_path / "x.jsonl"
    p.write_text("\n" + json.dumps({"a": 1}) + "\n\n" + json.dumps({"b": 2}) + "\n",
                 encoding="utf-8")
    rows = iter_jsonl(p)
    assert rows == [{"a": 1}, {"b": 2}]


def test_iter_jsonl_raises_on_bad_json(tmp_path: Path):
    p = tmp_path / "x.jsonl"
    p.write_text("{good:1}\n{bad\n", encoding="utf-8")
    with pytest.raises(ValueError, match="line 1"):
        iter_jsonl(p)


def test_stratify_by_domain_groups_correctly():
    records = (
        _records_domain("code", 5) +
        _records_domain("general", 3) +
        _records_domain("math", 2)
    )
    g = stratify_by_domain(records)
    assert len(g["code"]) == 5
    assert len(g["general"]) == 3
    assert len(g["math"]) == 2


def test_stratify_handles_missing_domain():
    records = [{"prompt_id": "x", "signals": {}}, {"prompt_id": "y"}]
    g = stratify_by_domain(records)
    assert "unknown" in g
    assert len(g["unknown"]) == 2


def test_sample_holdout_respects_quotas():
    records = (
        _records_domain("code", 100) +
        _records_domain("general", 100) +
        _records_domain("math", 100)
    )
    target = {"code": 0.5, "general": 0.3, "math": 0.2}
    samples, counts = sample_holdout(records, size=100, target_distribution=target, seed=42)
    assert counts == {"code": 50, "general": 30, "math": 20}
    assert len(samples) == 100


def test_sample_holdout_takes_full_bucket_on_shortfall():
    records = (
        _records_domain("code", 10) +  # only 10 code prompts available
        _records_domain("general", 100)
    )
    target = {"code": 0.5, "general": 0.5}
    samples, counts = sample_holdout(records, size=100, target_distribution=target, seed=42)
    assert counts["code"] == 10  # took the full bucket
    assert counts["general"] == 50
    assert len(samples) == 60


def test_sample_holdout_skips_target_domain_with_zero_records():
    records = _records_domain("code", 100)
    target = {"code": 0.5, "creative": 0.5}
    samples, counts = sample_holdout(records, size=100, target_distribution=target, seed=42)
    assert "creative" not in counts
    assert counts["code"] == 50


def test_sample_holdout_deterministic_by_seed():
    records = _records_domain("code", 100)
    target = {"code": 1.0}
    a, _ = sample_holdout(records, size=50, target_distribution=target, seed=42)
    b, _ = sample_holdout(records, size=50, target_distribution=target, seed=42)
    assert [r["prompt_id"] for r in a] == [r["prompt_id"] for r in b]


def test_sample_holdout_different_seeds_differ():
    records = _records_domain("code", 100)
    target = {"code": 1.0}
    a, _ = sample_holdout(records, size=50, target_distribution=target, seed=42)
    b, _ = sample_holdout(records, size=50, target_distribution=target, seed=43)
    assert [r["prompt_id"] for r in a] != [r["prompt_id"] for r in b]


# ---------------------------------------------------------------------------
# write_holdout
# ---------------------------------------------------------------------------


def test_write_holdout_stamps_rows_and_writes_manifest(tmp_path: Path):
    samples = _records_domain("code", 5)
    out = tmp_path / "holdout.jsonl"
    manifest = write_holdout(out, samples, holdout_version=1, seed=42)
    assert manifest["size"] == 5
    assert manifest["holdout_version"] == 1
    assert manifest["seed"] == 42
    assert manifest["per_domain"]["code"] == 5
    assert "output_sha256" in manifest
    # Each row stamped with holdout_version + seed
    rows = iter_jsonl(out)
    assert all(r["holdout_version"] == 1 for r in rows)
    assert all(r["seed"] == 42 for r in rows)


def test_write_holdout_sha_matches_disk(tmp_path: Path):
    """Manifest sha256 must match `shasum -a 256` on the file."""
    import hashlib
    samples = _records_domain("code", 3)
    out = tmp_path / "holdout.jsonl"
    manifest = write_holdout(out, samples, holdout_version=1, seed=42)
    h = hashlib.sha256()
    with out.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    assert manifest["output_sha256"] == h.hexdigest()
