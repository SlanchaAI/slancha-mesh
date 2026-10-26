"""Golden-vector coverage for public-key fingerprints; no crypto service needed."""

import base64
import importlib.util
from pathlib import Path
import unittest


# Load the standalone module without mesh.__init__ and its optional dependencies.
_SPEC = importlib.util.spec_from_file_location(
    "repohealth_identity", Path(__file__).resolve().parents[1] / "identity.py"
)
_IDENTITY = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_IDENTITY)
did_for = _IDENTITY.did_for


# Synthetic 32-byte public-key input, never a generated keypair.
PUBLIC_KEY = b"0123456789abcdef0123456789abcdef"
# Independently calculated with:
# printf '0123456789abcdef0123456789abcdef' | sha256sum
# Full digest: 3eb1bd439947eb762998e566ccc2e099c791118b2f40579cc4f7da2b5061b7f9
FINGERPRINT = "3eb1bd43"
PUBLIC_KEY_B64 = base64.b64encode(PUBLIC_KEY).decode("ascii")


class DidFingerprintTests(unittest.TestCase):
    def test_exact_golden_did(self):
        self.assertEqual(
            did_for("node-amber", PUBLIC_KEY_B64),
            f"did:wire:node-amber-{FINGERPRINT}",
        )

    def test_node_name_preserves_fingerprint(self):
        self.assertEqual(
            did_for("node-violet", PUBLIC_KEY_B64),
            f"did:wire:node-violet-{FINGERPRINT}",
        )

    def test_changed_key_changes_fingerprint(self):
        changed_key = PUBLIC_KEY[:-1] + b"g"
        changed_b64 = base64.b64encode(changed_key).decode("ascii")
        original = did_for("node-amber", PUBLIC_KEY_B64)
        changed = did_for("node-amber", changed_b64)
        self.assertNotEqual(changed.rsplit("-", 1)[1], original.rsplit("-", 1)[1])


if __name__ == "__main__":
    unittest.main()
