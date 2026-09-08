"""Bounded model usage and non-secret routing metadata."""


def safe_usage_count(value) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return parsed if parsed >= 0 else 0


def provider_route_metadata(provider) -> dict:
    """Copy only bounded, non-secret provider-routing metadata."""
    try:
        value = getattr(provider, "last_call_metadata", {})
        if callable(value):
            value = value()
    except Exception:
        return {}
    if not isinstance(value, dict):
        return {}
    allowed = {
        "selected_provider",
        "selected_model",
        "fallback_configured",
        "fallback_used",
        "failover_reason",
        "primary_error_type",
        "fallback_error_type",
        "primary_circuit_state",
        "fallback_circuit_state",
    }
    return {
        key: item[:200] if isinstance(item, str) else item
        for key, item in value.items()
        if key in allowed and isinstance(item, (str, bool, int, float))
    }
