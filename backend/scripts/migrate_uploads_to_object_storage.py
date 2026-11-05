"""Copy an existing install's uploads/ into the configured object store.

    cd backend
    BOW_STORAGE_BACKEND=s3 BOW_STORAGE_S3_BUCKET=... \\
        uv run python scripts/migrate_uploads_to_object_storage.py [--dry-run] [--force]

Run it once, before (or right after) switching ``storage.backend`` from
``local`` to ``s3`` / ``azure``, on a host that still has the old uploads/
volume mounted. Every file the app mirrors is uploaded under the same key the
app uses (``files/<uuid>_name``, ``thumbnails/<id>.png``, ...). Local-only
areas (git clones, PDF renders, compile caches) are skipped.

Idempotent and resumable: an object that already exists is left alone unless
``--force`` is given. Local files are never modified or deleted.
"""
import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=None, help="uploads directory (default: backend/uploads)")
    parser.add_argument("--dry-run", action="store_true", help="list what would be uploaded")
    parser.add_argument("--force", action="store_true", help="re-upload objects that already exist")
    args = parser.parse_args()

    from app.services import storage

    store = storage.get_store()
    if store is None:
        print("storage.backend is 'local' — set BOW_STORAGE_BACKEND=s3|azure (or bow-config.yaml) first.")
        return 2
    print(f"Target: {storage.check()}")

    root = Path(args.root) if args.root else storage.UPLOADS_ROOT
    if not root.is_dir():
        print(f"No uploads directory at {root}")
        return 2

    uploaded = skipped = existing = failed = 0
    total_bytes = 0
    started = time.monotonic()
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            key = storage.key_for(os.path.join(str(storage.UPLOADS_ROOT), rel))
            if key is None:
                skipped += 1
                continue
            try:
                if not args.force and store.etag(key) is not None:
                    existing += 1
                    continue
                size = os.path.getsize(path)
                if args.dry_run:
                    print(f"would upload {key} ({size} bytes)")
                else:
                    store.upload(key, path)
                uploaded += 1
                total_bytes += size
            except (OSError, storage.StorageError) as e:
                failed += 1
                print(f"FAILED {key}: {e}", file=sys.stderr)

    verb = "would upload" if args.dry_run else "uploaded"
    print(
        f"{verb} {uploaded} files ({total_bytes / 1e6:.1f} MB), {existing} already present, "
        f"{skipped} local-only skipped, {failed} failed in {time.monotonic() - started:.1f}s"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
