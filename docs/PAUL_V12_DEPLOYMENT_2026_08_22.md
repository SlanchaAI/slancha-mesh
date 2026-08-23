# Paul V12 adopted-specialist deployment

## Catalog artifact

`mesh/catalog/paul-voice-v12.toml` registers the existing Spark renderer as an adopted external endpoint. The card identifies `paul-v12-hyperfit-merged`, the stable `paul-voice-v12` service name, writing domain, 4,096-token context, 64 layers, about 52 GB storage, and about 60 GB runtime memory. The card does not download, train, convert, or quantize the model.

## Producer and consumer

Producer: the Spark node daemon advertises `paul-voice-v12` through node-info port 8088 and points clients at its private model endpoint on port 8004.

Consumer: `slancha-mesh discover` walks the approved tailnet and returns a host-pinned route to the gateway or private client. Tailnet membership and the `tag:specialist` access-control policy remain the authentication boundary.

## Live evidence

- Mac discovery returned `paul-voice-v12` at `http://spark-472e.taila93596.ts.net:8004`.
- Spark node registry reported the specialist loaded and healthy.
- A private completion requested and returned `paul-voice-v12`, with one choice, usage, and no fallback.
- A later live client check returned `register_review_required` with an empty outbound body, confirming that mesh health does not bypass the human register gate.

## Verification and rollback

- Forty catalog and mesh tests passed before deployment.
- Strict catalog validation passed.
- The branch commit containing the catalog card is pushed to GitHub.
- Rollback removes the V12 card from the deployed node copy and restarts the node daemon; the model service itself remains independently supervised on Spark.
