import tempfile
import unittest
from pathlib import Path

from unityport import plugins


class PluginReportTest(unittest.TestCase):
    def test_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "Game_Data"
            (data / "Plugins" / "x86_64").mkdir(parents=True)
            (data / "Managed").mkdir()
            for name in ("steam_api64", "Unused", "lib_burst_generated"):
                (data / "Plugins" / "x86_64" / f"{name}.dll").write_bytes(b"MZ")
            (data / "Managed" / "Assembly-CSharp.dll").write_bytes(b"\0STEAM_API64\0")
            found = plugins.windows_plugins(data)
            self.assertEqual({p.path.name: p.used_by for p in found},
                             {"lib_burst_generated.dll": [], "steam_api64.dll": ["Assembly-CSharp.dll"],
                              "Unused.dll": []})
            text = plugins.report(found, data.name, "x86_64")
            self.assertIn("steam_api64.dll (used by Assembly-CSharp.dll)", text)
            self.assertIn("Game_Data/Plugins/x86_64/libsteam_api64.so", text)
            self.assertIn("Unused.dll (not named by any game assembly", text)
            self.assertIn("remove its use from the game's code", text)
            self.assertIn("Note: lib_burst_generated.dll", text)
            self.assertNotIn("lib_burst_generated.dll (", text)

    def test_lib_prefixed_name_kept(self):
        found = [plugins.Plugin(Path("Plugins/x86_64/libfoo.dll"), ["Assembly-CSharp.dll"])]
        text = plugins.report(found, "Game_Data", "x86_64")
        self.assertIn("Game_Data/Plugins/x86_64/libfoo.so", text)

    def test_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(plugins.report(plugins.windows_plugins(Path(tmp)), "Game_Data", "x86_64"), "")


if __name__ == "__main__":
    unittest.main()
