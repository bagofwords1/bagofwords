from typing import ClassVar, Optional, Dict, Any
from pydantic import BaseModel


def xml_escape(value: str) -> str:
    return (value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def xml_tag(name: str, inner: str, attrs: Optional[Dict[str, Any]] = None) -> str:
    attrs_str = "".join(f' {k}="{xml_escape(str(v))}"' for k, v in (attrs or {}).items())
    return f"<{name}{attrs_str}>\n{inner}\n</{name}>"


class ContextSection(BaseModel):
    """Base class for all context sections.

    Sections are Pydantic models that can render themselves to a string (e.g., XML)
    and can be serialized to JSON for persistence.
    """

    tag_name: ClassVar[str]

    def to_dict(self) -> dict:
        return self.model_dump()

    def to_json(self) -> str:
        return self.model_dump_json()

    def render(self) -> str:
        raise NotImplementedError("Each section must implement render()")


