"""T3 synthetic file/SDK contracts, not live connector acceptance."""
import asyncio
import base64
from hashlib import sha256
import json
from pathlib import Path
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import canonical_fixture_admission
from sora_commander_next import file_reads, image_reads
from sora_commander_next.host import Host
from sora_commander_next.mcp_adapter import build_server, _validate_arguments
from sora_commander_next.policy import Policy

PNG = (b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR"
       + struct.pack(">IIBBBBB", 3, 2, 8, 2, 0, 0, 0) + bytes(4))
JPEG = b"\xff\xd8\xff\xc0\0\x0b\x08\0\x02\0\x03\x01\x01\x11\0\xff\xd9"


class ImageReadTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(inherited_test_temp())).resolve()
        self.enterContext(canonical_fixture_admission())
        self.host = Host(policy=Policy(roots=(), read_only_roots=(str(self.root),)))
        self.addCleanup(self.host.jobs.close)
        self.sdk = build_server(self.host)

    def call(self, name="picture.bin", data=PNG):
        path = self.root / name
        path.write_bytes(data)
        return asyncio.run(self.sdk.call_tool("read_image_file", {"path": str(path)}))

    def error(self, output, code):
        if isinstance(output, tuple):
            output = output[0]
        self.assertEqual(json.loads(output[0].text), {"status": "ERROR", "error": code})

    def test_png_jpeg_sdk_exact_content_order_read_only_root_and_hash(self):
        for raw, fmt, mime in ((PNG, "PNG", "image/png"), (JPEG, "JPEG", "image/jpeg")):
            with self.subTest(format=fmt):
                result = self.call(data=raw)
                if isinstance(result, tuple):
                    result = result[0]
                self.assertEqual([c.type for c in result], ["text", "image"])
                data = json.loads(result[0].text)
                self.assertEqual(data["format"], fmt)
                self.assertEqual([data["width"], data["height"]], [3, 2])
                self.assertEqual(data["bytes"], len(raw))
                self.assertEqual(data["sha256"], sha256(raw).hexdigest())
                self.assertEqual(result[1].mimeType, mime)
                self.assertEqual(base64.b64decode(result[1].data), raw)

    def test_not_image_and_limit(self):
        self.error(self.call("fake.png", b"ordinary text"), "NOT_IMAGE")
        self.error(self.call(data=PNG + bytes(image_reads.MAX_IMAGE_BYTES)), "IMAGE_TOO_LARGE")

    def test_exact_limit_and_jpeg_unknown_size(self):
        path = self.root / "image"
        raw = JPEG + bytes(image_reads.MAX_IMAGE_BYTES - len(JPEG))
        path.write_bytes(raw)
        self.assertEqual(image_reads.read_image(str(path), (str(self.root),))[0]["bytes"], len(raw))
        path.write_bytes(b"\xff\xd8\xff\xd9")
        data = image_reads.read_image(str(path), (str(self.root),))[0]
        self.assertIsNone(data["width"])
        self.assertIsNone(data["height"])

    def test_outside_denied_ads_traversal_and_short_names(self):
        path = self.root / "image.png"
        path.write_bytes(PNG)
        for value, roots in ((str(path), ()), (str(path), (str(self.root / "other"),)),
                             (str(path) + ":stream", (str(self.root),)),
                             (str(self.root / ".." / "image.png"), (str(self.root),)),
                             (str(self.root / "SECRET~1.PNG"), (str(self.root),))):
            with self.subTest(path=value), self.assertRaisesRegex(image_reads.ImageReadError, "PATH_NOT_ALLOWED"):
                image_reads.read_image(value, roots)
        for name in ("secret.png", ".env", "credentials.png"):
            self.error(self.call(name), "PATH_NOT_ALLOWED")

    def test_link_boundary_refused_without_creating_os_link(self):
        path = self.root / "image.png"
        path.write_bytes(PNG)
        real = Path.is_symlink
        with patch.object(Path, "is_symlink", lambda p: p == path or real(p)):
            with self.assertRaisesRegex(image_reads.ImageReadError, "PATH_NOT_ALLOWED"):
                image_reads.read_image(str(path), (str(self.root),))

    def test_reads_once_and_revalidates(self):
        path = self.root / "image.png"
        path.write_bytes(PNG)
        original = Path.open
        opens = []
        def opened(p, *args, **kwargs):
            opens.append((p, args))
            return original(p, *args, **kwargs)
        with patch.object(Path, "open", opened):
            image_reads.read_image(str(path), (str(self.root),))
        self.assertEqual(opens, [(path, ("rb",))])
        with patch.object(file_reads, "_verify_named_file", side_effect=file_reads.ReadBoundaryError("changed")):
            with self.assertRaises(image_reads.ImageReadError):
                image_reads.read_image(str(path), (str(self.root),))

    def test_no_approval_help_and_client_host_in_broker_mode(self):
        from sora_commander_next.owner_broker_dev import DevBrokerHost
        broker = object.__new__(DevBrokerHost)
        broker.policy = self.host.policy
        broker._call = Mock(side_effect=AssertionError("must not forward read"))
        sdk = build_server(broker)
        path = self.root / "image.png"
        path.write_bytes(PNG)
        with patch.object(Host, "run_command", side_effect=AssertionError("no approval")):
            result = asyncio.run(sdk.call_tool("read_image_file", {"path": str(path)}))
        broker._call.assert_not_called()
        tool = next(t for t in sdk._tool_manager.list_tools() if t.name == "read_image_file")
        for help_text in (tool.description, tool.parameters["properties"]["path"]["description"]):
            for word in ("image(item)", "content_items", "approval window", "4 MiB", "instructions"):
                self.assertIn(word, help_text)
        _validate_arguments("read_image_file", {"path": str(path)})
        with self.assertRaises(ValueError):
            _validate_arguments("read_image_file", {"path": str(path), "approve": True})


if __name__ == "__main__":
    unittest.main()
