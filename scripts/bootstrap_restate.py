"""Download and verify the official pinned runtime into an ignored local folder."""

import hashlib
import os
import platform
import tarfile
import urllib.request
from pathlib import Path

VERSION = "1.7.13"
ASSETS = {
    ("Darwin", "arm64"): ("aarch64-apple-darwin", "503c9a74eaad9b3579a373a5cd558b6910b179d17d87c2a75307cd975d1a299d"),
    ("Darwin", "x86_64"): ("x86_64-apple-darwin", "8bebb7787f003530e4959631111505b5003c87470e8ce3c070b6d02aff73ca89"),
    ("Linux", "x86_64"): ("x86_64-unknown-linux-musl", "5429b216b68f3fa52b7f0e6627a2c2a86fc0c75d19b1e152cd2e7dcd8bab4c82"),
    ("Linux", "aarch64"): ("aarch64-unknown-linux-musl", "c0218d5ae5052cc2c6f842bbcfa57fd10240cfe30795c381850c452fbcef2b61"),
}


def main():
    target, expected = ASSETS[(platform.system(), platform.machine())]
    tools = Path(__file__).resolve().parents[1] / ".tools"
    tools.mkdir(exist_ok=True)
    asset = f"restate-server-{target}.tar.xz"
    archive = tools / asset
    if not archive.exists():
        url = f"https://github.com/restatedev/restate/releases/download/v{VERSION}/{asset}"
        urllib.request.urlretrieve(url, archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
        archive.unlink()
        raise ValueError("Runtime archive failed SHA-256 verification")
    with tarfile.open(archive) as bundle:
        bundle.extractall(tools, filter="data")
    binary = tools / f"restate-server-{target}" / "restate-server"
    binary.chmod(binary.stat().st_mode | 0o111)
    print(f"Verified Restate {VERSION}: {binary}")
    print(f"RESTATE_SERVER={binary}")


if __name__ == "__main__":
    main()
