import os
from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, UUID
# from sqlalchemy.orm import relationship
from .base import BaseSchema
from sqlalchemy.orm import relationship
from app.models.report_file_association import report_file_association


class File(BaseSchema):
    __tablename__ = "files"

    filename = Column(String, index=True)
    path = Column(String, index=True)
    content_type = Column(String, index=True)

    user_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    user = relationship("User", back_populates="files")
    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False)
    organization = relationship("Organization", back_populates="files")

    reports = relationship("Report", secondary=report_file_association, back_populates="files")

    file_tags = relationship("FileTag", back_populates="file", lazy="selectin")
    sheet_schemas = relationship("SheetSchema", back_populates="file", lazy="selectin")

    def prompt_schema(self):
        context = []
        text = ""
        if self.content_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
            for sheet_schema in self.sheet_schemas:
                context.append(f"Sheet Name: {sheet_schema.sheet_name}")
                context.append(sheet_schema.schema)
            text = f"File: {self.filename} \nPath: {self.path}\n\nSheet Schemas: {context}"
        else:
            context.append(f"File Type: {self.content_type}")
            for file_tag in self.file_tags:
                context.append(f"{file_tag.key}: {file_tag.value}")
            text = f"File: {self.filename} \nPath: {self.path}\n\nFile Tags: {context}"
       
        return text
    
    @property
    def description(self):

        if self.content_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":    
            description = f"File: {self.filename} at {self.path}\n\nSheet Schemas:\n"
            for sheet_schema in self.sheet_schemas:
                description += f"Sheet Name: {sheet_schema.sheet_name}\n"
                description += f"Sheet index: {sheet_schema.sheet_index}\n"
        else:
            description = f"File: {self.filename} at {self.path}\n\nFile Tags:\n"
            #for file_tag in self.file_tags:
                #description += f"{file_tag.key}: {file_tag.value}\n"
            
        return description

