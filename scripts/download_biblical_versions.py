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


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LXX_COMMIT = "c91f6b1e8fb3ba37df701e6ae31f675ace71a2b2"
LXX_API = "https://api.github.com/repos/OpenScriptorium/lxx-morph/contents"
BRENTON_URL = "https://ebible.org/Scriptures/eng-Brenton_usfm.zip"
# eBible regenerates the (unversioned) zip, so the archive bytes are not stable.
# Pin the extracted USFM contents instead: SHA-256 of every book file.
BRENTON_USFM_SHA256 = {
    "00-FRTeng-Brenton.usfm": "6dcd75c8cc09dd24a25c9584bf632636a046f59f987343dcf227bc759bf4d0aa",
    "01-INTeng-Brenton.usfm": "7459d74ea8560c5087311ae05a30b8840606fc30e5bbf6bbcd55b0d12f8f0bc9",
    "02-GENeng-Brenton.usfm": "e2a893272a94fafd20a0efdc7fda0e0c72a2d623ac9d40ca5fd8d29f7d06035a",
    "03-EXOeng-Brenton.usfm": "a542eeecb12694d47adbda782746dd8604ec27001a7ff386c81084a6a98bb31a",
    "04-LEVeng-Brenton.usfm": "dac5ddb22d4ead14d64764332f2437c8ed1bd90c703ad9dc06a52c4437e65ee8",
    "05-NUMeng-Brenton.usfm": "89a288ff4c9161b04ed03942c15ae39072c699786edfb10af58fbc3a779c85fe",
    "06-DEUeng-Brenton.usfm": "544ee5d28db319e3d400cc596e082d8d39e0c06dba7decfdbfef38af84df9cf5",
    "07-JOSeng-Brenton.usfm": "d026604ec2eb73393f16b8137e65c1960552058ca39a817f23689421b0ffee5b",
    "08-JDGeng-Brenton.usfm": "a3fe9aa825e7cf458fef67256890149d364d3cf862d5c54b335930d6f793b541",
    "09-RUTeng-Brenton.usfm": "9acb07731583eda9b7ed8466b1ab2fddc2674b804583c4dcf00f11844af94c81",
    "10-1SAeng-Brenton.usfm": "5dc9e176628d4e64568c15a2c766a59647dde88cd72a7efbd4417395ce56dc03",
    "100-XXBeng-Brenton.usfm": "040292cb85aa66689bda5bd1df04d6ebf82aa6a88e9986e46944f09ac51487a8",
    "101-XXCeng-Brenton.usfm": "dcabe09054aa8d2c7fbdf381220065d35da42588925581733dd0c7f5205d7743",
    "11-2SAeng-Brenton.usfm": "20680eebfa8a90ac6561d0ac41627efb5ed586d60e56c50c12936d0a657a4c51",
    "12-1KIeng-Brenton.usfm": "4740049cac959b18ccd956ca1254f1f1d753f1cb4dc99ce83216af5d079cb286",
    "13-2KIeng-Brenton.usfm": "f9190dbfc9af272ae7dca54c4f31c97f5b9c35eacb65c9178c2bd2acdf217039",
    "14-1CHeng-Brenton.usfm": "70f457a282053e3a2f0bb186952ec759f85a006525aeb696ff2e756d017df415",
    "15-2CHeng-Brenton.usfm": "01d4fc6568e4dc8c950130b80f5b1ce4da00239be5fe68d79bff22593a43d200",
    "16-EZReng-Brenton.usfm": "865a7234643ed60d18d17a2b5f0cacd33de46780ecc7a3719559d542e5439ce9",
    "17-NEHeng-Brenton.usfm": "a6fd03ceae20d56dcc3959c86883dcb7cd991886c192cb0479ed0f70bdb24edc",
    "19-JOBeng-Brenton.usfm": "0fa566247526a5c0aa6255cbdb5f0fb47eb8ab0932220bcbb3b51f29659145e7",
    "20-PSAeng-Brenton.usfm": "12602415af372ce675b511eae878b94fd12e048dd882110a3ce130d0eeed8622",
    "21-PROeng-Brenton.usfm": "308af5a21c7f44f703d5f5b8cd6240d7fa19a20fb0ecc0ad996ea69f9b13bcd1",
    "22-ECCeng-Brenton.usfm": "e3336ad868ebfe8ce0c0a1a4a268e997d2fe4a533a8818f319178a8dc4bd4179",
    "23-SNGeng-Brenton.usfm": "386cb122c3a5a17e322214aa96cc260a2985d3555c85308da92f0b14f2d39852",
    "24-ISAeng-Brenton.usfm": "ecbec298da616ac51bc1acf9d9c77d197e5cf283d6bc13d1fddc1cf2b74585a0",
    "25-JEReng-Brenton.usfm": "bfb4da3699722a3947974db35490eda6d91a52f2c42a994ffc63610548ab0e51",
    "26-LAMeng-Brenton.usfm": "8bbc886d9507cac2c97d832b5fff53c389b61e7e0861284a0c756fe3487530aa",
    "27-EZKeng-Brenton.usfm": "ced36a383d829e2adf65a5a0d4d0fb63c0b19fcd6c913c1b247a6bbceb865837",
    "29-HOSeng-Brenton.usfm": "8aabfbab009e73180f7488943371f516eedb049f82f9eae89837812fcf0554d3",
    "30-JOLeng-Brenton.usfm": "a288df07320771b824f0c9228b4e1445df0167560de3e81511902542ca3634dd",
    "31-AMOeng-Brenton.usfm": "780a748c1791c11f01bc9e2d88bcf6a2f671705d862d144b012702bdb1bd80f2",
    "32-OBAeng-Brenton.usfm": "4a1534807a7fb218e368cd6c7bd0237919b304f5f746fddde3ef71b0a5cf144b",
    "33-JONeng-Brenton.usfm": "00eba3db479ff6b2221647ea990de1f2cc255525a0978ef5743ce1142eb7795b",
    "34-MICeng-Brenton.usfm": "7c8091709f4011d6d92771b9b6948bd13664ab26cc8f89bbf881033aebc67674",
    "35-NAMeng-Brenton.usfm": "ca061c9dd00400d8c62747addba5a1d5640580d74df6795f7169c944f9f8d3e2",
    "36-HABeng-Brenton.usfm": "303898e0114c07da1775d4c30486cafd79148d20b566ba15048676a5ff9e0345",
    "37-ZEPeng-Brenton.usfm": "74040a22bcfcd152f59074e237f73f02d92100671a80bf4dc9988e1452ed5d03",
    "38-HAGeng-Brenton.usfm": "f9a5d18c491183c94b769de2fe3e45f4ea90d700078913a630e5502d81142027",
    "39-ZECeng-Brenton.usfm": "8d37e97a11208189e559cd07a7cb17b23d22285e7db803ca9ecde4121db7320e",
    "40-MALeng-Brenton.usfm": "cc648f78cd5a9adbf423c9fefc5506f6d0793940fbb43e382374a666972b2493",
    "41-TOBeng-Brenton.usfm": "7577cc93b80697e2fdffcd5a584cc0bea3e14e32fabfd8249b0e94bda117c9b9",
    "42-JDTeng-Brenton.usfm": "ea2d3200f7a8df07ea545f8aab4c81af283bc345a6ddbf7fb051fee62fcfcfde",
    "43-ESGeng-Brenton.usfm": "802eaa26f9ed82c4f90e1a8906ba767ddba2ad8a43f28ba804e145bcb2f550ad",
    "45-WISeng-Brenton.usfm": "d830949475255fe2fc2ef75b9dff4cf6459e4d76e041e49379fcd8cfac1b4c88",
    "46-SIReng-Brenton.usfm": "bfea5307cd7861519072d11ea4ac5a0bda359fda3db5bd65777898e741acce28",
    "47-BAReng-Brenton.usfm": "c039e0e1324cf28f68f8db80e6ac5cf9f74fb3571533e2b3e42999227a21d92e",
    "48-LJEeng-Brenton.usfm": "d77ea94eb5301d25c087aee8b3f532ec190c1b2e10ebcef0279118e8bd7ca538",
    "50-SUSeng-Brenton.usfm": "608f30446689fa06c764595a94819def941edc25171f9cc27a1e1bc277dde328",
    "51-BELeng-Brenton.usfm": "da6eefa273d6a90383f2c83b8a825b1af946280d25850f5c79f00997be437459",
    "52-1MAeng-Brenton.usfm": "fede7fa79e0acdc0b11b1f1aba3208e8dc267d1170dec0b372b5c83f1bfe3ffd",
    "53-2MAeng-Brenton.usfm": "6de15136c844391cfd27eeec8728396ed73c7addbc8ea716caa23385ad958797",
    "54-1ESeng-Brenton.usfm": "332cc20ed61bf0cc620d950db3b70934676089ed1f51e08646aac9a9327c502f",
    "55-MANeng-Brenton.usfm": "9cfccbc3faa6e38130388baf1cc0f1062cbf9fa6216ad57d83dce5bf6d4d572d",
    "57-3MAeng-Brenton.usfm": "99d828e6907349d5fc109b0372a8dad13b1a7b16672fb516b8de9934cd163fc5",
    "59-4MAeng-Brenton.usfm": "e2a87a13d880b78c754b63064627a33ab730eaccc96af66913545bec65e94a2e",
    "66-DAGeng-Brenton.usfm": "dbc768d0006c457b320e3164a9d6a6dac87b2b228cf6c4c13b10006f8d36c435",
    "97-BAKeng-Brenton.usfm": "e7b9661f907ecab4f367c76d33ddedcc95de1cf8423f18a6f4f87485991f5961",
    "98-OTHeng-Brenton.usfm": "c92a439a7d34626302be4185cd559986e3f0000fa6a9723bbc0dd57b605069fe",
    "99-XXAeng-Brenton.usfm": "1f6c756fd1cc1b1ba2d85f984677a6c66ee026a4fa5525632b6c67f7e442f40a",
}
_FINAL_BOOK_RE = re.compile(r"^[a-z0-9-]+\.json$")
console = Console()


def _git_blob_sha(content: bytes) -> str:
    prefix = f"blob {len(content)}\0".encode()
    return hashlib.sha1(prefix + content).hexdigest()


def _fetch_verified(client: httpx.Client, destination: Path, url: str, expected_sha: str, force: bool) -> bytes:
    """Return file content matching a pinned Git blob hash, writing it only once verified.

    An existing file that does not match is reported and left untouched.
    """
    if destination.exists() and not force:
        content = destination.read_bytes()
        actual = _git_blob_sha(content)
        if actual != expected_sha:
            raise RuntimeError(
                f"Checksum mismatch for existing {destination}: {actual} != {expected_sha}. "
                "The file was left in place; rerun with --force to replace it."
            )
        return content
    response = client.get(url)
    response.raise_for_status()
    content = response.content
    actual = _git_blob_sha(content)
    if actual != expected_sha:
        raise RuntimeError(
            f"Checksum mismatch for downloaded {destination.name}: {actual} != {expected_sha}. "
            "Nothing was written."
        )
    destination.write_bytes(content)
    return content


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
        content = _fetch_verified(
            client, morphology_dir / item["name"], item["download_url"], item["sha"], force
        )
        manifest[f"morphology/{item['name']}"] = {
            "git_blob_sha1": item["sha"],
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
    mapping_content = _fetch_verified(
        client, target / "verse_pairs.jsonl", mapping_info["download_url"], mapping_info["sha"], force
    )
    manifest["verse_pairs.jsonl"] = {
        "git_blob_sha1": mapping_info["sha"],
        "bytes": len(mapping_content),
        "source_url": mapping_info["html_url"],
    }
    return manifest


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _download_brenton(client: httpx.Client, target: Path, force: bool) -> dict:
    """Install Brenton USFM whose per-book SHA-256 matches the pinned contents.

    Existing files are never deleted or overwritten unless --force is given and
    the downloaded replacement verifies.
    """
    brenton_dir = target / "brenton"
    existing = {
        name: (brenton_dir / name).read_bytes()
        for name in BRENTON_USFM_SHA256
        if (brenton_dir / name).is_file()
    }
    local_mismatches = sorted(
        name for name, content in existing.items() if _sha256(content) != BRENTON_USFM_SHA256[name]
    )
    if local_mismatches and not force:
        raise RuntimeError(
            f"Brenton files in {brenton_dir} do not match the pinned SHA-256 contents: "
            f"{', '.join(local_mismatches)}. They were left in place; rerun with --force "
            "to replace them with a verified download."
        )

    if force or len(existing) != len(BRENTON_USFM_SHA256):
        response = client.get(BRENTON_URL)
        response.raise_for_status()
        with zipfile.ZipFile(BytesIO(response.content)) as archive:
            downloaded = {
                Path(member.filename).name: archive.read(member)
                for member in archive.infolist()
                if Path(member.filename).name in BRENTON_USFM_SHA256
            }
        problems = sorted(
            f"{name} (missing from archive)" if name not in downloaded
            else f"{name} (sha256 {_sha256(downloaded[name])})"
            for name in BRENTON_USFM_SHA256
            if name not in downloaded or _sha256(downloaded[name]) != BRENTON_USFM_SHA256[name]
        )
        if problems:
            raise RuntimeError(
                f"The Brenton archive at {BRENTON_URL} no longer matches the pinned book "
                f"contents: {', '.join(problems)}. No files were written or deleted; update "
                "BRENTON_USFM_SHA256 after reviewing the upstream changes."
            )
        brenton_dir.mkdir(parents=True, exist_ok=True)
        for name, content in downloaded.items():
            (brenton_dir / name).write_bytes(content)
        existing = downloaded

    console.print(f"[green]✓[/green] Brenton USFM ({len(existing)} verified files)")
    return {
        "source_url": BRENTON_URL,
        "license": "Public domain",
        "files": {
            name: {"sha256": BRENTON_USFM_SHA256[name], "bytes": len(content)}
            for name, content in sorted(existing.items())
        },
    }


@click.command()
@click.option(
    "--data-dir",
    type=click.Path(path_type=Path),
    default=PROJECT_ROOT / "data" / "lxx",
    show_default=True,
)
@click.option("--force", is_flag=True, help="Redownload files already present")
def main(data_dir: Path, force: bool) -> None:
    """Download all 59 Rahlfs books, explicit MT mappings, and Brenton English."""
    data_dir.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": "studybible-mcp/1.0", "Accept": "application/vnd.github+json"}
    with httpx.Client(timeout=httpx.Timeout(180.0, connect=30.0), follow_redirects=True, headers=headers) as client:
        try:
            manifest = {
                "lxx_commit": LXX_COMMIT,
                "files": _download_lxx(client, data_dir, force),
                "brenton": _download_brenton(client, data_dir, force),
            }
        except RuntimeError as error:
            raise click.ClickException(str(error)) from error
    (data_dir / "source_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    console.print(f"\n[bold green]Sources verified in {data_dir}[/bold green]")


if __name__ == "__main__":
    main()
