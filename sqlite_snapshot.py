"""Verified SQLite snapshots and restoration into a new file."""
import argparse
import os
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path


def verify(connection):
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise ValueError("SQLite integrity check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("SQLite foreign key check failed")


def copy_verified(source, destination, *, timeout=60):
    """Use SQLite's online backup API so committed WAL frames are included."""
    source, destination = Path(source).resolve(strict=True), Path(destination).absolute()
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".phoenix-", dir=destination.parent)
    os.close(fd)
    temporary = Path(name)
    deadline = time.monotonic() + timeout

    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise TimeoutError("SQLite snapshot exceeded its time budget")

    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as reader:
            with closing(sqlite3.connect(temporary)) as writer:
                reader.backup(writer, pages=128, progress=progress, sleep=0.05)
                # Publish a self-contained, closed file.
                writer.execute("PRAGMA journal_mode=DELETE")
                verify(writer)
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        os.link(temporary, destination)  # Atomic publication without clobbering a concurrent copy.
        if os.name == "posix":
            directory_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        return destination
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("backup", "restore"))
    parser.add_argument("source", type=Path)
    parser.add_argument("new_file", type=Path)
    args = parser.parse_args()
    copy_verified(args.source, args.new_file)
    print("Verified SQLite copy created", args.new_file)
