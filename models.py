from sqlalchemy import Column, Integer, String, DateTime, Boolean, JSON, Text, ForeignKey
from sqlalchemy.orm import relationship, declarative_base
from datetime import datetime, timezone

Base = declarative_base()

def now_utc():
    return datetime.now(timezone.utc)

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(80), unique=True, nullable=False, index=True)
    password_hash = Column(String(256), nullable=True)
    display_name = Column(String(120), nullable=True)
    avatar = Column(String(256), nullable=True)
    created_at = Column(DateTime, default= now_utc)

class Message(Base):
    __tablename__ = "messages"
    id = Column(String(36), primary_key=True)
    author_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    author_username = Column(String(80), nullable=False)
    timestamp = Column(DateTime(timezone=True), default= now_utc)
    edited_at = Column(DateTime(timezone=True), nullable=True)    
    content = Column(Text)
    reply_to = Column(String(36), nullable=True)
    deleted = Column(Boolean, default=False)
    pinned = Column(Boolean, default=False)
    pinned_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    pinned_at = Column(DateTime(timezone=True), nullable=True)
    reactions = Column(JSON, default={}) #emoji -> list of user ids


class Favorite(Base):
    __tablename__ = "favorites"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    gif_id = Column(String(128), nullable=False)        # provider id (e.g. giphy id)
    url = Column(String(512), nullable=False)           # original gif url
    preview = Column(String(512), nullable=True)        # small thumb
    title = Column(String(256), nullable=True)
    metadata_json = Column(JSON, default={})
    created_at = Column(DateTime(timezone=True), default=now_utc)