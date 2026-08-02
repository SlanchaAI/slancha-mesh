"""Small, truthful CLI for the optional tuning distribution."""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import subprocess
import sys
from pathlib import Path


def _check() -> int:
    modules = ("mesh.training", "mesh.replay_store", "mesh.eval.gate")
    missing = [name for name in modules if importlib.util.find_spec(name) is None]
    if missing:
        print(f"slancha-mesh-tune incomplete: missing {', '.join(missing)}", file=sys.stderr)
        return 1
    version = importlib.metadata.version("slancha-mesh-tune")
    print(f"slancha-mesh-tune {version}: training and evaluation modules available")
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

    if importlib.util.find_spec("streamlit") is None:
        print(
            "dashboard needs slancha-mesh-tune[dashboard]",
            file=sys.stderr,
        )
        return 1
    if remainder[:1] == ["--"]:
        remainder = remainder[1:]
    app_path = Path(__file__).with_name("dashboard") / "streamlit_app.py"
    try:
        return subprocess.call(
            [
                sys.executable,
                "-m",
                "streamlit",
                "run",
                str(app_path),
                "--",
                *remainder,
            ]
        )
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
