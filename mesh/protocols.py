"""Trusted multimodal protocol definitions for the mesh router.

Catalog cards advertise support with ``protocol:<protocol_id>`` capability
tokens. Paths and wire formats stay here in source code; node metadata can
select a protocol, but cannot choose an upstream path.
"""

from __future__ import annotations

from dataclasses import dataclass


_MIB = 1024 * 1024


@dataclass(frozen=True)
class JsonProtocol:
    """One code-owned JSON request protocol and its bounded response contract."""

    protocol_id: str
    public_path: str
    upstream_path: str
    max_request_bytes: int
    max_response_bytes: int
    success_media_types: tuple[str, ...]
    require_b64_json: bool = False
    method: str = "POST"
    request_encoding: str = "json"

    @property
    def capability(self) -> str:
        return f"protocol:{self.protocol_id}"


JSON_PROTOCOLS = (
    JsonProtocol(
        protocol_id="openai.images.generations.v1",
        public_path="/v1/images/generations",
        upstream_path="/v1/images/generations",
        max_request_bytes=2 * _MIB,
        max_response_bytes=8 * _MIB,
        success_media_types=("application/json",),
    ),
    JsonProtocol(
        protocol_id="openai.audio.speech.v1",
        public_path="/v1/audio/speech",
        upstream_path="/v1/audio/speech",
        max_request_bytes=2 * _MIB,
        max_response_bytes=32 * _MIB,
        success_media_types=(
            "audio/aac",
            "audio/flac",
            "audio/mpeg",
            "audio/ogg",
            "audio/opus",
            "audio/pcm",
            "audio/wav",
            "audio/x-wav",
            "application/octet-stream",
        ),
    ),
    JsonProtocol(
        protocol_id="vllm_omni.audio.generate.v1",
        public_path="/v1/audio/generate",
        upstream_path="/v1/audio/generate",
        max_request_bytes=2 * _MIB,
        max_response_bytes=128 * _MIB,
        success_media_types=(
            "audio/aac",
            "audio/flac",
            "audio/mpeg",
            "audio/opus",
            "audio/pcm",
            "audio/wav",
            "audio/x-wav",
            "application/octet-stream",
        ),
    ),
    JsonProtocol(
        protocol_id="localai.video.v1",
        public_path="/video",
        upstream_path="/video",
        max_request_bytes=128 * _MIB,
        max_response_bytes=256 * _MIB,
        success_media_types=("application/json",),
        require_b64_json=True,
    ),
)

JSON_PROTOCOLS_BY_PATH = {protocol.public_path: protocol for protocol in JSON_PROTOCOLS}

if len(JSON_PROTOCOLS_BY_PATH) != len(JSON_PROTOCOLS):  # pragma: no cover - import guard
    raise RuntimeError("duplicate public path in JSON protocol registry")


__all__ = ["JSON_PROTOCOLS", "JSON_PROTOCOLS_BY_PATH", "JsonProtocol"]
