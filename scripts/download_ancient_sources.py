#!/usr/bin/env python3
"""Download checksum-verified ancient source corpora."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import click
import httpx
from rich.console import Console


PERSEUS_COMMIT = "01b725d835e6e733062ffd79e0efdbae1ba06e5c"
API_BASE = "https://api.github.com/repos/PerseusDL/canonical-greekLit/contents"
WORK_IDS = ("tlg001", "tlg002", "tlg003", "tlg004")
_TEXT_RE = re.compile(r"^tlg0526\.tlg00[1-4]\.perseus-(?:eng2|grc2)\.xml$")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
console = Console()


def _git_blob_sha(content: bytes) -> str:
    return hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()


def _download_josephus(client: httpx.Client, target: Path, force: bool) -> dict:
    target.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, dict] = {}
    for work_id in WORK_IDS:
        response = client.get(
            f"{API_BASE}/data/tlg0526/{work_id}",
            params={"ref": PERSEUS_COMMIT},
        )
        response.raise_for_status()
        items = [
            item for item in response.json()
            if item["type"] == "file"
            and (item["name"] == "__cts__.xml" or _TEXT_RE.fullmatch(item["name"]))
        ]
        if len(items) != 3:
            raise RuntimeError(f"Expected metadata, Greek, and English files for {work_id}")
        for item in items:
            filename = f"{work_id}.__cts__.xml" if item["name"] == "__cts__.xml" else item["name"]
            destination = target / filename
            if destination.exists() and not force:
                content = destination.read_bytes()
            else:
                download = client.get(item["download_url"])
                download.raise_for_status()
                content = download.content
                destination.write_bytes(content)
            actual = _git_blob_sha(content)
            if actual != item["sha"]:
                destination.unlink(missing_ok=True)
                raise RuntimeError(f"Checksum mismatch for {filename}: {actual} != {item['sha']}")
            if _TEXT_RE.fullmatch(item["name"]):
                decoded = content.decode("utf-8")
                if "creativecommons.org/licenses/by-sa/4.0/" not in decoded:
                    raise RuntimeError(f"Missing expected CC BY-SA 4.0 declaration in {filename}")
            manifest[filename] = {
                "git_blob_sha1": actual,
                "bytes": len(content),
                "source_url": item["html_url"],
            }
            console.print(f"[green]✓[/green] {filename}")
    return manifest


@click.command()
@click.option(
    "--data-dir",
    type=click.Path(path_type=Path),
    default=PROJECT_ROOT / "data" / "ancient_sources",
)
@click.option("--force", is_flag=True, help="Redownload files already present")
def main(data_dir: Path, force: bool) -> None:
    """Download Josephus now; this command is the extension point for later corpora."""
    headers = {"User-Agent": "studybible-mcp/1.0", "Accept": "application/vnd.github+json"}
    with httpx.Client(timeout=httpx.Timeout(180.0, connect=30.0), follow_redirects=True, headers=headers) as client:
        manifest = {
            "perseus_commit": PERSEUS_COMMIT,
            "license": "CC BY-SA 4.0",
            "josephus": _download_josephus(client, data_dir / "josephus", force),
        }
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "source_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    console.print(f"\n[bold green]Ancient sources verified in {data_dir}[/bold green]")


if __name__ == "__main__":
    main()
