"""Verify the operator contact in an actual signed Web Push request."""

from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time
import unittest
from unittest.mock import patch

from tasks_browser_push import configuration


@unittest.skipUnless(find_spec("pywebpush"), "Install the Web Push extra.")
class BrowserPushContactTest(unittest.TestCase):
    def test_signs_the_full_https_contact_and_keeps_mailto(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from py_vapid.jwt import decode
        from py_vapid.utils import b64urlencode
        from requests import Response

        private_key = ec.generate_private_key(ec.SECP256R1())
        device_key = ec.generate_private_key(ec.SECP256R1()).public_key()
        def point(key):
            return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)

        subscriber = {
            "endpoint": "https://fcm.googleapis.com/fcm/send/fictional",
            "p256dh": b64urlencode(point(device_key)),
            "auth": b64urlencode(bytes(16)),
            "enrollment": "fictional-device",
        }
        response = Response()
        response.status_code = 201

        with TemporaryDirectory() as directory:
            key_file = Path(directory) / "vapid.pem"
            key_file.write_bytes(private_key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
            ))
            for contact in ("https://example.org/projects/taskboard", "mailto:operator@example.test"):
                with self.subTest(contact=contact):
                    config = configuration({
                        "TASKS_WEB_PUSH_VAPID_KEY_FILE": str(key_file),
                        "TASKS_WEB_PUSH_CONTACT": contact,
                    })
                    with patch("requests.Session.post", return_value=response) as post:
                        self.assertEqual(config.send(subscriber, 60), 201)
                    post.assert_called_once()
                    endpoint, = post.call_args.args
                    self.assertEqual(endpoint, subscriber["endpoint"])
                    headers = {key.lower(): value for key, value in post.call_args.kwargs["headers"].items()}
                    scheme, credentials = headers["authorization"].split(" ", 1)
                    fields = dict(item.split("=", 1) for item in credentials.split(","))
                    token, public_key = fields["t"], fields["k"]
                    self.assertEqual(scheme, "vapid")
                    self.assertEqual(public_key, b64urlencode(point(private_key.public_key())))
                    self.assertEqual(public_key, config.public_key)
                    claims = decode(token, public_key)
                    self.assertEqual(claims["sub"], contact)
                    self.assertEqual(claims["aud"], "https://fcm.googleapis.com")
                    self.assertLess(time(), claims["exp"])
                    self.assertLessEqual(claims["exp"], time() + 24 * 3600)
                    self.assertEqual(headers["content-encoding"], "aes128gcm")
                    self.assertFalse(post.call_args.kwargs["allow_redirects"])

    def test_rejects_unsupported_contact_values(self):
        for contact in ("http://example.org/project", "https:///taskboard", "https://user@example.org/project"):
            with self.subTest(contact=contact), self.assertRaisesRegex(ValueError, "TASKS_WEB_PUSH_CONTACT"):
                configuration({
                    "TASKS_WEB_PUSH_VAPID_KEY_FILE": "/fictional/vapid.pem",
                    "TASKS_WEB_PUSH_CONTACT": contact,
                })
