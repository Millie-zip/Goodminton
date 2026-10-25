"""Public export boundary tests. No browser, keyring, or external services."""
import hashlib
import importlib.util
import json
import secrets
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from buaa_booking.config import load_config

spec = importlib.util.spec_from_file_location("public_release", ROOT / "scripts/build_public_release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class PublicReleaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for relative in release.PUBLIC_FILES:
            destination = self.root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, destination)

    def test_example_loads_without_private_configuration(self):
        config = load_config(self.root / "config/booking.example.json")
        self.assertEqual(config.notification.provider, "console")
        self.assertEqual(config.browser.captcha_mode, "manual")
        self.assertEqual(config.browser.profile_dir, self.root / "data/browser-profile")
        self.assertFalse((self.root / "config" / ("booking" + ".json")).exists())

    def test_export_excludes_private_data_and_verifies_checksums(self):
        marker = secrets.token_bytes(32)
        for relative in ("config/booking" + ".json", "data/state.sqlite3", "data/browser-profile/Cookies", ".git/config", "artifacts/private.png", "scripts/local_debug.py"):
            private = self.root / relative
            private.parent.mkdir(parents=True, exist_ok=True)
            private.write_bytes(marker)
        archive_path = release.build_release(self.root)
        with zipfile.ZipFile(archive_path) as archive:
            payload = {name.split("/", 1)[1]: archive.read(name) for name in archive.namelist()}
        self.assertEqual(set(payload), set(release.PUBLIC_FILES) | {"SHA256SUMS"})
        self.assertTrue(all(marker not in content for content in payload.values()))
        for line in payload["SHA256SUMS"].decode().splitlines():
            digest, relative = line.split("  ", 1)
            self.assertEqual(hashlib.sha256(payload[relative]).hexdigest(), digest)
        digest = archive_path.with_suffix(".zip.sha256").read_text().split()[0]
        self.assertEqual(hashlib.sha256(archive_path.read_bytes()).hexdigest(), digest)
        before = archive_path.read_bytes()
        self.assertEqual(release.build_release(self.root).read_bytes(), before)

    def test_personal_paths_and_access_links_block_publication(self):
        examples = (
            "D:" + "/Millie/private/config.txt",
            "https://private-machine.private-network" + ".ts.net",
            "https://example.test/?token=" + "a" * 32,
            "https://api.day.app/" + "a" * 24 + "/test",
            "-----BEGIN " + "PRIVATE KEY-----",
        )
        readme = self.root / "README.md"
        for sample in examples:
            with self.subTest(sample=sample[:12]):
                readme.write_text(sample, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "Potential"):
                    release.collect_public_files(self.root)

    def test_missing_public_file_blocks_publication(self):
        (self.root / "SECURITY.md").unlink()
        with self.assertRaises(FileNotFoundError):
            release.collect_public_files(self.root)

    def test_unsafe_template_blocks_publication(self):
        template = self.root / "config/booking.example.json"
        original = json.loads(template.read_text(encoding="utf-8"))
        for section, field, value in (("notification", "provider", "bark"), ("browser", "captcha_mode", "auto"), ("dashboard", "host", "0.0.0.0")):
            data = json.loads(json.dumps(original))
            data[section][field] = value
            template.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError):
                release.collect_public_files(self.root)

    def test_public_file_cannot_be_a_link(self):
        readme = self.root / "README.md"
        readme.unlink()
        try:
            readme.symlink_to(ROOT / "README.md")
        except OSError:
            self.skipTest("Creating symlinks requires OS permission")
        with self.assertRaisesRegex(ValueError, "Links/reparse"):
            release.collect_public_files(self.root)


if __name__ == "__main__":
    unittest.main()
