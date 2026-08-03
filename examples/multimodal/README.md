# Optional multimodal runtimes

These examples adopt an already-running LocalAI or vLLM-Omni server as a
Mesh specialist. Neither runtime, CUDA, model weights, nor a media codec is a
Slancha-Mesh dependency. Run the runtime on a specialist node, advertise only
the protocol capabilities that its loaded model actually supports, then send
media requests directly to Mesh `:8080`.

Chat remains behind vLLM Semantic Router `:8888`. Media bypasses that semantic
front door in the alpha because vLLM Semantic Router does not forward these
media endpoints. A typed local punt still returns to the caller; Barkeep or
another caller-owned policy layer decides whether a separate cloud request is
allowed.

## Network boundary

Bind the optional runtime to the node's Tailscale address, not a public or LAN
interface, and permit the port only from `tag:gateway`. The example cards use a
loopback `static_base_url`; pull discovery host-pins that URL to the specialist
peer before the gateway dials it. Do not expose LocalAI or vLLM-Omni with
Tailscale Funnel.

Set the direct Mesh URL and the specialist ID before using the examples:

```bash
export MESH_URL=http://127.0.0.1:8080
export MEDIA_SPECIALIST=replace-with-your-specialist-id
```

## LocalAI 4.7.1

[`localai/docker-compose.yml`](localai/docker-compose.yml) pins the runtime and
publishes its API only on a caller-supplied Tailscale address. It intentionally
contains no model. Configure model files under `./models`, then start it:

```bash
export TAILSCALE_IP="$(tailscale ip -4)"
docker compose -f examples/multimodal/localai/docker-compose.yml up -d
curl -fsS http://"${TAILSCALE_IP}":8090/v1/models
```

Copy [`localai/specialist.toml`](localai/specialist.toml), replace every
`REPLACE_` value and capacity estimate, and keep only capabilities proven by
the configured model. The supplied card conservatively advertises
transcription only. [`localai/protocols.json`](localai/protocols.json) maps all
LocalAI paths adopted by Mesh to the exact source-owned capability tokens.

```bash
# Image generation (JSON response)
curl -fsS "${MESH_URL}/v1/images/generations" \
  -H 'Content-Type: application/json' \
  -d '{"model":"'"${MEDIA_SPECIALIST}"'","prompt":"a red cube","response_format":"b64_json"}'

# Text to speech (binary response)
curl -fsS "${MESH_URL}/v1/audio/speech" \
  -H 'Content-Type: application/json' \
  -d '{"model":"'"${MEDIA_SPECIALIST}"'","input":"hello","voice":"default","response_format":"wav"}' \
  -o speech.wav

# Image edit (multipart)
curl -fsS "${MESH_URL}/v1/images/edits" \
  -F "model=${MEDIA_SPECIALIST}" -F 'prompt=make the background blue' \
  -F 'image=@input.png;type=image/png'

# Transcription (multipart)
curl -fsS "${MESH_URL}/v1/audio/transcriptions" \
  -F "model=${MEDIA_SPECIALIST}" -F 'file=@sample.wav;type=audio/wav'

# LocalAI synchronous video. Mesh requires inline output so node-relative
# URLs never leak across the trust boundary.
curl -fsS "${MESH_URL}/video" \
  -H 'Content-Type: application/json' \
  -d '{"model":"'"${MEDIA_SPECIALIST}"'","prompt":"a red cube rotates","response_format":"b64_json"}'
```

## vLLM-Omni 0.20.0

Install the optional runtime on a suitable accelerator node; this command can
download large dependencies, so it is an operator action, not part of Mesh
setup. Choose and pin a model/revision supported by vLLM-Omni, then bind only
to the node's Tailscale address:

```bash
uv pip install 'vllm-omni==0.20.0'
export TAILSCALE_IP="$(tailscale ip -4)"
vllm serve REPLACE_WITH_MODEL --omni --host "${TAILSCALE_IP}" --port 8091
```

Copy [`vllm-omni/specialist.toml`](vllm-omni/specialist.toml), replace every
`REPLACE_` value and capacity estimate, and advertise only the endpoint set
implemented by that one served model. The supplied card advertises asynchronous
video jobs only. [`vllm-omni/protocols.json`](vllm-omni/protocols.json) lists
the other adopted paths for separate model/card instances.

```bash
# Image generation
curl -fsS "${MESH_URL}/v1/images/generations" \
  -H 'Content-Type: application/json' \
  -d '{"model":"'"${MEDIA_SPECIALIST}"'","prompt":"a red cube","response_format":"b64_json"}'

# General audio generation (binary response)
curl -fsS "${MESH_URL}/v1/audio/generate" \
  -H 'Content-Type: application/json' \
  -d '{"model":"'"${MEDIA_SPECIALIST}"'","input":"soft ocean waves","audio_length":2}' \
  -o sound.wav

# Image edit
curl -fsS "${MESH_URL}/v1/images/edits" \
  -F "model=${MEDIA_SPECIALIST}" -F 'prompt=make the background blue' \
  -F 'image=@input.png;type=image/png'

# Create, poll, download, and delete an owner-pinned video job. The returned
# ID is a Mesh ID; keep using the same router so its durable owner map applies.
video_id=$(curl -fsS "${MESH_URL}/v1/videos" \
  -F "model=${MEDIA_SPECIALIST}" -F 'prompt=a red cube rotates' \
  -F 'width=512' -F 'height=512' -F 'num_frames=16' | jq -er '.id')
curl -fsS "${MESH_URL}/v1/videos/${video_id}"
curl -fsS "${MESH_URL}/v1/videos/${video_id}/content" -o output.mp4
curl -fsS -X DELETE "${MESH_URL}/v1/videos/${video_id}"
```

The adopted contracts come from LocalAI's
[image](https://localai.io/features/image-generation/),
[transcription](https://localai.io/features/audio-to-text/), and
[video](https://localai.io/features/video-generation/) API docs and
vLLM-Omni's [image edit](https://docs.vllm.ai/projects/vllm-omni/en/stable/serving/image_edit_api/),
[audio generation](https://docs.vllm.ai/projects/vllm-omni/en/stable/serving/audio_generate_api/),
and [video](https://docs.vllm.ai/projects/vllm-omni/en/stable/serving/videos_api/)
docs. WebSocket streaming video and vLLM-Omni's synchronous
`/v1/videos/sync` endpoint are not routed in this alpha.
