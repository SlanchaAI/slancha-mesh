from __future__ import annotations

from mesh import tune_cli


def test_check_reports_installed_distribution_version(monkeypatch, capsys):
    monkeypatch.setattr(tune_cli.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(tune_cli.importlib.metadata, "version", lambda name: "9.9.9")

    assert tune_cli.main(["check"]) == 0
    assert capsys.readouterr().out == (
        "slancha-mesh-tune 9.9.9: training and evaluation modules available\n"
    )


def test_dashboard_reports_missing_extra_without_traceback(monkeypatch, capsys):
    monkeypatch.setattr(
        tune_cli.importlib.util,
        "find_spec",
        lambda name: None if name == "streamlit" else object(),
    )

    rc = tune_cli.main(["dashboard"])

    assert rc == 1
    assert "slancha-mesh-tune[dashboard]" in capsys.readouterr().err


def test_dashboard_launches_streamlit_server_with_app_arguments(monkeypatch):
    monkeypatch.setattr(tune_cli.importlib.util, "find_spec", lambda name: object())
    seen = []
    monkeypatch.setattr(tune_cli.subprocess, "call", lambda argv: seen.append(argv) or 0)

    rc = tune_cli.main(["dashboard", "--", "--operator", "/tmp/dashboard"])

    assert rc == 0
    command = seen[0]
    assert command[:4] == [tune_cli.sys.executable, "-m", "streamlit", "run"]
    assert command[4].endswith("mesh/dashboard/streamlit_app.py")
    assert command[5:] == ["--", "--operator", "/tmp/dashboard"]


def test_dashboard_ctrl_c_exits_without_traceback(monkeypatch):
    monkeypatch.setattr(tune_cli.importlib.util, "find_spec", lambda name: object())

    def interrupted(argv):
        raise KeyboardInterrupt

    monkeypatch.setattr(tune_cli.subprocess, "call", interrupted)

    assert tune_cli.main(["dashboard"]) == 130
