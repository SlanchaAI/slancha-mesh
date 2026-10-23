# Stream usage coverage review

Fleet row: c48ab8948952. Source: `SlanchaAI/slancha-mesh:mesh/usage.py`.
Inspected default-branch SHA: `2bf33053294ab181ba0d4fd3d11c234332cee28c`.

## Caller and existing coverage

`mesh/router_app.py:1373` passes the buffered stream tail to
`parse_stream_usage`, inside the `build_usage_event` call at line 1368.
The buffer retains at most `MAX_TAIL_BYTES` (lines 1349–1351), so its
leading event can be cut mid-frame. Stream interruption can also leave an
incomplete trailing event. The parser keeps the last successfully parsed usage
dictionary and ignores empty or invalid data lines.

`mesh/tests/test_usage_emitter.py:260` covers valid final usage followed by
`[DONE]`; line 278 covers a stream without usage; line 289 covers sink failure
without disturbing response bytes. Builder tests at lines 144–178 cover event
shape, generated identifiers, and invalid counts. Repository-wide caller search
found no direct parser tests and no coverage for the three boundaries below.

## Added coverage

`mesh/tests/test_reset4_usage.py` adds three parametrized cases:

- A cut leading event and incomplete trailing usage JSON preserve the complete
  usage between them, and the builder emits exactly 7 input / 3 output tokens.
- Blank frames and empty `data:` payloads produce no usage and no event.
- Invalid usage JSON with a trailing comma produces no usage and no event.

Each case invokes both pure functions with synthetic bytes and fixed metadata.
The new tests create no sink, spool, network client, or service; no dependency
mocks are needed for these pure calls. Production code remains unchanged.
No defect was observed in these cases; no production fix is proposed.

## Executed verification

- `uv run --extra dev pytest -q mesh/tests/test_reset4_usage.py`:
  **3 passed** (0.09s).
- `uv run --extra dev pytest -q mesh/tests/test_usage_emitter.py`:
  **53 passed, 1 skipped** (0.71s). Existing module uses mocked upstream and
  receiver transports plus temporary spool files; no running service or real
  record was used.
- Row proof: `test -s docs/reviews/reset4-usage.md && (test ! -f mesh/tests/test_reset4_usage.py || (uv run --extra dev pytest -q mesh/tests/test_reset4_usage.py))`:
  **passed**, 3 tests passed (0.03s).

GitNexus MCP tools and CLI were unavailable in this worker. Direct source,
caller, and test inspection supplied scope evidence instead; only the two
row-authorized files were added. No existing symbol was modified.
