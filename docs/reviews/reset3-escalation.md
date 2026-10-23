# Escalation HTTP contract review

Fleet row: cz717808979d. Target: SlanchaAI/slancha-mesh:mesh/escalation.py.
Source: origin/main at `2bf33053294ab181ba0d4fd3d11c234332cee28c`.

## Result

Review-only deliverable: the direct response serializer handles the requested
synthetic cases, but a production caller overwrites its sanitized, bounded
reason header. The row explicitly permits a review/reproduction instead of
new tests when a production defect falls outside the test-only scope.
Production code is unchanged; no knowingly failing or duplicate tests added.

## Source, callers, and coverage

- `mesh/escalation.py:18` replaces CR and LF with spaces; lines 31 and 38
  bound reason to 256 characters and message to 512. Line 40 clamps attempts
  to zero; line 42 retains the supplied boolean.
- `mesh/router_app.py:2713` returns the helper response directly for an
  unavailable classifier-selected route.
- `mesh/router_app.py:944` calls the helper inside `_video_owner_punt`.
  Lines 952–960 then replace `X-Slancha-Reason` with a protocol prefix plus
  the original reason, bypassing both sanitization and the 256-character cap.
  This helper is called by video routes, including lines 2107 and 2124.
- `mesh/tests/test_escalation.py:10` already checks parsed JSON, HTTP 503,
  zero attempts, and `retryable=True`. Its test at line 36 already checks
  oversized inputs produce reason/message lengths of 256/512.
- Missing direct cases: CR/LF replacement, negative attempts, and parsed
  `retryable=False`. Existing zero/oversize tests should not be duplicated.
  Exact boundary preservation is not asserted by those existing tests.

## Exact execution and results

Executed from the isolated worktree on the source SHA above, with synthetic
values only; no remote requests or live services:

```sh
uv run --extra dev python - <<'PY'
import json
from mesh.escalation import PuntCode, punt_response
from mesh.router_app import _video_owner_punt
reason = 'owner\r\n' + 'r' * 300
for attempts in (-3, 0):
    response = punt_response(code=PuntCode.LOCAL_ROUTE_UNAVAILABLE, message='m' * 600, reason=reason, local_attempts=attempts, retryable=False)
    payload = json.loads(response.body)
    header = response.headers['X-Slancha-Reason']
    assert response.status_code == 503
    assert '\r' not in header and '\n' not in header
    assert len(header) == 256
    assert len(payload['error']['message']) == 512
    assert payload['error']['details']['local_attempts'] == 0
    assert payload['error']['details']['retryable'] is False
    print(f'direct: attempts={attempts}, status=503, reason=256, message=512, normalized=0, retryable=false, CR/LF removed')
response = _video_owner_punt(specialist_id='synthetic-specialist', node_id='synthetic-node', reason=reason, local_attempts=0)
header = response.headers['X-Slancha-Reason']
assert '\r\n' in header and len(header) > 256
assert any(b'\r\n' in value for name, value in response.raw_headers if name == b'x-slancha-reason')
print(f'caller defect reproduced: status={response.status_code}, reason_length={len(header)}, CR/LF preserved in raw_headers')
PY
```

Exit 0. Both direct cases returned 503, 256-character reasons without CR/LF,
512-character messages, zero normalized attempts, and JSON false.
The caller returned 503 with a **341-character reason containing CR/LF in
raw_headers**. This demonstrates the response construction defect, not a
claim of remotely exploitable header injection or socket transport behavior.

```sh
uv run --extra dev pytest -q mesh/tests/test_escalation.py
```

Exit 0: **2 passed in 0.06s** (CPython 3.14.7).

```sh
test -s docs/reviews/reset3-escalation.md && (test ! -f mesh/tests/test_reset3_escalation.py || (uv run --extra dev pytest -q mesh/tests/test_reset3_escalation.py))
```

Exit 0. Review exists; the optional new test module is intentionally absent.

## Remaining findings and limits

A separately authorized production fix should preserve sanitization and the
reason cap after adding the video protocol prefix, then test the caller's
final response. The three missing direct cases remain candidates for durable
regression tests alongside that fix. Synthetic reproduction does not establish
whether external input can reach this caller unchanged. No HTTP server or
network transport was exercised.

GitNexus MCP tools and CLI were unavailable; the repository's referenced
`.claude/skills/gitnexus/gitnexus-guide/SKILL.md` was absent.
`gitnexus_detect_changes` exited 127 (`command not found`). Source/caller
inspection and the outgoing diff provide the scope check under the global
services-fail-open rule. No existing symbols were edited.
