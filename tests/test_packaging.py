"""The plugin manifests point at real files and agree with the package version."""
import json, re, unittest
from pathlib import Path

import cc_estimate

ROOT = Path(__file__).resolve().parents[1]


def frontmatter(path):
    text = path.read_text()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    return dict(l.split(":", 1) for l in m.group(1).splitlines() if ":" in l) if m else {}


class Packaging(unittest.TestCase):
    def setUp(self):
        self.market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())

    def test_each_marketplace_plugin_has_a_manifest_with_the_same_name(self):
        for p in self.market["plugins"]:
            manifest = json.loads((ROOT / p["source"] / ".claude-plugin" / "plugin.json").read_text())
            self.assertEqual(manifest["name"], p["name"])

    def test_versions_agree(self):
        pyproject = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M).group(1)
        plugin = json.loads((ROOT / "plugin" / "cc-estimate" / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual({pyproject, plugin["version"], cc_estimate.__version__}, {pyproject})

    def test_every_skill_has_a_name_and_description(self):
        skills = list((ROOT / "plugin" / "cc-estimate" / "skills").glob("*/SKILL.md"))
        self.assertTrue(skills)
        for s in skills:
            fm = frontmatter(s)
            self.assertTrue(fm.get("name", "").strip() and fm.get("description", "").strip(), s)

    def test_the_skill_has_a_single_copy(self):
        self.assertFalse((ROOT / "skill").exists())


if __name__ == "__main__":
    unittest.main()
