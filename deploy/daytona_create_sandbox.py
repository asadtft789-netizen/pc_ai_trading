#!/usr/bin/env python3
"""Create a Daytona sandbox for the self-evolving trading AI with a strict
outbound network egress policy.

Only three domains are reachable from inside the sandbox:
  - paper-api.alpaca.markets        (broker / market data)
  - generativelanguage.googleapis.com (Google AI Studio LLM)
  - openrouter.ai                   (OpenRouter LLM fallback)

Custom per-sandbox domainAllowList enforcement requires a Daytona Tier 3 or
Tier 4 organization account. If your account is lower-tier, the allowlist is
silently ignored and Daytona's default AI/ML allowlist applies instead — in
that case verify outbound access manually (see DEPLOY.md).
"""

from __future__ import annotations

import os
import sys

# The three domains the trader needs. A custom allowlist OVERRIDES Daytona's
# default AI/ML essential-services allowlist, so every required host must be
# listed explicitly — no defaults are inherited.
ALLOWED_DOMAINS = (
    "paper-api.alpaca.markets,"
    "generativelanguage.googleapis.com,"
    "openrouter.ai"
)

# networkBlockAll=True would override the allowlist and block everything.
# We must NOT set it (or must set it False) so the allowlist takes effect.
NETWORK_BLOCK_ALL = False


def create_sandbox():
    """Create a Daytona sandbox with the strict egress policy."""
    try:
        from daytona import Daytona, CreateSandboxFromSnapshotParams  # type: ignore
    except ImportError as e:
        print(
            "daytona SDK not installed. Install with: pip install daytona\n"
            f"Original error: {e}",
            file=sys.stderr,
        )
        return 1

    snapshot = os.environ.get("DAYTONA_SNAPSHOT", "daytona-small")
    region = os.environ.get("DAYTONA_REGION", "eu")

    daytona = Daytona()
    sandbox = daytona.create(
        CreateSandboxFromSnapshotParams(
            snapshot=snapshot,
            region=region,
            domain_allow_list=ALLOWED_DOMAINS,
            network_block_all=NETWORK_BLOCK_ALL,
            env_vars={
                "ALPACA_API_KEY": os.environ.get("ALPACA_API_KEY", ""),
                "ALPACA_API_SECRET": os.environ.get("ALPACA_API_SECRET", ""),
                "LLM_API_KEY": os.environ.get("LLM_API_KEY", ""),
                "LLM_FALLBACK_API_KEY": os.environ.get("LLM_FALLBACK_API_KEY", ""),
                "LLM_PROVIDER": os.environ.get("LLM_PROVIDER", "google"),
                "LLM_FALLBACK_PROVIDER": os.environ.get(
                    "LLM_FALLBACK_PROVIDER", "openrouter"
                ),
                "DATA_DIR": "data",
                "DB_PATH": "data/memory.sqlite",
            },
            labels={"project": "self_evolving_trader"},
        )
    )

    print(f"Sandbox created: {sandbox.id}")
    print(f"  snapshot: {snapshot}")
    print(f"  region:  {region}")
    print(f"  egress:  {ALLOWED_DOMAINS}")
    print()
    print("Verify outbound access from inside the sandbox:")
    print("  curl -I https://paper-api.alpaca.markets --max-time 10")
    print("  # Expect: HTTP/1.1 401 Unauthorized (reached Alpaca, no creds yet)")
    return 0


if __name__ == "__main__":
    raise SystemExit(create_sandbox())