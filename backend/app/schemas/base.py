"""
Base schema utilities for Pydantic models.

Provides UTCDatetime type that ensures naive datetimes are serialized with UTC timezone indicator.
This fixes JavaScript's Date parsing which interprets naive datetime strings as local time.
"""

from datetime import datetime, timezone
from typing import Annotated, Optional

from pydantic import BaseModel, ConfigDict, PlainSerializer


def serialize_datetime_utc(value: datetime | None) -> str | None:
    """
    Serialize datetime to ISO format with UTC timezone indicator.
    
    - If the datetime has no timezone (naive), assume it's UTC and add 'Z' suffix
    - If the datetime has a timezone, convert to UTC and add 'Z' suffix
    - Returns None for None values
    """
    if value is None:
        return None
    
    if value.tzinfo is None:
        # Naive datetime - assume UTC
        return value.isoformat() + "Z"
    else:
        # Timezone-aware - convert to UTC
        utc_value = value.astimezone(timezone.utc)
        return utc_value.replace(tzinfo=None).isoformat() + "Z"


# Custom type that serializes datetime as UTC with 'Z' suffix
UTCDatetime = Annotated[
    datetime,
    PlainSerializer(serialize_datetime_utc, return_type=str)
]

# Optional version for nullable datetime fields
OptionalUTCDatetime = Annotated[
    Optional[datetime],
    PlainSerializer(serialize_datetime_utc, return_type=Optional[str])
]


class TimestampMixin(BaseModel):
    """
    Mixin for models with standard timestamp fields.
    Use UTCDatetime types for proper timezone handling.
    """
    created_at: OptionalUTCDatetime = None
    updated_at: OptionalUTCDatetime = None

    model_config = ConfigDict(from_attributes=True)




