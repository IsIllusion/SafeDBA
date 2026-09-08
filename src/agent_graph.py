"""LangGraph scheduling for a single bounded diagnostic run.

The graph owns transitions, not execution authority. SQLite incident leases and
operator grants remain the only durable authorization for database mutations.
There is deliberately no checkpointer, node retry, parallel tool execution, or
automatic replay in this graph.
"""

from collections.abc import Callable
from typing import Any, Literal, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context


class DiagnosticState(TypedDict):
    iteration: int
    route: Literal["model", "tools", "answer", "end"]
    # Replace snapshots rather than add_messages: provider IDs are not unique
    # across all compatible vendors, and must never overwrite earlier evidence.
    messages: list[dict[str, Any] | BaseMessage]
    usage: dict[str, int]
    evidence_refs: list[str]
    result: dict[str, Any] | None


Step = Callable[[int], dict[str, Any] | None]


def run_diagnostic_graph(
    *,
    model_step: Step,
    tools_step: Step,
    answer_step: Step,
    has_tool_calls: Callable[[], bool],
    snapshot: Callable[[], dict[str, Any]],
    exhausted: Callable[[], dict[str, Any]],
    max_iterations: int,
) -> dict[str, Any]:
    """Run policy-bearing nodes with independent graph/model turn budgets.

    Callbacks close over the run-local evidence ledger and telemetry/memory
    handles. These capabilities are intentionally not serializable graph state.
    Only snapshots enter state, and no framework persistence/export is enabled.
    """

    def update(iteration, route, result=None):
        return {**snapshot(), "iteration": iteration, "route": route, "result": result}

    def model(state: DiagnosticState):
        iteration = state["iteration"]
        if iteration >= max_iterations:
            return update(iteration, "end", exhausted())
        result = model_step(iteration)
        route = (
            "end" if result is not None else ("tools" if has_tool_calls() else "answer")
        )
        return update(iteration, route, result)

    def tools(state: DiagnosticState):
        result = tools_step(state["iteration"])
        return update(
            state["iteration"] + 1, "end" if result is not None else "model", result
        )

    def answer(state: DiagnosticState):
        result = answer_step(state["iteration"])
        return update(
            state["iteration"] + 1, "end" if result is not None else "model", result
        )

    graph = StateGraph(DiagnosticState)
    graph.add_node("model", model)
    graph.add_node("tools", tools)
    graph.add_node("answer", answer)
    graph.add_edge(START, "model")
    for node in ("model", "tools", "answer"):
        graph.add_conditional_edges(
            node,
            lambda state: state["route"],
            {"model": "model", "tools": "tools", "answer": "answer", "end": END},
        )
    compiled = graph.compile(checkpointer=False)
    # SQL and conversation payloads must not be exported because an inherited
    # LANGSMITH_TRACING / LANGCHAIN_TRACING_V2 environment variable is set.
    with tracing_context(enabled=False):
        state = compiled.invoke(
            update(0, "model"),
            config={"recursion_limit": 2 * max_iterations + 3, "callbacks": []},
        )
    if state["result"] is None:
        raise RuntimeError("Diagnostic graph ended without a terminal result.")
    return state["result"]
