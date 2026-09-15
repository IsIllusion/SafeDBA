"""Operator-only knowledge validation/publication. Never registered as a tool."""

import argparse
import json
from pathlib import Path

from knowledge_base import KnowledgeError, read_json, reviewed_bundle


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["validate", "publish"])
    parser.add_argument("--input", required=True, help="Local reviewed JSON payload")
    parser.add_argument(
        "--output", help="New bundle file; existing files are never overwritten"
    )
    parser.add_argument(
        "--approve-reviewed-content",
        action="store_true",
        help="Attest that sources, scope, expiry, text and secrets were reviewed",
    )
    args = parser.parse_args(argv)
    if args.command == "publish" and (
        not args.output or not args.approve_reviewed_content
    ):
        parser.error("Publication requires --output and --approve-reviewed-content.")
    try:
        bundle = reviewed_bundle(read_json(args.input))
        if args.command == "publish":
            # Exclusive creation: no replacement of active bundles or source files.
            with Path(args.output).open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(bundle, ensure_ascii=False) + "\n")
        print(
            json.dumps(
                {
                    "status": "published" if args.command == "publish" else "valid",
                    "documents": len(bundle["documents"]),
                    "sha256": bundle["sha256"],
                }
            )
        )
        return 0
    except (KnowledgeError, OSError):
        print(
            json.dumps(
                {
                    "status": "failed",
                    "reason": "Invalid input or unavailable output; no existing file was overwritten.",
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
