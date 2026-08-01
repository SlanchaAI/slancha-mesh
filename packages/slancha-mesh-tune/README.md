# Slancha-Mesh Tune

Optional fine-tuning, replay evaluation, and promotion tools for
[Slancha-Mesh](https://github.com/SlanchaAi/slancha-mesh).

Install this distribution only on machines that build or evaluate model
artifacts:

```bash
# Source install until the distribution is published to PyPI:
pip install -e "./packages/slancha-mesh-tune[train]"
```

The base `slancha-mesh` distribution serves, discovers, and routes models
without importing this package. The add-on contributes the existing
`mesh.training`, `mesh.replay_store`, `mesh.eval`, and related modules through
the `mesh` namespace so current library callers remain compatible.

Useful commands:

```bash
slancha-mesh-tune check
slancha-mesh-gate --help

pip install -e "./packages/slancha-mesh-tune[dashboard]"
slancha-mesh-tune dashboard -- --operator ./dashboard
```

`TrainingPass` still refuses its deterministic stub unless explicitly enabled.
The base serving daemon never starts training automatically.
