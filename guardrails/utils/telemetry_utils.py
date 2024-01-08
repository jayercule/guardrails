import logging
from functools import wraps
from operator import attrgetter
from typing import Any, Dict, List, Optional

from guardrails.stores.context import Tracer, TracerContext
from guardrails.stores.context import get_tracer as get_context_tracer
from guardrails.stores.context import get_tracer_context
from guardrails.utils.casting_utils import to_string
from guardrails.utils.logs_utils import ValidatorLogs
from guardrails.utils.reask_utils import ReAsk
from guardrails.validator_base import Filter, Refrain

try:
    from opentelemetry import context
    from opentelemetry.trace import Span
except ImportError:

    class Span:
        pass


def get_result_type(before_value: Any, after_value: Any, outcome: str):
    try:
        if isinstance(after_value, (Filter, Refrain, ReAsk)):
            name = after_value.__class__.__name__.lower()
        elif after_value != before_value:
            name = "fix"
        else:
            name = outcome
        return name
    except Exception:
        return type(after_value)


def get_error_code() -> int:
    try:
        from opentelemetry.trace import StatusCode

        return StatusCode.ERROR
    except Exception as e:
        logging.debug(f"Failed to import StatusCode from opentelemetry.trace: {str(e)}")
        return 2


def get_tracer(tracer: Tracer = None) -> Tracer:
    # TODO: Do we ever need to consider supporting non-otel tracers?
    _tracer = tracer if tracer is not None else get_context_tracer()
    return _tracer


def get_current_context() -> Optional[TracerContext]:
    otel_current_context = (
        context.get_current()
        if context is not None and hasattr(context, "get_current")
        else None
    )
    tracer_context = get_tracer_context()
    return otel_current_context or tracer_context


def get_span(span=None):
    if span is not None and hasattr(span, "add_event"):
        return span
    try:
        from opentelemetry import trace

        current_context = get_current_context()
        current_span = trace.get_current_span(current_context)
        return current_span
    except Exception as e:
        print(e)
        return None


def trace_validator_result(
    current_span, validator_log: ValidatorLogs, attempt_number: int, **kwargs
):
    (
        validator_name,
        value_before_validation,
        validation_result,
        value_after_validation,
        start_time,
        end_time,
        instance_id,
    ) = attrgetter(
        "registered_name",
        "value_before_validation",
        "validation_result",
        "value_after_validation",
        "start_time",
        "end_time",
        "instance_id",
    )(
        validator_log
    )
    result = (
        validation_result.outcome
        if hasattr(validation_result, "outcome")
        and validation_result.outcome is not None
        else "unknown"
    )
    result_type = get_result_type(
        value_before_validation, value_after_validation, result
    )

    event: Dict[str, str] = {
        "validator_name": validator_name,
        "attempt_number": attempt_number,
        "result": result,
        "result_type": result_type,
        "input": to_string(value_before_validation),
        "output": to_string(value_after_validation),
        "start_time": start_time.isoformat() if start_time else None,
        "end_time": end_time.isoformat() if end_time else None,
        "instance_id": instance_id,
        **kwargs,
    }
    current_span.add_event(
        f"{validator_name}_result",
        {k: v for k, v in event.items() if v is not None},
    )


# FIXME: It's creating two of every event
# Might be duplicate validator_logs?
def trace_validation_result(
    validation_logs: List[ValidatorLogs],
    attempt_number: int,
    current_span=None,
):
    # Duplicate logs are showing here
    # print("validation_logs.validator_logs: ", validation_logs.validator_logs)
    _current_span = get_span(current_span)
    if _current_span is not None:
        for log in validation_logs:
            # Duplicate logs are showing here
            # print("calling trace_validator_result with: ", log, attempt_number)
            trace_validator_result(_current_span, log, attempt_number)

        # CHECKME: disabled these because I think we flattened this structure?
        # if validation_logs.children:
        #     for child in validation_logs.children:
        #         # print("calling trace_validation_result with child logs")
        #         trace_validation_result(
        #             validation_logs.children.get(child), attempt_number, _current_span
        #         )


def trace_validator(
    validator_name: str,
    obj_id: int,
    # TODO - re-enable once we have namespace support
    # namespace: str = None,
    on_fail_descriptor: str = None,
    tracer: Optional[Tracer] = None,
    **init_kwargs,
):
    def trace_validator_wrapper(fn):
        _tracer = get_tracer(tracer)

        @wraps(fn)
        def with_trace(*args, **kwargs):
            span_name = (
                # TODO - re-enable once we have namespace support
                # f"{namespace}.{validator_name}.validate"
                # if namespace is not None
                # else f"{validator_name}.validate"
                f"{validator_name}.validate"
            )
            trace_context = get_current_context()
            with _tracer.start_as_current_span(
                span_name, trace_context
            ) as validator_span:
                try:
                    validator_span.set_attribute(
                        "on_fail_descriptor", on_fail_descriptor
                    )
                    validator_span.set_attribute(
                        "args",
                        to_string({k: to_string(v) for k, v in init_kwargs.items()}),
                    )
                    validator_span.set_attribute("instance_id", to_string(obj_id))

                    # NOTE: Update if Validator.validate method signature ever changes
                    if args is not None and len(args) > 1:
                        validator_span.set_attribute("input", to_string(args[1]))

                    return fn(*args, **kwargs)
                except Exception as e:
                    validator_span.set_status(
                        status=get_error_code(), description=str(e)
                    )
                    raise e

        @wraps(fn)
        def without_a_trace(*args, **kwargs):
            return fn(*args, **kwargs)

        if _tracer is not None and hasattr(_tracer, "start_as_current_span"):
            return with_trace
        else:
            return without_a_trace

    return trace_validator_wrapper


def trace(name: str, tracer: Optional[Tracer] = None):
    def trace_wrapper(fn):
        @wraps(fn)
        def to_trace_or_not_to_trace(*args, **kwargs):
            _tracer = get_tracer(tracer)

            if _tracer is not None and hasattr(_tracer, "start_as_current_span"):
                trace_context = get_current_context()
                with _tracer.start_as_current_span(name, trace_context) as trace_span:
                    try:
                        # TODO: Capture args and kwargs as attributes?
                        response = fn(*args, **kwargs)
                        return response
                    except Exception as e:
                        trace_span.set_status(
                            status=get_error_code(), description=str(e)
                        )
                        raise e
            else:
                return fn(*args, **kwargs)

        return to_trace_or_not_to_trace

    return trace_wrapper


def async_trace(name: str, tracer: Optional[Tracer] = None):
    def trace_wrapper(fn):
        @wraps(fn)
        async def to_trace_or_not_to_trace(*args, **kwargs):
            _tracer = get_tracer(tracer)

            if _tracer is not None and hasattr(_tracer, "start_as_current_span"):
                trace_context = get_current_context()
                with _tracer.start_as_current_span(name, trace_context) as trace_span:
                    try:
                        # TODO: Capture args and kwargs as attributes?
                        response = await fn(*args, **kwargs)
                        return response
                    except Exception as e:
                        trace_span.set_status(
                            status=get_error_code(), description=str(e)
                        )
                        raise e
            else:
                response = await fn(*args, **kwargs)
                return response

        return to_trace_or_not_to_trace

    return trace_wrapper