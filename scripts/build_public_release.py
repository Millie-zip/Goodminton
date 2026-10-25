"""Build a source-only public ZIP from an explicit, audited file list.

Uses only the Python standard library. Never reads private configuration,
runtime data, keyring credentials, or Git history.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import stat
import tempfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_FILES = (
    ".gitattributes",
    ".gitignore",
    "CONTRIBUTING.md",
    "MANIFEST.in",
    "README.md",
    "SECURITY.md",
    "config/booking.example.json",
    "docs/发布说明.md",
    "docs/项目逻辑说明.md",
    "pyproject.toml",
    "requirements.txt",
    "scripts/build_public_release.py",
    "scripts/install_tasks.ps1",
    "scripts/setup.ps1",
    "src/buaa_booking/__init__.py",
    "src/buaa_booking/__main__.py",
    "src/buaa_booking/artifacts.py",
    "src/buaa_booking/automation.py",
    "src/buaa_booking/calibration.py",
    "src/buaa_booking/captcha.py",
    "src/buaa_booking/cli.py",
    "src/buaa_booking/config.py",
    "src/buaa_booking/dashboard.py",
    "src/buaa_booking/models.py",
    "src/buaa_booking/notifier.py",
    "src/buaa_booking/secrets.py",
    "src/buaa_booking/store.py",
    "src/buaa_booking/tasks.py",
    "tests/test_availability.py",
    "tests/test_captcha.py",
    "tests/test_dates.py",
    "tests/test_release.py",
)
SENSITIVE_PATTERNS = {
    "personal absolute path": re.compile(r"(?i)(?:[a-z]:[\\/](?:Users|Millie)[\\/]|/(?:Users|home)/[\w.-]+/)"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "embedded access token": re.compile(r"(?i)[?&]token=[a-z0-9_-]{16,}"),
    "Bark device URL": re.compile(r"https://api\.day\.app/[a-zA-Z0-9_-]{16,}(?:[/\s\"']|$)"),
    "personal tailnet": re.compile(r"https?://(?!YOUR-PC\.YOUR-TAILNET\.ts\.net)[\w.-]+\.ts\.net", re.I),
}


def reject_links(root: Path, relative: str) -> Path:
    parts = PurePosixPath(relative).parts
    if not parts or ".." in parts or PurePosixPath(relative).is_absolute() or "\\" in relative:
        raise ValueError(f"Invalid public path: {relative}")
    current = root
    for part in parts:
        current = current / part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError(f"Links/reparse points are not allowed: {relative}")
    if not current.resolve().is_relative_to(root) or not current.is_file():
        raise ValueError(f"Not a regular project file: {relative}")
    return current


def collect_public_files(root: Path = ROOT) -> dict[str, bytes]:
    root = root.resolve()
    if len(PUBLIC_FILES) != len(set(PUBLIC_FILES)):
        raise ValueError("Duplicate public file")
    files = {}
    for relative in sorted(PUBLIC_FILES):
        content = reject_links(root, relative).read_bytes()
        text = content.decode("utf-8-sig")
        for label, pattern in SENSITIVE_PATTERNS.items():
            if pattern.search(text):
                # Do not echo the match: it may contain a credential.
                raise ValueError(f"Potential {label} in {relative}; review before publishing")
        if relative.endswith(".py"):
            ast.parse(text, filename=relative)
        files[relative] = content

    config = json.loads(files["config/booking.example.json"].decode("utf-8-sig"))
    if config["notification"]["provider"] != "console" or config["browser"]["captcha_mode"] != "manual":
        raise ValueError("Public example must default to console notifications and manual CAPTCHA")
    if config["dashboard"]["host"] not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Public example must bind to loopback")
    if config["browser"]["profile_dir"] != "data/browser-profile":
        raise ValueError("Public example must use the relative browser profile path")
    if config["dashboard"]["public_url"] != "https://YOUR-PC.YOUR-TAILNET.ts.net":
        raise ValueError("Public example must use the placeholder Dashboard URL")
    for relative, content in files.items():
        if relative.startswith("tests/") and re.search(rb"config[/\\]booking\.json", content):
            raise ValueError(f"Test depends on private configuration: {relative}")
    return files


def release_name(files: dict[str, bytes]) -> str:
    metadata = tomllib.loads(files["pyproject.toml"].decode("utf-8-sig"))["project"]
    name = f"{metadata['name']}-{metadata['version']}-public"
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+", name):
        raise ValueError("Invalid release name/version")
    return name


def build_release(root: Path = ROOT) -> Path:
    root = root.resolve()
    files = collect_public_files(root)
    name = release_name(files)
    destination = root / "dist"
    if destination.is_symlink() or destination.is_junction():
        raise ValueError("dist must be a regular project directory")
    destination.mkdir(exist_ok=True)
    output = destination / f"{name}.zip"
    checksum = output.with_suffix(output.suffix + ".sha256")
    for target in (output, checksum):
        if target.is_symlink() or target.is_junction():
            raise ValueError("Release output must not be a link")
    sums = "".join(f"{hashlib.sha256(content).hexdigest()}  {relative}\n" for relative, content in files.items())
    payload = {**files, "SHA256SUMS": sums.encode("utf-8")}
    with tempfile.NamedTemporaryFile(dir=destination, suffix=".zip.tmp", delete=False) as temp:
        temporary = Path(temp.name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for relative, content in sorted(payload.items()):
                entry = zipfile.ZipInfo(f"{name}/{relative}", date_time=(2020, 1, 1, 0, 0, 0))
                entry.create_system = 3
                entry.external_attr = (stat.S_IFREG | 0o644) << 16
                entry.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(entry, content)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    checksum.write_text(f"{hashlib.sha256(output.read_bytes()).hexdigest()}  {output.name}\n", encoding="utf-8")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check public files without creating an archive")
    args = parser.parse_args()
    if args.check:
        files = collect_public_files()
        print(f"Public files checked: {len(files)} ({release_name(files)})")
    else:
        output = build_release()
        print(f"Created: {output.relative_to(ROOT)}")
        print(f"Checksum: {output.with_suffix('.zip.sha256').relative_to(ROOT)}")


if __name__ == "__main__":
    main()
