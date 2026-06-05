from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent.browser_lifecycle import (
    BrowserSpec,
    MAC_APP_BY_BROWSER,
    PROCESS_NAME_BY_BROWSER,
    _process_name,
    count_processes,
    force_kill,
    graceful_close,
    launch,
    restart,
)


def test_process_name_known():
    assert _process_name("Chrome") == "chrome"
    assert _process_name("edge") == "msedge"
    assert _process_name("BRAVE") == "brave"


def test_process_name_unknown_raises():
    with pytest.raises(ValueError):
        _process_name("safari")


def test_browser_spec_defaults(tmp_path: Path):
    exe = tmp_path / "fake-chrome.exe"
    exe.write_bytes(b"")
    spec = BrowserSpec(name="chrome", executable=exe)
    assert spec.warmup_url == "https://work.1688.com"


def test_count_processes_parses_powershell_output(monkeypatch):
    monkeypatch.setattr("agent.browser_lifecycle.sys.platform", "win32")
    with patch("agent.browser_lifecycle.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="3\n")
        assert count_processes("chrome") == 3


def test_count_processes_handles_empty_output(monkeypatch):
    monkeypatch.setattr("agent.browser_lifecycle.sys.platform", "win32")
    with patch("agent.browser_lifecycle.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="")
        assert count_processes("chrome") == 0


def test_count_processes_handles_garbage(monkeypatch):
    monkeypatch.setattr("agent.browser_lifecycle.sys.platform", "win32")
    with patch("agent.browser_lifecycle.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="not a number")
        assert count_processes("chrome") == -1


def test_count_processes_macos_uses_pgrep(monkeypatch):
    monkeypatch.setattr("agent.browser_lifecycle.sys.platform", "darwin")
    with patch("agent.browser_lifecycle.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="10\n11\n")
        assert count_processes("chrome") == 2
        assert mock_run.call_args[0][0][:2] == ["pgrep", "-x"]
        assert mock_run.call_args[0][0][2] == "Google Chrome"


def test_graceful_close_parses_count(monkeypatch):
    monkeypatch.setattr("agent.browser_lifecycle.sys.platform", "win32")
    with patch("agent.browser_lifecycle.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="2\n")
        assert graceful_close("chrome") == 2


def test_graceful_close_macos_uses_osascript(monkeypatch):
    monkeypatch.setattr("agent.browser_lifecycle.sys.platform", "darwin")
    with patch("agent.browser_lifecycle.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        assert graceful_close("chrome") == 1
        assert mock_run.call_args[0][0][0] == "osascript"


def test_force_kill_zero_when_no_processes(monkeypatch):
    monkeypatch.setattr("agent.browser_lifecycle.sys.platform", "win32")
    with patch("agent.browser_lifecycle.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="0\n")
        # First call (count_processes) returns 0 — no processes to kill
        assert force_kill("chrome") == 0


def test_force_kill_counts_killed(monkeypatch):
    monkeypatch.setattr("agent.browser_lifecycle.sys.platform", "win32")
    call_count = {"n": 0}

    def fake_run(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return MagicMock(stdout="5\n")  # before: 5 procs
        if call_count["n"] == 2:
            return MagicMock(stdout="")  # the kill call
        return MagicMock(stdout="0\n")  # after: 0 procs

    with patch("agent.browser_lifecycle.subprocess.run", side_effect=fake_run):
        with patch("agent.browser_lifecycle.time.sleep"):
            killed = force_kill("chrome")
    assert killed == 5


def test_force_kill_by_executable_macos_matches_app_bundle(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("agent.browser_lifecycle.sys.platform", "darwin")
    app = tmp_path / "Google Chrome.app"
    binary = app / "Contents" / "MacOS" / "Google Chrome"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"")

    ps_output = f"111 {binary}\n222 /Applications/Other.app/Contents/MacOS/Other\n"
    with (
        patch("agent.browser_lifecycle.subprocess.run", return_value=MagicMock(returncode=0, stdout=ps_output)),
        patch("agent.browser_lifecycle.os.kill") as mock_kill,
    ):
        from agent.browser_lifecycle import force_kill_by_executable

        assert force_kill_by_executable(app) == 1
        mock_kill.assert_called_once()


def test_launch_missing_exe_returns_false(tmp_path: Path):
    spec = BrowserSpec(name="chrome", executable=tmp_path / "nonexistent.exe")
    assert launch(spec) is False


def test_launch_calls_popen(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("agent.browser_lifecycle.sys.platform", "win32")
    exe = tmp_path / "fake-chrome.exe"
    exe.write_bytes(b"")
    spec = BrowserSpec(name="chrome", executable=exe)
    with patch("agent.browser_lifecycle.subprocess.Popen") as mock_popen:
        assert launch(spec) is True
        mock_popen.assert_called_once()
        args = mock_popen.call_args[0][0]
        assert str(exe) in args
        assert "https://work.1688.com" in args


def test_launch_handles_oserror(tmp_path: Path):
    exe = tmp_path / "fake-chrome.exe"
    exe.write_bytes(b"")
    spec = BrowserSpec(name="chrome", executable=exe)
    with patch("agent.browser_lifecycle.subprocess.Popen", side_effect=OSError("denied")):
        assert launch(spec) is False


def test_launch_macos_app_uses_open(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("agent.browser_lifecycle.sys.platform", "darwin")
    app = tmp_path / "Google Chrome.app"
    app.mkdir()
    spec = BrowserSpec(name="chrome", executable=app)
    with patch("agent.browser_lifecycle.subprocess.Popen") as mock_popen:
        assert launch(spec) is True
        args = mock_popen.call_args[0][0]
        assert args[:3] == ["open", "-n", str(app)]
        assert "--args" in args


@pytest.mark.asyncio
async def test_restart_sequences_calls(tmp_path: Path):
    exe = tmp_path / "fake-chrome.exe"
    exe.write_bytes(b"")
    spec = BrowserSpec(name="chrome", executable=exe)

    calls: list[str] = []

    def fake_graceful(name):
        calls.append("graceful")
        return 1

    def fake_kill(executable):
        calls.append("kill")
        return 3

    def fake_launch(s):
        calls.append("launch")
        return True

    with patch("agent.browser_lifecycle.graceful_close", side_effect=fake_graceful), \
         patch("agent.browser_lifecycle.force_kill_by_executable", side_effect=fake_kill), \
         patch("agent.browser_lifecycle.launch", side_effect=fake_launch):
        result = await restart(spec, graceful_wait_secs=0, post_launch_wait_secs=0)

    assert calls == ["graceful", "kill", "launch"]
    assert result.browser == "chrome"
    assert result.graceful_window_count == 1
    assert result.force_killed == 3
    assert result.launch_ok is True
    assert result.reason == ""


@pytest.mark.asyncio
async def test_restart_launch_failure_marks_reason(tmp_path: Path):
    exe = tmp_path / "fake-chrome.exe"
    exe.write_bytes(b"")
    spec = BrowserSpec(name="chrome", executable=exe)

    with patch("agent.browser_lifecycle.graceful_close", return_value=0), \
         patch("agent.browser_lifecycle.force_kill_by_executable", return_value=0), \
         patch("agent.browser_lifecycle.launch", return_value=False):
        result = await restart(spec, graceful_wait_secs=0, post_launch_wait_secs=0)

    assert result.launch_ok is False
    assert result.reason == "launch_failed"


def test_known_browser_list():
    expected = {"chrome", "edge", "brave", "vivaldi", "opera", "chromium"}
    assert set(PROCESS_NAME_BY_BROWSER.keys()) == expected
    assert set(MAC_APP_BY_BROWSER.keys()) == expected
