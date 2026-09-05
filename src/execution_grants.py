"""Operator-owned, short-lived action grants, separate from Agent state.

The Agent must have no filesystem access to this database. There is no grant
creation endpoint on the worker's HTTP interface.
"""
from contextlib import closing
from pathlib import Path
import sqlite3
import time
import uuid
from execution_protocol import action_scope, canonical, digest


class GrantDenied(RuntimeError):
    pass


def preview_grant(incident, target_identity):
    pending = [action_scope(incident, a) for a in incident["actions"] if a["state"] in {"PLANNED", "REAPPROVAL_REQUIRED", "EXECUTING"}]
    review = {"incident_id": incident["incident_id"], "plan_revision": incident["plan_revision"], "database_identity": target_identity, "actions": pending}
    return {"review": review, "review_digest": digest(review)}


def require_review_digest(incident, target_identity, expected):
    if not isinstance(expected, str) or expected != preview_grant(incident, target_identity)["review_digest"]:
        raise GrantDenied("Review digest is absent or changed. Preview the exact current scope again before confirming.")


class ExecutionGrantStore:
    def __init__(self, path, *, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS grants (
                grant_id TEXT PRIMARY KEY, incident_id TEXT NOT NULL, action_id TEXT NOT NULL,
                scope_digest TEXT NOT NULL, target_digest TEXT NOT NULL, actor TEXT NOT NULL,
                issued_at REAL NOT NULL, expires_at REAL NOT NULL, state TEXT NOT NULL,
                operation_id TEXT UNIQUE, result_json TEXT
            )""")
            conn.commit()

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def issue(self, incident, target_identity, *, actor, ttl_seconds=120):
        if not isinstance(actor, str) or not 1 <= len(actor.strip()) <= 200 or type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 300:
            raise ValueError("A named operator and grant TTL of 1..300 seconds are required.")
        actions = [a for a in incident["actions"] if a["state"] in {"PLANNED", "REAPPROVAL_REQUIRED", "EXECUTING"}]
        if not actions or any(a["proposal"].get("type") != "TERMINATE_BACKEND" for a in actions):
            raise GrantDenied("Only pending lock actions may receive isolated execution grants.")
        if len(actions) > 10:
            raise GrantDenied("Grant action count exceeds the safety bound.")
        with closing(self.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = self.clock()
            issued = []
            for action in actions:
                # Never mint another grant for a consumed action. Recovery
                # must reconcile its database outcome, not replay a mutation.
                if conn.execute("SELECT 1 FROM grants WHERE incident_id=? AND action_id=? AND state IN ('CLAIMED','RESULT_RECORDED')", (incident["incident_id"], action["action_id"])).fetchone():
                    raise GrantDenied("A consumed action grant cannot be reissued.")
                conn.execute("UPDATE grants SET state='REVOKED' WHERE incident_id=? AND action_id=? AND state='ISSUED'", (incident["incident_id"], action["action_id"]))
                grant_id = str(uuid.uuid4())
                conn.execute("INSERT INTO grants VALUES (?,?,?,?,?,?,?,?,?,?,?)", (grant_id, incident["incident_id"], action["action_id"], digest(action_scope(incident, action)), digest(target_identity), actor.strip(), now, now + ttl_seconds, "ISSUED", None, None))
                issued.append(grant_id)
            conn.commit()
        return {"grant_ids": issued, "action_count": len(issued), "expires_at": now + ttl_seconds, "actor": actor.strip()}

    def claim(self, incident, action, target_identity, operation_id):
        uuid.UUID(operation_id)
        with closing(self.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            # Time is checked after acquiring the write lock.
            now = self.clock()
            row = conn.execute("SELECT * FROM grants WHERE incident_id=? AND action_id=? AND state='ISSUED' ORDER BY issued_at DESC LIMIT 1", (incident["incident_id"], action["action_id"])).fetchone()
            if row is None or row["expires_at"] <= now or row["scope_digest"] != digest(action_scope(incident, action)) or row["target_digest"] != digest(target_identity):
                raise GrantDenied("An unexpired operator grant matching the action and live database is required.")
            conn.execute("UPDATE grants SET state='CLAIMED', operation_id=? WHERE grant_id=? AND state='ISSUED'", (operation_id, row["grant_id"]))
            conn.commit()
            return {"grant_id": row["grant_id"], "scope_digest": row["scope_digest"], "expires_at": row["expires_at"]}

    def record_result(self, grant_id, result):
        with closing(self.connect()) as conn:
            cursor = conn.execute("UPDATE grants SET state='RESULT_RECORDED', result_json=? WHERE grant_id=? AND operation_id=? AND state='CLAIMED'", (canonical(result), grant_id, result["operation_id"]))
            if cursor.rowcount != 1:
                raise GrantDenied("Grant result identity/state mismatch.")
            conn.commit()
