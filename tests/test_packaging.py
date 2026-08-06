from __future__ import annotations

import json
import re
import struct
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
MARKETPLACE = PLUGIN_ROOT / ".agents" / "plugins" / "marketplace.json"
CLAUDE_MANIFEST = PLUGIN_ROOT / ".claude-plugin" / "plugin.json"
CLAUDE_MARKETPLACE = PLUGIN_ROOT / ".claude-plugin" / "marketplace.json"
HOOKS = PLUGIN_ROOT / "hooks" / "hooks.json"
CURSOR_MANIFEST = PLUGIN_ROOT / ".cursor-plugin" / "plugin.json"
CURSOR_HOOKS = PLUGIN_ROOT / "cursor" / "hooks.json"
RUNNER = PLUGIN_ROOT / "skills" / "test-once" / "scripts" / "test_once.py"
README = PLUGIN_ROOT / "README.md"
SKILL = PLUGIN_ROOT / "skills" / "test-once" / "SKILL.md"
BENCHMARK = PLUGIN_ROOT / "benchmarks" / "run_benchmark.py"
REAL_WORLD = PLUGIN_ROOT / "benchmarks" / "real-world.md"
DEMO = PLUGIN_ROOT / "assets" / "demo.gif"


class PackagingTest(unittest.TestCase):
    def test_runner_and_plugin_versions_match(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        runner = RUNNER.read_text(encoding="utf-8")
        match = re.search(r'^VERSION = "([^"]+)"$', runner, re.MULTILINE)

        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), manifest["version"])

    def test_claude_plugin_reuses_the_shared_skill_and_hooks(self) -> None:
        codex = json.loads(MANIFEST.read_text(encoding="utf-8"))
        claude = json.loads(CLAUDE_MANIFEST.read_text(encoding="utf-8"))
        marketplace = json.loads(CLAUDE_MARKETPLACE.read_text(encoding="utf-8"))
        hooks = json.loads(HOOKS.read_text(encoding="utf-8"))

        self.assertEqual(claude["name"], codex["name"])
        self.assertEqual(claude["version"], codex["version"])
        self.assertEqual(claude["skills"], "./skills/")
        self.assertEqual(marketplace["name"], "test-once")
        self.assertEqual(marketplace["version"], codex["version"])
        self.assertEqual(marketplace["plugins"][0]["source"], "./")
        self.assertEqual(
            marketplace["plugins"][0]["version"], codex["version"]
        )
        commands = [
            hook["command"]
            for groups in hooks["hooks"].values()
            for group in groups
            for hook in group["hooks"]
        ]
        self.assertTrue(commands)
        for command in commands:
            self.assertIn("${CLAUDE_PLUGIN_ROOT:-$PLUGIN_ROOT}", command)

    def test_cursor_plugin_uses_its_protocol_adapter(self) -> None:
        codex = json.loads(MANIFEST.read_text(encoding="utf-8"))
        cursor = json.loads(CURSOR_MANIFEST.read_text(encoding="utf-8"))
        hooks = json.loads(CURSOR_HOOKS.read_text(encoding="utf-8"))

        self.assertEqual(cursor["name"], codex["name"])
        self.assertEqual(cursor["version"], codex["version"])
        self.assertEqual(cursor["skills"], "./skills/")
        self.assertEqual(cursor["hooks"], "./cursor/hooks.json")
        self.assertEqual(hooks["version"], 1)
        self.assertEqual(hooks["hooks"]["preToolUse"][0]["matcher"], "Shell")
        commands = [
            hook["command"]
            for event_hooks in hooks["hooks"].values()
            for hook in event_hooks
        ]
        self.assertTrue(commands)
        for command in commands:
            self.assertIn("cursor/test_once_cursor_hook.py", command)
            self.assertIn("${CURSOR_PLUGIN_ROOT:-.}", command)

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

    def test_readme_has_install_paths_for_all_supported_agents(self) -> None:
        readme = README.read_text(encoding="utf-8")
        self.assertIn(
            "codex plugin marketplace add MascaraDeemo/test-once --ref main",
            readme,
        )
        self.assertIn("codex plugin add test-once@test-once", readme)
        self.assertIn(
            "claude plugin marketplace add MascaraDeemo/test-once", readme
        )
        self.assertIn("claude plugin install test-once@test-once", readme)
        self.assertIn("~/.cursor/plugins/local/test-once", readme)
        self.assertIn(".test-once.json", readme)
        self.assertIn(".codex/test-once.json", readme)

    def test_public_docs_use_calibrated_savings_semantics(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        readme = README.read_text(encoding="utf-8")
        skill = SKILL.read_text(encoding="utf-8")
        benchmark = BENCHMARK.read_text(encoding="utf-8")
        case_study = REAL_WORLD.read_text(encoding="utf-8")

        self.assertIn("warm-calibrated", manifest["description"])
        for public_text in (readme, skill, benchmark):
            self.assertNotIn("estimated_wall_seconds_saved", public_text)
        self.assertIn("calibrated_wall_seconds_saved", readme)
        self.assertIn("calibrated_wall_seconds_saved", skill)
        self.assertIn("calibrate --suite full-ut", readme)
        self.assertIn("bcef5c5bc68dddfb68a3d341f41fad44c11fb52e", case_study)
        self.assertIn("b465fdbfe175304d9b977da137b2c178ae1091d3", case_study)

    def test_demo_is_a_1200_by_640_animated_gif(self) -> None:
        content = DEMO.read_bytes()
        self.assertIn(content[:6], (b"GIF87a", b"GIF89a"))
        self.assertEqual(struct.unpack("<HH", content[6:10]), (1200, 640))
        self.assertGreater(content.count(b"\x21\xf9\x04"), 1)


if __name__ == "__main__":
    unittest.main()
