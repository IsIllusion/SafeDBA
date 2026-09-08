"""Compatibility example for direct agent.py execution; the supported CLI is main.py."""

import json


def run_example(run_agent):
    query = "\n    SELECT *\n    FROM cardinality_test\n    WHERE status = 'hot';\n    "
    question = f"\n    Diagnose the following PostgreSQL query using real\n    database evidence.\n\n    Identify the most likely root cause of any significant\n    performance or planner-estimation problem.\n\n    Collect additional database evidence when needed.\n\n    Do not assume that every Sequential Scan requires an index.\n\n    SQL:\n\n    {query}\n    "
    result = run_agent(question)
    print()
    print("=== SafeDBA Agent ===")
    print()
    print(result["answer"])
    print()
    print("=== Action Proposals ===")
    if result["proposals"]:
        for proposal in result["proposals"]:
            print(json.dumps(proposal, indent=2, ensure_ascii=False))
    else:
        print("No action proposals.")
    print()
    print("=== Tool Trace ===")
    print(json.dumps(result["tool_trace"], indent=2, ensure_ascii=False))
