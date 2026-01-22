from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import os
from urllib.parse import urlparse

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./dev.db") or "sqlite:///./dev.db"

# Render sometimes provides postgres:// which SQLAlchemy wants as postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Avoid printing credentials
try:
    u = urlparse(DATABASE_URL)
    safe = f"{u.scheme}://{u.hostname}:{u.port or ''}{u.path}"
    print("DATABASE_URL (safe):", safe)
except Exception:
    print("DATABASE_URL set")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)