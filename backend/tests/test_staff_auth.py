"""Password and CLI boundaries without external services."""

import subprocess
import sys
from getpass import GetPassWarning
from unittest.mock import Mock

import pytest

from app.core import cli
from app.core.auth import has_role, hash_password, verify_password
from app.core.staff_models import StaffUser


def test_argon2id_random_salts_and_safe_verification() -> None:
    password = "synthetic individual password"
    first, second = hash_password(password), hash_password(password)
    assert first.startswith("$argon2id$v=19$m=65536,t=3,p=4$")
    assert first != second and password not in first
    assert verify_password(first, password)
    assert not verify_password(first, "incorrect password")
    assert not verify_password("malformed", password)
    assert not verify_password(first, "x" * 1025)


@pytest.mark.parametrize("password", ["", "short", "x" * 1025, "я" * 513])
def test_password_policy(password: str) -> None:
    with pytest.raises(ValueError):
        hash_password(password)


@pytest.mark.parametrize(
    ("role", "active", "manager", "admin"),
    [
        ("admin", True, True, True),
        ("manager", True, True, False),
        ("admin", False, False, False),
        ("manager", False, False, False),
    ],
)
def test_role_matrix(role: str, active: bool, manager: bool, admin: bool) -> None:
    user = StaffUser(role=role, is_active=active)
    assert has_role(user, "manager") is manager
    assert has_role(user, "admin") is admin


@pytest.mark.parametrize(
    "argv",
    [
        ["staff-create", "--role", "manager", "--password", "synthetic-secret"],
        ["staff-bootstrap", "--password", "synthetic-secret"],
        ["staff-disable", "--user-id", "synthetic-secret"],
        ["staff-create", "--role", "synthetic-secret"],
        ["staff-create", "--role", "admin", "--actor-id", "synthetic-secret"],
    ],
)
def test_cli_rejects_password_and_actor_arguments_without_echo(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as failure:
        cli.main(argv)
    assert failure.value.code == 2
    captured = capsys.readouterr()
    assert "synthetic-secret" not in captured.out + captured.err


def test_cli_confirmation_failure_never_connects(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    values = iter(["synthetic-admin", "synthetic-secret-one", "synthetic-secret-two"])
    monkeypatch.setattr(cli, "getpass", lambda prompt: next(values))
    database = Mock()
    monkeypatch.setattr(cli, "Database", database)
    assert cli.main(["staff-bootstrap"]) == 1
    database.assert_not_called()
    captured = capsys.readouterr()
    assert "synthetic-secret" not in captured.out + captured.err


def test_cli_getpass_warning_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    import warnings

    def unavailable(prompt: str) -> str:
        warnings.warn("unavailable", GetPassWarning, stacklevel=2)
        raise AssertionError("Must never fall back to echoing input")

    monkeypatch.setattr(cli, "getpass", unavailable)
    assert cli.main(["staff-bootstrap"]) == 1


def test_cli_without_terminal_refuses_piped_password() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "app.core.cli", "staff-bootstrap"],
        input="synthetic-admin\nsynthetic-secret-password\n",
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    assert "synthetic-secret-password" not in result.stdout + result.stderr
    assert "Secure input unavailable" in result.stderr
