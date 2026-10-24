import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from unityport import config


class SourcesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.settings = Path(tmp.name) / "unityport" / "settings.json"
        for patcher in (mock.patch.object(config, "SETTINGS", self.settings),
                        mock.patch.object(config, "_overrides", {})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_defaults(self):
        self.assertEqual(config.download_base("abc123"), "https://download.unity3d.com/download_unity/abc123/")
        self.assertTrue(config.releases_api("2022.3.62f2").endswith("?version=2022.3.62f2"))

    def test_saved_values_used_and_defaults_not_stored(self):
        config.save({"download_url": "https://mirror.example/{rev}",
                      "releases_api": config.DEFAULTS["releases_api"]})
        self.assertEqual(json.loads(self.settings.read_text()), {"download_url": "https://mirror.example/{rev}"})
        # A trailing slash is added, as the file names are appended
        self.assertEqual(config.download_base("abc123"), "https://mirror.example/abc123/")
        config.save(dict(config.DEFAULTS))
        self.assertEqual(json.loads(self.settings.read_text()), {})

    def test_override_beats_saved(self):
        config.save({"download_url": "https://mirror.example/{rev}/"})
        config.override("download_url", "https://other.example/u/{rev}/")
        self.assertEqual(config.download_base("abc123"), "https://other.example/u/abc123/")

    def test_bad_values_rejected(self):
        self.assertIsNotNone(config.check("download_url", "https://mirror.example/"))
        self.assertIsNotNone(config.check("releases_api", "ftp://x/{version}"))
        with self.assertRaises(SystemExit):
            config.override("download_url", "https://mirror.example/")
        # A hand-edited bad value falls back to the default
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text(json.dumps({"download_url": "nonsense"}))
        self.assertEqual(config.get("download_url"), config.DEFAULTS["download_url"])

    def test_arch(self):
        self.assertEqual(config.get("arch"), "x86_64")
        config.save({"arch": "x86"})
        self.assertEqual(config.get("arch"), "x86")
        self.assertIsNotNone(config.check("arch", "x64"))

    @unittest.skipIf(os.name == "nt", "Linux layout")
    def test_old_linux_cache_moved(self):
        root = self.settings.parent.parent
        old = root / "xdg-cache" / "unityport" / "2022.3.62f2"
        old.mkdir(parents=True)
        home = root / "share" / "unityport"
        env = {k: v for k, v in os.environ.items() if k != "UNITYPORT_HOME"}
        env["XDG_CACHE_HOME"] = str(root / "xdg-cache")
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(config, "HOME", home), \
                mock.patch.object(config, "CACHE", home / "cache"):
            config.migrate()
        self.assertTrue((home / "cache" / "2022.3.62f2").is_dir())
        self.assertFalse(old.parent.exists())


if __name__ == "__main__":
    unittest.main()
