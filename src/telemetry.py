"""Optional, privacy-conscious OpenTelemetry tracing for SafeDBA."""

from __future__ import annotations

from contextlib import contextmanager
from threading import Lock
from typing import Any, Iterator

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode


INSTRUMENTATION_NAME = "safedba.agent"
INSTRUMENTATION_VERSION = "1.0.0"


def _attributes(values: dict[str, Any]) -> dict[str, Any]:
    """Keep span attributes scalar and bounded.

    Callers must still pass only low-cardinality operational metadata. Raw
    prompts, SQL, tool arguments/results, credentials, and session identifiers
    are deliberately excluded at the call sites.
    """

    result: dict[str, Any] = {}
    for key, value in values.items():
        if value is None:
            continue
        if isinstance(value, (bool, int, float)):
            result[key] = value
        elif isinstance(value, str):
            result[key] = value[:512]
    return result


class RunTelemetry:
    """A privacy-safe root span plus explicitly parented child operations."""

    def __init__(
        self,
        tracer,
        *,
        enabled: bool,
        span_name: str = "safedba.agent.run",
        attributes: dict[str, Any],
    ) -> None:
        self._tracer = tracer
        self._enabled = enabled
        self._finished = False
        self._root_span = tracer.start_span(
            span_name,
            attributes=_attributes(attributes),
        )
        self._root_context = trace.set_span_in_context(
            self._root_span
        )

    @property
    def trace_id(self) -> str | None:
        context = self._root_span.get_span_context()
        if not self._enabled or not context.is_valid:
            return None
        return f"{context.trace_id:032x}"

    @contextmanager
    def span(
        self,
        name: str,
        attributes: dict[str, Any] | None = None,
    ) -> Iterator[Span]:
        child = self._tracer.start_span(
            name,
            context=self._root_context,
            attributes=_attributes(attributes or {}),
        )
        try:
            yield child
        except Exception as exc:
            # Exception messages can contain SQL or credentials. Record only
            # the class, which is enough to group failures safely.
            error_type = type(exc).__name__
            child.set_attribute("error.type", error_type)
            child.set_status(
                Status(StatusCode.ERROR, error_type)
            )
            raise
        finally:
            child.end()

    def finish(
        self,
        *,
        status: str,
        stop_reason: str,
        llm_turns: int,
        tool_calls_attempted: int,
        tool_calls_succeeded: int,
        total_tokens: int,
        error_count: int,
    ) -> None:
        final_attributes = {
            "safedba.run.status": status,
            "safedba.run.stop_reason": stop_reason,
            "safedba.usage.llm_turns": llm_turns,
            "safedba.usage.tool_calls_attempted": tool_calls_attempted,
            "safedba.usage.tool_calls_succeeded": tool_calls_succeeded,
            "safedba.usage.total_tokens": total_tokens,
            "safedba.run.error_count": error_count,
        }
        self.finish_operation(
            status=status,
            attributes=final_attributes,
            error=status == "failed",
            error_type=stop_reason,
        )

    def finish_operation(
        self,
        *,
        status: str,
        attributes: dict[str, Any] | None = None,
        error: bool = False,
        error_type: str | None = None,
    ) -> None:
        """Finish a non-Agent operation without exporting payload text."""

        if self._finished:
            return
        self._finished = True
        final_attributes = {
            "safedba.operation.status": status,
            **(attributes or {}),
        }
        for key, value in _attributes(final_attributes).items():
            self._root_span.set_attribute(key, value)
        if error:
            safe_error_type = (error_type or "operation_failed")[:128]
            self._root_span.set_attribute(
                "error.type",
                safe_error_type,
            )
            self._root_span.set_status(
                Status(StatusCode.ERROR, safe_error_type)
            )
        else:
            self._root_span.set_status(Status(StatusCode.OK))
        self._root_span.end()


class TelemetryManager:
    """Creates Agent traces without depending on the global tracer provider."""

    def __init__(
        self,
        *,
        tracer=None,
        enabled: bool = False,
        provider=None,
    ) -> None:
        self.enabled = bool(enabled)
        self._provider = provider
        self._tracer = tracer or trace.get_tracer(
            INSTRUMENTATION_NAME,
            INSTRUMENTATION_VERSION,
        )

    def start_run(
        self,
        *,
        mode: str,
        memory_enabled: bool,
        experience_enabled: bool,
        environment_verified: bool,
    ) -> RunTelemetry:
        return RunTelemetry(
            self._tracer,
            enabled=self.enabled,
            span_name="safedba.agent.run",
            attributes={
                "safedba.agent.mode": mode,
                "safedba.memory.enabled": memory_enabled,
                "safedba.experience.enabled": experience_enabled,
                "safedba.environment.verification_enabled": (
                    environment_verified
                ),
            },
        )

    def start_incident_workflow(
        self,
        *,
        workflow_type: str,
        action_count: int,
        resumed: bool,
    ) -> RunTelemetry:
        """Start one durable workflow pass without exporting incident IDs."""

        return RunTelemetry(
            self._tracer,
            enabled=self.enabled,
            span_name="safedba.incident.run",
            attributes={
                "safedba.workflow.type": workflow_type,
                "safedba.workflow.action_count": action_count,
                "safedba.workflow.resumed": resumed,
            },
        )

    def shutdown(self) -> None:
        if self._provider is not None:
            self._provider.shutdown()


_manager: TelemetryManager | None = None
_manager_lock = Lock()


def get_telemetry_manager() -> TelemetryManager:
    """Return the process telemetry manager, configuring OTLP lazily."""

    global _manager
    if _manager is not None:
        return _manager

    with _manager_lock:
        if _manager is not None:
            return _manager

        import config

        if not config.OTEL_ENABLED:
            _manager = TelemetryManager(enabled=False)
            return _manager

        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(
            resource=Resource.create({
                "service.name": config.OTEL_SERVICE_NAME,
                "service.version": INSTRUMENTATION_VERSION,
                "deployment.environment.name": config.SAFEDBA_ENV,
            })
        )
        exporter = OTLPSpanExporter(
            endpoint=config.OTEL_ENDPOINT,
            timeout=config.OTEL_EXPORT_TIMEOUT_SECONDS,
        )
        provider.add_span_processor(
            BatchSpanProcessor(exporter)
        )
        _manager = TelemetryManager(
            tracer=provider.get_tracer(
                INSTRUMENTATION_NAME,
                INSTRUMENTATION_VERSION,
            ),
            enabled=True,
            provider=provider,
        )
        return _manager
