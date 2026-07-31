from __future__ import annotations

import json
import re
import struct
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
MARKETPLACE = PLUGIN_ROOT / ".agents" / "plugins" / "marketplace.json"
RUNNER = PLUGIN_ROOT / "skills" / "test-once" / "scripts" / "test_once.py"
README = PLUGIN_ROOT / "README.md"
DEMO = PLUGIN_ROOT / "assets" / "demo.gif"


class PackagingTest(unittest.TestCase):
    def test_runner_and_plugin_versions_match(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        runner = RUNNER.read_text(encoding="utf-8")
        match = re.search(r'^VERSION = "([^"]+)"$', runner, re.MULTILINE)

        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), manifest["version"])

    def test_repo_marketplace_points_to_the_root_plugin(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        marketplace = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
        self.assertEqual(marketplace["name"], "test-once")
        self.assertEqual(len(marketplace["plugins"]), 1)
        entry = marketplace["plugins"][0]
        self.assertEqual(entry["name"], manifest["name"])
        self.assertEqual(
            entry["source"],
            {
                "source": "url",
                "url": "https://github.com/MascaraDeemo/test-once.git",
                "ref": "main",
            },
        )
        self.assertEqual(entry["policy"]["installation"], "AVAILABLE")
        self.assertEqual(entry["policy"]["authentication"], "ON_INSTALL")

    def test_readme_has_native_two_command_install(self) -> None:
        readme = README.read_text(encoding="utf-8")
        self.assertIn(
            "codex plugin marketplace add MascaraDeemo/test-once --ref main",
            readme,
        )
        self.assertIn("codex plugin add test-once@test-once", readme)

    def test_demo_is_a_1200_by_640_animated_gif(self) -> None:
        content = DEMO.read_bytes()
        self.assertIn(content[:6], (b"GIF87a", b"GIF89a"))
        self.assertEqual(struct.unpack("<HH", content[6:10]), (1200, 640))
        self.assertGreater(content.count(b"\x21\xf9\x04"), 1)


if __name__ == "__main__":
    unittest.main()
