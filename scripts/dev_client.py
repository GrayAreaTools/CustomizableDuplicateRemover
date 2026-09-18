#!/usr/bin/env python3
"""Development helper: run a GraphQL query against a live Stash using .env credentials.

For diagnosis and testing only. The plugin runtime never uses this — Stash passes a
session cookie in `server_connection`, so the plugin authenticates itself.

    python scripts/dev_client.py version
    python scripts/dev_client.py plugins
    python scripts/dev_client.py phash-coverage
    python scripts/dev_client.py raw '{version{version}}'
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_env(path: str | None = None) -> dict[str, str]:
    """Parse a .env file. Absent file is an error, since there is no safe fallback."""
    path = path or os.path.join(REPO_ROOT, ".env")
    if not os.path.exists(path):
        raise SystemExit(
            f"No .env at {path}. Copy .env.example to .env and fill in STASH_API_KEY.\n"
            f".env is gitignored and must stay that way."
        )
    env: dict[str, str] = {}
    with open(path, encoding="utf-8") as stream:
        for raw in stream:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip("'\"")
    return env


def call(query: str, variables: dict | None = None, *, timeout: float = 120.0) -> dict:
    env = load_env()
    url = env.get("STASH_URL", "").rstrip("/")
    api_key = env.get("STASH_API_KEY", "")
    if not url:
        raise SystemExit("STASH_URL is not set in .env")
    if not api_key:
        raise SystemExit("STASH_API_KEY is not set in .env")

    payload = json.dumps({"query": query, "variables": variables or {}}).encode()
    request = urllib.request.Request(
        f"{url}/graphql",
        data=payload,
        headers={"Content-Type": "application/json", "ApiKey": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read(2000).decode("utf-8", "replace")
        # Never echo the key itself, only whether one was sent.
        raise SystemExit(
            f"HTTP {exc.code} from {url}/graphql "
            f"(api key {'present' if api_key else 'missing'}): {detail[:400]}"
        ) from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SystemExit(f"Could not reach {url}: {exc}") from None

    if body.get("errors"):
        raise SystemExit(
            "GraphQL errors: "
            + "; ".join(str(e.get("message", e)) for e in body["errors"])
        )
    return body.get("data") or {}


QUERIES = {
    "version": "{ version { version hash } }",
    "plugins": "{ plugins { id name version enabled errors } }",
    "library": "{ configuration { general { stashes { path } } } }",
}


def phash_coverage() -> None:
    """How much of the library can participate in duplicate detection at all."""
    data = call(
        """
        {
          all: findScenes(filter: {per_page: 1}) { count }
          missing: findScenes(scene_filter: {is_missing: "phash"},
                              filter: {per_page: 1}) { count }
        }
        """
    )
    total = data["all"]["count"]
    missing = data["missing"]["count"]
    covered = total - missing
    pct = (covered / total * 100) if total else 0.0
    print(f"scenes total:      {total}")
    print(f"with a phash:      {covered}  ({pct:.1f}%)")
    print(f"missing a phash:   {missing}  (invisible to duplicate detection)")
    if missing:
        print("\nRun Generate with 'Perceptual hashes' enabled to cover the rest.")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        print("commands:", ", ".join(list(QUERIES) + ["phash-coverage", "raw"]))
        return 1

    command = sys.argv[1]
    if command == "phash-coverage":
        phash_coverage()
        return 0
    if command == "raw":
        if len(sys.argv) < 3:
            raise SystemExit("raw needs a query argument")
        print(json.dumps(call(sys.argv[2]), indent=2))
        return 0
    if command in QUERIES:
        print(json.dumps(call(QUERIES[command]), indent=2))
        return 0

    raise SystemExit(f"Unknown command '{command}'. Try: {', '.join(QUERIES)}")


if __name__ == "__main__":
    sys.exit(main())
