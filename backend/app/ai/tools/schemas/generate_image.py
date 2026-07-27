from typing import Optional, Literal
from pydantic import BaseModel, Field


class GenerateImageInput(BaseModel):
    """Input for the generate_image tool."""

    prompt: str = Field(
        ...,
        description=(
            "Text description of the image to generate. Be specific about subject, "
            "style, composition, and colors. This is passed verbatim to the "
            "image-generation model."
        ),
    )
    title: Optional[str] = Field(
        None,
        description="Short human-readable title for the generated image (used as the file name).",
    )
    size: Optional[Literal["1024x1024", "1536x1024", "1024x1536", "auto"]] = Field(
        None,
        description="Image dimensions. Defaults to a square 1024x1024 when omitted.",
    )
    quality: Optional[Literal["low", "medium", "high", "auto"]] = Field(
        None,
        description="Rendering quality/effort. Higher quality costs more and is slower.",
    )


class GenerateImageOutput(BaseModel):
    """Output from the generate_image tool."""

    success: bool = Field(..., description="Whether an image was generated and stored.")
    file_id: Optional[str] = Field(
        None,
        description="ID of the stored image File. Pass this to create_artifact / edit_artifact "
                    "(as a file reference) to embed the image in a dashboard.",
    )
    filename: Optional[str] = Field(None, description="Stored file name.")
    content_type: Optional[str] = Field(None, description="MIME type of the stored image (image/png).")
    revised_prompt: Optional[str] = Field(
        None, description="The provider's rewritten prompt, when returned."
    )
    error_message: Optional[str] = Field(None, description="Error detail when success is False.")
