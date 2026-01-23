from sqlalchemy import Column, Integer, String, DateTime, Boolean, JSON, Text, ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import relationship, declarative_base
from datetime import datetime, timezone
import uuid

Base = declarative_base()

def _uuid():
    return str(uuid.uuid4())

def now_utc():
    return datetime.now(timezone.utc)

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(80), unique=True, nullable=False, index=True)
    password_hash = Column(String(256), nullable=True)
    display_name = Column(String(120), nullable=True)
    avatar = Column(String(256), nullable=True)
    status = Column(String(32), nullable=True)
    bio = Column(String(200), nullable=True)
    avatar_color = Column(String(16), nullable=True)
    created_at = Column(DateTime(timezone=True), default= now_utc, nullable=False)

class Room(Base):
    __tablename__ = "rooms"

    id = Column(String(36), primary_key=True, default=_uuid)
    name = Column(String(80), unique=True, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=now_utc, nullable=False)

    messages = relationship("Message", back_populates="room")

class Message(Base):
    __tablename__ = "messages"

    id = Column(String(36), primary_key=True)
    author_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    author_username = Column(String(80), nullable=False)

    timestamp = Column(DateTime(timezone=True), default=now_utc, nullable=False)
    edited_at = Column(DateTime(timezone=True), nullable=True)

    content = Column(Text, default="", nullable=False)
    reply_to = Column(String(36), nullable=True)

    deleted = Column(Boolean, default=False, nullable=False)

    pinned = Column(Boolean, default=False, nullable=False)
    pinned_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    pinned_at = Column(DateTime(timezone=True), nullable=True)

    reactions = Column(JSON, default=dict, nullable=False)

    # NEW
    room_id = Column(String(36), ForeignKey("rooms.id"), nullable=False, index=True)
    room = relationship("Room", back_populates="messages")

Index("ix_messages_room_time", Message.room_id, Message.timestamp)

class Favorite(Base):
    __tablename__ = "favorites"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    gif_id = Column(String(128), nullable=False)
    url = Column(String(512), nullable=False)
    preview = Column(String(512), nullable=True)
    title = Column(String(256), nullable=True)
    metadata_json = Column(JSON, default=dict, nullable=False)
    created_at = Column(DateTime(timezone=True), default=now_utc, nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "gif_id", name="uq_favorites_user_gif"),
    )

class RoomMember(Base):
    __tablename__ = "room_members"
    id = Column(Integer, primary_key=True, autoincrement=True)
    room_id = Column(String(36), ForeignKey("rooms.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    role = Column(String(20), default="member", nullable=False)
    joined_at = Column(DateTime(timezone=True), default=now_utc, nullable=False)

    __table_args__ = (
        Index("ix_room_members_unique", "room_id", "user_id", unique=True),
    )