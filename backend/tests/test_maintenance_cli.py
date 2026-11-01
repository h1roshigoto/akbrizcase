"""Secrets must not reach argparse diagnostics or a nonsecure input fallback."""

from getpass import GetPassWarning
from unittest.mock import patch

import pytest

from app.modules.orders.maintenance_cli import main


def test_cli_does_not_echo_unknown_secret_argument(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        main(["audit", "--password", "SYNTHETIC-DO-NOT-ECHO"])
    captured = capsys.readouterr()
    assert "SYNTHETIC-DO-NOT-ECHO" not in captured.out + captured.err


@pytest.mark.parametrize("failure", [GetPassWarning, EOFError, KeyboardInterrupt])
def test_cli_refuses_insecure_input_before_database_access(
    capsys: pytest.CaptureFixture[str],
    failure: type[BaseException],
) -> None:
    with (
        patch("app.modules.orders.maintenance_cli.hidden", side_effect=failure),
        patch("app.modules.orders.maintenance_cli.Database") as database,
    ):
        assert main(["audit"]) == 1
        database.assert_not_called()
    assert "Maintenance outcome unconfirmed" in capsys.readouterr().err
