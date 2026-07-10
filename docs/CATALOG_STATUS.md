# Catalog status — what's been bring-up-validated and what hasn't

The catalog (`mesh/catalog/*.toml`) is the single source of truth for which
specialists slancha-mesh knows about. **The fact that a card is in the
catalog says only that it parses cleanly and has plausible VRAM math.** It
does NOT mean a node has been brought up and seen to serve + heartbeat +
route end-to-end for that specialist. This file records that distinction
honestly, so contributors and users know which cards are safe to point a
node at and which are still DRAFT.

## How a card moves to "validated"

A specialist is "validated" when, on at least one mesh node:

1. `slancha-mesh up --specialist <id> --base-port <P>` brings the backend
   up healthy (vLLM `/health` 200, or Ollama `/api/ps` shows the tag
   loaded).
2. The node's `/heartbeat` POST reports it loaded with sane `runtime_gb`
   / VRAM numbers (no OOM, no truncation).
3. A `slancha-mesh discover` from another box on the tailnet sees the
   specialist as `reachable=1` with the expected `node_url`.
4. A real OpenAI-compat `POST /v1/chat/completions` to the discovered
   `node_url` returns a coherent completion (≥1 token, no `error`).

If you've done that, edit this file (move the card from DRAFT → VALIDATED),
note the hardware you tested on, and add a short note. PRs are welcome.

## Validated

| specialist_id | engine | hardware | date | notes |
|---|---|---|---|---|
| `qwen3-coder-30b-a3b-fp8` | vllm (FP8) | DGX Spark GB10, ~49 GB GPU free, `gpu-mem-util 0.12` stand-in | 2026-05-29 | discover + e2e route through MagicDNS host worked (2026-05-29 live capstone demo on the GB10 tailnet). **Caveat:** the 30B-FP8 itself OOMs on consumer GB10 sm_121 due to the vLLM 0.17 cutlass FP8 gap; validated via a Qwen2.5-7B stand-in under the same served-model-name — so this row proves the mesh plumbing (serve/heartbeat/discover/route), not the 30B weights on that box. |

## DRAFT — unvalidated

These cards' VRAM / runtime numbers are derived from upstream Ollama /
vLLM / llama.cpp specs and community benchmarks, but no slancha-mesh
bring-up has been run against them. Treat as "best-effort, please report
back" — the persistent `_validated: false` flag would require a card
schema change; today's marker is this file plus a leading DRAFT comment in
each TOML.

| specialist_id | engine | upstream | first-load box you'd expect to work |
|---|---|---|---|
| `nemotron-math-7b-q4` | vllm | `nvidia/OpenReasoning-Nemotron-7B` | any ≥ 8 GB VRAM CUDA + vLLM (Linux/WSL) |
| `ministral-3-8b-q4` | vllm | `mistralai/Ministral-3-8B-Instruct-2512` | any ≥ 8 GB VRAM CUDA + vLLM (Linux/WSL) |
| `qwen3-8b-q4` | vllm | `Qwen/Qwen3-8B` | any ≥ 8 GB VRAM CUDA + vLLM (Linux/WSL) |
| `phi-4-14b-q4` | vllm | `microsoft/phi-4` | any ≥ 14 GB VRAM CUDA + vLLM (Linux/WSL) |
| `ministral-3-8b-q4-ollama` | ollama | `mistralai/Ministral-3-8B-Instruct-2512` | any 8 GB box w/ Ollama (Mac M-series 16+ GB, RTX 3060+, Windows + Ollama) |
| `qwen2.5-coder-7b-q4-ollama` | ollama | `Qwen/Qwen2.5-Coder-7B-Instruct` | any 6 GB box w/ Ollama (Mac M-series 16+ GB, RTX 3060, GB10) |
| `phi-4-mini-q4-ollama` | ollama | `microsoft/Phi-4-mini-instruct` | tiny — Mac mini 8 GB, RTX 3060, Pi 5 + eGPU |
| `gemma-4-12b-q4-ollama` | ollama | `google/gemma-4-12B-it` | 8 GB+ Ollama box (multilingual fallback) |
| `ministral-3-14b-q4-ollama` | ollama | `mistralai/Ministral-3-14B-Instruct-2512` | 11 GB+ Ollama box (tools + reasoning) |
| `devstral-24b-q4-ollama` | ollama | `mistralai/Devstral-Small-2505` | 24 GB Ollama box (RTX 3090/4090; agentic code, the non-Chinese code card) |

A note on the `estimated_tps_at` tables in these cards: they are
**derived, not measured** — computed from the §3.2 roofline in
[`SIZING_BANDWIDTH_BRIEF.md`](SIZING_BANDWIDTH_BRIEF.md)
(`tps = MBU 0.8 × datasheet bandwidth ÷ runtime_gb`, batch-1, short-ctx
upper bound; MBU 0.8 per the measured 0.82 on the RTX PRO 6000). Each
TOML carries the derivation comment inline. The allocator uses these only
for tie-breaking when no live bandwidth probe exists; bring-up reports
with real tok/s numbers are exactly what moves a card out of DRAFT.

## Retired / repointed (2026-07 SOTA audit, #139 + #141)

Every handle below was verified live against HF `raw/main/config.json` on
2026-07-09 before this pass landed. Two cards pointed at HF repos that
**never existed** (`config.json` → 401, not 404 — Qwen repos are normally
ungated, so 401 means nonexistent, not gated); the rest were superseded by
a newer/better release at the same size or blocked by license. If you
bookmarked one of the old IDs below, it's gone — the replacement (if any)
is in the DRAFT table above.

| old specialist_id | what happened | why |
|---|---|---|
| `qwen3-coder-7b-q4` | **retired**, no replacement card | pointed at `Qwen/Qwen3-Coder-7B-Instruct`, which never shipped (Qwen3-Coder ships only as 30B-A3B / 480B-A35B / Coder-Next 80B-A3B). `qwen2.5-coder-7b-q4-ollama` is still the 7B code leader; `qwen3-coder-30b-a3b-fp8` already covers tier-1 code. |
| `qwen3-math-7b-q4` | repointed → `nemotron-math-7b-q4` | pointed at `Qwen/Qwen3-Math-7B-Instruct`, which never shipped. `nvidia/OpenReasoning-Nemotron-7B` (CC-BY-4.0) is the verified 7B math leader. |
| `aya-expanse-8b-q4` | repointed → `qwen3-8b-q4` | `CohereForAI/aya-expanse-8b` is **CC-BY-NC** — a commercial-use blocker, verified verbatim on the model card. |
| `llama-3.1-8b-instruct-q4` | repointed → `ministral-3-8b-q4` | Llama open line is frozen/EOL, no open successor. |
| `llama-3.1-8b-instruct-q5-ollama` | repointed → `ministral-3-8b-q4-ollama` | same reason; also dropped from Q5_K_M to Q4_K_M — Ollama's `ministral-3` library ships no Q5 tag. |
| `gemma-2-9b-q4-ollama` | repointed → `gemma-4-12b-q4-ollama` | Gemma 4 (2026-03) is the first Apache-2.0 Gemma release (gemma-2 was gemma-terms-of-use). |
| `mistral-nemo-12b-q4-ollama` | repointed → `ministral-3-14b-q4-ollama` | Ministral 3 (2512), Apache-2.0, native vision, direct successor class. |
| `phi-3.5-mini-q5-ollama` | repointed → `phi-4-mini-q4-ollama` | direct family successor; also dropped Q5_K_M → Q4_K_M for the same Ollama-tag-availability reason as Ministral above. |
| `deepseek-coder-v2-16b-lite-q4-ollama` | **retired**, no replacement card | DeepSeek discontinued the small-Coder line; `qwen3-coder-30b-a3b-fp8` already covers this role. |

Lane-hedge status: the retirements above initially left the **code** lane
all-Qwen; `devstral-24b-q4-ollama` (`mistralai/Devstral-Small-2505`,
Apache-2.0, verified live on HF + ollama.com) was added in the same PR to
close that gap, so every lane (code, math, multilingual, general,
reasoning) now keeps at least one non-Chinese-origin card.

## Why the brutal honesty

A catalog that silently ships cards "as if validated" is the failure
mode the LocalLLaMA crowd will spot immediately — top comment becomes
"I tried X, it OOM'd, this is fake." The opposite mistake — refusing
to ship any card without validation — leaves the catalog too thin to
demo the actual product (heterogeneous mesh routing). The compromise:
ship the cards, mark them DRAFT, document the bring-up criterion, accept
PRs from anyone who runs the bring-up and reports back.
