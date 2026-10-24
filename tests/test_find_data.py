import tempfile
import unittest
from pathlib import Path

from unityport import version


class FindDataTest(unittest.TestCase):
    def test_plain_data_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp)
            (game / "Data").mkdir()
            (game / "Data" / "globalgamemanagers").write_bytes(b"")
            (game / "Deepest Sword.exe").write_bytes(b"MZ")
            (game / "UnityCrashHandler64.exe").write_bytes(b"MZ")
            data = version.find_data_dir(game)
            self.assertEqual(data, game / "Data")
            self.assertEqual(version.game_name(game, data), "Deepest Sword")

    def test_named_data_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp)
            (game / "Game_Data").mkdir()
            (game / "Game.exe").write_bytes(b"MZ")
            data = version.find_data_dir(game)
            self.assertEqual(version.game_name(game, data), "Game")


if __name__ == "__main__":
    unittest.main()
