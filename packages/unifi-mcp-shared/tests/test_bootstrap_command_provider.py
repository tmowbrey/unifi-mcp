"""Security and lifecycle contract for the ``_COMMAND`` credential provider.

The helper is a program, so three properties are load-bearing and each has a
test here: it resolves nothing out of the working directory the MCP client
picked, nothing it writes to either stream is ever logged, and its whole process
tree dies when it runs out of time. The prose contract is in
``docs/credential-providers.md``.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import textwrap
import time
from pathlib import Path

import pytest
from unifi_mcp_shared import bootstrap

SENTINEL = "SENTINEL-helper-secret-4d91"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    for name in list(os.environ):
        if name.startswith("UNIFI_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(bootstrap, "_TRUSTED_VARS", None)
    yield


def _resolve(key: str = "password"):
    return bootstrap.resolve_env(key, env_prefix="NETWORK", logger=logging.getLogger("test"))


@pytest.fixture
def helper(tmp_path):
    """Write an executable /bin/sh helper and return its absolute path."""

    def _make(body: str, name: str = "helper.sh") -> Path:
        path = tmp_path / name
        path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    return _make


@contextlib.contextmanager
def _refused(caplog, level=logging.DEBUG):
    """Assert the block refuses startup with the credential exit code."""
    with caplog.at_level(level), pytest.raises(SystemExit) as exc:
        yield
    assert exc.value.code == 6


# ---------------------------------------------------------------------------
# P1 — the helper must not resolve anything out of the project directory
# ---------------------------------------------------------------------------


def test_project_directory_module_does_not_shadow_a_module_helper(monkeypatch, tmp_path, caplog):
    """`python -m helper` in an untrusted project must not import that project's helper.py."""
    marker = tmp_path / "SHADOW-RAN"
    (tmp_path / "helper.py").write_text(
        textwrap.dedent(f"""
            import pathlib
            pathlib.Path({str(marker)!r}).write_text("ran")
            print({SENTINEL!r})
        """),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("UNIFI_NETWORK_PASSWORD_COMMAND", f"{sys.executable} -m helper")

    with _refused(caplog):
        _resolve()

    assert not marker.exists(), "the project's helper.py was imported and executed"
    assert SENTINEL not in caplog.text


def test_project_directory_script_is_not_reachable_by_a_bare_name(monkeypatch, tmp_path, caplog, helper):
    """A bare executable name resolves on PATH, never against the project directory."""
    helper(f"printf '%s' {SENTINEL}", name="unifi-helper-shim")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("UNIFI_NETWORK_PASSWORD_COMMAND", "unifi-helper-shim")

    with _refused(caplog):
        _resolve()

    assert "PATH" in caplog.text


def test_relative_path_executable_is_refused_by_name(monkeypatch, tmp_path, caplog, helper):
    """`./helper` names the untrusted directory explicitly; refuse rather than guess."""
    helper(f"printf '%s' {SENTINEL}")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("UNIFI_NETWORK_PASSWORD_COMMAND", "./helper.sh")

    with _refused(caplog):
        _resolve()

    assert "absolute" in caplog.text.lower()


def test_absolute_helper_still_runs_and_its_value_is_used(monkeypatch, helper):
    monkeypatch.setenv("UNIFI_NETWORK_PASSWORD_COMMAND", str(helper("printf '%s' from-helper")))
    assert _resolve() == "from-helper"


def test_helper_does_not_run_in_the_project_directory(monkeypatch, tmp_path, helper):
    """The helper's cwd is a neutral directory, not the project the client opened."""
    project = tmp_path / "project"
    project.mkdir()
    path = helper("pwd", name="pwd-helper.sh")
    monkeypatch.chdir(project)
    monkeypatch.setenv("UNIFI_NETWORK_PASSWORD_COMMAND", str(path))

    where = _resolve()

    assert os.path.realpath(where) != os.path.realpath(project)


# ---------------------------------------------------------------------------
# P1 — a failing helper's stderr must not reach the log
# ---------------------------------------------------------------------------


def test_failing_helper_stderr_is_never_logged(monkeypatch, caplog, helper):
    monkeypatch.setenv("UNIFI_NETWORK_PASSWORD_COMMAND", str(helper(f"echo '{SENTINEL}' >&2\nexit 3", name="noisy.sh")))

    with _refused(caplog):
        _resolve()

    assert SENTINEL not in caplog.text
    # Fixed operation context stays: the variable, the executable and the status.
    assert "UNIFI_NETWORK_PASSWORD_COMMAND" in caplog.text
    assert "noisy.sh exited with status 3" in caplog.text


@pytest.mark.parametrize("exit_code", [0, 1])
def test_helper_stdout_is_never_logged(monkeypatch, caplog, helper, exit_code):
    """The credential arrives on stdout; neither a success nor a failure may log it."""
    path = helper(f"printf '%s' {SENTINEL}\nexit {exit_code}")
    monkeypatch.setenv("UNIFI_NETWORK_PASSWORD_COMMAND", str(path))

    with caplog.at_level(logging.DEBUG):
        if exit_code:
            with pytest.raises(SystemExit) as exc:
                _resolve()
            assert exc.value.code == 6
        else:
            assert _resolve() == SENTINEL

    assert SENTINEL not in caplog.text


# ---------------------------------------------------------------------------
# P2 — the timeout must own the whole process tree
# ---------------------------------------------------------------------------


def test_timeout_terminates_helper_descendants(monkeypatch, tmp_path, caplog, helper):
    """A descendant that outlives the direct child must not survive the timeout."""
    marker = tmp_path / "DESCENDANT-RAN"
    path = helper(f"(sleep 1; touch {marker}) &\nsleep 10", name="spawner.sh")
    monkeypatch.setattr(bootstrap, "_SECRET_COMMAND_TIMEOUT_S", 0.3)
    monkeypatch.setenv("UNIFI_NETWORK_PASSWORD_COMMAND", str(path))

    with _refused(caplog):
        _resolve()

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        assert not marker.exists(), "a descendant of the helper outlived the timeout"
        time.sleep(0.1)


def test_timeout_message_names_the_variable_without_helper_text(monkeypatch, caplog, helper):
    path = helper(f"echo '{SENTINEL}' >&2\nsleep 10", name="slow.sh")
    monkeypatch.setattr(bootstrap, "_SECRET_COMMAND_TIMEOUT_S", 0.3)
    monkeypatch.setenv("UNIFI_NETWORK_PASSWORD_COMMAND", str(path))

    with _refused(caplog):
        _resolve()

    assert SENTINEL not in caplog.text
    assert "UNIFI_NETWORK_PASSWORD_COMMAND" in caplog.text
    assert "did not finish" in caplog.text


# ---------------------------------------------------------------------------
# Contract carried over from the file provider
# ---------------------------------------------------------------------------


def test_command_from_dotenv_is_refused_when_a_snapshot_was_taken(monkeypatch, tmp_path, caplog, helper):
    marker = tmp_path / "RAN"
    path = helper(f"touch {marker}\nprintf '%s' {SENTINEL}")
    monkeypatch.setattr(bootstrap, "_TRUSTED_VARS", frozenset({"UNIFI_NETWORK_HOST"}))
    monkeypatch.setenv("UNIFI_NETWORK_PASSWORD_COMMAND", str(path))

    with _refused(caplog):
        _resolve()

    assert not marker.exists()
    assert ".env" in caplog.text


def test_helper_does_not_see_dotenv_injected_loader_variables(monkeypatch, helper):
    """A .env arriving after the snapshot must not reach the helper's environment."""
    path = helper('printf "%s" "${EVIL_FROM_DOTENV:-clean}"', name="echo-env.sh")
    monkeypatch.setattr(bootstrap, "_TRUSTED_VARS", frozenset(os.environ) | {"UNIFI_NETWORK_PASSWORD_COMMAND"})
    monkeypatch.setenv("EVIL_FROM_DOTENV", "1")  # arrives after the snapshot
    monkeypatch.setenv("UNIFI_NETWORK_PASSWORD_COMMAND", str(path))

    assert _resolve() == "clean"


def test_three_spellings_at_one_level_are_refused(monkeypatch, tmp_path, caplog):
    (tmp_path / "pw").write_text("x")
    monkeypatch.setenv("UNIFI_NETWORK_PASSWORD", "plain")
    monkeypatch.setenv("UNIFI_NETWORK_PASSWORD_FILE", str(tmp_path / "pw"))
    monkeypatch.setenv("UNIFI_NETWORK_PASSWORD_COMMAND", "/bin/true")

    with _refused(caplog):
        _resolve()

    assert "plain" not in caplog.text
