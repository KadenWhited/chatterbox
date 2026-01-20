from passlib.context import CryptContext
from jose import jwt, JWTError
from datetime import datetime, timedelta, timezone
import os

pwd_ctx = CryptContext(schemes=["bcrypt_sha256"], deprecated="auto")

SECRET = os.environ.get("JWT_SECRET", "dev-secret-change-me")
ALGO = "HS256"
ACCESS_EXPIRE_MINUTES = 60*24*7

def hash_password(password: str) -> str:
    if password is None:
        raise ValueError("password required")
    if isinstance(password, bytes):
        password = password.decode("utf-8", "ignore")
    if len(password) < 8:
        raise ValueError("password too short")
    if len(password) > 4096:
        raise ValueError("password too long")
    return pwd_ctx.hash(password)

def verify_password(password: str, hashval: str) -> bool:
    try:
        return pwd_ctx.verify(password, hashval)
    except Exception:
        return False

def create_access_token(user_id: int, minutes: int = JWT_EXPIRES_MINUTES):
    exp = datetime.utcnow() + timedelta(minutes=minutes)
    payload = {"sub": str(user_id), "exp": exp.isoformat()}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)

def decode_access_token(token: str):
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
        sub = payload.get("sub")
        return int(sub) if sub is not None else None
    except Exception:
        return None