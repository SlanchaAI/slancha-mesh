# Discovery coverage review

Fleet row: c4b121f18fc4. Source: `SlanchaAI/slancha-mesh:mesh/discovery.py`.
Inspected default-branch SHA: `2bf33053294ab181ba0d4fd3d11c234332cee28c`.

## Source and callers

`pin_host` (`mesh/discovery.py:147`) reconstructs the authority from the peer
and advertised port, preserving path, query and fragment. Its production caller
is `_specialists_from_models` at `mesh/discovery.py:213`; that caller catches
`ValueError` and skips malformed advertised URLs. `discover_specialists` invokes
that parser at `mesh/discovery.py:261`. Real discovery callers are
`cmd_discover` at `mesh/cli.py:299` and the router refresh at `mesh/cli.py:482`.
The CLI passes explicit LAN peers through `synthesize_lan_status` at
`mesh/cli.py:286`, so literal IPv6 peer input can reach this path.

## Existing and added coverage

Inspected `mesh/tests/test_discovery.py`, plus discovery references in
`mesh/tests/test_membership_e2e.py` and
`mesh/tests/test_router_discovery_snapshot.py`.

- Existing tests at `test_discovery.py:129` and `:133` cover host replacement,
  scheme, port, simple path, and absent port.
- Existing tests at `:203`, `:209`, and `:224` cover a nonnumeric port raising,
  isolation of a malformed peer advertisement, and retaining valid URLs beside
  malformed ones. No duplicate malformed-port tests were added.
- Two new parameterized cases cover bracketed IPv6 replacement and exact
  preservation of an encoded path, repeated query parameters and fragment.
  Each exercises both `pin_host` and `discover_specialists`, with a synthetic
  peer map and an injected in-memory fetch. Assertions include the fetched
  host/port, reachability and final routing URL. No network or real records.

## Defect reproduction: bare IPv6 peer loses required brackets

Executed from the inspected worktree:

```sh
uv run --extra dev python - <<'PY'
from urllib.parse import urlsplit
from mesh.discovery import pin_host, discover_specialists, synthesize_lan_status
url = pin_host('http://advertised.example:8003/v1', '2001:db8::2')
print('pinned:', url)
try:
 print(urlsplit(url).port)
except ValueError as exc:
 print(type(exc).__name__ + ': ' + str(exc))
payload = {'data': [{'id': 'synthetic', 'routing_meta': {'node_urls': ['http://advertised.example:8003/v1']}}]}
result = discover_specialists(synthesize_lan_status(['2001:db8::2']), fetch=lambda host, port: payload)
print('discovered:', result.specialists['synthetic'].node_urls)
PY
```

Observed `pinned: http://2001:db8::2:8003/v1`; parsing its port raises
`ValueError: Port could not be cast to integer value as 'db8::2:8003'`.
Discovery retains `('http://2001:db8::2:8003/v1',)` instead of a usable bracketed
URL. Expected URL: `http://[2001:db8::2]:8003/v1`.
Already-bracketed peers pass the new test. Bare IPv6 requires a production
change, outside this row; reproduction is recorded instead of a failing test.
No live CLI, HTTP fetch or service was invoked.

## Validation

- Clean baseline: `uv run --extra dev pytest -q mesh/tests/test_discovery.py`
  — 19 passed.
- `uv run --extra dev pytest -q mesh/tests/test_reset4_discovery.py mesh/tests/test_discovery.py`
  — 21 passed.
- Row proof: `test -s docs/reviews/reset4-discovery.md && (test ! -f mesh/tests/test_reset4_discovery.py || (uv run --extra dev pytest -q mesh/tests/test_reset4_discovery.py))`
  — passed, 2 tests passed.

GitNexus tools and CLI are unavailable in this worker, and the repository's
referenced GitNexus skill file is absent. Used direct source/caller inspection
and scoped diff review under the global fail-open rule. No existing symbols or
production files changed.
