"""Small, truthful CLI for the optional tuning distribution."""

from __future__ import annotations

import argparse
import importlib.util
import sys


def _check() -> int:
    modules = ("mesh.training", "mesh.replay_store", "mesh.eval.gate")
    missing = [name for name in modules if importlib.util.find_spec(name) is None]
    if missing:
        print(f"slancha-mesh-tune incomplete: missing {', '.join(missing)}", file=sys.stderr)
        return 1
    print("slancha-mesh-tune 0.0.6: training and evaluation modules available")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="slancha-mesh-tune",
        description="Optional Slancha-Mesh training and evaluation tools.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="Verify that the add-on modules are importable.")
    sub.add_parser(
        "dashboard",
        help="Run the Streamlit dashboard renderer (requires the dashboard extra).",
    )
    args, remainder = parser.parse_known_args(argv)

    if args.command == "check":
        if remainder:
            parser.error("check accepts no additional arguments")
        return _check()

    try:
        from mesh.dashboard.streamlit_app import render
    except ImportError as exc:
        print(
            "dashboard needs slancha-mesh-tune[dashboard]: " + str(exc),
            file=sys.stderr,
        )
        return 1
    if remainder[:1] == ["--"]:
        remainder = remainder[1:]
    render(remainder)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

