# Deploy Database to Fly

This skill manages the study_bible.db database deployment lifecycle: download from Fly, upgrade and edit locally, then upload alongside the live database and swap it in atomically. Production must never be left without a database.

## Database Overview

- **Local path**: `db/study_bible.db` (~680MB before the evidence/LXX/Josephus upgrade, ~750MB after)
- **Remote path**: `/data/study_bible.db` on Fly volume (app: `studybible-mcp`)
- **Environment variable**: `STUDY_BIBLE_DB=/data/study_bible.db`
- The database is mounted via Fly.io persistent volume, NOT baked into the Docker image
- The code (tools.py, server.py, parsers) is deployed separately via `fly deploy`

## Step 1: Download from Fly (if needed)

If the local `db/study_bible.db` is stale or missing, download from the running Fly instance:

```bash
curl -o db/study_bible.db https://studybible-mcp.fly.dev/download/study_bible.db
```

This uses the `/download/study_bible.db` endpoint built into the server.

## Step 2: Upgrade the Schema and Sources

Always run the in-place upgrade after downloading and before uploading:

```bash
.venv/bin/python3 scripts/upgrade_database.py --database db/study_bible.db
```

It downloads the pinned LXX/Brenton and Josephus sources (files already present
are skipped; every file is checksum-verified), migrates the legacy textual-variant
tables, imports MT/LXX, Josephus, and the bundled variant cases, then runs
`PRAGMA integrity_check` and `PRAGMA foreign_key_check` on a temporary copy. The
original file is replaced only if every step succeeds, and re-running it is safe.
Use `--offline` when the sources are already under `data/`.

## Step 3: Make Other Local Changes (optional)

ANE context data lives in `data/ane_context/*.json` (13 dimension files). To re-import after editing:

```bash
# Clear old ANE data
sqlite3 db/study_bible.db "DELETE FROM ane_book_mappings; DELETE FROM ane_entries;"

# Re-import from JSON files
.venv/bin/python3 -c "
import sqlite3
from pathlib import Path
from scripts.build_database import import_ane_context
conn = sqlite3.connect('db/study_bible.db')
import_ane_context(conn, Path('data/ane_context'))
conn.close()
"
```

For a full database rebuild (all data, not just ANE):
```bash
.venv/bin/python3 scripts/build_database.py
```

## Step 4: Upload to Fly

`scripts/upload-db.sh` automates this step. The manual sequence is below. The live
database stays in place until the verified replacement is moved over it.

```bash
# 1. Compress locally and record the checksum of the uncompressed file
gzip -c db/study_bible.db > /tmp/study_bible.db.gz
sha256sum db/study_bible.db
ls -l db/study_bible.db /tmp/study_bible.db.gz

# 2. Check free space on the volume. /data must hold the live DB, the
#    compressed upload, and the decompressed replacement at the same time.
fly ssh console -a studybible-mcp -C "df -h /data"
fly ssh console -a studybible-mcp -C "ls -l /data"

# 3. Upload alongside the live DB under a temporary name
echo "put /tmp/study_bible.db.gz /data/study_bible.db.new.gz" | fly ssh sftp shell -a studybible-mcp

# 4. Decompress to a temporary name and remove the compressed copy
fly ssh console -a studybible-mcp -C "sh -c 'gzip -dc /data/study_bible.db.new.gz > /data/study_bible.db.new && rm /data/study_bible.db.new.gz'"

# 5. Verify size and checksum match the local values from step 1
fly ssh console -a studybible-mcp -C "ls -l /data/study_bible.db.new"
fly ssh console -a studybible-mcp -C "sha256sum /data/study_bible.db.new"

# 6. Atomically replace the live DB (same filesystem, so mv is a rename)
fly ssh console -a studybible-mcp -C "mv /data/study_bible.db.new /data/study_bible.db"

# 7. Restart app to pick up new DB (frees the old file's space)
fly apps restart studybible-mcp

# 8. Clean up
rm -f /tmp/study_bible.db.gz
```

If step 5 shows a mismatch, delete `/data/study_bible.db.new` and repeat from
step 3; the live database is untouched.

### If the volume is too small

The upgraded database (~750MB) plus the live one (~680MB) and the compressed
upload (~300MB) need about 1.75GB, so the default 1GB volume is too small for
this upgrade. If `df` shows less free space than the
compressed plus uncompressed sizes, extend the volume first (volumes can grow
but not shrink; the machine may restart):

```bash
fly volumes list -a studybible-mcp
fly volumes extend <volume-id> --size 3 -a studybible-mcp
fly ssh console -a studybible-mcp -C "df -h /data"
```

Never free space by deleting the live `/data/study_bible.db`.

## Step 5: Verify

```bash
# Health check
curl -s https://studybible-mcp.fly.dev/health | python3 -m json.tool

# Exercise every tool against the local copy you uploaded
.venv/bin/python3 scripts/smoke_server.py --db db/study_bible.db
```

## Code Deployment (separate from DB)

When only code changes (tools.py, server.py, parsers, system_prompt) — no DB changes:

```bash
fly deploy
```

This rebuilds the Docker image and deploys. The persistent volume with the DB is NOT affected.

## Important Notes

- **Order when both code and data change**: upgrade and upload the database first, then `fly deploy` the code straight away. The previous code keeps serving every other tool from the upgraded database, but its `get_textual_variant` reads the dropped legacy table and returns an error until the new code is live. If the code goes out first instead, the new `get_textual_variant` tells callers to run `scripts/upgrade_database.py`, and the newly added tools have no data until the database is replaced.
- **NEVER use raw sftp for the full uncompressed file**: it silently truncates and produces a corrupt DB. Always upload the gzip, then decompress on the server and compare the checksum.
- **NEVER delete the live database before the replacement is verified**: upload under a temporary name and `mv` it over the old file.
- The download endpoint (`/download/study_bible.db`) serves the file from the running instance.
