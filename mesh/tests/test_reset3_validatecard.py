"""Public linter coverage for adopted aliases and malformed input ordering."""

from pathlib import Path

import pytest

from mesh.validate_card import validate_paths


_CARD = '''\
model_id = "fixture/model"
specialist_id = "fixture"
domain = "general"
difficulty_tiers = ["medium"]
required_backend = "{backend}"
storage_gb = 1.0
runtime_gb = 2.0
min_vram_gb = 1.0
context_window = 1024
n_layers = 2
revision = "abc123def456"
'''


@pytest.fixture
def card_path(tmp_path):
    return tmp_path / "fixture.toml"


def test_external_endpoint_accepts_nonempty_alias(card_path):
    card_path.write_text(
        _CARD.format(backend="external")
        + 'static_base_url = "http://127.0.0.1:9"\n'
        + 'served_model_name = "upstream-model"\n'
    )
    assert validate_paths([card_path]).findings == []


@pytest.mark.parametrize("backend", ["vllm", "mlx", "llamacpp", "ollama"])
def test_managed_backend_alias_reports_one_warning(card_path, backend):
    card_path.write_text(
        _CARD.format(backend=backend) + 'served_model_name = "upstream-model"\n'
    )
    report = validate_paths([card_path])
    assert [(f.path, f.severity, f.code) for f in report.findings] == [
        (card_path, "warning", "SERVED_MODEL_NAME_NON_EXTERNAL")
    ]
    assert 'served_model_name "upstream-model"' in report.findings[0].message
    assert f'required_backend is "{backend}"' in report.findings[0].message
    assert report.has_warnings
    assert not report.has_errors


@pytest.mark.parametrize("backend", ["external", "vllm", "mlx", "llamacpp", "ollama"])
@pytest.mark.parametrize("alias_line", ["", 'served_model_name = ""\n'])
def test_absent_or_empty_alias_is_clean(card_path, backend, alias_line):
    card_path.write_text(_CARD.format(backend=backend) + alias_line)
    assert validate_paths([card_path]).findings == []


def test_malformed_inputs_preserve_diagnostic_order_and_repeatability(tmp_path):
    parse_path = tmp_path / "parse.toml"
    parse_path.write_text("model_id = [")
    schema_path = tmp_path / "schema.toml"
    schema_path.write_text(
        _CARD.format(backend="external")
        .replace('specialist_id = "fixture"', 'specialist_id = "schema"')
        .replace("context_window = 1024", 'context_window = "invalid-context"')
        .replace("n_layers = 2", 'n_layers = "invalid-layers"')
    )
    clean_path = tmp_path / "fixture.toml"
    clean_path.write_text(_CARD.format(backend="external"))
    paths: list[Path] = [schema_path, parse_path, clean_path]

    report = validate_paths(iter(paths))
    assert [(f.path, f.severity, f.code) for f in report.findings] == [
        (schema_path, "error", "PYDANTIC_VALIDATION"),
        (schema_path, "error", "PYDANTIC_VALIDATION"),
        (parse_path, "error", "TOML_PARSE"),
    ]
    for finding, field, value in zip(
        report.findings[:2],
        ["context_window", "n_layers"],
        ["invalid-context", "invalid-layers"],
    ):
        assert finding.message.startswith(f"{field}:")
        assert f"(got: {value!r})" in finding.message
    assert report.findings[2].message.startswith("TOML parse error:")
    assert report.has_errors
    assert not report.has_warnings
    assert validate_paths(iter(paths)).findings == report.findings
    assert validate_paths(reversed(paths)).findings == [
        report.findings[2], *report.findings[:2]
    ]
