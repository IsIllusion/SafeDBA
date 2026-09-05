"""Small, strict, dependency-free remote execution wire protocol."""
import hashlib
import json
import math
import uuid

MAX_BODY = 32_768


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def strict_json(raw):
    if len(raw) > MAX_BODY:
        raise ValueError("Execution payload exceeds its size bound.")
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("Duplicate execution key.")
            value[key] = item
        return value
    def reject(value):
        raise ValueError("Non-finite execution value.")
    def finite_float(value):
        number = float(value)
        return number if math.isfinite(number) else reject(value)
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=reject, parse_float=finite_float)


def validate_request(value):
    keys = {"version", "incident_id", "action_id", "operation_id", "worker_id", "plan_revision", "proposal_digest"}
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("Unsupported execution request fields.")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("Unsupported execution protocol.")
    for key in ("incident_id", "action_id", "operation_id"):
        if not isinstance(value[key], str):
            raise ValueError("Execution identifiers must be UUID strings.")
        uuid.UUID(value[key])
    if type(value["plan_revision"]) is not int or value["plan_revision"] < 1:
        raise ValueError("Invalid plan revision.")
    if not isinstance(value["worker_id"], str) or not 1 <= len(value["worker_id"]) <= 200:
        raise ValueError("Invalid worker identity.")
    if not isinstance(value["proposal_digest"], str) or len(value["proposal_digest"]) != 64 or any(c not in "0123456789abcdef" for c in value["proposal_digest"]):
        raise ValueError("Invalid proposal digest.")
    return value


def action_scope(incident, action):
    return {
        "incident_id": incident["incident_id"], "plan_revision": incident["plan_revision"],
        "action_id": action["action_id"], "proposal": action["proposal"],
        "target": action["target"], "approved_waiters": action["approved_waiters"],
        "allowed_blocked_pids": action["allowed_blocked_pids"],
    }
