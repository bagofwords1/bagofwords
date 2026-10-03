# Audit stream destination registry
# Licensed under the BOW Enterprise License
# See backend/app/ee/LICENSE for details

from typing import Dict, Type

from app.ee.audit.streams.destinations.base import Destination, SendResult  # noqa: F401
from app.ee.audit.streams.destinations.http import (
    DatadogDestination,
    HttpsDestination,
    SentinelDestination,
    SplunkDestination,
)
from app.ee.audit.streams.destinations.s3 import GcsDestination, S3Destination
from app.ee.audit.streams.destinations.syslog import SyslogDestination

REGISTRY: Dict[str, Type[Destination]] = {
    cls.type: cls
    for cls in (
        DatadogDestination,
        SplunkDestination,
        SentinelDestination,
        S3Destination,
        GcsDestination,
        HttpsDestination,
        SyslogDestination,
    )
}


def build_destination(destination: str, config: dict, secrets: dict) -> Destination:
    return REGISTRY[destination](config, secrets)
