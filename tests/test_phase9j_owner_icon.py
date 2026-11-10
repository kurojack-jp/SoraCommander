"""Real Owner branding regression (NOT dummy art or a live plugin registration).

Source ICO already present on Owner's Windows PC was visually reviewed
against the 1254px PNG the Owner supplied in chat. The 128px PNG is
extracted byte-for-byte from that local ICO (not re-illustrated).
"""
import hashlib
from pathlib import Path
import struct
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
BRANDING = ROOT / "branding"
ICO = BRANDING / "SoraCommander.ico"
PNG = BRANDING / "plugin-icon.png"
APPROVED_PNG_SHA256 = "14a834e58cb7999472b53a83511d2e82dddc42cecbd710a2c14437bba8abbc52"

from sora_commander_next.plugin_icon import pinned_plugin_icon, inspect_icon_png
from sora_commander_next.release_inventory import (
    ASSETS, PLUGIN_ICON, PACKAGE, make_candidate, verify_candidate
)


class RealOwnerLogoTests(unittest.TestCase):
    def test_approved_image_sha_png_and_source_ico_integrity(self):
        png = PNG.read_bytes()
        self.assertEqual(hashlib.sha256(png).hexdigest(), APPROVED_PNG_SHA256)
        self.assertEqual(inspect_icon_png(png), (128, 128))
        source = ICO.read_bytes()
        self.assertEqual(source[:4], b"\x00\x00\x01\x00")
        entries = struct.unpack_from("<H", source, 4)[0]
        self.assertEqual(entries, 7)
        entry_offset = 6 + 5 * 16  # 128x128 PNG entry
        self.assertEqual((source[entry_offset], source[entry_offset+1]), (128,128))
        length,offset = struct.unpack_from("<II", source,entry_offset+8)
        self.assertEqual(source[offset:offset+length], png)

    def test_official_icon_is_registered_in_real_mcp_sdk(self):
        try:
            from mcp.types import Icon
        except ImportError:
            self.skipTest("MCP SDK unavailable in minimal Python -S mode")
        from sora_commander_next.host import Host
        from sora_commander_next.mcp_adapter import build_server
        host = Host()
        try:
            server = build_server(host, approved_icon_path=PNG,
                                  approved_icon_sha256=APPROVED_PNG_SHA256)
            self.assertEqual(len(server.icons), 1)
            self.assertEqual(server.icons[0].sizes, ["128x128"])
            self.assertEqual(len(server._tool_manager.list_tools()), 47)
            self.assertTrue(all(t.icons == server.icons for t in
                                server._tool_manager.list_tools()))
            self.assertFalse(host.status()["remote_ready"])
        finally:
            host.jobs.close()

    def test_signed_candidate_inventory_includes_actual_logo(self):
        with tempfile.TemporaryDirectory(prefix="sora-logo-") as tmp:
            dest=Path(tmp)/"stage"
            manifest_sha = make_candidate(ROOT/"src"/PACKAGE,dest,
                                          approved_icon_png=PNG)
            self.assertEqual((dest/ASSETS/PLUGIN_ICON).read_bytes(),PNG.read_bytes())
            self.assertTrue(verify_candidate(dest,
                trusted_manifest_sha256=manifest_sha))
            self.assertFalse((dest/PACKAGE/"__pycache__").exists())


if __name__=="__main__":
    unittest.main()
