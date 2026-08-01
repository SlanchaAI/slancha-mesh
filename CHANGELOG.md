# Changelog

This project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and intends to use semantic versioning after the alpha interface settles.

## Unreleased

### Added

- Stable typed-punt response for unsuitable or exhausted local routes.
- Per-binding runtime circuits and request-readiness health fields.
- Durable node and router service roles for systemd, launchd, and Windows.
- Validated vLLM Semantic Router composition example.

### Changed

- Fine-tuning, replay evaluation, promotion gates, and the dashboard now ship
  in the optional `slancha-mesh-tune` distribution.
- Core packaging excludes tests, tuning code, corpora, and training assets.

### Fixed

- Service pass-through arguments now retain the required `up` or `router`
  subcommand.
- Automatic routing filters open circuits before specialist selection and
  preserves the selector's chosen node as the primary binding.
- Router services default to their own artifact name, and service installation
  returns nonzero when registration or startup fails.
- `slancha-mesh-tune dashboard` now launches a real Streamlit server and exits
  cleanly on interruption.
