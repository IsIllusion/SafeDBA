import json

from config import AUDIT_LOG_PATH


def write_audit_log(record: dict) -> None:
    AUDIT_LOG_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with AUDIT_LOG_PATH.open(
        "a",
        encoding="utf-8",
    ) as f:
        f.write(
            json.dumps(
                record,
                ensure_ascii=False,
            )
            + "\n"
        )