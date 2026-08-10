import json

from db_tools import get_lock_waits


result = get_lock_waits()

print()
print("=== Lock Wait Evidence ===")

print(
    json.dumps(
        result,
        indent=2,
        ensure_ascii=False,
        default=str,
    )
)