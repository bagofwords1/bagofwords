from sqlalchemy import Column, Integer, String, ForeignKey, UUID
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema

class Prompt(BaseSchema):
    __tablename__ = 'prompts'

    title = Column(String, nullable=True)  
    text = Column(String, nullable=False)
    user_id = Column(String(36), ForeignKey('users.id'), nullable=True)
    organization_id = Column(String(36), ForeignKey('organizations.id'), nullable=True)

    #user = relationship("User", back_populates="prompts")
    organization = relationship("Organization", back_populates="prompts")