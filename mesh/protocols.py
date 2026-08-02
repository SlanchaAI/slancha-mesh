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


@dataclass(frozen=True)
class MultipartProtocol(JsonProtocol):
    """One code-owned multipart request protocol with explicit part limits."""

    max_file_bytes: int = 25 * _MIB
    max_field_bytes: int = 64 * 1024
    max_files: int = 1
    max_fields: int = 16
    allowed_fields: tuple[str, ...] = ()
    allowed_file_fields: tuple[str, ...] = ()
    required_file_fields: tuple[str, ...] = ()
    allowed_file_media_types: tuple[str, ...] = ()
    response_media_types_by_format: tuple[tuple[str, tuple[str, ...]], ...] = ()
    default_response_format: str | None = None
    request_encoding: str = "multipart"

    def success_media_types_for(self, response_format: str | None) -> tuple[str, ...]:
        """Return the exact response allowlist for the requested output mode."""

        selected = response_format or self.default_response_format
        if self.response_media_types_by_format:
            for name, media_types in self.response_media_types_by_format:
                if name == selected:
                    return media_types
            return ()
        return self.success_media_types


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


MULTIPART_PROTOCOLS = (
    MultipartProtocol(
        protocol_id="openai.images.edits.v1",
        public_path="/v1/images/edits",
        upstream_path="/v1/images/edits",
        max_request_bytes=64 * _MIB,
        max_response_bytes=8 * _MIB,
        success_media_types=("application/json",),
        max_file_bytes=32 * _MIB,
        max_field_bytes=64 * 1024,
        max_files=17,
        max_fields=32,
        allowed_fields=(
            "model",
            "prompt",
            "n",
            "size",
            "response_format",
            "user",
            "background",
            "input_fidelity",
            "output_compression",
            "output_format",
            "quality",
        ),
        allowed_file_fields=("image", "mask"),
        required_file_fields=("image",),
        allowed_file_media_types=("image/jpeg", "image/png", "image/webp"),
    ),
    MultipartProtocol(
        protocol_id="openai.audio.transcriptions.v1",
        public_path="/v1/audio/transcriptions",
        upstream_path="/v1/audio/transcriptions",
        max_request_bytes=26 * _MIB,
        max_response_bytes=4 * _MIB,
        success_media_types=(
            "application/json",
            "application/x-subrip",
            "text/plain",
            "text/vtt",
        ),
        max_file_bytes=25 * _MIB,
        max_field_bytes=64 * 1024,
        max_files=1,
        max_fields=16,
        allowed_fields=(
            "model",
            "language",
            "prompt",
            "response_format",
            "temperature",
            "timestamp_granularities[]",
            "include[]",
        ),
        allowed_file_fields=("file",),
        required_file_fields=("file",),
        allowed_file_media_types=(
            "application/octet-stream",
            "audio/m4a",
            "audio/mp3",
            "audio/mp4",
            "audio/mpeg",
            "audio/mpga",
            "audio/wav",
            "audio/webm",
            "audio/x-m4a",
            "audio/x-wav",
            "video/mp4",
            "video/webm",
        ),
        response_media_types_by_format=(
            ("json", ("application/json",)),
            ("verbose_json", ("application/json",)),
            ("diarized_json", ("application/json",)),
            ("text", ("text/plain",)),
            ("srt", ("application/x-subrip", "text/plain")),
            ("vtt", ("text/vtt", "text/plain")),
        ),
        default_response_format="json",
    ),
)

MULTIPART_PROTOCOLS_BY_PATH = {
    protocol.public_path: protocol for protocol in MULTIPART_PROTOCOLS
}

if len(MULTIPART_PROTOCOLS_BY_PATH) != len(  # pragma: no cover - import guard
    MULTIPART_PROTOCOLS
):
    raise RuntimeError("duplicate public path in multipart protocol registry")


__all__ = [
    "JSON_PROTOCOLS",
    "JSON_PROTOCOLS_BY_PATH",
    "MULTIPART_PROTOCOLS",
    "MULTIPART_PROTOCOLS_BY_PATH",
    "JsonProtocol",
    "MultipartProtocol",
]
