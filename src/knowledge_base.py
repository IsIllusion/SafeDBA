"""Bounded, local, reviewed reference knowledge; no model or database authority.

The deployment owns the bundle and its filesystem ACLs. Checksums detect
accidental changes, not malicious replacement by someone who can write it.
Sources are metadata only: this module never opens URLs or document paths.
"""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
from typing import Protocol
import unicodedata
from urllib.parse import urlsplit

from serialization import json_digest

MAX_BUNDLE_BYTES = 2_000_000
MAX_DOCUMENTS = 200
MAX_CHUNKS = 2_000
CHUNK_CHARS = 1_200
ENVIRONMENTS = {"development", "staging", "production", "benchmark"}
_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{0,79}\Z")
_TERMS = re.compile(r"[a-z0-9_]+|[\u3400-\u9fff]+")
_SECRET = re.compile(
    r"(?i)\b(?:password|api[_-]?key|access[_-]?token|secret)\s*[:=]\s*[^\s,;]{4,}"
    r"|\bbearer\s+[a-z0-9._~-]{8,}|\bsk-[a-z0-9_-]{12,}"
    r"|\b[a-z][a-z0-9+.-]*://[^:/\s]+:[^@/\s]+@"
)


class KnowledgeError(ValueError):
    """A safe-to-display validation error; never include input text or paths."""


def _text(value, field, maximum):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(ord(char) < 32 and char not in "\n\r\t" for char in value)
        or any(0xD800 <= ord(char) <= 0xDFFF for char in value)
        or _SECRET.search(value)
    ):
        raise KnowledgeError(f"Invalid knowledge {field}.")
    return value


def _identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise KnowledgeError("Invalid knowledge identifier.")
    return value


def parse_timestamp(value):
    _text(value, "timestamp", 64)
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise KnowledgeError("Invalid knowledge timestamp.") from None


def _list(value, field, validate, maximum=16):
    if not isinstance(value, list) or not 1 <= len(value) <= maximum:
        raise KnowledgeError(f"Invalid knowledge {field}.")
    items = [validate(item) for item in value]
    if len(set(items)) != len(items):
        raise KnowledgeError(f"Duplicate knowledge {field}.")
    return items


def _environment(value):
    if not isinstance(value, str) or value not in ENVIRONMENTS:
        raise KnowledgeError("Invalid knowledge environment.")
    return value


def _major(value):
    if type(value) is not int or not 10 <= value <= 99:
        raise KnowledgeError("Invalid knowledge PostgreSQL major version.")
    return value


@dataclass(frozen=True)
class KnowledgeScope:
    """Trusted deployment scope, never supplied through model tool arguments."""

    scope_id: str
    environment: str
    postgres_major: int

    def __post_init__(self):
        _identifier(self.scope_id)
        _environment(self.environment)
        _major(self.postgres_major)


def validate_document(value):
    required = {
        "id",
        "title",
        "source",
        "revision",
        "reviewed_by",
        "reviewed_at",
        "expires_at",
        "scope_ids",
        "environments",
        "postgres_majors",
        "text",
    }
    if not isinstance(value, dict) or set(value) != required | (
        {"keywords"} & value.keys()
    ):
        raise KnowledgeError("Invalid knowledge document fields.")
    result = dict(value)
    _identifier(result["id"])
    for field, maximum in (
        ("title", 200),
        ("source", 500),
        ("revision", 80),
        ("reviewed_by", 100),
        ("text", 40_000),
    ):
        _text(result[field], field, maximum)
    source = result["source"]
    try:
        url = urlsplit(source)
        valid_source = (
            url.scheme == "https"
            and bool(url.hostname)
            and not url.username
            and not url.password
            and not url.query
        ) or (source.startswith("urn:safedba:") and bool(source[12:]))
    except ValueError:
        valid_source = False
    if not valid_source or any(char.isspace() for char in source):
        raise KnowledgeError(
            "Knowledge source must be a credential-free HTTPS URL or SafeDBA URN."
        )
    reviewed = parse_timestamp(result["reviewed_at"])
    expires = parse_timestamp(result["expires_at"])
    if expires <= reviewed:
        raise KnowledgeError("Knowledge expiry must follow review time.")
    _list(result["scope_ids"], "scope IDs", _identifier)
    _list(result["environments"], "environments", _environment, 4)
    _list(result["postgres_majors"], "PostgreSQL versions", _major)
    if "keywords" in result:
        _list(
            result["keywords"], "keywords", lambda item: _text(item, "keyword", 80), 32
        )
    return result


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise KnowledgeError("Duplicate knowledge JSON key.")
        result[key] = value
    return result


def _reject_constant(value):
    raise KnowledgeError("Knowledge must use finite JSON values.")


def read_json(path):
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_BUNDLE_BYTES + 1)
        if len(raw) > MAX_BUNDLE_BYTES:
            raise KnowledgeError("Knowledge bundle exceeds its byte limit.")
        return json.loads(
            raw.decode("utf-8-sig"),
            object_pairs_hook=_unique,
            parse_constant=_reject_constant,
        )
    except KnowledgeError:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise KnowledgeError("Knowledge bundle is unavailable or invalid.") from None


def validate_payload(payload):
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "documents"}
        or type(payload["schema_version"]) is not int
        or payload["schema_version"] != 1
        or not isinstance(payload["documents"], list)
        or not 1 <= len(payload["documents"]) <= MAX_DOCUMENTS
    ):
        raise KnowledgeError("Invalid knowledge payload.")
    documents = [validate_document(item) for item in payload["documents"]]
    if len({doc["id"] for doc in documents}) != len(documents):
        raise KnowledgeError(
            "Knowledge document IDs must be unique; publish one active revision per ID."
        )
    if sum(math.ceil(len(doc["text"]) / CHUNK_CHARS) for doc in documents) > MAX_CHUNKS:
        raise KnowledgeError("Knowledge exceeds its chunk limit.")
    return documents


def reviewed_bundle(payload):
    """Validate and seal an operator-reviewed payload. Does not grant trust."""
    validate_payload(payload)
    result = {**payload, "sha256": json_digest(payload)}
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_BUNDLE_BYTES:
        raise KnowledgeError("Knowledge bundle exceeds its byte limit.")
    return result


def terms(text):
    """English identifiers plus CJK bigrams; no external tokenizer or model."""
    output = []
    for part in _TERMS.findall(unicodedata.normalize("NFKC", text).casefold()):
        if "\u3400" <= part[0] <= "\u9fff":
            if len(part) > 1:
                output.extend(part[i : i + 2] for i in range(len(part) - 1))
            else:
                output.append(part)
        else:
            output.append(part)
    return Counter(output)


class KnowledgeRetriever(Protocol):
    def search(
        self, query: str, *, scope: KnowledgeScope, limit: int = 4
    ) -> list[dict]: ...
    def active_refs(self, *, scope: KnowledgeScope) -> set[str]: ...


class FileKnowledgeBase:
    """Read and validate on every use, so expiry/revocation is not cached away."""

    def __init__(self, path, *, clock=None):
        self.path = Path(path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _chunks(self, scope):
        bundle = read_json(self.path)
        if not isinstance(bundle, dict) or set(bundle) != {
            "schema_version",
            "documents",
            "sha256",
        }:
            raise KnowledgeError("Knowledge bundle has not been published.")
        payload = {key: bundle[key] for key in ("schema_version", "documents")}
        documents = validate_payload(payload)
        if bundle["sha256"] != json_digest(payload):
            raise KnowledgeError("Knowledge bundle checksum mismatch.")
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise KnowledgeError("Knowledge clock must be timezone-aware.")
        chunks = []
        for doc in documents:
            # Filter BEFORE ranking. No out-of-scope content or counts leave here.
            if (
                scope.scope_id not in doc["scope_ids"]
                or scope.environment not in doc["environments"]
                or scope.postgres_major not in doc["postgres_majors"]
                or not parse_timestamp(doc["reviewed_at"])
                <= now
                < parse_timestamp(doc["expires_at"])
            ):
                continue
            digest = json_digest(doc)
            for offset in range(0, len(doc["text"]), CHUNK_CHARS):
                number = offset // CHUNK_CHARS + 1
                chunks.append(
                    {
                        "ref": "kb-" + json_digest([digest, number])[:20],
                        "document_id": doc["id"],
                        "title": doc["title"],
                        "source": doc["source"],
                        "revision": doc["revision"],
                        "reviewed_at": doc["reviewed_at"],
                        "expires_at": doc["expires_at"],
                        "applicability": {
                            "status": "applicable_at_check",
                            "checked_at": now.astimezone(timezone.utc).isoformat(),
                        },
                        "content_sha256": digest,
                        "chunk": number,
                        "text": doc["text"][offset : offset + CHUNK_CHARS],
                        "_keywords": " ".join(doc.get("keywords", [])),
                    }
                )
        return chunks

    def active_refs(self, *, scope):
        return {chunk["ref"] for chunk in self._chunks(scope)}

    def search(self, query, *, scope, limit=4):
        _text(query, "query", 500)
        if type(limit) is not int or not 1 <= limit <= 8:
            raise KnowledgeError("Knowledge result limit must be between 1 and 8.")
        query_terms = terms(query)
        if not query_terms:
            return []
        chunks = self._chunks(scope)
        if not chunks:
            return []
        tokenized = [
            terms(item["title"] + " " + item["_keywords"] + " " + item["text"])
            for item in chunks
        ]
        frequencies = Counter(term for item in tokenized for term in item)
        lengths = [sum(item.values()) for item in tokenized]
        average = max(sum(lengths) / len(lengths), 1)
        ranked = []
        for item, tokens, length in zip(chunks, tokenized, lengths):
            score = 0.0
            for term in query_terms:
                count = tokens[term]
                if count:
                    idf = math.log(
                        1
                        + (len(chunks) - frequencies[term] + 0.5)
                        / (frequencies[term] + 0.5)
                    )
                    score += (
                        idf
                        * count
                        * 2.2
                        / (count + 1.2 * (0.25 + 0.75 * length / average))
                    )
            if score > 0:
                ranked.append(
                    {
                        key: value
                        for key, value in item.items()
                        if not key.startswith("_")
                    }
                    | {"score": round(score, 6)}
                )
        ranked.sort(
            key=lambda item: (-item["score"], item["document_id"], item["chunk"])
        )
        return ranked[:limit]
