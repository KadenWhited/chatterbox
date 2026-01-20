# auth_utils.py
import os
from datetime import datetime, timedelta
from passlib.context import CryptContext
import jwt
from typing import Optional

# Password hashing — pbkdf2_sha256 avoids bcrypt binary problems and 72-byte limit.
pwd_ctx = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")

# JWT settings (make sure to set JWT_SECRET in your Render environment variables)
JWT_SECRET = os.environ.get("JWT_SECRET", "dev-secret-change-me")
JWT_ALGO = "HS256"
# Time (minutes) the access token is valid
JWT_EXPIRES_MINUTES = int(os.environ.get("JWT_EXPIRES_MINUTES", 60 * 24 * 7))  # default 7 days

def hash_password(password: str) -> str:
    if password is None:
        raise ValueError("password required")
    if isinstance(password, bytes):
        password = password.decode("utf-8", "ignore")
    if len(password) < 6:
        raise ValueError("password too short")
    if len(password) > 4096:
        raise ValueError("password too long")
    return pwd_ctx.hash(password)

def verify_password(password: str, hashval: str) -> bool:
    try:
        return pwd_ctx.verify(password, hashval)
    except Exception:
        return False

def create_access_token(user_id: int, minutes: Optional[int] = None) -> str:
    if minutes is None:
        minutes = JWT_EXPIRES_MINUTES
    exp = datetime.utcnow() + timedelta(minutes=int(minutes))
    payload = {"sub": str(user_id), "exp": int(exp.timestamp())}
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)
    # jwt.encode returns str for pyjwt>=2.x
    return token

def decode_access_token(token: str) -> Optional[int]:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
        sub = payload.get("sub")
        if sub is None:
            return None
        return int(sub)
    except Exception:
        return None
