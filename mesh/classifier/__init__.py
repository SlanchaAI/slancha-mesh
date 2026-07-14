"""Prompt classifier — the perception layer behind `model: "auto"`.

Ported from slancha-local (`slancha_local.classifier`), which vendored it
from slancha-api `app/classifier/`. Perception only: this package turns a
prompt into `mesh.select.ClassifierSignals`; selection stays in
`mesh.select` where it already lives.

Heavy deps (onnxruntime, tokenizers, treelite, numpy) install via the
`classifier` extra. Nothing here imports them at package-import time, so
`import mesh.classifier` is always safe; construction fails loud with an
install hint when the extra is missing.
"""
