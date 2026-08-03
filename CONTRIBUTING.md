# Contributing

Slancha-Mesh is alpha software. Small, test-backed changes are easiest to
review.

## Development setup

```bash
git clone https://github.com/SlanchaAi/slancha-mesh.git
cd slancha-mesh
uv sync --all-packages --extra dev --extra signing
uv run pytest -q
uv run ruff check mesh packages/slancha-mesh-tune/mesh
uv run python -m mesh.validate_card --strict
```

Use `pip install -e .` for a core-only environment. Use
`pip install -e ./packages/slancha-mesh-tune` when changing the optional
training and evaluation add-on.

## Pull requests

- Explain the user-visible problem and the smallest complete fix.
- Add or update tests for behavior changes.
- Keep provider credentials and paid-cloud execution outside core.
- Keep fine-tuning, corpora, replay evaluation, and promotion code in
  `packages/slancha-mesh-tune`.
- For routing changes, include an in-situ OpenAI-compatible request proof.
- Do not commit model weights, private fleet names, tokens, traffic bodies, or
  generated training data.

By contributing, you agree that your contribution is licensed under
Apache-2.0.
