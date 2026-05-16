"""Specialist catalog — loads SpecialistCards from TOML files in this dir."""

from __future__ import annotations

import tomllib
from pathlib import Path

from mesh.models import SpecialistCard

_CATALOG_DIR = Path(__file__).parent


def load_card(path: Path) -> SpecialistCard:
    """Parse a single specialist TOML file into a SpecialistCard."""
    with path.open("rb") as f:
        data = tomllib.load(f)
    return SpecialistCard(**data)


def load_catalog(directory: Path | None = None) -> list[SpecialistCard]:
    """Load every *.toml in the catalog directory.

    Returns cards sorted by `specialist_id` for deterministic test runs.
    """
    directory = directory or _CATALOG_DIR
    cards: list[SpecialistCard] = []
    for path in sorted(directory.glob("*.toml")):
        cards.append(load_card(path))
    return cards


__all__ = ["load_card", "load_catalog"]
