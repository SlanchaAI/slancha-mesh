# Issue #162: private dependencies broke pin and gate-parity

The historical failures were checkout authorization failures, not schema or gate
mismatches. Current main suppresses those failures when its cross-repository
credential is absent; the latest main run is green with both actual checks skipped.
Thus “fail for every PR” describes the reported incident, not current behavior.
The workflow also has pull-request path filters, so it never covered every PR.

Investigation for [SlanchaAI/slancha-mesh#162](https://github.com/SlanchaAI/slancha-mesh/issues/162),
observed 2026-10-23 UTC. Current source baseline:
`2bf33053294ab181ba0d4fd3d11c234332cee28c`. Historical baseline:
`e44c34af98d7d71214e759b422f5483f14fd94c1`.
Only repository source and read-only GitHub metadata/logs were inspected; no
service probes, dependency installs, workflow dispatches, or infrastructure changes.
Trust annotations: P = primary source; confidence `[TBD: verify]` rather than an
invented numeric score. Issue assertions are distinguished from observed logs.

## Failure path and source locations

Historical locations below refer to `e44c34a`:

| Job / surface | Source | Consequence |
| --- | --- | --- |
| Workflow trigger | `.github/workflows/cross-repo-schema-pin.yml:17` | Path-filtered PRs and pushes to main run the integration workflow. |
| Token permission | `.github/workflows/cross-repo-schema-pin.yml:44` | `contents: read` does not grant access to another private repository. |
| `pin` checkout | `.github/workflows/cross-repo-schema-pin.yml:56` | Checks out `SlanchaAi/slancha-shared`, `main`, with no explicit cross-repo token; comment assumes public access. |
| Schema check | `.github/workflows/cross-repo-schema-pin.yml:75` | Sets `SLANCHA_SHARED_PATH`, then runs `pytest -q mesh/tests/test_cross_repo_usage_event.py`; unreachable after failed checkout. Catalog validation follows at line 82. |
| `gate-parity` checkout | `.github/workflows/cross-repo-schema-pin.yml:111` | Checks out **slancha-local**, not slancha-shared; this job also assumes public access. |
| Consumer install / guard | `.github/workflows/cross-repo-schema-pin.yml:122` | Installs local's dev extra; line 128 checks mesh gate source exists; line 133 runs `tests/test_gate_cross_repo_compat.py` with `SLANCHA_MESH_PATH`. Checkout failure prevents all three. |
| Optional dependency | `pyproject.toml:86`, `pyproject.toml:96` | `[loop]` directly references `slancha-local` through a Git HTTPS URL, assuming anonymous read access. |

[P, repository source at the named commits, TBD: verify]

The breaking changes were repository visibility changes in **slancha-shared and
slancha-local**. These are GitHub settings, not an identified source-code commit.
Issue #162 reports both went private; `gh repo view ... --json visibility` returned
`PRIVATE` for each during this investigation. No visibility audit history was
available here, so the actor and exact transition time remain unknown. The
checkout logs below independently establish access failure. They do not establish
when visibility changed. [P, issue #162 and repository metadata, TBD: verify]

The issue also reports that re-resolving the lock broke PR #159 until it was
refrozen. PR #159 includes `1dabd3cd30518ef79b7c463fb4053d2cee59e594`, “chore:
relock uv.lock for the classifier extra”; this supports the lock-change history,
not an independently recovered install-error trace. Today the private Git
reference remains at `packages/slancha-mesh-tune/pyproject.toml:43` and
`uv.lock:4795` (resolved revision `d26d0dddb729ac0c6a6265537cd4f24b6f37da3a`).
`.github/workflows/ci.yml:45` uses a frozen sync without selecting `loop`; a frozen
lock is not anonymous authorization to install the private optional dependency.
[P, https://github.com/SlanchaAI/slancha-mesh/pull/159 and source, TBD: verify]

## Read-only run evidence

Commands used (all with `--repo SlanchaAI/slancha-mesh`):

```sh
gh run list --limit 20 --json databaseId,workflowName,conclusion,headSha,createdAt,url
gh run list --workflow cross-repo-schema-pin --status failure --limit 3 --json databaseId,createdAt,headSha,url
gh run view 30720506605 --log-failed
gh run view 29376986795 --log-failed
gh run view 29368941936 --log-failed
gh run view 30844765371 --json jobs
```

| Run | Observation |
| --- | --- |
| [30720506605](https://github.com/SlanchaAI/slancha-mesh/actions/runs/30720506605), 2026-08-01, `20b4e75` | Most recent failing schema-pin run returned by the query. `pin`: slancha-shared repository not found, git exit 128 (22:05:17–43 UTC). `gate-parity`: slancha-local repository not found, git exit 128 (22:05:20–54 UTC). Both retry checkout and fail before tests. |
| [29376986795](https://github.com/SlanchaAI/slancha-mesh/actions/runs/29376986795), 2026-07-14, `e44c34a` | Listed as failure; `--log-failed` returned HTTP 410. No log-content claim made. |
| [29368941936](https://github.com/SlanchaAI/slancha-mesh/actions/runs/29368941936), 2026-07-14, `da6c846` | Listed as failure; `--log-failed` returned HTTP 410. No log-content claim made. |
| [30844765371](https://github.com/SlanchaAI/slancha-mesh/actions/runs/30844765371), 2026-08-03, `2bf3305` | Both jobs conclude success. Both access steps succeed, but private checkout and actual schema/parity test steps explicitly conclude **skipped**. |

[P, linked GitHub Actions run records and retained failed log, TBD: verify]

The latest general `ci` records returned were successful October runs; these do
not demonstrate cross-repository parity. Historical logs are partly expired,
so available evidence supports repeated checkout failures, not a quantified claim
about every PR. Issue #162 says these checks were non-required; branch protection
was not audited here.

## What main already changed

Commit `aeb1c2b8127fc9474d7e872cd47412c974eb506a` (“Stabilize public pull request
checks”, August 1) added optional `SLANCHA_CROSS_REPO_TOKEN`, supplied it to both
private checkouts, and conditioned subsequent steps on its presence.
At current main, access checks are
`.github/workflows/cross-repo-schema-pin.yml:54` and `:126`; token inputs are
`:76` and `:148`. Schema execution is `:92`; consumer install is `:157`, source
sanity is `:165`, and parity execution is `:171` with the split tune source path
at `:175`. The old checkout failure is mitigated, but skipped tests provide no
live contract assurance. [P, commit and current source, TBD: verify]

## Ranked fix options

1. **Restore authenticated checks in a trusted context using existing wiring.**
   Have the owner provision the named credential with contents-read access to
   exactly the two dependency repositories. Smallest remaining operational fix:
   no workflow edit needed to exercise the existing path. Restrict secret-bearing
   execution to reviewed/trusted code; a credential must not be exposed to
   arbitrary fork or untrusted PR code. Do not switch to `pull_request_target`
   while checking out and running arbitrary PR code. Public forks still need an
   explicit policy separating unavailable integration coverage from verified parity.
2. **Restore public access to the contracts and guard.** Owner may re-publicize
   dependencies only after reviewing their contents, or publish a narrow public
   contract/guard package. Re-publicizing shared alone fixes only `pin`; local
   parity and the loop extra remain private. Current token-presence conditions
   would also need adjusting for anonymous checkouts.
3. **Vendor schemas and parity fixtures.** Removes checkout authorization needs,
   but adds update ownership and loses live comparison with consumer main unless
   an independent synchronization check is maintained. Largest implementation change.

For the public loop extra, separately choose authenticated/private distribution,
a public supported dependency, or removal of the public-install promise and extra.
The existing CI checkout token does not automatically authenticate pip's Git URL.
This documentation makes no visibility, secret, packaging, or workflow change.

## Smallest fix and decisive proof

Recommend option 1 for trusted maintainer CI, pending owner provisioning and
trust-boundary review. Merely rerunning today's tokenless workflow is not proof.
After that separately authorized change, inspect a trusted run at the intended
mesh SHA with `gh run view <run-id> --json jobs` and its test logs. Require:

- Both private checkouts execute successfully, with dependency revisions recorded.
- Schema-pin pytest executes and passes against the checked-out shared schema.
- Gate source sanity executes; parity pytest executes against the selected mesh
  tune path and reports actual passed tests, not an all-skipped suite.
- Neither test step is skipped. A green job-level conclusion alone fails this proof.
- In an isolated follow-up validation, a deliberately incompatible schema fixture
  or mirrored gate shape makes the relevant check fail, proving detection remains wired.

No such run was dispatched for this doc-only row. Remaining unknowns: credential
availability in intended trusted contexts, consumer compatibility after checkout,
visibility-change audit history, and the owner's public-loop distribution decision.

The row's local documentation proof is:

```sh
test -s docs/investigations/issue-162-ci-pin-gate-parity.md && grep -qiE 'gate-parity|parity' docs/investigations/issue-162-ci-pin-gate-parity.md
```
