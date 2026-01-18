from passlib.context import CryptContext
from jose import jwt, JWTError
from datetime import datetime, timedelta, timezone
import os

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
SECRET = os.environ.get("JWT_SECRET", "dev-secret-change-me")
ALGO = "HS256"
ACCESS_EXPIRE_MINUTES = 60*24*7

def hash_password(password: str) -> str:
    return pwd_ctx.hash(password)

def verify_password(plain, hashed):
    return pwd_ctx.verify(plain, hashed)

def create_access_token(sub: str, expires_minutes: int = ACCESS_EXPIRE_MINUTES):
    to_encode = {"sub": str(sub), "exp": (datetime.now(timezone.utc) + timedelta(minutes=expires_minutes)).timestamp()}
    return jwt.encode(to_encode, SECRET, algorithm=ALGO)

def decode_access_token(token: str):
    try:
        payload = jwt.decode(token, SECRET, algorithms=[ALGO])
        return payload.get("sub")
    except JWTError:
        return None