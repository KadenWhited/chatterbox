# auth_utils.py
from passlib.context import CryptContext
from jose import jwt, JWTError
from datetime import datetime, timedelta, timezone
import os

# Use pbkdf2_sha256 to avoid native bcrypt binary issues on some hosts.
pwd_ctx = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")

SECRET = os.environ.get("JWT_SECRET", "dev-secret-change-me")
ALGO = "HS256"
ACCESS_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days

def hash_password(password: str) -> str:
    if password is None:
        raise ValueError("password required")
    if isinstance(password, bytes):
        password = password.decode("utf-8", "ignore")
    if len(password) < 8:
        raise ValueError("password too short (min 8)")
    if len(password) > 4096:
        raise ValueError("password too long")
    return pwd_ctx.hash(password)

def verify_password(plain: str, hashed: str) -> bool:
    try:
        return pwd_ctx.verify(plain, hashed)
    except Exception:
        return False

def create_access_token(sub: str, expires_minutes: int = ACCESS_EXPIRE_MINUTES) -> str:
    """
    Returns a JWT with numeric 'exp' (seconds since epoch).
    `sub` should be a user id (string or int).
    """
    exp = datetime.now(timezone.utc) + timedelta(minutes=expires_minutes)
    payload = {"sub": str(sub), "exp": int(exp.timestamp())}
    return jwt.encode(payload, SECRET, algorithm=ALGO)

def decode_access_token(token: str):
    try:
        payload = jwt.decode(token, SECRET, algorithms=[ALGO])
        return payload.get("sub")
    except JWTError:
        return None