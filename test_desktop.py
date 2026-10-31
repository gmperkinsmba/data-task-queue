"""Native policy and owned backend lifecycle tests."""
import json
import plistlib
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from unittest.mock import patch

from app import make_server
from build_desktop import BUNDLE_ID, OWNER_MARKER, owned_bundle
from desktop_policy import navigation_policy


class DesktopTests(unittest.TestCase):
    def test_make_server_uses_ephemeral_loopback_without_reverse_dns(self):
        with tempfile.TemporaryDirectory() as runtime, patch("socket.getfqdn", side_effect=AssertionError("reverse DNS")):
            server = make_server(runtime, port=0)
            self.assertEqual(server.server_name, "127.0.0.1")
            self.assertGreater(server.server_port, 0)
            server.server_close()

    def test_dynamic_host_origin_and_owned_shutdown(self):
        with tempfile.TemporaryDirectory() as runtime:
            server = make_server(runtime, port=0)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                with urlopen(base + "/", timeout=3) as response:
                    self.assertEqual(response.status, 200)
                    self.assertIn(b"<h1>Data Task Queue</h1>", response.read())
                with urlopen(base + "/desktop.css", timeout=3) as response:
                    self.assertIn(b"32px", response.read())
                for headers in ({"Host": "evil.test"}, {"Origin": "http://127.0.0.1:1"}, {"Origin": "http://["}):
                    request = Request(base + "/api/state", headers=headers)
                    with self.assertRaises(HTTPError) as denied:
                        urlopen(request, timeout=3)
                    self.assertEqual(denied.exception.code, 403)
                request = Request(base + "/api/settings", data=b'{"scope":"sandbox"}', headers={"Content-Type":"application/json","X-Local-Request":"1"}, method="POST")
                with urlopen(request, timeout=3) as response:
                    self.assertEqual(json.loads(response.read())["scope"], "sandbox")
            finally:
                server.shutdown(); server.server_close(); worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(server.fileno(), -1)
            with self.assertRaises((URLError, OSError)):
                urlopen(base + "/", timeout=0.5)

    def test_navigation_policy_limits_remote_and_blob_navigation(self):
        origin = "http://127.0.0.1:49125"
        self.assertEqual(navigation_policy(origin + "/", origin, False, False), "allow")
        self.assertEqual(navigation_policy("blob:" + origin + "/export", origin, False, True), "download")
        self.assertEqual(navigation_policy("file:///etc/passwd", origin, False, False), "cancel")
        self.assertEqual(navigation_policy("https://example.com", origin, False, False), "cancel")
        self.assertEqual(navigation_policy("https://example.com", origin, True, False), "external")

    def test_owned_bundle_requires_expected_identity_and_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            bundle = Path(temp) / "Product.app"
            resources = bundle / "Contents" / "Resources"
            resources.mkdir(parents=True)
            with (bundle / "Contents" / "Info.plist").open("wb") as stream:
                plistlib.dump({"CFBundleIdentifier": BUNDLE_ID}, stream)
            marker = Path(__file__).with_name(OWNER_MARKER.name).read_bytes()
            (resources / OWNER_MARKER.name).write_bytes(marker)
            self.assertTrue(owned_bundle(bundle))
            (bundle / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "unowned.bundle"}))
            self.assertFalse(owned_bundle(bundle))


if __name__ == "__main__":
    unittest.main()
