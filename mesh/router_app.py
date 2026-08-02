"""OpenAI-compatible router app — drop-in `/v1` endpoint over a mesh of nodes.

The point of this module: a LocalLLaMA user runs `slancha-mesh` on a few
boxes, then points Open WebUI / LiteLLM / their own `curl` at ONE URL.
The router picks the right node behind the scenes — same OpenAI shape
clients already speak, no per-node fan-out, no manual `discover` →
`curl` two-step.

What it does:

  - `GET  /v1/models`            → list every specialist the snapshot
                                   currently knows about, in OpenAI's
                                   list shape.
  - `POST /v1/chat/completions`  → look up `body.model` (a specialist_id),
                                   pick the first reachable binding, proxy
                                   the request to that node's `/v1/chat/
                                   completions`, return the response.

What it does NOT do (yet — incremental scope):

  - Streaming SSE. Phase-1 ships non-streaming only; the response body
    is awaited end-to-end before returning. Followup PR adds
    `text/event-stream` passthrough.
  - Fallback-on-upstream-5xx. Phase-1 returns the upstream error
    verbatim. Followup PR walks the snapshot's secondary bindings.

Classifier-driven routing: pass `auto_router=` (see
`mesh.classifier.auto.build_auto_router`, needs the `classifier` extra)
and `model = "auto"` resolves per-prompt via the classifier +
`mesh.select.select_mesh_route`. Without it, `model` must be an explicit
specialist_id, as before.

Mounting:

    from mesh.router_app import create_router_app
    app = create_router_app(registry=shared_registry)
    # serve standalone via uvicorn, or mount into slancha-api

Auth: same `SLANCHA_NODE_TOKEN` bearer the registry app uses — set the
env var to enforce, unset for dev. When set, the router also forwards
the bearer to the upstream node's `/v1/chat/completions` (the node may
be behind its own token gate).

Observability: every routed response carries three response headers so a
client can audit what the router picked without parsing logs:

  - `X-Slancha-Specialist`: the catalog id served
  - `X-Slancha-Node`:       the node id picked
  - `X-Slancha-Reason`:     short human string ("primary, queue=120ms")
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import math
import os
import sqlite3
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Callable, Protocol
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from python_multipart.exceptions import MultipartParseError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from mesh.discovery import DiscoveryResult
from mesh.escalation import PuntCode, punt_response
from mesh.job_store import (
    DEFAULT_VIDEO_JOB_DB,
    VideoDeleteClaim,
    VideoJob,
    VideoJobStore,
)
from mesh.models import (
    MeshSelectionResult,
    NodeBinding,
    NodeSummary,
    RegistrySnapshot,
    SpecialistCard,
)
from mesh.protocols import (
    JSON_PROTOCOLS,
    MULTIPART_PROTOCOLS,
    VIDEO_JOB_PROTOCOL,
    JsonProtocol,
    MultipartProtocol,
)
from mesh.registry import MeshRegistry
from mesh.runtime_health import RouterRuntimeHealth, binding_key
from mesh.usage import (
    MAX_TAIL_BYTES,
    NullSink,
    UsageSink,
    build_usage_event,
    parse_response_body,
    parse_stream_usage,
    safe_emit,
)

NODE_TOKEN_ENV = "SLANCHA_NODE_TOKEN"
UPSTREAM_TIMEOUT_S = 120.0  # generous; covers a cold Ollama load on the upstream
# DoS bounds (#101). Streaming completions can run for minutes (long generations,
# slow models), so there's no *overall* read cap — but `read` is the per-chunk
# IDLE timeout: a slow-loris upstream that emits no byte for this long is dropped
# instead of pinning a connection/task forever. `_IDLE_READ_S` env-overridable.
_IDLE_READ_S = float(os.environ.get("SLANCHA_ROUTER_IDLE_READ_S", "60"))
DEFAULT_STREAMING_TIMEOUT = httpx.Timeout(connect=10.0, read=_IDLE_READ_S, write=10.0, pool=10.0)
# Reject an oversized request body before buffering it (a 1GB POST would be read
# into memory then re-serialized to N upstreams). Env-overridable.
MAX_REQUEST_BYTES = int(os.environ.get("SLANCHA_ROUTER_MAX_BODY_BYTES", str(8 * 1024 * 1024)))
# Cap how many nodes one client request fans out to (amplification / token
# double-spend / node-enumeration bound).
MAX_FALLBACK_ATTEMPTS = int(os.environ.get("SLANCHA_ROUTER_MAX_FALLBACK", "3"))

_log = logging.getLogger(__name__)

SnapshotSource = Callable[[], RegistrySnapshot]

#: The pseudo-model id that triggers classifier-driven routing.
AUTO_MODEL_ID = "auto"


class AutoRouterLike(Protocol):
    """What `create_router_app(auto_router=...)` needs: request body +
    snapshot in, selection result out. `mesh.classifier.auto.AutoRouter`
    is the production impl; tests inject fakes."""

    def select(
        self, body: dict, snapshot: RegistrySnapshot
    ) -> MeshSelectionResult:  # pragma: no cover - protocol
        ...


class LocalRoutesExhausted(Exception):
    """Every request-ready local binding failed before response bytes."""

    def __init__(self, *, attempts: int, detail: str) -> None:
        super().__init__(detail)
        self.attempts = attempts
        self.detail = detail


# ---------------------------------------------------------------------------
# Auth (mirror of mesh.registry_app.verify_node_token so the router can be
# mounted standalone without dragging the registry_app dependency)
# ---------------------------------------------------------------------------


def _expected_token() -> str | None:
    tok = os.environ.get(NODE_TOKEN_ENV, "").strip()
    return tok or None


def verify_router_token(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Validate Bearer token against `SLANCHA_NODE_TOKEN`.

    Returns silently if auth is disabled OR the token matches.
    Raises 401 on missing / malformed header, 403 on wrong token.
    """
    expected = _expected_token()
    if expected is None:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or malformed Authorization header",
            headers={"WWW-Authenticate": 'Bearer realm="slancha-mesh-router"'},
        )
    received = authorization[len("Bearer ") :].strip()
    if not hmac.compare_digest(received, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="invalid bearer token",
        )


# ---------------------------------------------------------------------------
# Routing helpers — pure, testable in isolation from FastAPI
# ---------------------------------------------------------------------------


def _reachable_bindings(
    specialist_id: str,
    snapshot: RegistrySnapshot,
) -> list[NodeBinding]:
    """Return every reachable `NodeBinding` for the specialist, in order.

    "Reachable" = health is not `unreachable` AND `node_url` is set.
    The snapshot's binding order reflects insertion order (heartbeat
    arrival), which is deterministic per replay. Callers use the head
    as the primary and walk down the tail as the fallback chain — when
    the primary returns a 5xx or connect-fails, the next binding is
    tried, and so on until one succeeds OR the list is exhausted.

    A future PR can rank by quality / queue depth / p95 using
    `MeshSelectionResult` semantics directly; today the order is a
    first-good-wins shape.
    """
    out: list[NodeBinding] = []
    for b in snapshot.specialists.get(specialist_id) or []:
        if b.health == "unreachable":
            continue
        if not b.node_url:
            continue
        out.append(b)
    return out


def _runtime_ready_snapshot(
    snapshot: RegistrySnapshot,
    runtime_health: RouterRuntimeHealth,
) -> RegistrySnapshot:
    """Filter open bindings before automatic specialist/node selection."""

    specialists = {
        specialist_id: [
            binding
            for binding in _reachable_bindings(specialist_id, snapshot)
            if runtime_health.peek_routable(
                binding_key(specialist_id, binding.node_id)
            )
        ]
        for specialist_id in snapshot.specialists
    }
    specialists = {
        specialist_id: bindings
        for specialist_id, bindings in specialists.items()
        if bindings
    }
    return snapshot.model_copy(
        update={"specialists": specialists, "ranked_routes": {}}
    )


# Statuses worth retrying on. 5xx = upstream service problem (worth trying
# another node); 4xx = client error (retrying changes nothing — same body
# would be rejected by the next node too). 2xx / 3xx obviously don't retry.
_RETRY_UPSTREAM_STATUSES = frozenset({502, 503, 504})


def _is_retriable_status(status_code: int) -> bool:
    return status_code in _RETRY_UPSTREAM_STATUSES


def _rewrite_model_for_upstream(
    body: dict,
    specialist_id: str,
    snapshot: RegistrySnapshot,
) -> dict:
    """If the chosen specialist's backend is Ollama, swap `model` → `ollama_tag`.

    The mesh-facing model id is the catalog `specialist_id` (`qwen2.5-coder
    -7b-q4-ollama`); Ollama's `/v1/chat/completions` needs the engine tag
    (`qwen2.5-coder:7b-instruct-q4_K_M`). For vLLM specialists the
    upstream's `--served-model-name` is already set to `specialist_id`
    (see `mesh.backends.VLLMBackend.start`), so no rewrite needed.

    Returns a shallow copy of `body` so the caller's dict isn't mutated.
    """
    card = snapshot.catalog.get(specialist_id)
    if card is None:
        return dict(body)
    if card.required_backend == "ollama" and card.ollama_tag:
        return {**body, "model": card.ollama_tag}
    if card.required_backend == "external" and card.served_model_name:
        # Adopted endpoint serving under a name the mesh doesn't control
        # (e.g. an external vLLM launched with its own --served-model-name).
        # Gated on the external backend: mesh-spawned vLLM serves under the
        # specialist_id, so an alias there would 404 every request.
        return {**body, "model": card.served_model_name}
    return dict(body)


def _upstream_headers() -> dict[str, str]:
    """Headers to forward to the upstream node.

    SECURITY (#99): never relay the *client's* Authorization to upstream nodes.
    The fallback chain tries multiple nodes, so a single malicious/compromised
    node could return a retriable 5xx to force fallback and harvest every
    caller's bearer (or a real upstream API key passed through by an OpenAI
    client). If the upstream nodes require a credential, configure a SEPARATE
    one via ``SLANCHA_UPSTREAM_TOKEN`` (sent as a Bearer); otherwise no
    Authorization is sent (local inference servers typically need none).
    """
    h = {"Content-Type": "application/json"}
    upstream = os.environ.get("SLANCHA_UPSTREAM_TOKEN", "").strip()
    if upstream:
        h["Authorization"] = f"Bearer {upstream}"
    return h


# Media types the router will relay from an upstream node to the client. A
# malicious/compromised node could otherwise set content-type: text/html (XSS in
# a browser client) or a sniffable type; we only pass known OpenAI-shaped types
# and fall back to a safe default otherwise (#109).
_ALLOWED_MEDIA_TYPES = ("application/json", "text/event-stream", "text/plain")


def _safe_media_type(raw: str | None, default: str) -> str:
    if not raw:
        return default
    base = raw.split(";", 1)[0].strip().lower()  # drop any ;charset=...
    return raw if base in _ALLOWED_MEDIA_TYPES else default


def _base_media_type(raw: str | None) -> str:
    """Normalize a response media type for exact protocol allowlisting."""

    if not raw:
        return ""
    return raw.split(";", 1)[0].strip().lower()


def _validated_node_origin(node_url: str | None) -> str | None:
    """Return a strict HTTP(S) origin, rejecting path/query-controlled targets."""

    if not node_url:
        return None
    parts = urlsplit(node_url)
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        return None
    if (
        not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
        or any(char.isspace() for char in parts.hostname)
    ):
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    host = parts.hostname
    if ":" in host:
        host = f"[{host}]"
    port_suffix = f":{port}" if port is not None else ""
    return f"{scheme}://{host}{port_suffix}"


async def _read_bounded_request(request: Request, *, max_bytes: int) -> bytes:
    """Consume an ASGI request incrementally and stop before exceeding its cap."""

    content = bytearray()
    async for chunk in request.stream():
        if len(chunk) > max_bytes - len(content):
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"request body exceeds {max_bytes} bytes",
            )
        content.extend(chunk)
    return bytes(content)


async def _read_bounded_response(
    response: httpx.Response,
    *,
    max_bytes: int,
) -> bytes:
    """Read one upstream response without trusting its Content-Length."""

    declared = response.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > max_bytes:
        raise ValueError(f"response body exceeds {max_bytes} bytes")
    content = bytearray()
    async for chunk in response.aiter_bytes():
        content.extend(chunk)
        if len(content) > max_bytes:
            raise ValueError(f"response body exceeds {max_bytes} bytes")
    return bytes(content)


def _media_audit_headers(
    *,
    protocol: JsonProtocol,
    binding: NodeBinding,
    reason: str,
) -> dict[str, str]:
    return {
        "X-Slancha-Specialist": binding.specialist_id,
        "X-Slancha-Node": binding.node_id,
        "X-Slancha-Reason": f"protocol={protocol.protocol_id}; {reason}",
    }


def _has_only_inline_video_data(content: bytes) -> bool:
    """Require non-empty LocalAI video data with no node-relative URL fields."""

    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        return False
    return all(
        isinstance(item, dict)
        and "url" not in item
        and isinstance(item.get("b64_json"), str)
        and bool(item["b64_json"])
        for item in data
    )


async def _proxy_json_media(
    client: httpx.AsyncClient,
    *,
    protocol: JsonProtocol,
    binding: NodeBinding,
    node_origin: str,
    specialist_id: str,
    upstream_body: dict,
    runtime_health: RouterRuntimeHealth,
) -> Response:
    """Send one non-idempotent media request to exactly one selected node."""

    key = binding_key(specialist_id, binding.node_id)
    upstream_url = f"{node_origin}{protocol.upstream_path}"
    started_at = time.perf_counter()
    try:
        async with client.stream(
            protocol.method,
            upstream_url,
            json=upstream_body,
            headers=_upstream_headers(),
            follow_redirects=False,
        ) as upstream:
            if 300 <= upstream.status_code < 400:
                runtime_health.record_failure(key, f"http_{upstream.status_code}")
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="upstream media redirects are not accepted",
                    headers=_media_audit_headers(
                        protocol=protocol,
                        binding=binding,
                        reason="redirect_rejected",
                    ),
                )

            media_type = _base_media_type(upstream.headers.get("content-type"))
            accepted = (
                protocol.success_media_types
                if upstream.status_code < 400
                else ("application/json", "text/plain")
            )
            if media_type not in accepted:
                runtime_health.record_failure(key, "unexpected_media_type")
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=(
                        "upstream returned unexpected content type "
                        f"{media_type or '<missing>'!r} for {protocol.protocol_id}"
                    ),
                    headers=_media_audit_headers(
                        protocol=protocol,
                        binding=binding,
                        reason="unexpected_media_type",
                    ),
                )
            try:
                content = await _read_bounded_response(
                    upstream,
                    max_bytes=protocol.max_response_bytes,
                )
            except ValueError as exc:
                runtime_health.record_failure(key, "response_too_large")
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=str(exc),
                    headers=_media_audit_headers(
                        protocol=protocol,
                        binding=binding,
                        reason="response_too_large",
                    ),
                ) from exc
            if (
                upstream.status_code < 300
                and protocol.require_b64_json
                and not _has_only_inline_video_data(content)
            ):
                runtime_health.record_failure(key, "invalid_inline_video")
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="LocalAI video response lacks safe inline b64_json data",
                    headers=_media_audit_headers(
                        protocol=protocol,
                        binding=binding,
                        reason="invalid_inline_video",
                    ),
                )
    except HTTPException:
        raise
    except (httpx.HTTPError, OSError) as exc:
        runtime_health.record_failure(key, exc.__class__.__name__)
        _log.warning(
            "[router] media upstream %s for %s failed after selection: %s",
            binding.node_id,
            protocol.protocol_id,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"selected local node failed for {protocol.protocol_id}; "
                "request was not retried"
            ),
            headers=_media_audit_headers(
                protocol=protocol,
                binding=binding,
                reason="transport_failure",
            ),
        ) from exc

    latency_ms = int((time.perf_counter() - started_at) * 1000)
    if upstream.status_code >= 500:
        runtime_health.record_failure(key, f"http_{upstream.status_code}")
    elif upstream.status_code >= 400:
        runtime_health.record_client_error()
    else:
        runtime_health.record_success(key, latency_ms)
    return Response(
        content=content,
        status_code=upstream.status_code,
        media_type=media_type,
        headers=_media_audit_headers(
            protocol=protocol,
            binding=binding,
            reason=(
                "primary; "
                f"queue_depth={binding.queue_depth} "
                f"p95={binding.p95_latency_ms_60s}"
            ),
        ),
    )


@dataclass(frozen=True)
class _MultipartPart:
    name: str
    value: str | bytes
    filename: str | None = None
    content_type: str | None = None


def _safe_multipart_filename(filename: str | None) -> str | None:
    """Accept a leaf filename with no controls or path semantics."""

    if (
        not filename
        or filename in {".", ".."}
        or len(filename.encode("utf-8")) > 255
        or "/" in filename
        or "\\" in filename
        or any(ord(char) < 32 or ord(char) == 127 for char in filename)
    ):
        return None
    return filename


def _multipart_upstream_headers() -> dict[str, str]:
    """Build node-only auth headers; httpx supplies a fresh multipart boundary."""

    upstream = os.environ.get("SLANCHA_UPSTREAM_TOKEN", "").strip()
    if upstream:
        return {"Authorization": f"Bearer {upstream}"}
    return {}


async def _parse_bounded_multipart(
    request: Request,
    *,
    protocol: MultipartProtocol,
) -> list[_MultipartPart]:
    """Structurally parse one bounded multipart body and materialize safe parts."""

    declared = request.headers.get("content-length")
    if (
        declared is not None
        and declared.isdigit()
        and int(declared) > protocol.max_request_bytes
    ):
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"request body exceeds {protocol.max_request_bytes} bytes",
        )
    raw = await _read_bounded_request(request, max_bytes=protocol.max_request_bytes)

    async def _body_stream():
        yield raw

    parser = MultiPartParser(
        Headers({"content-type": request.headers["content-type"]}),
        _body_stream(),
        max_files=protocol.max_files,
        max_fields=protocol.max_fields,
        max_part_size=protocol.max_field_bytes,
    )
    try:
        form = await parser.parse()
    except MultiPartException as exc:
        detail = str(exc)
        limit_error = detail.startswith(("Part exceeded", "Too many"))
        raise HTTPException(
            status_code=(
                status.HTTP_413_CONTENT_TOO_LARGE
                if limit_error
                else status.HTTP_400_BAD_REQUEST
            ),
            detail=f"invalid multipart body: {detail}",
        ) from exc
    except (MultipartParseError, KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid multipart body: {exc}",
        ) from exc

    parts: list[_MultipartPart] = []
    try:
        for name, value in form.multi_items():
            if isinstance(value, UploadFile):
                if name not in protocol.allowed_file_fields:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"unsupported multipart file field {name!r}",
                    )
                filename = _safe_multipart_filename(value.filename)
                if filename is None:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"unsafe multipart filename for field {name!r}",
                    )
                content_type = _base_media_type(value.content_type)
                if content_type not in protocol.allowed_file_media_types:
                    raise HTTPException(
                        status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                        detail=(
                            f"unsupported file content type {content_type or '<missing>'!r} "
                            f"for field {name!r}"
                        ),
                    )
                content = await value.read(protocol.max_file_bytes + 1)
                if len(content) > protocol.max_file_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                        detail=(
                            f"multipart file {name!r} exceeds "
                            f"{protocol.max_file_bytes} bytes"
                        ),
                    )
                parts.append(
                    _MultipartPart(
                        name=name,
                        value=content,
                        filename=filename,
                        content_type=content_type,
                    )
                )
                continue

            if name not in protocol.allowed_fields:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"unsupported multipart field {name!r}",
                )
            if len(value.encode("utf-8")) > protocol.max_field_bytes:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail=(
                        f"multipart field {name!r} exceeds "
                        f"{protocol.max_field_bytes} bytes"
                    ),
                )
            parts.append(_MultipartPart(name=name, value=value))
    finally:
        await form.close()

    return parts


def _multipart_outbound_parts(
    parts: list[_MultipartPart],
    *,
    upstream_model: str,
) -> list[tuple[str, tuple]]:
    """Reconstruct validated multipart parts with a rewritten model alias."""

    outbound: list[tuple[str, tuple]] = []
    for part in parts:
        if part.filename is None:
            value = upstream_model if part.name == "model" else part.value
            outbound.append((part.name, (None, value)))
        else:
            outbound.append(
                (part.name, (part.filename, part.value, part.content_type))
            )
    return outbound


async def _proxy_multipart_media(
    client: httpx.AsyncClient,
    *,
    protocol: MultipartProtocol,
    binding: NodeBinding,
    node_origin: str,
    specialist_id: str,
    parts: list[_MultipartPart],
    upstream_model: str,
    response_format: str | None,
    runtime_health: RouterRuntimeHealth,
) -> Response:
    """Send one validated multipart request to one selected node."""

    key = binding_key(specialist_id, binding.node_id)
    upstream_url = f"{node_origin}{protocol.upstream_path}"
    started_at = time.perf_counter()
    try:
        async with client.stream(
            protocol.method,
            upstream_url,
            files=_multipart_outbound_parts(parts, upstream_model=upstream_model),
            headers=_multipart_upstream_headers(),
            follow_redirects=False,
        ) as upstream:
            if 300 <= upstream.status_code < 400:
                runtime_health.record_failure(key, f"http_{upstream.status_code}")
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="upstream media redirects are not accepted",
                    headers=_media_audit_headers(
                        protocol=protocol,
                        binding=binding,
                        reason="redirect_rejected",
                    ),
                )

            media_type = _base_media_type(upstream.headers.get("content-type"))
            accepted = (
                protocol.success_media_types_for(response_format)
                if upstream.status_code < 400
                else ("application/json", "text/plain")
            )
            if media_type not in accepted:
                runtime_health.record_failure(key, "unexpected_media_type")
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=(
                        "upstream returned unexpected content type "
                        f"{media_type or '<missing>'!r} for {protocol.protocol_id}"
                    ),
                    headers=_media_audit_headers(
                        protocol=protocol,
                        binding=binding,
                        reason="unexpected_media_type",
                    ),
                )
            try:
                content = await _read_bounded_response(
                    upstream,
                    max_bytes=protocol.max_response_bytes,
                )
            except ValueError as exc:
                runtime_health.record_failure(key, "response_too_large")
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=str(exc),
                    headers=_media_audit_headers(
                        protocol=protocol,
                        binding=binding,
                        reason="response_too_large",
                    ),
                ) from exc
    except HTTPException:
        raise
    except (httpx.HTTPError, OSError) as exc:
        runtime_health.record_failure(key, exc.__class__.__name__)
        _log.warning(
            "[router] multipart upstream %s for %s failed after selection: %s",
            binding.node_id,
            protocol.protocol_id,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"selected local node failed for {protocol.protocol_id}; "
                "request was not retried"
            ),
            headers=_media_audit_headers(
                protocol=protocol,
                binding=binding,
                reason="transport_failure",
            ),
        ) from exc

    latency_ms = int((time.perf_counter() - started_at) * 1000)
    if upstream.status_code >= 500:
        runtime_health.record_failure(key, f"http_{upstream.status_code}")
    elif upstream.status_code >= 400:
        runtime_health.record_client_error()
    else:
        runtime_health.record_success(key, latency_ms)
    response_headers = _media_audit_headers(
        protocol=protocol,
        binding=binding,
        reason=(
            "primary; "
            f"queue_depth={binding.queue_depth} "
            f"p95={binding.p95_latency_ms_60s}"
        ),
    )
    response_headers["content-type"] = upstream.headers["content-type"]
    return Response(
        content=content,
        status_code=upstream.status_code,
        headers=response_headers,
    )


_VIDEO_STATUSES = frozenset({"queued", "in_progress", "completed", "failed"})
_VIDEO_CONTENT_MEDIA_TYPES = frozenset(
    {"application/octet-stream", "video/mp4", "video/webm"}
)
_VIDEO_CONTENT_MAX_BYTES = 512 * 1024 * 1024
_VIDEO_CONTENT_SPOOL_MEMORY_BYTES = 8 * 1024 * 1024
_VIDEO_DELETE_LEASE_S = 300.0
_VIDEO_DELETE_DEADLINE_S = 30.0
_UPSTREAM_VIDEO_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
)


def _valid_public_video_id(value: str) -> bool:
    return (
        len(value) == 38
        and value.startswith("video_")
        and all(char in "0123456789abcdef" for char in value[6:])
    )


def _parse_video_job_payload(content: bytes) -> tuple[dict[str, Any], str, str]:
    """Validate an upstream job record and retain only path-free metadata."""

    try:
        payload = json.loads(content)
    except (TypeError, ValueError) as exc:
        raise ValueError("upstream video job response is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("upstream video job response must be an object")

    upstream_id = payload.get("id")
    if (
        not isinstance(upstream_id, str)
        or not upstream_id
        or upstream_id in {".", ".."}
        or len(upstream_id) > 1024
        or any(char not in _UPSTREAM_VIDEO_ID_CHARS for char in upstream_id)
    ):
        raise ValueError("upstream video job response has an invalid id")
    job_status = payload.get("status")
    if not isinstance(job_status, str) or job_status not in _VIDEO_STATUSES:
        raise ValueError("upstream video job response has an invalid status")

    safe: dict[str, Any] = {"status": job_status}
    object_type = payload.get("object")
    if object_type is not None:
        if object_type != "video":
            raise ValueError("upstream video job response has an invalid object type")
        safe["object"] = "video"
    for field, limit in (
        ("size", 64),
        ("seconds", 32),
        ("quality", 64),
        ("media_type", 128),
    ):
        value = payload.get(field)
        if isinstance(value, str) and len(value) <= limit:
            safe[field] = value
    progress = payload.get("progress")
    if isinstance(progress, int) and not isinstance(progress, bool) and 0 <= progress <= 100:
        safe["progress"] = progress
    for field in ("created_at", "completed_at", "expires_at"):
        value = payload.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            safe[field] = value
    for field in ("inference_time_s", "peak_memory_mb"):
        value = payload.get(field)
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value >= 0
        ):
            safe[field] = value
    error = payload.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        message = error.get("message")
        safe_error: dict[str, Any] = {}
        if isinstance(code, (int, str)) and not isinstance(code, bool):
            safe_error["code"] = str(code)[:128]
        if isinstance(message, str):
            safe_error["message"] = message.replace("\r", " ").replace("\n", " ")[:512]
        if safe_error:
            safe["error"] = safe_error
    return safe, upstream_id, job_status


def _video_audit_headers(binding: NodeBinding, reason: str) -> dict[str, str]:
    return _media_audit_headers(
        protocol=VIDEO_JOB_PROTOCOL,
        binding=binding,
        reason=reason,
    )


def _video_owner_punt(
    *,
    specialist_id: str,
    node_id: str,
    reason: str,
    local_attempts: int,
) -> JSONResponse:
    response = punt_response(
        code=PuntCode.LOCAL_ROUTE_UNAVAILABLE,
        message="The recorded local owner for this video job is unavailable.",
        reason=reason,
        local_attempts=local_attempts,
        retryable=True,
    )
    response.headers.update(
        {
            "X-Slancha-Specialist": specialist_id,
            "X-Slancha-Node": node_id,
            "X-Slancha-Reason": (
                f"protocol={VIDEO_JOB_PROTOCOL.protocol_id}; {reason}"
            ),
        }
    )
    response.headers["X-Slancha-Outcome"] = "punt"
    return response


def _recorded_video_owner(
    job: VideoJob,
    snapshot: RegistrySnapshot,
) -> NodeBinding | None:
    for binding in snapshot.specialists.get(job.specialist_id) or []:
        if binding.node_id != job.owner_node_id or binding.health == "unreachable":
            continue
        if _validated_node_origin(binding.node_url) == job.owner_origin:
            return binding
    return None


def _video_upstream_error(
    *,
    binding: NodeBinding,
    status_code: int,
    reason: str,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "type": "upstream_video_error",
                "message": "The recorded video owner rejected the request.",
            }
        },
        headers=_video_audit_headers(binding, reason),
    )


def _video_preflight_punt(
    response: JSONResponse,
    *,
    specialist_id: str,
    reason: str,
) -> JSONResponse:
    response.headers.update(
        {
            "X-Slancha-Specialist": specialist_id,
            "X-Slancha-Node": "unselected",
            "X-Slancha-Reason": (
                f"protocol={VIDEO_JOB_PROTOCOL.protocol_id}; {reason}"
            ),
        }
    )
    return response


# ---------------------------------------------------------------------------
# Discovery → snapshot translation — lets the router run without a registry
# ---------------------------------------------------------------------------


def _node_id_for_url(url: str) -> str:
    """Stable node id derived from a `node_url` (host[:port]).

    The pull discovery has no real node ids — peers are just hosts that
    answered `/models`. We synthesize a deterministic id so `NodeBinding`s
    have something to put in `node_id` (and the routing-audit header
    surfaces something human-meaningful).
    """
    parts = urlsplit(url)
    host = parts.hostname or "unknown"
    if parts.port:
        return f"{host}:{parts.port}"
    return host


def discovery_to_snapshot(
    discovery: DiscoveryResult,
    *,
    catalog: list[SpecialistCard] | None = None,
) -> RegistrySnapshot:
    """Translate a pull-discovery result into a `RegistrySnapshot`.

    The router needs a snapshot for `_reachable_bindings` + `_rewrite_model_for_
    upstream`. In push mode the snapshot comes from `MeshRegistry.snapshot()`;
    in pull mode we synthesize one from `discover_specialists`. Bindings
    are minimal — we know the URL is currently reachable (we just GET'd
    `/models`) but not the queue depth or p95 latency, which only the
    heartbeat-push topology carries.

    `catalog` is the LOCAL catalog (`load_catalog()`); it provides
    `ollama_tag` / `required_backend` per specialist so the router's
    upstream-model-rewrite path works. Specialists in the discovery
    result that aren't in the local catalog still route (no rewrite —
    `model = specialist_id` flows through unchanged).
    """
    now = datetime.now(timezone.utc)
    cards_by_id = {c.specialist_id: c for c in (catalog or [])}

    bindings_by_specialist: dict[str, list[NodeBinding]] = {}
    nodes: dict[str, NodeSummary] = {}

    for sid, spec in discovery.specialists.items():
        for url in spec.node_urls:
            node_id = _node_id_for_url(url)
            binding = NodeBinding(
                node_id=node_id,
                specialist_id=sid,
                health="healthy",
                queue_depth=0,
                p95_latency_ms_60s=None,
                node_url=url,
                last_seen=now,
            )
            bindings_by_specialist.setdefault(sid, []).append(binding)
            # Multiple specialists may live behind one node URL; keep the
            # first NodeSummary we synthesize (they all look the same here).
            nodes.setdefault(
                node_id,
                NodeSummary(
                    node_id=node_id,
                    friendly_name=node_id,
                    health="healthy",
                    last_seen=now,
                    loaded_specialist_ids=[sid],
                    queue_depth=0,
                    p95_latency_ms_60s=None,
                    node_url=url,
                ),
            )

    return RegistrySnapshot(
        snapshot_ts=now,
        nodes=nodes,
        specialists=bindings_by_specialist,
        coverage={},
        ranked_routes={},
        catalog=cards_by_id,
    )


class _RefreshingSnapshot:
    """Thread-safe snapshot holder + background refresher for pull mode.

    The router's `snapshot_source` is called per request. Calling
    `discover_specialists` per request would hammer every peer's
    `/models` endpoint with the same frequency as inbound traffic
    (potentially many qps); instead, we keep a cached snapshot and
    refresh it on a fixed cadence (default 5 s, matching the
    heartbeat interval).

    Start the refresher with `.start()`; stop with `.stop()`. The
    background thread is daemonized so a process exit doesn't deadlock
    on it.
    """

    def __init__(
        self,
        refresher: Callable[[], DiscoveryResult],
        *,
        catalog: list[SpecialistCard] | None = None,
        refresh_s: float = 5.0,
        retain_s: float | None = None,
    ) -> None:
        self._refresher = refresher
        self._catalog = list(catalog or [])
        self._refresh_s = refresh_s
        # Staleness tolerance for the whole-snapshot swap. A discovery pass that
        # transiently misses a peer (fetch timeout, node mid-restart, or a
        # specialist whose node_url isn't populated yet during staggered
        # warm-up) would otherwise evict that specialist from the fresh
        # snapshot, 404ing a live route for up to one refresh cycle. We retain a
        # vanished binding until it is `retain_s` old so a single missed pass
        # can't drop it. Default = 2 cycles; env-tunable; 0 restores the strict
        # drop-immediately behaviour.
        env_retain = os.environ.get("SLANCHA_ROUTER_BINDING_RETAIN_S")
        if retain_s is not None:
            self._retain_s = retain_s
        elif env_retain not in (None, ""):
            self._retain_s = float(env_retain)  # type: ignore[arg-type]
        else:
            self._retain_s = 2.0 * refresh_s
        self._lock = threading.Lock()
        self._snapshot: RegistrySnapshot | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def get(self) -> RegistrySnapshot:
        """Return the latest cached snapshot; refresh inline if none yet."""
        with self._lock:
            if self._snapshot is not None:
                return self._snapshot
        # Cold start — do a synchronous refresh under no lock so a
        # concurrent .get() can still serve a slightly older snapshot
        # later if the background loop has started by then.
        snap = discovery_to_snapshot(self._refresher(), catalog=self._catalog)
        with self._lock:
            self._snapshot = snap
        return snap

    def refresh_once(self) -> None:
        """Run one discovery pass + swap the snapshot under the lock.

        The swap is staleness-tolerant: bindings the prior snapshot had that
        this pass transiently missed are carried over until they are
        `retain_s` old, so one missed discovery pass can't 404 a live route.
        """
        fresh = discovery_to_snapshot(self._refresher(), catalog=self._catalog)
        with self._lock:
            self._snapshot = self._merge_retaining_recent(self._snapshot, fresh)

    def _merge_retaining_recent(
        self, prior: RegistrySnapshot | None, fresh: RegistrySnapshot
    ) -> RegistrySnapshot:
        """Fresh discovery wins; carry over prior bindings it transiently missed.

        A prior ``(specialist_id, node_url)`` binding absent from ``fresh`` is
        retained iff it was last seen within ``retain_s`` — bridging a single
        missed discovery pass. Genuinely-gone nodes age out (their ``last_seen``
        stops advancing once discovery no longer reports them); until then the
        chat fallback chain skips a retained-but-dead node on connect error, so
        the cost of retention is a bounded connect attempt, not a wrong answer.
        """
        if prior is None or self._retain_s <= 0:
            return fresh
        cutoff = fresh.snapshot_ts - timedelta(seconds=self._retain_s)
        fresh_keys = {
            (sid, b.node_url)
            for sid, bindings in fresh.specialists.items()
            for b in bindings
        }
        merged = {sid: list(bindings) for sid, bindings in fresh.specialists.items()}
        merged_nodes = dict(fresh.nodes)
        retained = 0
        for sid, bindings in prior.specialists.items():
            for b in bindings:
                if (sid, b.node_url) in fresh_keys or b.last_seen < cutoff:
                    continue
                merged.setdefault(sid, []).append(b)
                retained += 1
                if b.node_id not in merged_nodes and b.node_id in prior.nodes:
                    merged_nodes[b.node_id] = prior.nodes[b.node_id]
        if retained == 0:
            return fresh
        _log.debug(
            "[router] retained %d recent binding(s) a discovery pass missed", retained
        )
        return RegistrySnapshot(
            snapshot_ts=fresh.snapshot_ts,
            nodes=merged_nodes,
            specialists=merged,
            coverage=fresh.coverage,
            ranked_routes=fresh.ranked_routes,
            catalog=fresh.catalog,
        )

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh_once()
            except Exception as exc:  # noqa: BLE001 — refresh must not crash the router
                _log.warning("[router] discovery refresh failed: %s", exc)
            # Wait either for the refresh interval OR an early stop.
            self._stop.wait(timeout=self._refresh_s)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)


# ---------------------------------------------------------------------------
# Streaming proxy — SSE passthrough
# ---------------------------------------------------------------------------


async def _proxy_stream_with_fallback(
    client: httpx.AsyncClient,
    *,
    bindings: list[NodeBinding],
    specialist_id: str,
    upstream_body: dict,
    sink: UsageSink,
    runtime_health: RouterRuntimeHealth,
    user_field: Any = None,
) -> StreamingResponse:
    """Open a streaming request, falling through the binding chain on failure.

    OpenAI chat-completions SSE is a sequence of `data: {...}\\n\\n` lines
    ending in `data: [DONE]\\n\\n`. Both vLLM and Ollama emit it natively;
    we pass bytes through unchanged.

    **Retry boundary is "before first byte forwarded":** if opening the
    stream raises (`httpx.HTTPError` / `OSError`) or the upstream replies
    with a retriable 5xx status (502 / 503 / 504), we close + try the
    next binding. Once we start yielding bytes from `aiter_bytes()`,
    we're stuck with that binding — retrying mid-stream would emit
    duplicate `delta` chunks downstream, which violates OpenAI's
    streaming contract.

    All bindings exhausted → 502 BAD GATEWAY, mirroring the
    non-streaming path.

    Mid-stream upstream death → the generator raises out, the chunked
    response closes early. Most clients tolerate this as end-of-message;
    we deliberately do NOT inject a synthesized `[DONE]` because that
    could mask a real upstream truncation.
    """
    last_status: int | None = None
    last_detail: str | None = None
    attempts = 0
    for idx, binding in enumerate(bindings):
        key = binding_key(specialist_id, binding.node_id)
        if not runtime_health.is_routable(key):
            continue
        attempts += 1
        upstream_url = f"{binding.node_url.rstrip('/')}/v1/chat/completions"  # type: ignore[union-attr]
        stream_ctx = client.stream(
            "POST",
            upstream_url,
            json=upstream_body,
            headers=_upstream_headers(),
        )
        t0 = time.perf_counter()  # per-attempt; the winner's t0 closes into gen()
        try:
            response = await stream_ctx.__aenter__()
        except (httpx.HTTPError, OSError) as exc:
            last_detail = f"{exc.__class__.__name__} at {upstream_url}"
            runtime_health.record_failure(key, exc.__class__.__name__)
            _log.warning(
                "[router] stream connect to %s for %s failed: %s; trying next",
                binding.node_id,
                specialist_id,
                exc,
            )
            continue
        if _is_retriable_status(response.status_code):
            last_status = response.status_code
            runtime_health.record_failure(key, f"http_{response.status_code}")
            _log.warning(
                "[router] stream upstream %s for %s returned %d; trying next",
                binding.node_id,
                specialist_id,
                response.status_code,
            )
            await stream_ctx.__aexit__(None, None, None)
            continue

        # This binding wins — set up the generator that holds the
        # stream context open until the upstream closes.
        media_type = _safe_media_type(response.headers.get("content-type"), "text/event-stream")
        upstream_status = response.status_code
        if upstream_status >= 500:
            runtime_health.record_failure(key, f"http_{upstream_status}")
        elif upstream_status >= 400:
            runtime_health.record_client_error()
        fallback_fired = idx > 0
        slancha_headers = {
            "X-Slancha-Specialist": specialist_id,
            "X-Slancha-Node": binding.node_id,
            "X-Slancha-Reason": (
                f"primary; queue_depth={binding.queue_depth} "
                f"p95={binding.p95_latency_ms_60s}"
            ),
        }

        async def gen(
            _ctx=stream_ctx,
            _resp=response,
            _t0=t0,
            _status=upstream_status,
            _fallback=fallback_fired,
            _key=key,
        ):
            # Observe-only usage tap: the bytes yielded to the client are NEVER gated on
            # it, and every tap/emit line is guarded — a tap failure can't corrupt or
            # stall the stream. Usage is emitted in `finally` (skipped, honestly, when the
            # caller didn't request `stream_options.include_usage`).
            first_byte_at: float | None = None
            tail = bytearray()
            completed = False
            try:
                async for chunk in _resp.aiter_bytes():
                    if chunk:
                        if first_byte_at is None:
                            first_byte_at = time.perf_counter()
                        try:
                            tail.extend(chunk)
                            if len(tail) > MAX_TAIL_BYTES:
                                del tail[:-MAX_TAIL_BYTES]
                        except Exception:  # noqa: BLE001 — tap must never affect the yield
                            pass
                        yield chunk
                completed = True
            finally:
                await _ctx.__aexit__(None, None, None)
                if completed and _status < 400:
                    runtime_health.record_success(
                        _key,
                        latency_ms=int((time.perf_counter() - _t0) * 1000),
                    )
                elif not completed:
                    runtime_health.record_failure(_key, "stream_interrupted")
                try:
                    latency_ms = int((time.perf_counter() - _t0) * 1000)
                    ttft_ms = int((first_byte_at - _t0) * 1000) if first_byte_at else None
                    ev = build_usage_event(
                        specialist_id=specialist_id,
                        user_field=user_field,
                        status_code=_status,
                        latency_ms=latency_ms,
                        usage=parse_stream_usage(bytes(tail)),
                        ttft_ms=ttft_ms,
                        fallback_fired=_fallback,
                    )
                    safe_emit(sink, ev)
                except Exception as exc:  # noqa: BLE001 — telemetry never breaks a stream
                    _log.warning("stream usage tap failed (%s); skipped", type(exc).__name__)

        return StreamingResponse(
            gen(),
            status_code=upstream_status,
            media_type=media_type,
            headers=slancha_headers,
        )

    # All bindings exhausted.
    detail = (
        f"all {len(bindings)} reachable node(s) failed to open a stream for "
        f"{specialist_id!r}: last_status={last_status} last_error={last_detail}"
    )
    raise LocalRoutesExhausted(attempts=attempts, detail=detail)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_router_app(
    registry: MeshRegistry | None = None,
    *,
    snapshot_source: SnapshotSource | None = None,
    http_client: httpx.AsyncClient | None = None,
    auto_router: AutoRouterLike | None = None,
    usage_sink: UsageSink | None = None,
    runtime_health: RouterRuntimeHealth | None = None,
    video_job_db_path: str | os.PathLike[str] | None = None,
) -> FastAPI:
    """Build the OpenAI-compatible router app.

    Snapshot source resolution:
      - `snapshot_source` (callable returning `RegistrySnapshot`) wins
        when provided — the seam used by tests + the discovery-driven
        deployment shape.
      - Else, `registry.snapshot()` is called per request.
      - Both None → `ValueError`. The router needs *some* way to know
        what's reachable.

    `auto_router` enables classifier-driven routing: when set, requests
    with `model = "auto"` resolve to a specialist per-prompt (see
    `mesh.classifier.auto`), and `"auto"` shows up in `GET /v1/models`.
    When None (the default), `"auto"` 404s with an install hint — no
    behavior change for explicit specialist_ids either way.

    `http_client` is the `httpx.AsyncClient` used for upstream calls.
    Tests inject one wired to `httpx.MockTransport` so no real socket is
    opened. Production passes `None` and the factory builds one with the
    streaming-friendly timeout policy (tight connect/write, no overall
    read cap — a long generation may take minutes).

    Async throughout because (a) handlers can `await client.post()` /
    `async with client.stream(...)` without blocking the event loop —
    the previous sync `Client` inside `async def` serialized outbound
    requests — and (b) SSE passthrough needs an async iterator into
    `StreamingResponse` anyway.
    """
    if snapshot_source is None and registry is None:
        raise ValueError(
            "create_router_app needs either `registry=` (push mode) or "
            "`snapshot_source=` (discovery-driven pull mode); both were None."
        )

    def _snapshot() -> RegistrySnapshot:
        if snapshot_source is not None:
            return snapshot_source()
        # registry is guaranteed non-None here by the ValueError above.
        return registry.snapshot()  # type: ignore[union-attr]

    client = http_client or httpx.AsyncClient(timeout=DEFAULT_STREAMING_TIMEOUT)

    # `usage_sink` is a neutral telemetry seam (mesh/usage.py). Default NullSink →
    # telemetry off, zero behavior change. A real SpoolDrainSink (wired from env in
    # cli.py) is duck-typed drainable: the lifespan starts its background drain task on
    # startup and closes it on shutdown. NullSink has no start/aclose → nothing runs.
    resolved_sink: UsageSink = usage_sink or NullSink()
    resolved_runtime = runtime_health or RouterRuntimeHealth()
    video_store_lock = threading.Lock()
    video_store: VideoJobStore | None = None

    def _video_store() -> VideoJobStore:
        nonlocal video_store
        with video_store_lock:
            if video_store is None:
                video_store = VideoJobStore(video_job_db_path or DEFAULT_VIDEO_JOB_DB)
            return video_store

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        sink = app.state.usage_sink
        if hasattr(sink, "start"):
            try:
                sink.start()
            except Exception as exc:  # noqa: BLE001 — telemetry must never block startup
                _log.warning("[router] usage sink start failed: %s", exc)
        yield
        if hasattr(sink, "aclose"):
            try:
                await sink.aclose()
            except Exception as exc:  # noqa: BLE001
                _log.warning("[router] usage sink aclose failed: %s", exc)

    app = FastAPI(
        title="Slancha-Mesh OpenAI-compatible router",
        version="0.0.8",
        description=(
            "Drop-in OpenAI `/v1` endpoint over a mesh of self-hosted nodes. "
            "Pick `model = <specialist_id>`; the router proxies to the right "
            "node and returns the upstream response."
        ),
        lifespan=_lifespan,
    )
    app.state.registry = registry
    app.state.snapshot_source = _snapshot
    app.state.http_client = client
    app.state.usage_sink = resolved_sink
    app.state.runtime_health = resolved_runtime
    app.state.video_job_db_path = video_job_db_path or DEFAULT_VIDEO_JOB_DB

    @app.get("/v1/models", summary="OpenAI-compatible list of mesh specialists")
    def list_models(
        _: Annotated[None, Depends(verify_router_token)],
    ) -> dict:
        snap = _snapshot()
        ids = sorted(snap.specialists)
        if auto_router is not None and AUTO_MODEL_ID not in ids:
            ids.insert(0, AUTO_MODEL_ID)
        return {
            "object": "list",
            "data": [
                {
                    "id": sid,
                    "object": "model",
                    "owned_by": "slancha-mesh",
                }
                for sid in ids
            ],
        }

    def _media_handler(protocol: JsonProtocol):
        async def json_media(
            request: Request,
            _: Annotated[None, Depends(verify_router_token)],
        ) -> Response:
            content_type = _base_media_type(request.headers.get("content-type"))
            if content_type != "application/json":
                raise HTTPException(
                    status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                    detail=f"{protocol.public_path} accepts application/json",
                )

            declared = request.headers.get("content-length")
            if (
                declared is not None
                and declared.isdigit()
                and int(declared) > protocol.max_request_bytes
            ):
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail=f"request body exceeds {protocol.max_request_bytes} bytes",
                )
            raw = await _read_bounded_request(
                request,
                max_bytes=protocol.max_request_bytes,
            )
            try:
                body = json.loads(raw)
            except Exception as exc:  # noqa: BLE001 — malformed JSON is a 400
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"request body is not valid JSON: {exc}",
                ) from exc
            if not isinstance(body, dict):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="request body must be a JSON object",
                )

            specialist_id = body.get("model")
            if not isinstance(specialist_id, str) or not specialist_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="`model` must be a non-empty specialist_id",
                )
            if protocol.require_b64_json and body.get("response_format") != "b64_json":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="LocalAI video requires response_format=b64_json",
                )

            snapshot = _snapshot()
            if specialist_id not in snapshot.specialists:
                resolved_runtime.record_client_error()
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=(
                        f"unknown specialist {specialist_id!r}; "
                        "check `GET /v1/models` for available model ids."
                    ),
                )
            card = snapshot.catalog.get(specialist_id)
            if card is None or protocol.capability not in card.capabilities:
                resolved_runtime.record_punt()
                return punt_response(
                    code=PuntCode.NO_SUITABLE_LOCAL_ROUTE,
                    message="The requested specialist does not support this media protocol.",
                    reason=(
                        f"specialist {specialist_id!r} lacks capability "
                        f"{protocol.capability!r}"
                    ),
                    local_attempts=0,
                    retryable=True,
                )

            discovered = _reachable_bindings(specialist_id, snapshot)
            if not discovered:
                resolved_runtime.record_punt()
                return punt_response(
                    code=PuntCode.LOCAL_ROUTE_UNAVAILABLE,
                    message="The requested local specialist has no healthy binding.",
                    reason=f"no reachable node for specialist {specialist_id!r}",
                    local_attempts=0,
                    retryable=True,
                )

            selected: tuple[NodeBinding, str] | None = None
            for candidate in discovered:
                node_origin = _validated_node_origin(candidate.node_url)
                if node_origin is None:
                    _log.warning(
                        "[router] ignoring non-origin node URL for media route %s/%s",
                        specialist_id,
                        candidate.node_id,
                    )
                    continue
                if resolved_runtime.is_routable(
                    binding_key(specialist_id, candidate.node_id)
                ):
                    selected = (candidate, node_origin)
                    break
            if selected is None:
                resolved_runtime.record_punt()
                return punt_response(
                    code=PuntCode.LOCAL_ROUTE_UNAVAILABLE,
                    message="The requested local specialist is temporarily unavailable.",
                    reason=(
                        "no request-safe, runtime-ready media binding for "
                        f"specialist {specialist_id!r}"
                    ),
                    local_attempts=0,
                    retryable=True,
                )
            binding, node_origin = selected

            upstream_body = _rewrite_model_for_upstream(
                body,
                specialist_id,
                snapshot,
            )
            return await _proxy_json_media(
                client,
                protocol=protocol,
                binding=binding,
                node_origin=node_origin,
                specialist_id=specialist_id,
                upstream_body=upstream_body,
                runtime_health=resolved_runtime,
            )

        return json_media

    for protocol in JSON_PROTOCOLS:
        app.add_api_route(
            protocol.public_path,
            _media_handler(protocol),
            methods=[protocol.method],
            name=f"media:{protocol.protocol_id}",
            summary=f"Route {protocol.protocol_id} to a capable mesh node",
        )

    def _multipart_media_handler(protocol: MultipartProtocol):
        async def multipart_media(
            request: Request,
            _: Annotated[None, Depends(verify_router_token)],
        ) -> Response:
            content_type = _base_media_type(request.headers.get("content-type"))
            if content_type != "multipart/form-data":
                raise HTTPException(
                    status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                    detail=f"{protocol.public_path} accepts multipart/form-data",
                )

            parts = await _parse_bounded_multipart(request, protocol=protocol)
            model_parts = [
                part.value
                for part in parts
                if part.name == "model" and isinstance(part.value, str)
            ]
            if len(model_parts) != 1 or not model_parts[0].strip():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="multipart body must contain exactly one non-empty `model` field",
                )
            specialist_id = model_parts[0]

            present_file_fields = {
                part.name for part in parts if part.filename is not None
            }
            missing_files = set(protocol.required_file_fields) - present_file_fields
            if missing_files:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        "multipart body lacks required file field(s): "
                        + ", ".join(sorted(missing_files))
                    ),
                )

            response_formats = [
                part.value
                for part in parts
                if part.name == "response_format" and isinstance(part.value, str)
            ]
            if len(response_formats) > 1:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="multipart body may contain at most one `response_format` field",
                )
            response_format = response_formats[0] if response_formats else None
            if not protocol.success_media_types_for(response_format):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"unsupported response_format {response_format!r}",
                )

            snapshot = _snapshot()
            if specialist_id not in snapshot.specialists:
                resolved_runtime.record_client_error()
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=(
                        f"unknown specialist {specialist_id!r}; "
                        "check `GET /v1/models` for available model ids."
                    ),
                )
            card = snapshot.catalog.get(specialist_id)
            if card is None or protocol.capability not in card.capabilities:
                resolved_runtime.record_punt()
                return punt_response(
                    code=PuntCode.NO_SUITABLE_LOCAL_ROUTE,
                    message="The requested specialist does not support this media protocol.",
                    reason=(
                        f"specialist {specialist_id!r} lacks capability "
                        f"{protocol.capability!r}"
                    ),
                    local_attempts=0,
                    retryable=True,
                )

            discovered = _reachable_bindings(specialist_id, snapshot)
            if not discovered:
                resolved_runtime.record_punt()
                return punt_response(
                    code=PuntCode.LOCAL_ROUTE_UNAVAILABLE,
                    message="The requested local specialist has no healthy binding.",
                    reason=f"no reachable node for specialist {specialist_id!r}",
                    local_attempts=0,
                    retryable=True,
                )

            selected: tuple[NodeBinding, str] | None = None
            for candidate in discovered:
                node_origin = _validated_node_origin(candidate.node_url)
                if node_origin is None:
                    _log.warning(
                        "[router] ignoring non-origin node URL for media route %s/%s",
                        specialist_id,
                        candidate.node_id,
                    )
                    continue
                if resolved_runtime.is_routable(
                    binding_key(specialist_id, candidate.node_id)
                ):
                    selected = (candidate, node_origin)
                    break
            if selected is None:
                resolved_runtime.record_punt()
                return punt_response(
                    code=PuntCode.LOCAL_ROUTE_UNAVAILABLE,
                    message="The requested local specialist is temporarily unavailable.",
                    reason=(
                        "no request-safe, runtime-ready media binding for "
                        f"specialist {specialist_id!r}"
                    ),
                    local_attempts=0,
                    retryable=True,
                )
            binding, node_origin = selected

            upstream_model = _rewrite_model_for_upstream(
                {"model": specialist_id},
                specialist_id,
                snapshot,
            )["model"]
            return await _proxy_multipart_media(
                client,
                protocol=protocol,
                binding=binding,
                node_origin=node_origin,
                specialist_id=specialist_id,
                parts=parts,
                upstream_model=upstream_model,
                response_format=response_format,
                runtime_health=resolved_runtime,
            )

        return multipart_media

    for protocol in MULTIPART_PROTOCOLS:
        app.add_api_route(
            protocol.public_path,
            _multipart_media_handler(protocol),
            methods=[protocol.method],
            name=f"media:{protocol.protocol_id}",
            summary=f"Route {protocol.protocol_id} to a capable mesh node",
        )

    async def _load_video_job(public_id: str) -> VideoJob:
        if not _valid_public_video_id(public_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")
        try:
            store = await run_in_threadpool(_video_store)
            job = await run_in_threadpool(store.get, public_id)
        except (OSError, sqlite3.Error, ValueError) as exc:
            _log.error("[router] video owner store unavailable: %s", type(exc).__name__)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="video ownership store is unavailable",
            ) from exc
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")
        if job.protocol_id != VIDEO_JOB_PROTOCOL.protocol_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")
        return job

    async def _claim_video_delete(
        public_id: str,
    ) -> tuple[VideoJobStore, VideoDeleteClaim]:
        if not _valid_public_video_id(public_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")
        try:
            store = await run_in_threadpool(_video_store)
            claim = await run_in_threadpool(
                store.claim_delete,
                public_id,
                lease_s=_VIDEO_DELETE_LEASE_S,
            )
        except (OSError, sqlite3.Error, ValueError) as exc:
            _log.error("[router] video delete claim unavailable: %s", type(exc).__name__)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="video ownership store is unavailable",
            ) from exc
        if claim is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")
        if claim.job.protocol_id != VIDEO_JOB_PROTOCOL.protocol_id:
            await run_in_threadpool(store.release_delete, public_id, claim.token)
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")
        return store, claim

    async def _release_video_delete(
        store: VideoJobStore,
        claim: VideoDeleteClaim,
    ) -> None:
        try:
            await run_in_threadpool(
                store.release_delete,
                claim.job.public_id,
                claim.token,
            )
        except (OSError, sqlite3.Error):
            _log.error("[router] video delete claim release failed")

    @app.post(
        VIDEO_JOB_PROTOCOL.public_path,
        summary="Create an owner-pinned asynchronous video job",
    )
    async def create_video_job(
        request: Request,
        _: Annotated[None, Depends(verify_router_token)],
    ) -> Response:
        if _base_media_type(request.headers.get("content-type")) != "multipart/form-data":
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail="/v1/videos accepts multipart/form-data",
            )
        parts = await _parse_bounded_multipart(request, protocol=VIDEO_JOB_PROTOCOL)
        model_parts = [
            part.value
            for part in parts
            if part.name == "model" and isinstance(part.value, str)
        ]
        prompt_parts = [
            part.value
            for part in parts
            if part.name == "prompt" and isinstance(part.value, str)
        ]
        if len(model_parts) != 1 or not model_parts[0].strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="multipart body must contain exactly one non-empty `model` field",
            )
        if len(prompt_parts) != 1 or not prompt_parts[0].strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="multipart body must contain exactly one non-empty `prompt` field",
            )
        specialist_id = model_parts[0]
        snapshot = _snapshot()
        if specialist_id not in snapshot.specialists:
            resolved_runtime.record_client_error()
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown specialist")
        card = snapshot.catalog.get(specialist_id)
        if card is None or VIDEO_JOB_PROTOCOL.capability not in card.capabilities:
            resolved_runtime.record_punt()
            return _video_preflight_punt(
                punt_response(
                    code=PuntCode.NO_SUITABLE_LOCAL_ROUTE,
                    message="The requested specialist does not support asynchronous video jobs.",
                    reason=(
                        f"specialist {specialist_id!r} lacks capability "
                        f"{VIDEO_JOB_PROTOCOL.capability!r}"
                    ),
                    local_attempts=0,
                    retryable=True,
                ),
                specialist_id=specialist_id,
                reason="capability_missing",
            )
        selected: tuple[NodeBinding, str] | None = None
        for candidate in _reachable_bindings(specialist_id, snapshot):
            origin = _validated_node_origin(candidate.node_url)
            if origin is None:
                continue
            if resolved_runtime.is_routable(binding_key(specialist_id, candidate.node_id)):
                selected = candidate, origin
                break
        if selected is None:
            resolved_runtime.record_punt()
            return _video_preflight_punt(
                punt_response(
                    code=PuntCode.LOCAL_ROUTE_UNAVAILABLE,
                    message="The requested local video specialist is unavailable.",
                    reason=f"no runtime-ready binding for specialist {specialist_id!r}",
                    local_attempts=0,
                    retryable=True,
                ),
                specialist_id=specialist_id,
                reason="no_runtime_ready_binding",
            )
        binding, owner_origin = selected
        upstream_model = _rewrite_model_for_upstream(
            {"model": specialist_id}, specialist_id, snapshot
        )["model"]
        key = binding_key(specialist_id, binding.node_id)
        started_at = time.perf_counter()
        try:
            async with client.stream(
                "POST",
                f"{owner_origin}{VIDEO_JOB_PROTOCOL.upstream_path}",
                files=_multipart_outbound_parts(parts, upstream_model=upstream_model),
                headers=_multipart_upstream_headers(),
                follow_redirects=False,
            ) as upstream:
                if 300 <= upstream.status_code < 400:
                    resolved_runtime.record_failure(key, f"http_{upstream.status_code}")
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail="upstream video redirects are not accepted",
                        headers=_video_audit_headers(binding, "redirect_rejected"),
                    )
                if _base_media_type(upstream.headers.get("content-type")) != "application/json":
                    resolved_runtime.record_failure(key, "unexpected_media_type")
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail="upstream video job response must be application/json",
                        headers=_video_audit_headers(binding, "unexpected_media_type"),
                    )
                try:
                    content = await _read_bounded_response(
                        upstream, max_bytes=VIDEO_JOB_PROTOCOL.max_response_bytes
                    )
                except ValueError as exc:
                    resolved_runtime.record_failure(key, "response_too_large")
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail=str(exc),
                        headers=_video_audit_headers(binding, "response_too_large"),
                    ) from exc
                if upstream.status_code >= 500:
                    resolved_runtime.record_failure(key, f"http_{upstream.status_code}")
                    return _video_owner_punt(
                        specialist_id=specialist_id,
                        node_id=binding.node_id,
                        reason=f"http_{upstream.status_code}",
                        local_attempts=1,
                    )
                if upstream.status_code >= 400:
                    resolved_runtime.record_client_error()
                    return _video_upstream_error(
                        binding=binding,
                        status_code=upstream.status_code,
                        reason=f"http_{upstream.status_code}",
                    )
        except HTTPException:
            raise
        except (httpx.HTTPError, OSError) as exc:
            resolved_runtime.record_failure(key, type(exc).__name__)
            return _video_owner_punt(
                specialist_id=specialist_id,
                node_id=binding.node_id,
                reason="transport_failure",
                local_attempts=1,
            )

        try:
            safe, upstream_id, upstream_status = _parse_video_job_payload(content)
        except ValueError as exc:
            resolved_runtime.record_failure(key, "invalid_job_response")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="upstream video job response is invalid",
                headers=_video_audit_headers(binding, "invalid_job_response"),
            ) from exc
        try:
            store = await run_in_threadpool(_video_store)
            job = await run_in_threadpool(
                store.create,
                protocol_id=VIDEO_JOB_PROTOCOL.protocol_id,
                specialist_id=specialist_id,
                owner_node_id=binding.node_id,
                owner_origin=owner_origin,
                upstream_job_id=upstream_id,
                status=upstream_status,
            )
        except (OSError, sqlite3.Error, ValueError) as exc:
            _log.warning(
                "[router] orphan video job owner=%s specialist=%s upstream_id=%s store=%s",
                binding.node_id[:128],
                specialist_id[:128],
                upstream_id[:256],
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="upstream video job was created but ownership could not be persisted",
                headers=_video_audit_headers(binding, "ownership_persistence_failed"),
            ) from exc
        resolved_runtime.record_success(
            key, int((time.perf_counter() - started_at) * 1000)
        )
        safe["id"] = job.public_id
        return JSONResponse(
            content=safe,
            headers=_video_audit_headers(binding, "created_owner_pinned"),
        )

    async def _proxy_video_job_status(job: VideoJob, binding: NodeBinding) -> Response:
        key = binding_key(job.specialist_id, job.owner_node_id)
        if not resolved_runtime.is_routable(key):
            return _video_owner_punt(
                specialist_id=job.specialist_id,
                node_id=job.owner_node_id,
                reason="circuit_open",
                local_attempts=0,
            )
        started_at = time.perf_counter()
        try:
            async with client.stream(
                "GET",
                f"{job.owner_origin}/v1/videos/{job.upstream_job_id}",
                headers=_multipart_upstream_headers(),
                follow_redirects=False,
            ) as upstream:
                if 300 <= upstream.status_code < 400:
                    resolved_runtime.record_failure(key, f"http_{upstream.status_code}")
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail="upstream video redirects are not accepted",
                        headers=_video_audit_headers(binding, "redirect_rejected"),
                    )
                if _base_media_type(upstream.headers.get("content-type")) != "application/json":
                    resolved_runtime.record_failure(key, "unexpected_media_type")
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail="upstream video status must be application/json",
                        headers=_video_audit_headers(binding, "unexpected_media_type"),
                    )
                try:
                    content = await _read_bounded_response(
                        upstream, max_bytes=VIDEO_JOB_PROTOCOL.max_response_bytes
                    )
                except ValueError as exc:
                    resolved_runtime.record_failure(key, "response_too_large")
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail=str(exc),
                        headers=_video_audit_headers(binding, "response_too_large"),
                    ) from exc
                upstream_status_code = upstream.status_code
        except HTTPException:
            raise
        except (httpx.HTTPError, OSError) as exc:
            resolved_runtime.record_failure(key, type(exc).__name__)
            return _video_owner_punt(
                specialist_id=job.specialist_id,
                node_id=job.owner_node_id,
                reason="transport_failure",
                local_attempts=1,
            )

        try:
            safe, upstream_id, upstream_status = _parse_video_job_payload(content)
            if upstream_id != job.upstream_job_id:
                raise ValueError("upstream video status returned a mismatched id")
        except ValueError as exc:
            if upstream_status_code >= 500:
                resolved_runtime.record_failure(key, f"http_{upstream_status_code}")
                return _video_owner_punt(
                    specialist_id=job.specialist_id,
                    node_id=job.owner_node_id,
                    reason=f"http_{upstream_status_code}",
                    local_attempts=1,
                )
            if upstream_status_code >= 400:
                resolved_runtime.record_client_error()
                return _video_upstream_error(
                    binding=binding,
                    status_code=upstream_status_code,
                    reason=f"http_{upstream_status_code}",
                )
            resolved_runtime.record_failure(key, "invalid_job_response")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="upstream video status response is invalid",
                headers=_video_audit_headers(binding, "invalid_job_response"),
            ) from exc
        try:
            store = await run_in_threadpool(_video_store)
            updated = await run_in_threadpool(
                store.update_status, job.public_id, upstream_status
            )
            if updated is None:
                raise ValueError("video ownership expired while status was in flight")
        except (OSError, sqlite3.Error) as exc:
            _log.error("[router] video status store update failed: %s", type(exc).__name__)
            resolved_runtime.record_failure(key, "ownership_store_failure")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="video ownership status could not be persisted",
                headers=_video_audit_headers(binding, "ownership_store_failure"),
            ) from exc
        except ValueError as exc:
            resolved_runtime.record_failure(key, "invalid_job_response")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="video ownership expired while status was in flight",
                headers=_video_audit_headers(binding, "invalid_job_response"),
            ) from exc
        resolved_runtime.record_success(
            key, int((time.perf_counter() - started_at) * 1000)
        )
        safe["id"] = job.public_id
        return JSONResponse(
            content=safe,
            status_code=upstream_status_code,
            headers=_video_audit_headers(binding, "owner_pinned_status"),
        )

    @app.get("/v1/videos/{public_id}/content", summary="Download owner-pinned video content")
    async def video_job_content(
        public_id: str,
        _: Annotated[None, Depends(verify_router_token)],
    ) -> Response:
        job = await _load_video_job(public_id)
        owner = _recorded_video_owner(job, _snapshot())
        if owner is None:
            return _video_owner_punt(
                specialist_id=job.specialist_id,
                node_id=job.owner_node_id,
                reason="owner_absent",
                local_attempts=0,
            )
        key = binding_key(job.specialist_id, job.owner_node_id)
        if not resolved_runtime.is_routable(key):
            return _video_owner_punt(
                specialist_id=job.specialist_id,
                node_id=job.owner_node_id,
                reason="circuit_open",
                local_attempts=0,
            )
        started_at = time.perf_counter()
        try:
            upstream = await client.send(
                client.build_request(
                    "GET",
                    f"{job.owner_origin}/v1/videos/{job.upstream_job_id}/content",
                    headers=_multipart_upstream_headers(),
                ),
                stream=True,
                follow_redirects=False,
            )
        except (httpx.HTTPError, OSError) as exc:
            resolved_runtime.record_failure(key, type(exc).__name__)
            return _video_owner_punt(
                specialist_id=job.specialist_id,
                node_id=job.owner_node_id,
                reason="transport_failure",
                local_attempts=1,
            )
        if 300 <= upstream.status_code < 400:
            await upstream.aclose()
            resolved_runtime.record_failure(key, f"http_{upstream.status_code}")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="upstream video redirects are not accepted",
                headers=_video_audit_headers(owner, "redirect_rejected"),
            )
        if upstream.status_code >= 500:
            await upstream.aclose()
            resolved_runtime.record_failure(key, f"http_{upstream.status_code}")
            return _video_owner_punt(
                specialist_id=job.specialist_id,
                node_id=job.owner_node_id,
                reason=f"http_{upstream.status_code}",
                local_attempts=1,
            )
        if upstream.status_code >= 400:
            await upstream.aclose()
            resolved_runtime.record_client_error()
            return _video_upstream_error(
                binding=owner,
                status_code=upstream.status_code,
                reason=f"http_{upstream.status_code}",
            )
        media_type = _base_media_type(upstream.headers.get("content-type"))
        if media_type not in _VIDEO_CONTENT_MEDIA_TYPES:
            await upstream.aclose()
            resolved_runtime.record_failure(key, "unexpected_media_type")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="upstream video content has an unsupported media type",
                headers=_video_audit_headers(owner, "unexpected_media_type"),
            )
        declared = upstream.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > _VIDEO_CONTENT_MAX_BYTES:
            await upstream.aclose()
            resolved_runtime.record_failure(key, "response_too_large")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"response body exceeds {_VIDEO_CONTENT_MAX_BYTES} bytes",
                headers=_video_audit_headers(owner, "response_too_large"),
            )

        try:
            spool = tempfile.SpooledTemporaryFile(
                max_size=min(
                    _VIDEO_CONTENT_SPOOL_MEMORY_BYTES,
                    _VIDEO_CONTENT_MAX_BYTES,
                ),
                mode="w+b",
            )
        except OSError as exc:
            await upstream.aclose()
            resolved_runtime.record_failure(key, "content_spool_failure")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="video content could not be buffered safely",
                headers=_video_audit_headers(owner, "content_spool_failure"),
            ) from exc
        total = 0
        try:
            async for chunk in upstream.aiter_bytes():
                if len(chunk) > _VIDEO_CONTENT_MAX_BYTES - total:
                    resolved_runtime.record_failure(key, "response_too_large")
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail=f"response body exceeds {_VIDEO_CONTENT_MAX_BYTES} bytes",
                        headers=_video_audit_headers(owner, "response_too_large"),
                    )
                total += len(chunk)
                await run_in_threadpool(spool.write, chunk)
        except HTTPException:
            spool.close()
            raise
        except httpx.HTTPError as exc:
            spool.close()
            resolved_runtime.record_failure(key, type(exc).__name__)
            return _video_owner_punt(
                specialist_id=job.specialist_id,
                node_id=job.owner_node_id,
                reason="transport_failure",
                local_attempts=1,
            )
        except OSError as exc:
            spool.close()
            resolved_runtime.record_failure(key, "content_spool_failure")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="video content could not be buffered safely",
                headers=_video_audit_headers(owner, "content_spool_failure"),
            ) from exc
        finally:
            await upstream.aclose()
        await run_in_threadpool(spool.seek, 0)
        resolved_runtime.record_success(
            key, int((time.perf_counter() - started_at) * 1000)
        )

        async def video_bytes():
            try:
                while chunk := await run_in_threadpool(spool.read, 64 * 1024):
                    yield chunk
            finally:
                spool.close()

        response_headers = _video_audit_headers(owner, "owner_pinned_content")
        response_headers["content-length"] = str(total)
        return StreamingResponse(
            video_bytes(),
            media_type=media_type,
            headers=response_headers,
        )

    @app.get("/v1/videos/{public_id}", summary="Poll an owner-pinned video job")
    async def video_job_status(
        public_id: str,
        _: Annotated[None, Depends(verify_router_token)],
    ) -> Response:
        job = await _load_video_job(public_id)
        owner = _recorded_video_owner(job, _snapshot())
        if owner is None:
            return _video_owner_punt(
                specialist_id=job.specialist_id,
                node_id=job.owner_node_id,
                reason="owner_absent",
                local_attempts=0,
            )
        return await _proxy_video_job_status(job, owner)

    @app.delete("/v1/videos/{public_id}", summary="Delete an owner-pinned video job")
    async def delete_video_job(
        public_id: str,
        _: Annotated[None, Depends(verify_router_token)],
    ) -> Response:
        store, claim = await _claim_video_delete(public_id)
        job = claim.job
        owner_confirmed = False
        try:
            owner = _recorded_video_owner(job, _snapshot())
            if owner is None:
                return _video_owner_punt(
                    specialist_id=job.specialist_id,
                    node_id=job.owner_node_id,
                    reason="owner_absent",
                    local_attempts=0,
                )
            key = binding_key(job.specialist_id, job.owner_node_id)
            if not resolved_runtime.is_routable(key):
                return _video_owner_punt(
                    specialist_id=job.specialist_id,
                    node_id=job.owner_node_id,
                    reason="circuit_open",
                    local_attempts=0,
                )
            started_at = time.perf_counter()
            try:
                if _VIDEO_DELETE_DEADLINE_S >= _VIDEO_DELETE_LEASE_S:
                    raise RuntimeError("video delete deadline must be shorter than claim lease")
                async with asyncio.timeout(_VIDEO_DELETE_DEADLINE_S):
                    async with client.stream(
                        "DELETE",
                        f"{job.owner_origin}/v1/videos/{job.upstream_job_id}",
                        headers=_multipart_upstream_headers(),
                        follow_redirects=False,
                    ) as upstream:
                        if 300 <= upstream.status_code < 400:
                            resolved_runtime.record_failure(
                                key, f"http_{upstream.status_code}"
                            )
                            raise HTTPException(
                                status_code=status.HTTP_502_BAD_GATEWAY,
                                detail="upstream video redirects are not accepted",
                                headers=_video_audit_headers(owner, "redirect_rejected"),
                            )
                        if (
                            _base_media_type(upstream.headers.get("content-type"))
                            != "application/json"
                        ):
                            resolved_runtime.record_failure(key, "unexpected_media_type")
                            raise HTTPException(
                                status_code=status.HTTP_502_BAD_GATEWAY,
                                detail="upstream video delete must return application/json",
                                headers=_video_audit_headers(
                                    owner, "unexpected_media_type"
                                ),
                            )
                        try:
                            content = await _read_bounded_response(
                                upstream,
                                max_bytes=VIDEO_JOB_PROTOCOL.max_response_bytes,
                            )
                        except ValueError as exc:
                            resolved_runtime.record_failure(key, "response_too_large")
                            raise HTTPException(
                                status_code=status.HTTP_502_BAD_GATEWAY,
                                detail="upstream video delete response is too large",
                                headers=_video_audit_headers(
                                    owner, "response_too_large"
                                ),
                            ) from exc
                        upstream_status_code = upstream.status_code
            except TimeoutError:
                resolved_runtime.record_failure(key, "upstream_delete_timeout")
                return _video_owner_punt(
                    specialist_id=job.specialist_id,
                    node_id=job.owner_node_id,
                    reason="upstream_delete_timeout",
                    local_attempts=1,
                )
            except HTTPException:
                raise
            except (httpx.HTTPError, OSError) as exc:
                resolved_runtime.record_failure(key, type(exc).__name__)
                return _video_owner_punt(
                    specialist_id=job.specialist_id,
                    node_id=job.owner_node_id,
                    reason="transport_failure",
                    local_attempts=1,
                )

            if upstream_status_code >= 500:
                resolved_runtime.record_failure(key, f"http_{upstream_status_code}")
                return _video_owner_punt(
                    specialist_id=job.specialist_id,
                    node_id=job.owner_node_id,
                    reason=f"http_{upstream_status_code}",
                    local_attempts=1,
                )
            if upstream_status_code >= 400:
                resolved_runtime.record_client_error()
                return _video_upstream_error(
                    binding=owner,
                    status_code=upstream_status_code,
                    reason=f"http_{upstream_status_code}",
                )
            if upstream_status_code != status.HTTP_200_OK:
                resolved_runtime.record_failure(key, "unexpected_success_status")
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="upstream video delete did not return HTTP 200",
                    headers=_video_audit_headers(owner, "unexpected_success_status"),
                )
            try:
                payload = json.loads(content)
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="upstream video delete response is invalid",
                    headers=_video_audit_headers(owner, "invalid_delete_response"),
                ) from exc
            if (
                not isinstance(payload, dict)
                or payload.get("id") != job.upstream_job_id
                or payload.get("deleted") is not True
                or payload.get("object") != "video.deleted"
            ):
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="upstream video delete response is invalid",
                    headers=_video_audit_headers(owner, "invalid_delete_response"),
                )

            owner_confirmed = True
            try:
                deleted = await run_in_threadpool(
                    store.confirm_delete,
                    job.public_id,
                    claim.token,
                )
            except (OSError, sqlite3.Error) as exc:
                _log.error("[router] confirmed video delete could not be persisted")
                resolved_runtime.record_failure(key, "ownership_store_failure")
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="confirmed video delete could not be persisted",
                    headers=_video_audit_headers(owner, "ownership_store_failure"),
                ) from exc
            if not deleted:
                resolved_runtime.record_failure(key, "delete_claim_lost")
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="video delete ownership claim was lost",
                    headers=_video_audit_headers(owner, "delete_claim_lost"),
                )
            resolved_runtime.record_success(
                key, int((time.perf_counter() - started_at) * 1000)
            )
            return JSONResponse(
                content={
                    "id": job.public_id,
                    "deleted": True,
                    "object": "video.deleted",
                },
                headers=_video_audit_headers(owner, "owner_pinned_delete"),
            )
        finally:
            if not owner_confirmed:
                await _release_video_delete(store, claim)

    @app.post(
        "/v1/chat/completions",
        summary="OpenAI-compatible chat completions, proxied to the picked node",
    )
    async def chat_completions(
        request: Request,
        _: Annotated[None, Depends(verify_router_token)],
    ) -> Response:
        # Reject an oversized body BEFORE buffering it (#101). Honor a declared
        # Content-Length, and also bound the actual read so a lying/chunked
        # client can't exceed the cap.
        clen = request.headers.get("content-length")
        if clen is not None and clen.isdigit() and int(clen) > MAX_REQUEST_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"request body exceeds {MAX_REQUEST_BYTES} bytes",
            )
        raw = await request.body()
        if len(raw) > MAX_REQUEST_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"request body exceeds {MAX_REQUEST_BYTES} bytes",
            )
        try:
            import json as _json
            body = _json.loads(raw)
        except Exception as exc:  # noqa: BLE001 — anything not-JSON is a 400
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"request body is not valid JSON: {exc}",
            ) from exc
        if not isinstance(body, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="request body must be a JSON object",
            )
        specialist_id = body.get("model")
        if not isinstance(specialist_id, str) or not specialist_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="`model` must be a non-empty specialist_id",
            )

        snap = _snapshot()

        preferred_node_id: str | None = None
        if specialist_id == AUTO_MODEL_ID:
            if auto_router is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=(
                        'model "auto" needs classifier-driven routing: start the '
                        "router with --auto-route (requires slancha-mesh[classifier]), "
                        "or pass an explicit specialist_id from GET /v1/models."
                    ),
                )
            # CPU-bound (embed + heads) → threadpool so the event loop
            # keeps serving concurrent requests.
            ready_snapshot = _runtime_ready_snapshot(snap, resolved_runtime)
            selection = await run_in_threadpool(
                auto_router.select, body, ready_snapshot
            )
            if selection.specialist_id is None:
                resolved_runtime.record_punt()
                return punt_response(
                    code=PuntCode.NO_SUITABLE_LOCAL_ROUTE,
                    message="No healthy local specialist satisfies this request.",
                    reason=selection.reason,
                    local_attempts=0,
                    retryable=True,
                )
            specialist_id = selection.specialist_id
            preferred_node_id = selection.node_id
            # The upstream must see the resolved id, not "auto": the rewrite
            # below maps specialist_id → ollama_tag / served_model_name, and
            # vLLM specialists without an alias serve under the specialist_id
            # verbatim.
            body = {**body, "model": specialist_id}
            _log.info("[router] auto → %s (%s)", specialist_id, selection.reason)

        if specialist_id not in snap.specialists:
            resolved_runtime.record_client_error()
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    f"unknown specialist {specialist_id!r}; "
                    "check `GET /v1/models` for available model ids."
                ),
            )

        discovered_bindings = _reachable_bindings(specialist_id, snap)
        if not discovered_bindings:
            resolved_runtime.record_punt()
            return punt_response(
                code=PuntCode.LOCAL_ROUTE_UNAVAILABLE,
                message="The requested local specialist has no healthy binding.",
                reason=f"no reachable node for specialist {specialist_id!r}",
                local_attempts=0,
                retryable=True,
            )
        bindings = [
            binding
            for binding in discovered_bindings
            if resolved_runtime.peek_routable(
                binding_key(specialist_id, binding.node_id)
            )
        ]
        if preferred_node_id is not None:
            bindings.sort(
                key=lambda binding: binding.node_id != preferred_node_id
            )
        if not bindings:
            resolved_runtime.record_punt()
            return punt_response(
                code=PuntCode.LOCAL_ROUTE_UNAVAILABLE,
                message="The requested local specialist is temporarily unavailable.",
                reason=f"all runtime circuits open for specialist {specialist_id!r}",
                local_attempts=0,
                retryable=True,
            )
        # Bound fan-out (#101): one client request retries at most this many nodes.
        bindings = bindings[:MAX_FALLBACK_ATTEMPTS]

        upstream_body = _rewrite_model_for_upstream(body, specialist_id, snap)
        # Caller-asserted user (OpenAI `user` param) — the only user handle the router
        # has (auth is a single shared node token, not a per-user principal). Preserved
        # through the auto-route body rewrite above.
        user_field = body.get("user")

        if body.get("stream") is True:
            try:
                return await _proxy_stream_with_fallback(
                    client,
                    bindings=bindings,
                    specialist_id=specialist_id,
                    upstream_body=upstream_body,
                    sink=app.state.usage_sink,
                    runtime_health=resolved_runtime,
                    user_field=user_field,
                )
            except LocalRoutesExhausted as exc:
                resolved_runtime.record_punt()
                return punt_response(
                    code=PuntCode.LOCAL_ROUTE_UNAVAILABLE,
                    message="Every local streaming route failed before response bytes.",
                    reason=exc.detail,
                    local_attempts=exc.attempts,
                    retryable=True,
                )

        # Non-streaming fallback chain: try each binding in order; first
        # success wins; retriable failures fall through to the next.
        last_status: int | None = None
        last_detail: str | None = None
        attempts = 0
        for idx, binding in enumerate(bindings):
            key = binding_key(specialist_id, binding.node_id)
            if not resolved_runtime.is_routable(key):
                continue
            attempts += 1
            upstream_url = f"{binding.node_url.rstrip('/')}/v1/chat/completions"  # type: ignore[union-attr]
            t0 = time.perf_counter()  # per-attempt; latency_ms measures the WINNING call only
            try:
                upstream = await client.post(
                    upstream_url,
                    json=upstream_body,
                    headers=_upstream_headers(),
                )
            except (httpx.HTTPError, OSError) as exc:
                last_detail = f"{exc.__class__.__name__} at {upstream_url}"
                resolved_runtime.record_failure(key, exc.__class__.__name__)
                _log.warning(
                    "[router] upstream %s for %s unreachable: %s; trying next",
                    binding.node_id,
                    specialist_id,
                    exc,
                )
                continue
            if _is_retriable_status(upstream.status_code):
                last_status = upstream.status_code
                resolved_runtime.record_failure(key, f"http_{upstream.status_code}")
                _log.warning(
                    "[router] upstream %s for %s returned %d; trying next",
                    binding.node_id,
                    specialist_id,
                    upstream.status_code,
                )
                continue
            # Win — forward as-is.
            latency_ms = int((time.perf_counter() - t0) * 1000)
            if upstream.status_code >= 500:
                resolved_runtime.record_failure(key, f"http_{upstream.status_code}")
            elif upstream.status_code >= 400:
                resolved_runtime.record_client_error()
            else:
                resolved_runtime.record_success(key, latency_ms)
            position = "primary" if idx == 0 else f"fallback#{idx}"
            slancha_headers = {
                "X-Slancha-Specialist": specialist_id,
                "X-Slancha-Node": binding.node_id,
                "X-Slancha-Reason": (
                    f"{position}; queue_depth={binding.queue_depth} "
                    f"p95={binding.p95_latency_ms_60s}"
                ),
            }
            media_type = _safe_media_type(upstream.headers.get("content-type"), "application/json")
            response = Response(
                content=upstream.content,
                status_code=upstream.status_code,
                media_type=media_type,
                headers=slancha_headers,
            )
            # Usage tap (guarded — telemetry NEVER faults a completion). Only the win
            # branch emits; the 4xx/5xx/no-node error paths above never do.
            try:
                rbody = parse_response_body(upstream.content)
                safe_emit(
                    app.state.usage_sink,
                    build_usage_event(
                        specialist_id=specialist_id,
                        user_field=user_field,
                        status_code=upstream.status_code,
                        latency_ms=latency_ms,
                        usage=rbody.get("usage"),
                        response_id=rbody.get("id"),
                        fallback_fired=idx > 0,
                    ),
                )
            except Exception as exc:  # noqa: BLE001 — never break a completion on telemetry
                _log.warning("[router] usage tap failed (%s); skipped", type(exc).__name__)
            return response

        # All bindings exhausted.
        resolved_runtime.record_punt()
        return punt_response(
            code=PuntCode.LOCAL_ROUTE_UNAVAILABLE,
            message="Every attempted local route failed.",
            reason=(
                f"all {len(bindings)} reachable node(s) failed for "
                f"{specialist_id!r}: last_status={last_status} "
                f"last_error={last_detail}"
            ),
            local_attempts=attempts,
            retryable=True,
        )

    @app.get("/health")
    def health() -> JSONResponse:
        """Liveness — the one open endpoint, mirrors registry_app's posture."""
        snap = _snapshot()
        reachable = {
            sid: [
                binding
                for binding in bindings
                if binding.health != "unreachable" and binding.node_url
            ]
            for sid, bindings in snap.specialists.items()
        }
        routable = {
            sid: [
                binding
                for binding in bindings
                if resolved_runtime.peek_routable(binding_key(sid, binding.node_id))
            ]
            for sid, bindings in reachable.items()
        }
        runtime_snapshot = resolved_runtime.snapshot()
        specialists_reachable = sum(bool(bindings) for bindings in reachable.values())
        specialists_routable = sum(bool(bindings) for bindings in routable.values())
        open_circuits = int(runtime_snapshot["open_circuits"])
        degraded_reasons: list[str] = []
        if open_circuits:
            degraded_reasons.append("open_circuits")
        if specialists_routable < specialists_reachable:
            degraded_reasons.append("bindings_not_request_ready")
        return JSONResponse(
            {
                "status": "degraded" if degraded_reasons else "ok",
                "auth_required": _expected_token() is not None,
                "specialists_reachable": specialists_reachable,
                "specialists_routable": specialists_routable,
                "bindings_routable": sum(len(bindings) for bindings in routable.values()),
                "queue_depth": sum(
                    max(0, binding.queue_depth)
                    for bindings in routable.values()
                    for binding in bindings
                ),
                "snapshot_age_s": max(
                    0.0,
                    (datetime.now(timezone.utc) - snap.snapshot_ts).total_seconds(),
                ),
                "degraded_reasons": degraded_reasons,
                **{
                    key: value
                    for key, value in runtime_snapshot.items()
                    if key != "bindings"
                },
                "runtime_bindings": runtime_snapshot["bindings"],
            }
        )

    return app


__all__ = [
    "AUTO_MODEL_ID",
    "NODE_TOKEN_ENV",
    "UPSTREAM_TIMEOUT_S",
    "AutoRouterLike",
    "create_router_app",
    "discovery_to_snapshot",
    "verify_router_token",
]
