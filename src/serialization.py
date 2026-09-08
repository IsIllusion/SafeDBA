"""Canonical strict JSON shared by audit, incident state, and worker protocol.

This is not the lossy redaction or permissive observation serializer. Keeping
those separate preserves security boundaries and historical hash compatibility.
"""

import hashlib
import json


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def json_digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
