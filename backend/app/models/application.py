# Path: backend/app/models/application.py

from sqlalchemy import Column, Integer, String, Table, ForeignKey
from sqlalchemy.orm import relationship

from .base import BaseSchema

class Application(BaseSchema):
    __tablename__ = 'applications'

    name = Column(String, index=True, nullable=False, unique=True, default="")
    datasources = relationship('DataSourceApplicationAssociation', back_populates='application')

    