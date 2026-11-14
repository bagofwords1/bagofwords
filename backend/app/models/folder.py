from sqlalchemy import Boolean, Column, ForeignKey, Index, String, func

from app.models.base import BaseSchema


class Folder(BaseSchema):
    __tablename__ = "folders"

    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False)
    parent_id = Column(String(36), ForeignKey("folders.id"), nullable=True)
    creator_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    name = Column(String(160), nullable=False)
    inherits_access = Column(Boolean, nullable=False, default=True)

    __table_args__ = (
        Index("uq_folder_sibling", organization_id, func.coalesce(parent_id, ""), func.lower(name), unique=True),
    )


def _attachment(name, key, target):
    from sqlalchemy import Table
    return Table(name, BaseSchema.metadata,
                 Column("folder_id", String(36), ForeignKey("folders.id", ondelete="CASCADE"), primary_key=True),
                 Column(key, String(36), ForeignKey(target + ".id", ondelete="CASCADE"), primary_key=True),
                 Index("ix_" + name + "_item", key))


folder_instructions = _attachment("folder_instructions", "instruction_id", "instructions")
folder_files = _attachment("folder_files", "file_id", "files")
folder_eval_suites = _attachment("folder_eval_suites", "suite_id", "test_suites")
