"""Run top-level Phoenix tests with isolated state and loopback-only Python networking."""
import ipaddress
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def guard(event, args):
    if event not in ("socket.connect", "socket.getaddrinfo", "socket.sendto"):
        return
    address = args[0] if event == "socket.getaddrinfo" else args[1]
    host = address[0] if isinstance(address, tuple) else address
    if host == "localhost":
        return
    try:
        if ipaddress.ip_address(host).is_loopback:
            return
    except ValueError:
        pass
    raise PermissionError("Offline Phoenix test attempted external networking")


def application_modules():
    excluded = {"skills", "venv", "__pycache__", "data"}
    for directory, dirs, files in os.walk(ROOT):
        dirs[:] = sorted(name for name in dirs if name not in excluded and not name.startswith("."))
        for filename in sorted(files):
            if filename.startswith("test_") and filename.endswith(".py"):
                path = (Path(directory) / filename).relative_to(ROOT).with_suffix("")
                yield ".".join(path.parts)


def main():
    for name in tuple(os.environ):
        if name.upper() not in {"PATH","PATHEXT","SYSTEMROOT","WINDIR","COMSPEC","TMP","TEMP","TMPDIR",
                                "LANG","LC_ALL","PYTHONUTF8","LD_LIBRARY_PATH","SYSTEMDRIVE","NUMBER_OF_PROCESSORS"}:
            del os.environ[name]
    sys.addaudithook(guard)
    vendor = sys.argv[1:] == ["--vendored"]
    modules = sys.argv[1:] or list(application_modules())
    with tempfile.TemporaryDirectory() as work:
        os.environ.update(HOME=work, USERPROFILE=work, PAPER_DB=str(Path(work)/"paper.sqlite"),
                          PYTHONPATH=str(ROOT), PYTHON_DOTENV_DISABLED="1")
        os.chdir(work)
        try:
            loader = unittest.defaultTestLoader
            suite = (loader.discover(str(ROOT / "skills"), top_level_dir=str(ROOT))
                     if vendor else loader.loadTestsFromNames(modules))
            return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
        finally:
            os.chdir(ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
