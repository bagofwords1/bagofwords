from typing import Optional
from pydantic import BaseModel, Field


class ReadImageInput(BaseModel):
    """Input for the read_image tool."""

    file_id: str = Field(
        ...,
        description=(
            "ID of an image file to read into context so the model can see it — "
            "e.g. the file_id returned by a previous generate_image call, or an "
            "uploaded image. The image is attached to the next model turn for vision."
        ),
    )


class ReadImageOutput(BaseModel):
    """Output from the read_image tool."""

    success: bool = Field(..., description="Whether the image was read.")
    file_id: Optional[str] = Field(None, description="The image file id.")
    content_type: Optional[str] = Field(None, description="MIME type of the image.")
    filename: Optional[str] = Field(None, description="Stored file name.")
    error_message: Optional[str] = Field(None, description="Error detail when success is False.")
