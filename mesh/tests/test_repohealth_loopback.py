"""Regression coverage for loopback bind classification without DNS or sockets."""

import os
import unittest
from unittest.mock import patch

from mesh.auth import _is_loopback_bind, assert_bind_safe


class LoopbackBindTests(unittest.TestCase):
    def test_loopback_addresses_and_existing_aliases(self):
        for host in ("127.0.0.1", "127.1.2.3", "localhost", "::1", "", " LOCALHOST "):
            with self.subTest(host=host):
                self.assertTrue(_is_loopback_bind(host))
                assert_bind_safe(host, token_present=False)

    def test_non_loopback_addresses_and_hostnames(self):
        for host in ("127.attacker.example", "127.0.0.1.example", "0.0.0.0", "192.0.2.1"):
            with self.subTest(host=host):
                self.assertFalse(_is_loopback_bind(host))

    def test_tokenless_hostnames_require_auth_without_opt_out(self):
        with patch.dict(os.environ):
            os.environ.pop("SLANCHA_AUTH_REQUIRED", None)
            for host in ("127.attacker.example", "127.0.0.1.example"):
                with self.subTest(host=host):
                    with self.assertRaises(SystemExit):
                        assert_bind_safe(host, token_present=False)


if __name__ == "__main__":
    unittest.main()
