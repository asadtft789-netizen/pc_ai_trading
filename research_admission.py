"""Research-only limits. Reservations are charged even when a worker dies."""
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from paper_store import db, get, put


def reserve_lanes(path, lanes):
    if type(lanes) is not int or not 1 <= lanes <= 5:
        raise ValueError("invalid research batch")
    day = datetime.now(timezone.utc).date().isoformat()
    with db(path, True) as c:
        key = "development_reserved/" + day
        used = int(get(c, key, 0))
        if used < 0 or used + lanes > 100:
            return False
        put(c, key, used + lanes)
    return True


def available_mib():
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024
    except (OSError, ValueError):
        pass
    return None


class MemoryAdmission:
    def __init__(self, path):
        self.path = path
        with db(path) as c:
            self.paused = get(c, "research_memory_paused") == "YES"
        self.since = self.last = None

    def sample(self, available, tick):
        paused = self.paused
        if available is None or available < 256:
            paused = True
        if paused:
            if available is None or available < 384:
                self.since = None
            elif self.since is None or self.last is None or not 0 <= tick - self.last <= 65:
                self.since = tick
            elif tick - self.since >= 300:
                paused = False
        self.last = tick
        if paused != self.paused:
            with db(self.path, True) as c:
                put(c, "research_memory_paused", "YES" if paused else "NO")
            self.paused = paused
            self.since = None
        return not paused

    def eligible(self):
        memory_ok = self.sample(available_mib(), time.monotonic())
        try:
            disk_ok = shutil.disk_usage(Path(self.path).parent).free >= 1 << 30
        except OSError:
            disk_ok = False
        return memory_ok and disk_ok

    def wait(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.eligible()
            time.sleep(min(30, max(0, end - time.monotonic())))
