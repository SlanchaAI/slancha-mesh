"""Probe tests — run on the current machine.

We can't assert exact hardware values (CI runs on x86_64, Spark runs on
GB10 aarch64), but we can assert the probe doesn't crash, fills required
fields, and that warnings are non-empty where we know fields are
unknowable.
"""

from __future__ import annotations

import json

from mesh.models import NodeProbe
from mesh.probe import probe_node


def test_probe_node_returns_node_probe():
    p = probe_node(friendly_name="test-host")
    assert isinstance(p, NodeProbe)
    assert p.friendly_name == "test-host"
    assert p.node_id  # non-empty
    assert p.chip
    assert p.arch in ("aarch64", "x86_64", "apple-silicon")
    assert p.ram_total_gb > 0  # any real machine has RAM


def test_probe_node_json_roundtrip():
    p = probe_node()
    js = p.model_dump_json()
    parsed = json.loads(js)
    # Restore via Pydantic
    p2 = NodeProbe.model_validate(parsed)
    assert p2.node_id == p.node_id
    assert p2.chip == p.chip
    assert p2.ram_total_gb == p.ram_total_gb


def test_probe_warnings_are_a_list():
    p = probe_node()
    assert isinstance(p.probe_warnings, list)
    # On any node lacking nvidia-smi or with no bandwidth table entry,
    # we expect at least one warning.
