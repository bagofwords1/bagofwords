from sqlalchemy import Column, String, Text, JSON, ForeignKey, Table
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema

# Default agents for a project: copied onto each new report created inside the
# project (mirrors report_data_source_association, which stays the runtime
# source of truth for what a report can query).
project_data_source_association = Table(
    'project_data_source_association',
    BaseSchema.metadata,
    Column('project_id', String(36), ForeignKey('projects.id')),
    Column('data_source_id', String(36), ForeignKey('data_sources.id')),
)

# Default files for a project: copied onto each new report created inside the
# project (mirrors report_file_association).
project_file_association = Table(
    'project_file_association',
    BaseSchema.metadata,
    Column('project_id', String(36), ForeignKey('projects.id')),
    Column('file_id', String(36), ForeignKey('files.id')),
)


class Project(BaseSchema):
    """A shared folder that groups reports and carries defaults for them.

    Access model: private by default (owner-only). Sharing happens through
    ResourceGrant rows with resource_type='project' (permissions 'view' /
    'manage'), or by setting access='org' which makes the project visible to
    every org member. A report's membership in a project is the nullable
    reports.project_id FK — moving a report is a one-column update. Deleting
    a project archives its reports (same soft state as single-report delete)
    and stops their scheduled prompts.
    """
    __tablename__ = 'projects'

    name = Column(String, nullable=False, index=True)
    description = Column(Text, nullable=True)
    color = Column(String, nullable=True)  # hex accent for the folder icon
    # Project-local instructions: free text injected into the agent context
    # for every report in the project. Not related to the Instruction model.
    instructions = Column(Text, nullable=True)
    # 'private' = owner + grantees only; 'org' = visible to every org member.
    access = Column(String, nullable=False, default='private', server_default='private')
    # Lightweight defaults applied at report creation (e.g. default mode/model).
    settings = Column(JSON, nullable=True, default=dict)

    user_id = Column(String(36), ForeignKey('users.id'), nullable=False, index=True)
    user = relationship("User", lazy="joined")

    organization_id = Column(String(36), ForeignKey('organizations.id'), nullable=False, index=True)
    organization = relationship("Organization")

    reports = relationship("Report", back_populates="project", lazy="noload")
    data_sources = relationship(
        "DataSource",
        secondary="project_data_source_association",
        lazy="noload",
    )
    files = relationship(
        "File",
        secondary="project_file_association",
        lazy="noload",
    )
