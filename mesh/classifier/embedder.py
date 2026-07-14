"""mmBERT-small ONNX INT8 embedder. Ported from slancha-local, origin slancha-api.

Loads asset bytes via importlib.resources so the model ships in the wheel
and resolves regardless of working directory. Requires the `classifier`
extra (onnxruntime + tokenizers).
"""

from __future__ import annotations

import logging
from importlib.resources import files
from pathlib import Path

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

logger = logging.getLogger(__name__)

EMBED_DIM = 512  # mmBERT-small actual hidden_size (config says 512, NOT 384)
MAX_LENGTH = 512

_ASSET_ROOT = Path(str(files("mesh.classifier.assets") / "mmbert_small_onnx_int8"))
_ONNX_PATH = _ASSET_ROOT / "model_quantized.onnx"
_TOKENIZER_PATH = _ASSET_ROOT / "tokenizer.json"

_session: ort.InferenceSession | None = None
_tokenizer: Tokenizer | None = None
_input_names: list[str] | None = None


def _get_session() -> ort.InferenceSession:
    # Publish-last: concurrent threadpool callers must never observe a
    # session without its _input_names (or a half-configured tokenizer
    # below). Double construction on a first-burst race is benign.
    global _session, _input_names
    if _session is None:
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        session = ort.InferenceSession(
            str(_ONNX_PATH), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        _input_names = [i.name for i in session.get_inputs()]
        _session = session
    return _session


def _get_tokenizer() -> Tokenizer:
    global _tokenizer
    if _tokenizer is None:
        tokenizer = Tokenizer.from_file(str(_TOKENIZER_PATH))
        tokenizer.enable_truncation(max_length=MAX_LENGTH)
        tokenizer.enable_padding(pad_id=0, pad_token="<pad>")
        _tokenizer = tokenizer
    return _tokenizer


def embed(texts: list[str]) -> np.ndarray:
    """Embed a batch of texts → (N, EMBED_DIM) L2-normalized float32 array."""
    session = _get_session()
    tokenizer = _get_tokenizer()

    encodings = tokenizer.encode_batch(texts)
    input_ids = np.array([e.ids for e in encodings], dtype=np.int64)
    attention_mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)

    feeds: dict[str, np.ndarray] = {"input_ids": input_ids, "attention_mask": attention_mask}
    if "token_type_ids" in (_input_names or []):
        feeds["token_type_ids"] = np.zeros_like(input_ids)

    hidden = session.run(None, feeds)[0]
    mask = attention_mask[..., np.newaxis]
    pooled = (hidden * mask).sum(axis=1) / mask.sum(axis=1).clip(min=1e-9)
    norms = np.linalg.norm(pooled, axis=1, keepdims=True).clip(min=1e-9)
    return (pooled / norms).astype(np.float32)


def embed_single(text: str) -> np.ndarray:
    """Embed a single text → (EMBED_DIM,) L2-normalized float32 array."""
    return embed([text])[0]


def warmup() -> None:
    embed(["warmup"])
