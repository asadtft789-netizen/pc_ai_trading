"""Read the supervisor's display projection and the statistical release policy."""
import json
import os
from pathlib import Path
import re
import stat

from operational import freshness


def release_policy():
    try:
        from statistical_release import BLOCKER, RELEASED_PROTOCOLS
    except ModuleNotFoundError as error:
        if error.name != "statistical_release":
            raise
        return "F16_statistical_hold_integration_pending", frozenset()
    status = "protocol_release_review_required" if RELEASED_PROTOCOLS else BLOCKER
    return status, RELEASED_PROTOCOLS


def read_projection(path):
    path = Path(path)
    directory = path.parent.lstat()
    if not stat.S_ISDIR(directory.st_mode) or directory.st_mode & 0o022:
        raise ValueError("unsafe routing projection directory")
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, encoding="utf-8") as stream:
        attributes = os.fstat(stream.fileno())
        if (not stat.S_ISREG(attributes.st_mode) or attributes.st_uid != directory.st_uid
                or attributes.st_mode & 0o022 or attributes.st_size > 65536):
            raise ValueError("unsafe routing projection file")
        return json.loads(stream.read(65537))


def identity(value, length):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{" + str(length) + "}", value) is not None


def strategy_status(path, revision):
    evidence, released_protocols = release_policy()
    result = {"evidence": evidence, "routing": {"state": "unknown"}, "validated_strategies": []}
    try:
        report = read_projection(path)
        if (report["format"] != 1 or not identity(revision, 40)
                or report["revision"] != revision
                or freshness(report["as_of"], 45) != "current"):
            return result
        generation, routes = report["generation"], report["routes"]
        if type(generation) is not int or generation < 0 or not isinstance(routes, dict):
            return result
        allowed = {"global", "trend_up", "trend_down", "range_quiet", "vol_expansion"}
        if not routes or not set(routes) <= allowed:
            return result
        if any(candidate is not None and not identity(candidate, 64) for candidate in routes.values()):
            return result
        validated = []
        if not isinstance(report["validated"], list):
            return result
        for item in report["validated"]:
            if (identity(item["candidate"], 64) and identity(item["certificate"], 64)
                    and identity(item["protocol"], 64) and item["protocol"] in released_protocols):
                validated.append({"candidate": item["candidate"], "certificate": item["certificate"]})
        result.update(
            routing={"state": "observed", "as_of": report["as_of"], "generation": generation, "routes": routes},
            validated_strategies=validated,
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return result
