"""SpecialistCard TOML linter — catches authoring bugs at edit time.

Caught here (instead of at test_each_card_is_specialist_card time):
- Pydantic validation errors (missing required fields, wrong types)
- runtime_gb < storage_gb (the invariant that broke when demo-model-v1
  landed with runtime_gb=48 storage_gb=54)
- Unknown capability strings — typos like "tooluse" instead of "tools"
- Unknown domains — typos like "wrtiing" instead of "writing"
- Unknown languages — keeps the language tag pool curated
- Duplicate specialist_id across files
- File-stem-vs-specialist_id mismatch — easy copy-paste bug

Usage:
    python -m mesh.validate_card                                # lint all in mesh/catalog/
    python -m mesh.validate_card mesh/catalog/demo-model-v2.toml  # one file
    python -m mesh.validate_card --all                          # equivalent to no-args
    python -m mesh.validate_card --strict                       # also fail on warnings

Exit codes:
    0  all good
    1  errors found
    2  bad invocation (file missing, etc.)

For CI: drop the `python -m mesh.validate_card` call before pytest in
your pyproject.toml's pre-commit hook or workflow.
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from pydantic import ValidationError

from mesh.models import SpecialistCard

# Canonical sets — extend when new vocabulary lands. Out-of-set values
# emit warnings (strict mode promotes to errors) rather than hard errors
# so additive vocab doesn't break old TOMLs.
KNOWN_CAPABILITIES = {
    "streaming",
    "system_prompt",
    "tools",
    "json_mode",
    "json_schema",
    "vision",
    "seed",
    "parallel_tool_calls",
    "reasoning",
    "cache_control",
}

KNOWN_DOMAINS = {
    "writing",
    "code",
    "math",
    "reasoning",
    "general",
    "multilingual",
    "creative",
    "summarization",
    "tool_use",
}

KNOWN_LANGUAGE_TAGS = {
    # Common ISO 639-1 codes the catalog has used. Open for additions
    # — flag unknown ones so typos like "engish" don't slip through.
    "en", "es", "fr", "de", "it", "pt", "nl", "ru", "zh", "ja", "ko",
    "ar", "hi", "bn", "ur", "tr", "vi", "id", "th", "pl", "uk", "cs",
    "sv", "no", "da", "fi", "el", "he", "fa",
}

# Codes allowed to be emitted as soft advisories. `advise()` findings are
# never promoted by `--strict`, so this set is the explicit membrane a
# security-severity finding cannot cross: advise() raises on any code not
# listed here (#148 design note). Add a code only when the finding is a
# genuine migration-in-flight nudge — NOT a correctness/security failure,
# which must use error()/warning() so CI's `--strict` gate fails on it.
_ADVISORY_CODES = frozenset({"REVISION_UNPINNED"})


@dataclass(frozen=True)
class Finding:
    path: Path
    severity: str  # "error" | "warning"
    code: str      # short stable identifier — caller can grep
    message: str


@dataclass
class _Report:
    findings: list[Finding] = field(default_factory=list)

    def error(self, path: Path, code: str, message: str) -> None:
        self.findings.append(Finding(path=path, severity="error", code=code, message=message))

    def warning(self, path: Path, code: str, message: str) -> None:
        self.findings.append(Finding(path=path, severity="warning", code=code, message=message))

    def advise(self, path: Path, code: str, message: str) -> None:
        """A soft, never-hard-fail finding — not promoted by `--strict`.

        For checks that are correct-in-principle but not yet enforceable
        because the fleet hasn't caught up (e.g. #142 revision pins on the
        remaining tier-2+ vllm cards). `warning()` is for authoring bugs CI
        should gate on; `advise()` is for a migration that's still in flight.

        The `code` MUST be in `_ADVISORY_CODES`. This is a structural guard,
        not a formality: soft advisories dodge `--strict`, so without it a
        future security/correctness check could be silently routed through
        advise() and never fail CI (#148). A non-allowlisted code is a
        programming error — fail loud at author time, not silently in prod.
        """
        if code not in _ADVISORY_CODES:
            raise ValueError(
                f"advise() called with non-allowlisted code {code!r}; soft "
                f"advisories are reserved for migration nudges (see "
                f"_ADVISORY_CODES in mesh/validate_card.py). A correctness or "
                f"security finding must use error() or warning() so `--strict` "
                f"gates on it."
            )
        self.findings.append(Finding(path=path, severity="advisory", code=code, message=message))

    @property
    def has_errors(self) -> bool:
        return any(f.severity == "error" for f in self.findings)

    @property
    def has_warnings(self) -> bool:
        return any(f.severity == "warning" for f in self.findings)


# ── Per-card checks ─────────────────────────────────────────────────────────


def _check_runtime_vs_storage(card: SpecialistCard, path: Path, report: _Report) -> None:
    if card.runtime_gb < card.storage_gb:
        report.error(
            path,
            "RUNTIME_LT_STORAGE",
            f"runtime_gb ({card.runtime_gb}) < storage_gb ({card.storage_gb}); "
            f"runtime budget must include weights",
        )


def _check_domain(card: SpecialistCard, path: Path, report: _Report) -> None:
    if card.domain not in KNOWN_DOMAINS:
        report.warning(
            path,
            "UNKNOWN_DOMAIN",
            f"domain={card.domain!r} not in canonical set; "
            f"close matches: {sorted(KNOWN_DOMAINS)[:5]}...",
        )


def _check_capabilities(card: SpecialistCard, path: Path, report: _Report) -> None:
    for cap in card.capabilities:
        if cap not in KNOWN_CAPABILITIES:
            report.warning(
                path,
                "UNKNOWN_CAPABILITY",
                f"capability={cap!r} not in canonical set; "
                f"typo? known: {sorted(KNOWN_CAPABILITIES)}",
            )


def _check_languages(card: SpecialistCard, path: Path, report: _Report) -> None:
    for lang in card.languages:
        if lang not in KNOWN_LANGUAGE_TAGS:
            report.warning(
                path,
                "UNKNOWN_LANGUAGE_TAG",
                f"language={lang!r} not in canonical set; typo?",
            )


def _check_specialist_id_matches_filename(
    card: SpecialistCard, path: Path, report: _Report
) -> None:
    """File `nemotron-math-7b-q4.toml` should declare specialist_id `nemotron-math-7b-q4`.

    Loose check — file stem vs specialist_id, case-sensitive. Catches
    accidental copy-paste where someone duplicates a TOML and forgets to
    rename the specialist_id field.
    """
    if path.stem != card.specialist_id:
        report.warning(
            path,
            "FILENAME_ID_MISMATCH",
            f"file stem {path.stem!r} != specialist_id {card.specialist_id!r}; "
            f"intentional? if not, rename the file or fix the field",
        )


def _check_quality_consistency(card: SpecialistCard, path: Path, report: _Report) -> None:
    """quality.router_observed should never be set directly in a TOML.

    That field is written by the Phase 6 probe service; an operator
    setting it manually means tests + audits will be lying to consumers.
    """
    if card.quality_router_observed is not None:
        report.error(
            path,
            "QUALITY_OBSERVED_HARDCODED",
            f"quality_router_observed should be NULL at authoring time "
            f"(was {card.quality_router_observed}); it's populated by "
            f"`python -m mesh.quality_probe`. Use quality_node_self_reported "
            f"if you mean self-reported.",
        )


def _check_kv_geometry(card: SpecialistCard, path: Path, report: _Report) -> None:
    """KV geometry must be complete for the allocator decode bytes model (§3.1).

    A standard-attention card (mha/gqa/mqa) needs both n_kv_heads and head_dim,
    or the allocator silently falls back to a weights-only tok/s estimate that
    loses the long-context KV term.
    """
    if card.kv_arch in ("mha", "gqa", "mqa") and (
        card.n_kv_heads is None or card.head_dim is None
    ):
        report.warning(
            path,
            "KV_GEOMETRY_INCOMPLETE",
            f"kv_arch={card.kv_arch!r} but n_kv_heads/head_dim missing; the "
            f"allocator decode estimate falls back to weights-only (loses the "
            f"long-context KV term)",
        )
    if card.active_params_gb is not None and card.active_params_gb >= card.runtime_gb:
        report.warning(
            path,
            "ACTIVE_PARAMS_NOT_LESS_THAN_TOTAL",
            f"active_params_gb ({card.active_params_gb}) >= runtime_gb "
            f"({card.runtime_gb}); the active decode slice should be SMALLER "
            f"than the total resident footprint (MoE), else drop the field",
        )


def _check_revision_pin(card: SpecialistCard, path: Path, report: _Report) -> None:
    """A vllm-engine card without a `revision` resolves a mutable HF ref (#142).

    Severity tracks the ACTUAL risk of the unpinned ref, not just the tier:

    * `trust_remote_code=true` + no pin → hard ERROR at ANY tier. This is the
      direct #142 RCE precondition: vllm serve executes the repo's own
      `modeling_*.py` from a MUTABLE ref, so a rename/squat repoints it to
      attacker code on the next restart. Enabling remote code without a pin is
      never acceptable — the tier heuristic (below) is a proxy for "does a
      default deploy run this", but remote-code execution is the real trigger
      and must gate independently of coverage_tier (#148 security review).
    * tier-1 (the essentials a default deploy runs) + no pin → WARNING, so
      CI's `--strict` gate fails it.
    * tier-2+ + no pin → soft advisory while the remaining migration finishes:
      the nudge without the fleet-wide break.
    """
    if card.required_backend != "vllm" or card.revision:
        return
    if card.trust_remote_code:
        report.error(
            path,
            "TRUST_REMOTE_CODE_UNPINNED",
            "trust_remote_code=true with no `revision` pin: vllm serve would "
            "execute the repo's modeling_*.py from a mutable HF ref, which a "
            "rename/squat can repoint to attacker code between node restarts "
            "(#142). Enabling remote code REQUIRES pinning an HF commit SHA — "
            "this gates at every tier, not just tier-1.",
        )
    elif card.coverage_tier == 1:
        report.warning(
            path,
            "REVISION_UNPINNED",
            "tier-1 required_backend=vllm card without a `revision` pin: "
            "vllm serve resolves the mutable default-branch ref, which a "
            "repo rename/squat can repoint to attacker weights between node "
            "restarts. Tier-1 essentials MUST pin an HF commit SHA "
            "(#142/#148) — set `revision` from the model's HF commit.",
        )
    else:
        report.advise(
            path,
            "REVISION_UNPINNED",
            "required_backend=vllm but no `revision` set; vllm serve resolves "
            "the mutable default-branch ref, which can change (or point at a "
            "renamed/squatted repo) between node restarts. Pin an HF commit "
            "SHA or tag when you have one.",
        )


def _check_one(path: Path) -> tuple[SpecialistCard | None, _Report]:
    """Validate a single TOML. Returns (card_or_None, findings)."""
    report = _Report()

    if not path.exists():
        report.error(path, "FILE_NOT_FOUND", f"no such file: {path}")
        return None, report
    if path.suffix != ".toml":
        report.error(path, "NOT_TOML", f"file is not *.toml: {path}")
        return None, report

    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        report.error(path, "TOML_PARSE", f"TOML parse error: {exc}")
        return None, report

    try:
        card = SpecialistCard(**data)
    except ValidationError as exc:
        for err in exc.errors():
            loc = ".".join(str(p) for p in err.get("loc", []))
            report.error(
                path,
                "PYDANTIC_VALIDATION",
                f"{loc}: {err['msg']} (got: {err.get('input')!r})",
            )
        return None, report

    _check_runtime_vs_storage(card, path, report)
    _check_domain(card, path, report)
    _check_capabilities(card, path, report)
    _check_languages(card, path, report)
    _check_specialist_id_matches_filename(card, path, report)
    _check_quality_consistency(card, path, report)
    _check_kv_geometry(card, path, report)
    _check_revision_pin(card, path, report)

    return card, report


def validate_paths(paths: Iterable[Path]) -> _Report:
    """Validate every TOML at the given paths + cross-file duplicate-ID check."""
    aggregate = _Report()
    ids_seen: dict[str, Path] = {}

    for path in paths:
        card, rep = _check_one(path)
        aggregate.findings.extend(rep.findings)
        if card is not None:
            prior = ids_seen.get(card.specialist_id)
            if prior is not None:
                aggregate.error(
                    path,
                    "DUPLICATE_SPECIALIST_ID",
                    f"specialist_id={card.specialist_id!r} also declared in {prior}",
                )
            else:
                ids_seen[card.specialist_id] = path

    return aggregate


# ── CLI ─────────────────────────────────────────────────────────────────────


_DEFAULT_CATALOG = Path(__file__).parent / "catalog"


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("files", nargs="*", help="TOMLs to validate. Default: every *.toml in mesh/catalog/")
    p.add_argument("--all", action="store_true", help="Explicitly validate the whole catalog")
    p.add_argument("--strict", action="store_true", help="Promote warnings to errors (CI-friendly)")
    return p


def _format_finding(f: Finding, *, strict: bool) -> str:
    sev = f.severity.upper()
    if strict and f.severity == "warning":
        sev = "WARNING-AS-ERROR"
    return f"  [{sev}] {f.code} {f.path}: {f.message}"


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.files and not args.all:
        paths = [Path(f) for f in args.files]
    else:
        paths = sorted(_DEFAULT_CATALOG.glob("*.toml"))

    if not paths:
        print(f"no TOMLs to validate (looked in {_DEFAULT_CATALOG})", file=sys.stderr)
        return 2

    report = validate_paths(paths)
    if not report.findings:
        print(f"OK — {len(paths)} card(s) clean")
        return 0

    by_severity = defaultdict(int)
    for f in report.findings:
        by_severity[f.severity] += 1
        print(_format_finding(f, strict=args.strict), file=sys.stderr)

    err_count = by_severity["error"]
    warn_count = by_severity["warning"]
    advise_count = by_severity["advisory"]
    print(
        f"\nSummary: {err_count} error(s), {warn_count} warning(s), "
        f"{advise_count} advisory(ies) across {len(paths)} file(s)",
        file=sys.stderr,
    )

    if err_count > 0 or (args.strict and warn_count > 0):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
