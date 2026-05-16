# Snapback

![Python](https://img.shields.io/badge/python-3.x-3776AB?style=flat-square&logo=python&logoColor=white)
![Dependencies](https://img.shields.io/badge/dependencies-zero-111827?style=flat-square)
![License: MIT](https://img.shields.io/badge/license-MIT-2ea44f?style=flat-square)

Small, dependable backups for project folders.

Snapback creates dated snapshots of your local projects, stores only changed file content, and lets you restore a
whole project or one lost file from a specific day. It is built for developers who have more important things to do
than babysit backup scripts.

![Snapback terminal preview](docs/snapback-terminal.svg)

## Why Snapback Exists

Git is great until the file was never committed.

Cloud sync is convenient until it syncs the mistake.

Full-folder copies work until they become huge, slow, and impossible to search.

Snapback sits in the quiet middle: a single Python CLI that keeps incremental, date-addressable backups of your
project directories on a local disk, external drive, or mounted volume.

## What It Does

- Scans every top-level directory inside `--src` as a separate project.
- Creates snapshots only when something changed.
- Stores new file content in compressed zip archives.
- Deduplicates file blobs by SHA-256, so identical content is stored once.
- Tracks project state in a small SQLite database.
- Restores a complete project as it looked on a date.
- Restores one specific file without unpacking an entire project.
- Skips common generated noise such as virtualenvs, build folders, caches, Git metadata, and compiled artifacts.

Snapback is not a Git replacement, a cloud backup service, or an encryption tool. It is a fast local safety net for
the messy parts of real project work.

## Quick Start

Clone the repository or download `snapback.py`, then run it with Python:

```bash
python3 snapback.py --help
```

Create a backup destination on a disk you trust:

```bash
mkdir -p /Volumes/Backup/snapback
```

Back up all projects inside `~/Projects`:

```bash
python3 snapback.py backup --src ~/Projects --dest /Volumes/Backup/snapback
```

List projects that have backups:

```bash
python3 snapback.py list --dest /Volumes/Backup/snapback
```

List snapshots for one project:

```bash
python3 snapback.py list --project myapp --dest /Volumes/Backup/snapback
```

See which files existed in a project on a date:

```bash
python3 snapback.py show \
  --project myapp \
  --date 2026-05-12 \
  --dest /Volumes/Backup/snapback
```

Restore the whole project:

```bash
python3 snapback.py restore \
  --project myapp \
  --date 2026-05-12 \
  --dest /Volumes/Backup/snapback \
  --out ./restored/myapp
```

Restore one file:

```bash
python3 snapback.py restore \
  --project myapp \
  --date 2026-05-12 \
  --file src/main.py \
  --dest /Volumes/Backup/snapback \
  --out ./restored/myapp
```

## Storage Layout

Snapback writes everything under the destination directory:

```text
/Volumes/Backup/snapback/
├── snapback.db
└── snapshots/
    └── 2026-05-12_21-44-08/
        └── data.zip
```

The SQLite database stores snapshot metadata and file state. Zip files store file content. If the same file content
appears again, Snapback links to the already stored blob instead of writing a duplicate.

## Practical Workflow

Run Snapback before risky edits, before dependency upgrades, or from a scheduled job:

```bash
python3 snapback.py backup --src ~/Projects --dest /Volumes/Backup/snapback
```

When something goes wrong, list the available snapshots, inspect the date you need, and restore only the project or
file you actually want back.

For macOS, a simple daily `launchd` job works well. On Linux, cron or a systemd timer is enough. Snapback has no
background daemon and no runtime dependencies outside the Python standard library.

## License

MIT License. See [LICENSE](LICENSE).
