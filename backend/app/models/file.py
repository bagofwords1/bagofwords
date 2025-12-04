import os
from datetime import datetime
from typing import Any, Dict, Optional
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, UUID, JSON
from .base import BaseSchema
from sqlalchemy.orm import relationship
from app.models.report_file_association import report_file_association


class File(BaseSchema):
    __tablename__ = "files"

    filename = Column(String, index=True)
    path = Column(String, index=True)
    content_type = Column(String, index=True)
    
    # Raw preview data (no LLM) - stores sheet names, raw cells, text preview, etc.
    preview = Column(JSON, nullable=True)

    user_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    user = relationship("User", back_populates="files")
    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False)
    organization = relationship("Organization", back_populates="files")

    reports = relationship("Report", secondary=report_file_association, back_populates="files")

    file_tags = relationship("FileTag", back_populates="file", lazy="selectin")
    sheet_schemas = relationship("SheetSchema", back_populates="file", lazy="selectin")

    def prompt_schema(self):
        """Legacy method - returns description for backward compatibility."""
        return self.description
    
    @property
    def description(self) -> str:
        """
        Render file description for LLM/coder context.
        
        Uses raw preview if available, falls back to legacy schema/tags.
        """
        # Use new preview-based description if preview exists
        if self.preview:
            return self._render_preview_description()
        
        # Legacy fallback: use sheet_schemas or file_tags
        return self._render_legacy_description()
    
    def _render_preview_description(self) -> str:
        """Render description from raw preview data."""
        from app.services.file_preview import render_file_description
        return render_file_description(self.preview, self.path)
    
    def _render_legacy_description(self) -> str:
        """Legacy description using LLM-extracted schema/tags (for backward compat)."""
        if self.content_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":    
            description = f"Excel File: {self.filename}\nPath: {self.path}\n\nSheet Schemas:\n"
            for sheet_schema in self.sheet_schemas:
                description += f"Sheet Name: {sheet_schema.sheet_name}\n"
                description += f"Sheet index: {sheet_schema.sheet_index}\n"
                if sheet_schema.schema:
                    description += f"Schema: {sheet_schema.schema}\n"
        elif self.content_type == "application/vnd.ms-excel":
            description = f"Excel File: {self.filename}\nPath: {self.path}\n\nSheet Schemas:\n"
            for sheet_schema in self.sheet_schemas:
                description += f"Sheet Name: {sheet_schema.sheet_name}\n"
                description += f"Sheet index: {sheet_schema.sheet_index}\n"
        elif self.content_type == "application/pdf":
            description = f"PDF File: {self.filename}\nPath: {self.path}\n\nFile Tags:\n"
            for file_tag in self.file_tags:
                description += f"{file_tag.key}: {file_tag.value}\n"
        elif self.content_type in ["text/csv", "application/csv"]:
            description = f"CSV File: {self.filename}\nPath: {self.path}\n"
        else:
            description = f"File: {self.filename}\nPath: {self.path}\nType: {self.content_type}\n"
            for file_tag in self.file_tags:
                description += f"{file_tag.key}: {file_tag.value}\n"
            
        return description

