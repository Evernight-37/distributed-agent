"""Device-to-device transfer: authentication, range support and resumption."""

import hashlib
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from agent_harness import direct_transfer


def keypair():
    key = Ed25519PrivateKey.generate()
    return {
        "private": key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()).hex(),
        "public": key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex(),
    }


def b64key(value):
    import base64

    return base64.b64encode(bytes.fromhex(value)).decode("ascii")


class DirectTransferTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.source = self.root / "large.bin"
        self.payload = bytes(range(256)) * 4096  # 1 MiB
        self.source.write_bytes(self.payload)
        self.receiver = keypair()
        self.stranger = keypair()
        self.sender = direct_transfer.Sender(
            self.source,
            "transfer-1",
            b64key(self.receiver["public"]),
            host="127.0.0.1",
        )
        port = self.sender.start()
        self.addCleanup(self.sender.stop)
        self.url = "http://127.0.0.1:%d/transfer/transfer-1" % port

    def test_the_intended_recipient_gets_the_file_intact(self):
        destination = self.root / "out" / "large.bin"
        meta = direct_transfer.probe(
            self.url,
            b64key(self.receiver["private"]),
            b64key(self.receiver["public"]),
            "transfer-1",
        )
        self.assertEqual(meta["size"], len(self.payload))
        direct_transfer.fetch(
            self.url,
            b64key(self.receiver["private"]),
            b64key(self.receiver["public"]),
            "transfer-1",
            destination,
            expect_size=len(self.payload),
        )
        self.assertEqual(hashlib.sha256(destination.read_bytes()).hexdigest(), hashlib.sha256(self.payload).hexdigest())

    def test_a_stranger_cannot_fetch_it(self):
        with self.assertRaises(direct_transfer.TransferError):
            direct_transfer.probe(
                self.url,
                b64key(self.stranger["private"]),
                b64key(self.stranger["public"]),
                "transfer-1",
            )
        destination = self.root / "stolen.bin"
        with self.assertRaises(direct_transfer.TransferError):
            direct_transfer.fetch(
                self.url,
                b64key(self.stranger["private"]),
                b64key(self.stranger["public"]),
                "transfer-1",
                destination,
            )
        self.assertFalse(destination.exists())

    def test_a_stale_signature_is_refused(self):
        import time

        stale = int(time.time()) - 3600
        header = direct_transfer.authorization(
            b64key(self.receiver["private"]),
            b64key(self.receiver["public"]),
            "transfer-1",
            "/transfer/transfer-1/meta",
            stamp=stale,
        )
        self.assertFalse(self.sender._authorized("/transfer/transfer-1/meta", header))

    def test_an_interrupted_transfer_resumes_from_the_partial_file(self):
        destination = self.root / "out" / "resumed.bin"
        destination.parent.mkdir(parents=True)
        part = destination.with_name(destination.name + ".part")
        half = len(self.payload) // 2
        part.write_bytes(self.payload[:half])
        direct_transfer.fetch(
            self.url,
            b64key(self.receiver["private"]),
            b64key(self.receiver["public"]),
            "transfer-1",
            destination,
            expect_size=len(self.payload),
        )
        self.assertEqual(destination.read_bytes(), self.payload)

    def test_a_short_transfer_is_reported_instead_of_renamed(self):
        destination = self.root / "out" / "short.bin"
        with self.assertRaises(direct_transfer.TransferError):
            direct_transfer.fetch(
                self.url,
                b64key(self.receiver["private"]),
                b64key(self.receiver["public"]),
                "transfer-1",
                destination,
                expect_size=len(self.payload) + 1,
            )
        self.assertFalse(destination.exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
