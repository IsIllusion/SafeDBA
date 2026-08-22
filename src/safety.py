RISK_LEVELS = {
    "EXPLAIN": "LOW",
    # Runs only through the observer role, READ ONLY transaction,
    # preflight cost gate, extended single-statement protocol, and timeout.
    "EXPLAIN_ANALYZE": "LOW",
    "SELECT": "LOW",
    "REWRITE_QUERY": "LOW",

    "CREATE_INDEX": "MEDIUM",
    "ANALYZE": "MEDIUM",
    "ANALYZE_TABLE": "MEDIUM",

    "ALTER_SYSTEM": "CRITICAL",
    "UPDATE": "HIGH",
    "DELETE": "HIGH",
    "DROP_INDEX": "HIGH",
    "TERMINATE_BACKEND": "HIGH",

    "DROP_TABLE": "CRITICAL",

}

def assess_risk(operation_type: str) -> str:
    return RISK_LEVELS.get(
        operation_type,
        "CRITICAL",
    )


def requires_approval(risk: str) -> bool:
    return risk in {
        "MEDIUM",
        "HIGH",
        "CRITICAL",
    }


def is_operation_allowed(risk: str) -> bool:
    return risk != "CRITICAL"
