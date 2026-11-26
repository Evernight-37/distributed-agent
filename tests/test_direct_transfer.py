"""Device-to-device transfer: authentication, range support and resumption."""

import hashlib
import tempfile
import time
import unittest
from functools import partial
from pathlib import Path
from unittest.mock import patch

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
        with self.assertRaisesRegex(direct_transfer.TransferError, "对方拒绝连接：403"):
            direct_transfer.probe(
                self.url,
                b64key(self.stranger["private"]),
                b64key(self.stranger["public"]),
                "transfer-1",
            )
        destination = self.root / "stolen.bin"
        with self.assertRaisesRegex(direct_transfer.TransferError, "传输被拒绝：403"):
            direct_transfer.fetch(
                self.url,
                b64key(self.stranger["private"]),
                b64key(self.stranger["public"]),
                "transfer-1",
                destination,
            )
        self.assertFalse(destination.exists())

    def test_an_authentic_request_with_clock_skew_is_refused_with_a_diagnostic(self):
        destination = self.root / "out" / "clock-skew.bin"
        sign = direct_transfer.authorization
        for offset in (-3600, 3600):
            with (
                self.subTest(offset=offset),
                patch.object(direct_transfer, "authorization", partial(sign, stamp=int(time.time()) + offset)),
            ):
                keys = (b64key(self.receiver["private"]), b64key(self.receiver["public"]))
                with self.assertRaisesRegex(direct_transfer.TransferError, "请校准两端时间后重试"):
                    direct_transfer.probe(self.url, *keys, "transfer-1")
                with self.assertRaisesRegex(direct_transfer.TransferError, "请校准两端时间后重试"):
                    direct_transfer.fetch(self.url, *keys, "transfer-1", destination)
        self.assertFalse(destination.exists())
        self.assertFalse(destination.with_name(destination.name + ".part").exists())

    def test_clock_diagnostic_requires_a_signature_for_the_exact_resource(self):
        sign = direct_transfer.authorization

        def sign_other_resource(private_key, public_key, transfer_id, resource):
            return sign(private_key, public_key, transfer_id, resource + "/other", stamp=int(time.time()) - 3600)

        with patch.object(direct_transfer, "authorization", sign_other_resource):
            with self.assertRaisesRegex(direct_transfer.TransferError, "对方拒绝连接：403"):
                direct_transfer.probe(
                    self.url, b64key(self.receiver["private"]), b64key(self.receiver["public"]), "transfer-1"
                )

        with patch.object(direct_transfer, "authorization", partial(sign, stamp=int(time.time()) - 3600)):
            with self.assertRaisesRegex(direct_transfer.TransferError, "对方拒绝连接：403"):
                direct_transfer.probe(
                    self.url, b64key(self.stranger["private"]), b64key(self.receiver["public"]), "transfer-1"
                )

    def test_an_interrupted_transfer_resumes_from_the_partial_file(self):
        destination = self.root / "out" / "resumed.bin"
        destination.parent.mkdir(parents=True)
        part = destination.with_name(destination.name + ".part")
        half = len(self.payload) // 2
        part.write_bytes(self.payload[:half])
        # A real interrupted transfer always leaves the provenance marker behind.
        part.with_name(part.name + ".json").write_text(
            '{"transfer_id": "transfer-1", "size": %d, "mtime": null, "digest": null}' % len(self.payload),
            encoding="utf-8",
        )
        seen = []
        direct_transfer.fetch(
            self.url,
            b64key(self.receiver["private"]),
            b64key(self.receiver["public"]),
            "transfer-1",
            destination,
            expect_size=len(self.payload),
            progress=seen.append,
        )
        self.assertEqual(destination.read_bytes(), self.payload)
        self.assertGreater(seen[0], half, "the download did not resume from the partial file")

    def test_a_changed_source_restarts_instead_of_splicing_two_versions(self):
        destination = self.root / "out" / "changed.bin"
        destination.parent.mkdir(parents=True)
        part = destination.with_name(destination.name + ".part")
        half = len(self.payload) // 2
        part.write_bytes(self.payload[:half])
        part.with_name(part.name + ".json").write_text(
            '{"transfer_id": "transfer-1", "size": %d, "mtime": null, "digest": "%s"}'
            % (len(self.payload), hashlib.sha256(self.payload).hexdigest()),
            encoding="utf-8",
        )
        changed = b"C" * half + self.payload[half:]
        self.source.write_bytes(changed)
        direct_transfer.fetch(
            self.url,
            b64key(self.receiver["private"]),
            b64key(self.receiver["public"]),
            "transfer-1",
            destination,
            expect_size=len(changed),
            expect_digest=hashlib.sha256(changed).hexdigest(),
        )
        self.assertEqual(destination.read_bytes(), changed)

    def test_a_body_that_does_not_match_the_sender_digest_is_discarded(self):
        destination = self.root / "out" / "wrong.bin"
        with self.assertRaises(direct_transfer.TransferError):
            direct_transfer.fetch(
                self.url,
                b64key(self.receiver["private"]),
                b64key(self.receiver["public"]),
                "transfer-1",
                destination,
                expect_size=len(self.payload),
                expect_digest="0" * 64,
            )
        self.assertFalse(destination.exists())
        self.assertFalse(destination.with_name(destination.name + ".part").exists())

    def test_an_unclaimed_sender_asks_for_a_retry_instead_of_refusing(self):
        sender = direct_transfer.Sender(self.source, "transfer-u", "", host="127.0.0.1")
        port = sender.start()
        self.addCleanup(sender.stop)
        url = "http://127.0.0.1:%d/transfer/transfer-u" % port
        with self.assertRaises(direct_transfer.TransferPending):
            direct_transfer.probe(url, b64key(self.receiver["private"]), b64key(self.receiver["public"]), "transfer-u")
        with self.assertRaises(direct_transfer.TransferPending):
            direct_transfer.fetch(
                url,
                b64key(self.receiver["private"]),
                b64key(self.receiver["public"]),
                "transfer-u",
                self.root / "out" / "pending.bin",
            )
        self.assertFalse((self.root / "out" / "pending.bin").exists())

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
