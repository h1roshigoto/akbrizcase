"""Private service acceptance across Compose exposed/published port formats."""

import unittest

from notification_retry_browser_smoke import assert_private_ports


class PrivatePortTests(unittest.TestCase):
    def test_unexposed_and_unbound_expose_formats(self):
        assert_private_ports(None, {}, {})
        assert_private_ports([], None, {"5432/tcp": None})
        assert_private_ports(
            [{"URL": "", "TargetPort": 5432, "PublishedPort": 0, "Protocol": "tcp"}],
            {},
            {"5432/tcp": None},
        )

    def test_rejects_actual_bindings_even_when_summary_is_empty_or_zero(self):
        for publishers, configured, active in [
            ([{"URL": "127.0.0.1", "PublishedPort": 5432}], {}, {}),
            ([], {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "5432"}]}, {}),
            ([], {"5432/tcp": [{"HostIp": "", "HostPort": "0"}]}, {}),
            (
                [{"URL": "", "PublishedPort": 0}],
                {},
                {"5432/tcp": [{"HostIp": "0.0.0.0", "HostPort": "49152"}]},
            ),
        ]:
            with self.subTest(configured=configured, active=active):
                with self.assertRaises(AssertionError):
                    assert_private_ports(publishers, configured, active)

    def test_rejects_malformed_port_state(self):
        for publishers, configured, active in [
            ("invalid", {}, {}),
            ([{}], {}, {}),
            ([{"PublishedPort": False}], {}, {}),
            ([], [], {}),
            ([], {}, {"5432/tcp": {}}),
        ]:
            with self.subTest(
                publishers=publishers, configured=configured, active=active
            ):
                with self.assertRaises(AssertionError):
                    assert_private_ports(publishers, configured, active)
