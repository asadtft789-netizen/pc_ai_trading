"""Offline experiment registry interface. All timestamps come from the process clock."""
import argparse
from datetime import date
import json
from pathlib import Path

from campaign_calibration import register_calibration
from campaign_evidence import close_campaign, verify_eligibility
from campaign_registry import frozen_campaign, register


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", required=True, type=Path, help="existing authoritative Phoenix operational store")
    commands = p.add_subparsers(dest="action", required=True)
    commands.add_parser("register").add_argument("manifest", type=Path)
    commands.add_parser("calibrate").add_argument("design", type=Path)
    commands.add_parser("show").add_argument("campaign")
    commands.add_parser("close").add_argument("campaign")
    commands.add_parser("check-certificate").add_argument("expected", type=Path)
    args = p.parse_args(argv)
    if not args.db.is_file():
        p.error("use the existing operational store")
    path = args.db
    if args.action == "register":
        spec = json.loads(args.manifest.read_text(encoding="utf-8"))
        spec["first"] = date.fromisoformat(spec["first"])
        result = register(path, **spec)
    elif args.action == "calibrate":
        result = register_calibration(path, **json.loads(args.design.read_text(encoding="utf-8")))
    elif args.action == "show":
        result = frozen_campaign(path, args.campaign)
    elif args.action == "close":
        result = close_campaign(path, args.campaign)
    else:
        result = verify_eligibility(path, **json.loads(args.expected.read_text(encoding="utf-8")))
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return int(result is False)


if __name__ == "__main__":
    raise SystemExit(main())
