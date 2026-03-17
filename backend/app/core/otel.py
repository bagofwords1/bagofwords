"""
OpenTelemetry instrumentation configuration for BagOfWords backend.

This module sets up distributed tracing and metrics collection using OpenTelemetry.
It automatically instruments FastAPI, SQLAlchemy, httpx, and logging.
"""

import logging
from typing import Optional

from opentelemetry import trace, metrics
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter as GRPCSpanExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter as HTTPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from app.settings.bow_config import OTELConfig

logger = logging.getLogger(__name__)

def setup_telemetry(config: Optional[OTELConfig] = None) -> None:
    """
    Initialize OpenTelemetry instrumentation.

    Args:
        config: TelemetryConfig instance. If None, will be disabled
    """

    if config is None or not config.enabled:
        logger.info("OpenTelemetry is disabled")
        return
    else:
        logger.info("OpenTelemetry is enabled")

    logger.info(f"Initializing OpenTelemetry for {config.service_name}")

    # Create resource with service information
    resource = Resource.create({SERVICE_NAME: config.service_name})

    # Setup Tracing
    _setup_tracing(config, resource)

    # Setup Metrics
    _setup_metrics(config, resource)

    # Setup automatic instrumentation
    _setup_instrumentors()

    logger.info(
        f"OpenTelemetry initialized successfully",
        extra={
            "service": config.service_name,
            "otlp_endpoint": config.traces_endpoint,
            "protocol": config.protocol,
        }
    )


def _setup_tracing(config: OTELConfig, resource: Resource) -> None:
    """Setup trace provider and exporters."""
    provider = TracerProvider(resource=resource)

    # Add OTLP exporter
    if config.protocol == "grpc":
        otlp_exporter = GRPCSpanExporter(
            endpoint=config.traces_endpoint,
            headers=config.headers,
        )
    else:  # http/protobuf
        otlp_exporter = HTTPSpanExporter(
            endpoint=config.traces_endpoint,
            headers=config.headers,
        )

    provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
    logger.info(f"Added OTLP trace exporter: {config.traces_endpoint} ({config.protocol})")

    trace.set_tracer_provider(provider)


def _setup_metrics(config: OTELConfig, resource: Resource) -> None:
    """not implement yet."""
    return None


def _setup_instrumentors() -> None:
    """Setup automatic instrumentation for various libraries."""

    # Instrument logging
    LoggingInstrumentor().instrument(set_logging_format=True)
    logger.info("Instrumented: logging")

    # Instrument httpx (for outgoing HTTP requests)
    HTTPXClientInstrumentor().instrument()
    logger.info("Instrumented: httpx")

    # Note: FastAPI and SQLAlchemy will be instrumented after app/engine creation
    # See instrument_app() and instrument_db() functions below


def instrument_app(app, config: OTELConfig) -> None:
    """
    Instrument FastAPI application.
    Call this after creating the FastAPI app instance.

    Args:
        :param FastAPI application instance
        :param config:
    """
    if not config.enabled:
        return

    FastAPIInstrumentor.instrument_app(app)
    logger.info("Instrumented: FastAPI")


def instrument_db(engine, config: OTELConfig) -> None:
    """
    Instrument SQLAlchemy engine.
    Call this after creating the SQLAlchemy engine.

    Args:
        :param engine: SQLAlchemy engine instance
        :param config:
    """
    if not config.enabled:
        return

    SQLAlchemyInstrumentor().instrument(
        engine=engine,
        enable_commenter=True,  # Adds trace context as SQL comments
    )
    logger.info("Instrumented: SQLAlchemy")


def get_tracer(name: str = __name__):
    """
    Get a tracer for manual instrumentation.

    Args:
        name: Name for the tracer (typically __name__ of the module)

    Returns:
        Tracer instance
    """
    return trace.get_tracer(name)


def get_meter(name: str = __name__):
    """
    Get a meter for manual metrics.

    Args:
        name: Name for the meter (typically __name__ of the module)

    Returns:
        Meter instance
    """
    return metrics.get_meter(name)
