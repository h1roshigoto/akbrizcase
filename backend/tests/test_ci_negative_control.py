"""Temporary failure injection for the MVP-056 CI acceptance check."""


def test_ci_rejects_a_failing_test() -> None:
    raise AssertionError("MVP-056 deliberate CI negative control")
