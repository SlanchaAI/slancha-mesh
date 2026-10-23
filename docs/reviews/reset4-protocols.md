# Output-mode coverage review

Fleet row c4ed1844e75a. Source: SlanchaAI/slancha-mesh:mesh/protocols.py.
Inspected origin/main SHA: `2bf33053294ab181ba0d4fd3d11c234332cee28c`.

## Contract and caller

`MultipartProtocol.success_media_types_for` (`mesh/protocols.py:51`) uses
`response_format or default_response_format`. A declared format table returns
only the selected tuple, or an empty tuple for an unknown format. Without a
table it returns the general success tuple. The transcription declaration
maps absent input to JSON; SRT allows both application/x-subrip and text/plain.
Image edits and video jobs have no format table and retain application/json.

Actual caller `mesh/router_app.py:1726` rejects an empty allowlist with HTTP 400
before selecting or contacting an upstream. `_proxy_multipart_media` at
`mesh/router_app.py:757` checks successful upstream responses against the
selected allowlist; mismatch produces HTTP 502. Error responses use a separate
JSON/plain-text allowlist. Source inspection and GitNexus context confirmed
both callers; production symbols remain unchanged.

## Existing coverage and additions

`mesh/tests/test_multimodal_router.py:678` exercises image edits without a
format table and transcription with explicit text output, including model
rewriting and response preservation. Its test at line 919 rejects a JSON
response when text was requested. No direct success_media_types_for assertion
or unsupported transcription format case existed in mesh/tests.

Added exactly three parametrized cases in `mesh/tests/test_reset4_protocols.py`:

- Absent transcription format: exact JSON tuple, one mocked upstream request,
  HTTP 200 and preserved JSON response.
- Supported SRT: exact two-media-type tuple, one mocked upstream request,
  HTTP 200 and preserved application/x-subrip response.
- Unsupported format: empty tuple, HTTP 400 with specific detail, zero upstream
  requests.

Each case checks the declared registry object and executes the real router
caller using existing synthetic snapshot helpers, TestClient and MockTransport.
No running service, real record, or production change is involved. These are
bounded missing cases, not exhaustive combinations of every format and MIME
alias. No defect found in the inspected behavior.

## Verification

- `uv run --extra dev pytest -q mesh/tests/test_reset4_protocols.py mesh/tests/test_multimodal_router.py`
  passed: 79 tests (3 new, 76 existing).
- `test -s docs/reviews/reset4-protocols.md && (test ! -f mesh/tests/test_reset4_protocols.py || (uv run --extra dev pytest -q mesh/tests/test_reset4_protocols.py))`
  passed: 3 tests.

Tooling lesson: GitNexus registered this worktree as `slancha-mesh`, not its
folder basename; querying the basename failed. Use the registered repository
name reported by the CLI. Local index built with `--index-only`; graph truncation
warnings mean caller source inspection remains necessary.
