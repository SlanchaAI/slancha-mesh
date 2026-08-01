"""Held-out eval-set sampling and persistence.

Why this exists: the recirculating loop claim ("the router self-improves")
must be measurable. A frozen held-out set lets us re-route the same N
prompts through every router version and watch mean judge score climb
(or not). Without a held-out set, we can only point at REDEPLOY animations
and assert improvement.

Sampling is deterministic via seed → same call always produces same set,
so a re-run is auditable. We stratify by routing-taxonomy domain so the
eval set has the same domain distribution as the operator's target
(code 27.5% / general 22.5% / reasoning 15% / math 10% / multilingual 10%
/ creative 10% / tool-use 5%) — otherwise mean-score deltas would be
confounded by domain mix shift.

Usage:

    python -m mesh.eval.holdout \\
        --corpus  corpus/training/v3.1-mmbert/prompts.jsonl \\
        --output  corpus/eval/holdout_v1.jsonl \\
        --size    500 \\
        --seed    0xC0FFEE

Output is a JSONL with one prompt per row, schema:
    {prompt_id, prompt_text, signals, source, holdout_version: 1, seed: int}

Loaders + tests live in mesh/dashboard/eval.py and the test module.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any


# Operator's routing-taxonomy target — duplicated from build_corpus_v3_1.py
# rather than imported so this module stays free of the build-time HF
# datasets dependency. Keep in sync when the operator preset changes.
DEFAULT_HOLDOUT_DISTRIBUTION: dict[str, float] = {
    "code":         0.275,
    "general":      0.225,
    "reasoning":    0.15,
    "math":         0.10,
    "multilingual": 0.10,
    "creative":     0.10,
    "tool-use":     0.05,
}


def iter_jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for i, raw in enumerate(f, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}: invalid JSON at line {i} ({exc})") from exc
    return out


def stratify_by_domain(
    records: list[dict[str, Any]],
    domain_field_path: tuple[str, ...] = ("signals", "domain"),
) -> dict[str, list[dict[str, Any]]]:
    """Group records by domain. `domain_field_path` walks nested dict keys."""
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rec in records:
        cur: Any = rec
        for k in domain_field_path:
            if not isinstance(cur, dict):
                cur = None
                break
            cur = cur.get(k)
        domain = cur if isinstance(cur, str) else "unknown"
        out[domain].append(rec)
    return dict(out)


def sample_holdout(
    records: list[dict[str, Any]],
    size: int,
    target_distribution: dict[str, float],
    seed: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Sample N records stratified by domain.

    Returns (samples, per_domain_counts). When a target domain has fewer
    records available than its target count, takes the full bucket — the
    caller can decide whether to backfill or accept the shortfall.
    """
    rng = random.Random(seed)
    by_domain = stratify_by_domain(records)
    needed: dict[str, int] = {
        d: int(round(pct * size))
        for d, pct in target_distribution.items()
    }
    samples: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for domain, want in needed.items():
        bucket = by_domain.get(domain, [])
        take = min(want, len(bucket))
        if take == 0:
            continue
        samples.extend(rng.sample(bucket, take))
        counts[domain] = take
    return samples, counts


def write_holdout(
    output: Path,
    samples: list[dict[str, Any]],
    holdout_version: int,
    seed: int,
    source_corpus_path: Path | None = None,
) -> dict[str, Any]:
    """Write samples as JSONL + return a manifest summary."""
    output.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    per_domain: dict[str, int] = defaultdict(int)
    with output.open("w", encoding="utf-8") as f:
        for rec in samples:
            # Stamp each row so future readers know which holdout set it's from
            rec = dict(rec)
            rec["holdout_version"] = holdout_version
            rec["seed"] = seed
            line = json.dumps(rec, ensure_ascii=False)
            f.write(line + "\n")
            d = (rec.get("signals") or {}).get("domain", "unknown")
            per_domain[d] += 1
    with output.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return {
        "holdout_version": holdout_version,
        "seed":            seed,
        "source_corpus":   str(source_corpus_path) if source_corpus_path else None,
        "size":            len(samples),
        "per_domain":      dict(per_domain),
        "output_path":     str(output),
        "output_sha256":   h.hexdigest(),
        "built_at":        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Sample a held-out eval set from a corpus.")
    ap.add_argument(
        "--corpus", type=Path, required=True,
        help="Source corpus JSONL (e.g., corpus/training/v3.1-mmbert/prompts.jsonl)",
    )
    ap.add_argument(
        "--output", type=Path, required=True,
        help="Output JSONL path (e.g., corpus/eval/holdout_v1.jsonl)",
    )
    ap.add_argument(
        "--manifest", type=Path, default=None,
        help="Optional manifest JSON output path (defaults to <output>.manifest.json)",
    )
    ap.add_argument("--size", type=int, default=500, help="Total held-out set size")
    ap.add_argument("--seed", type=lambda s: int(s, 0), default=0xC0FFEE)
    ap.add_argument(
        "--holdout-version", type=int, default=1,
        help="Integer version tag stamped on each row + manifest",
    )
    args = ap.parse_args(argv)

    if not args.corpus.exists():
        print(f"corpus not found: {args.corpus}", file=sys.stderr)
        return 2

    records = iter_jsonl(args.corpus)
    samples, _ = sample_holdout(
        records=records, size=args.size,
        target_distribution=DEFAULT_HOLDOUT_DISTRIBUTION, seed=args.seed,
    )
    manifest = write_holdout(
        output=args.output, samples=samples,
        holdout_version=args.holdout_version, seed=args.seed,
        source_corpus_path=args.corpus,
    )
    manifest_path = args.manifest or args.output.with_suffix(".manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(
        f"[holdout] wrote {manifest['size']} rows → {args.output} "
        f"(sha256 {manifest['output_sha256'][:16]}…); manifest → {manifest_path}",
        file=sys.stderr,
    )
    print(f"[holdout] per-domain: {manifest['per_domain']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
