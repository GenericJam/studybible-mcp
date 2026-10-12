#!/usr/bin/env python3
"""Download pinned, licensed MT/LXX comparison sources."""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from io import BytesIO
from pathlib import Path

import click
import httpx
from rich.console import Console


LXX_COMMIT = "c91f6b1e8fb3ba37df701e6ae31f675ace71a2b2"
LXX_API = "https://api.github.com/repos/OpenScriptorium/lxx-morph/contents"
BRENTON_URL = "https://ebible.org/Scriptures/eng-Brenton_usfm.zip"
BRENTON_SHA256 = "5813078f2213e472a3d4946ccf468a6e17cf145f7b0f7afd154dced36203c6d3"
_FINAL_BOOK_RE = re.compile(r"^[a-z0-9-]+\.json$")
console = Console()


def _git_blob_sha(content: bytes) -> str:
    prefix = f"blob {len(content)}\0".encode()
    return hashlib.sha1(prefix + content).hexdigest()


def _download_lxx(client: httpx.Client, target: Path, force: bool) -> dict:
    morphology_dir = target / "morphology"
    morphology_dir.mkdir(parents=True, exist_ok=True)
    response = client.get(
        f"{LXX_API}/db/seeds/lxx_morph",
        params={"ref": LXX_COMMIT},
    )
    response.raise_for_status()
    items = [
        item for item in response.json()
        if item["type"] == "file"
        and _FINAL_BOOK_RE.fullmatch(item["name"])
        and not item["name"].endswith(".auto.json")
    ]
    if len(items) != 59:
        raise RuntimeError(f"Pinned LXX tree has {len(items)} final books; expected 59")

    manifest: dict[str, dict] = {}
    for item in sorted(items, key=lambda entry: entry["name"]):
        destination = morphology_dir / item["name"]
        if destination.exists() and not force:
            content = destination.read_bytes()
        else:
            content = client.get(item["download_url"]).content
            destination.write_bytes(content)
        actual = _git_blob_sha(content)
        if actual != item["sha"]:
            destination.unlink(missing_ok=True)
            raise RuntimeError(f"Checksum mismatch for {item['name']}: {actual} != {item['sha']}")
        manifest[f"morphology/{item['name']}"] = {
            "git_blob_sha1": actual,
            "bytes": len(content),
            "source_url": item["html_url"],
        }
        console.print(f"[green]✓[/green] {item['name']}")

    mapping_response = client.get(
        f"{LXX_API}/db/seeds/mt_alignment/verse_pairs.jsonl",
        params={"ref": LXX_COMMIT},
    )
    mapping_response.raise_for_status()
    mapping_info = mapping_response.json()
    mapping_path = target / "verse_pairs.jsonl"
    if mapping_path.exists() and not force:
        mapping_content = mapping_path.read_bytes()
    else:
        mapping_content = client.get(mapping_info["download_url"]).content
        mapping_path.write_bytes(mapping_content)
    mapping_sha = _git_blob_sha(mapping_content)
    if mapping_sha != mapping_info["sha"]:
        mapping_path.unlink(missing_ok=True)
        raise RuntimeError("Checksum mismatch for verse_pairs.jsonl")
    manifest["verse_pairs.jsonl"] = {
        "git_blob_sha1": mapping_sha,
        "bytes": len(mapping_content),
        "source_url": mapping_info["html_url"],
    }
    return manifest


def _download_brenton(client: httpx.Client, target: Path, force: bool) -> dict:
    brenton_dir = target / "brenton"
    existing = sorted(brenton_dir.glob("*.usfm")) if brenton_dir.exists() else []
    archive_path = target / "eng-Brenton_usfm.zip"
    if archive_path.exists() and not force:
        archive = archive_path.read_bytes()
    else:
        response = client.get(BRENTON_URL)
        response.raise_for_status()
        archive = response.content
        target.mkdir(parents=True, exist_ok=True)
        archive_path.write_bytes(archive)
    actual = hashlib.sha256(archive).hexdigest()
    if actual != BRENTON_SHA256:
        archive_path.unlink(missing_ok=True)
        raise RuntimeError(f"Brenton checksum mismatch: {actual} != {BRENTON_SHA256}")

    if force or not existing:
        brenton_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(BytesIO(archive)) as source:
            for member in source.infolist():
                filename = Path(member.filename)
                if filename.name != member.filename or filename.suffix.lower() != ".usfm":
                    continue
                (brenton_dir / filename.name).write_bytes(source.read(member))
    console.print(f"[green]✓[/green] Brenton USFM ({len(list(brenton_dir.glob('*.usfm')))} files)")
    return {
        "sha256": actual,
        "bytes": len(archive),
        "source_url": BRENTON_URL,
        "license": "Public domain",
    }


@click.command()
@click.option("--data-dir", type=click.Path(path_type=Path), default=Path("data/lxx"))
@click.option("--force", is_flag=True, help="Redownload files already present")
def main(data_dir: Path, force: bool) -> None:
    """Download all 59 Rahlfs books, explicit MT mappings, and Brenton English."""
    data_dir.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": "studybible-mcp/1.0", "Accept": "application/vnd.github+json"}
    with httpx.Client(timeout=httpx.Timeout(180.0, connect=30.0), follow_redirects=True, headers=headers) as client:
        manifest = {
            "lxx_commit": LXX_COMMIT,
            "files": _download_lxx(client, data_dir, force),
            "brenton": _download_brenton(client, data_dir, force),
        }
    (data_dir / "source_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    console.print(f"\n[bold green]Sources verified in {data_dir}[/bold green]")


if __name__ == "__main__":
    main()
