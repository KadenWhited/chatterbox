from typing import Dict, Any, Optional
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from fastapi import FastAPI, WebSocket, Request, WebSocketDisconnect, UploadFile, File, Header, Query
import json
import uuid
import datetime as _dt
from fastapi.staticfiles import StaticFiles
import pathlib
import requests
from bs4 import BeautifulSoup
import aiofiles
import os
from cachetools import TTLCache

#Authentication/Database
from database import SessionLocal
from models import User, Message, Favorite, Room, RoomMember
from auth_utils import hash_password, verify_password, create_access_token, decode_access_token
from starlette.status import HTTP_401_UNAUTHORIZED
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError, DataError
from sqlalchemy.orm import Session
from fastapi import Depends, HTTPException, BackgroundTasks
from fastapi.concurrency import run_in_threadpool

#Security/Webhosting
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi import Response
import hashlib
import ipaddress
import socket
from urllib.parse import urlparse
import secrets
#----------------------------------------------------------------------------------------------



app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

origins = [
    "http://localhost:3000",
    "http://127.0.0.1:8000",
    "http://localhost:8000"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

#app.add_middleware(TrustedHostMiddleware, allowed_hosts=["chatterbox-eq7f.onrender.com", "localhost", "127.0.0.1"])

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

HERE = pathlib.Path(__file__).resolve().parent
EMOJI_FILE = HERE / "static" / "emojis.json"
EMOJI_LIST = []
EMOJI_ALIASES = {}

MAX_PINNED = 100
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
ALLOWED_UPLOAD_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")

def require_admin_token(x_admin_token: str | None = Header(None)):
    if not ADMIN_TOKEN or x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="forbidden")


def load_emojis():
    global EMOJI_LIST, EMOJI_ALIASES
    if not EMOJI_FILE.exists():
        print("[emoji] static/emojis.json not found - EMOJI_LIST empty (looked at)", EMOJI_FILE)
        EMOJI_LIST = []
        EMOJI_ALIASES = {}
        return
    text = EMOJI_FILE.read_text(encoding="UTF-8")
    print("[emoji] loading from", EMOJI_FILE, "size", len(text))
    data = json.loads(text)
    EMOJI_LIST = [item.get("char") for item in data if item.get("char")]

    aliases = {}
    for item in data:
        ch = item.get("char")
        for alias in item.get("aliases", []):
            aliases[alias.lower()] = ch
    EMOJI_ALIASES = aliases
    print(f"[emoji] loaded {len(EMOJI_LIST)} emojis, {len(EMOJI_ALIASES)} aliases")

#load emojis on startup
load_emojis()

templates = Jinja2Templates(directory="templates")


UPLOAD_DIR = pathlib.Path(os.environ.get("UPLOAD_DIR", "/var/data/uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")

GIPHY_KEY = os.environ.get("GIPHY_API_KEY")
gif_search_cache = TTLCache(maxsize= 256, ttl= 60)

class ConnectionManager:
    def __init__(self):
        # websocket -> metadata dict {"user_id": int, "username": str, "room_id": str}
        self.active_connections: Dict[WebSocket, Dict[str, Any]] = {}

        # recent message cache (global, capped)
        self.messages: list[Dict[str, Any]] = []
        self.messages_index: Dict[str, Dict[str, Any]] = {}

        self.MAX_CACHE_MESSAGES = 1000

        self.voice_members: Dict[str, set[str]] = {}
        self.voice_states: Dict[str, Dict[str, Dict[str, Any]]] = {}
        # shape: room_id -> username -> {"muted": bool, "speaking": bool, "ts": iso}

    async def connect(self, websocket: WebSocket, client_meta: Dict[str, Any]):
        self.active_connections[websocket] = client_meta
        print(f"[connect] {client_meta}")

    def disconnect(self, websocket: WebSocket):
        meta = self.active_connections.pop(websocket, None)
        print(f"[disconnect] {meta}")
        return meta

    def usernames_online_in_room(self, room_id: str):
        return sorted({
            meta.get("username")
            for meta in self.active_connections.values()
            if meta.get("username") and meta.get("room_id") == room_id
        })

    async def send_personal_message(self, payload: Dict[str, Any], websocket: WebSocket):
        try:
            await websocket.send_text(json.dumps(payload))
        except Exception as e:
            print("[send_personal_message] failed to send to", self.active_connections.get(websocket), "error:", repr(e))
            raise

    async def broadcast_room(self, room_id: str, payload: Dict[str, Any], exclude: WebSocket = None):
        text = json.dumps(payload)
        dead = []

        for ws, meta in list(self.active_connections.items()):
            if meta.get("room_id") != room_id:
                continue
            if exclude is not None and ws == exclude:
                continue
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)

        for ws in dead:
            self.disconnect(ws)

    async def broadcast(self, payload: Dict[str, Any], exclude: WebSocket = None):
        text = json.dumps(payload)
        dead = []

        for ws, meta in list(self.active_connections.items()):
            if exclude is not None and ws == exclude:
                continue
            try:
                await ws.send_text(text)
            except Exception as e:
                print(f"[broadcast] removing broken socket {meta}: {e}")
                dead.append(ws)

        for ws in dead:
            self.disconnect(ws)

    async def broadcast_presence_snapshot_room(self, room_id: str):
        await self.broadcast_room(room_id, {
            "type": "presence_snapshot",
            "online": self.usernames_online_in_room(room_id),
        })

    async def broadcast_presence_change_room(self, room_id: str, username: str, status: str):
        await self.broadcast_room(room_id, {
            "type": "presence",
            "user": username,
            "status": status,
        })

    def store_message(self, msg: Dict[str, Any]):
        """
        Store/update in cache. Capped FIFO.
        """
        mid = msg.get("id")
        if not mid:
            return

        # overwrite index for same id (edits/pins/reactions can update the dict)
        self.messages_index[mid] = msg
        self.messages.append(msg)

        # cap
        if len(self.messages) > self.MAX_CACHE_MESSAGES:
            old = self.messages.pop(0)
            oid = old.get("id")
            if oid:
                # only remove if index still points to this exact object (avoids removing newer updates)
                if self.messages_index.get(oid) is old:
                    self.messages_index.pop(oid, None)

    def cache_message_if_absent(self, msg: Dict[str, Any]):
        mid = msg.get("id")
        if not mid:
            return
        if mid not in self.messages_index:
            self.store_message(msg)

    def find_message(self, message_id: str):
        """
        In-memory first; DB fallback if not found.
        """
        if not message_id:
            return None

        m = self.messages_index.get(message_id)
        if m:
            return m

        # fallback to DB: try to load and materialize into same dict shape
        db = SessionLocal()
        try:
            row = db.query(Message).filter(Message.id == message_id).first()
            if not row:
                return None

            msg = {
                "type": "message",
                "id": row.id,
                "author": row.author_username,
                "author_id": row.author_id,
                "timestamp": row.timestamp.isoformat().replace("+00:00", "Z"),
                "content": row.content,
                "reply_to": row.reply_to,
                "deleted": row.deleted,
                "pinned": row.pinned,
                "pinned_by": row.pinned_by,
                "pinned_at": row.pinned_at.isoformat().replace("+00:00", "Z") if row.pinned_at else None,
                "edited_at": row.edited_at.isoformat().replace("+00:00", "Z") if row.edited_at else None,
                "reactions": row.reactions or {},
                "room_id": row.room_id,
            }
            self.store_message(msg)
            return msg
        finally:
            db.close()

    def voice_members_in_room(self, room_id: str):
        return sorted(self.voice_members.get(room_id, set()))
    
    def add_voice_member(self, room_id: str, username: str):
        s = self.voice_members.setdefault(room_id, set())
        s.add(username)

    def remove_voice_member(self, room_id: str, username: str):
        s = self.voice_members.get(room_id)
        if not s:
            return
        s.discard(username)
        if not s:
            self.voice_members.pop(room_id, None)

    def find_ws_by_username(self, room_id: str, username: str) -> Optional[WebSocket]:
        for ws, meta in self.active_connections.items():
            if meta.get("room_id") == room_id and meta.get("username") == username:
                return ws
        return None
    
    def set_voice_state(self, room_id: str, username: str, *, muted: Optional[bool]=None, speaking: Optional[bool]=None):
        room = self.voice_states.setdefault(room_id, {})
        st = room.setdefault(username, {"muted": False, "speaking": False, "ts": None})
        if muted is not None:
            st["muted"] = bool(muted)
        if speaking is not None:
            st["speaking"] = bool(speaking)
        st["ts"] = _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")

    def clear_voice_state(self, room_id: str, username: str):
        room = self.voice_states.get(room_id)
        if not room:
            return
        room.pop(username, None)
        if not room:
            self.voice_states.pop(room_id, None)

    def voice_states_in_room(self, room_id: str):
        # return a plain dict safe for json
        return self.voice_states.get(room_id, {})


    
def query_giphy_search(q: str, limit: int = 20, offset: int = 0):
    if not GIPHY_KEY:
        print("[gif] Warning: GIPHY_API_KEY not set")
        return {"data": []}

    cache_key = f"giphy:search:{q}:{limit}:{offset}"
    if cache_key in gif_search_cache:
        return gif_search_cache[cache_key]

    url = "https://api.giphy.com/v1/gifs/search"
    params = {
        "api_key": GIPHY_KEY,
        "q": q,
        "limit": limit,
        "offset": offset,
        "rating": "r",
        "lang": "en",
    }

    try:
        r = requests.get(url, params=params, timeout=(3.05, 12))
        r.raise_for_status()
        result = r.json()
    except requests.exceptions.RequestException as e:
        print("[gif] search request failed:", repr(e))
        result = {"data": []}

    gif_search_cache[cache_key] = result
    return result


def query_giphy_trending(limit: int = 20, offset: int = 0):
    if not GIPHY_KEY:
        print("[gif] Warning: GIPHY_API_KEY not set")
        return {"data": []}

    cache_key = f"giphy:trending:{limit}:{offset}"
    if cache_key in gif_search_cache:
        return gif_search_cache[cache_key]

    url = "https://api.giphy.com/v1/gifs/trending"
    params = {"api_key": GIPHY_KEY, "limit": limit, "offset": offset}

    try:
        r = requests.get(url, params=params, timeout=(3.05, 12))
        r.raise_for_status()
        result = r.json()
    except requests.exceptions.RequestException as e:
        print("[gif] trending request failed:", repr(e))
        result = {"data": []}  # keep shape consistent with your route

    gif_search_cache[cache_key] = result
    return result

def get_user_from_token_sync(token: str):
    """
    Return a small plain dict for the user (not a SQLAlchemy model).
    This avoids accidentally returning ORM objects to FastAPI response encoding.
    """
    uid = decode_access_token(token)
    if not uid:
        return None
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.id == int(uid)).first()
        if not u:
            return None
        # Return a plain dict only containing the fields we need
        return {"id": u.id, "username": u.username}
    finally:
        db.close()

def sniff_image_type(first_bytes: bytes) -> str | None:
    if first_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if first_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if first_bytes.startswith(b"GIF87a") or first_bytes.startswith(b"GIF89a"):
        return "image/gif"
    if first_bytes.startswith(b"RIFF") and b"WEBP" in first_bytes[8:16]:
        return "image/webp"
    return None

def _extract_bearer(auth: str | None) -> str | None:
    if not auth:
        return None
    parts = auth.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None

def make_invite_code() -> str:
    return secrets.token_urlsafe(16)

async def get_current_user(
        authorization: str | None = Header(None),
        token: str | None = Header(None),
):
    raw = _extract_bearer(authorization) or token
    if not raw:
        raise HTTPException(status_code=401, detail="missing token")
    user = await run_in_threadpool(get_user_from_token_sync, raw)
    if not user:
        raise HTTPException(status_code=401, detail="invalid token")
    return user


connectionmanager = ConnectionManager()

class RegisterRequest(BaseModel):
    username: str
    password: str

class RoomCreate(BaseModel):
    name: str

class ProfileOut(BaseModel):
    id: int
    username: str
    display_name: Optional[str] = None
    status: Optional[str] = None
    bio: Optional[str] = None
    avatar: Optional[str] = None
    avatar_color: Optional[str] = None

class ProfileUpdate(BaseModel):
    display_name: Optional[str] = None
    status: Optional[str] = None
    bio: Optional[str] = None
    avatar_color: Optional[str] = None

@app.middleware("http")
async def add_security_headers(request, call_next):
    resp: Response = await call_next(request)
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer-when-downgrade"
    return resp

@app.post("/admin/emojis/reload")
def reload_emojis(
    background: BackgroundTasks, 
    current_user=Depends(get_current_user),
    _=Depends(require_admin_token),
):
    try:
        load_emojis()
        return {"ok": True, "loaded": len(EMOJI_LIST)}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/auth/register")
def register(payload: RegisterRequest, db=Depends(get_db)):
    try:
        username = payload.username.strip()
        password = payload.password
        if not username or not password:
            raise HTTPException(status_code=400, detail="username and password are required")
        if len(password) < 8:
            raise HTTPException(status_code=400, detail="password too short")
        
        existing = db.query(User).filter(User.username == username).first()
        if existing:
            raise HTTPException(status_code=400, detail="username already taken")
        
        user = User(username=username, password_hash=hash_password(password))
        db.add(user)
        try:
            db.commit()
        except IntegrityError as e:
            db.rollback()
            raise HTTPException(status_code=400, detail="username already taken")
        db.refresh(user)

        token = create_access_token(user.id)
        return {"access_token": token, "username": user.username, "user_id": user.id}
    
    except HTTPException:
        raise

    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="internal server error (see server logs)")

@app.post("/auth/login")
def login(payload: RegisterRequest, db=Depends(get_db)):
    username = (payload.username or "").strip()
    password = payload.password or ""

    if not username or not password:
        raise HTTPException(status_code=400, detail="username and password are required")

    user = db.query(User).filter(User.username == username).first()
    if not user or not user.password_hash:
        raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="invalid credentials")
    if not verify_password(password, user.password_hash):
        raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="invalid credentials")
    token = create_access_token(user.id)
    return {"access_token": token, "username": user.username, "user_id": user.id}

@app.post("/rooms")
async def create_room(payload: RoomCreate, current_user=Depends(get_current_user)):
    name = (payload.name or "").strip()
    if not name or len(name) > 80:
        raise HTTPException(status_code=400, detail="invalid room name")
    
    def _create():
        db = SessionLocal()
        try:
            existing = db.query(Room).filter(Room.name == name).first()
            if existing:
                m = db.query(RoomMember).filter(
                    RoomMember.room_id == existing.id,
                    RoomMember.user_id == current_user["id"]
                ).first()
                if not m:
                    db.add(RoomMember(room_id=existing.id, user_id=current_user["id"], role="member"))
                    db.commit()
                return {"id": existing.id, "name": existing.name, "created": False}
            
            r = Room(name=name)
            db.add(r)
            db.flush()
            db.add(RoomMember(room_id=r.id, user_id=current_user["id"], role="owner"))
            db.commit()
            db.refresh(r)
            return {"id": r.id, "name": r.name, "created": True}
        finally:
            db.close()

    return await run_in_threadpool(_create)

@app.post("/rooms/{room_id}/join")
async def join_room(room_id: str, current_user=Depends(get_current_user)):
    def _join():
        db = SessionLocal()
        try:
            r = db.query(Room).filter(Room.id == room_id).first()
            if not r:
                raise HTTPException(404, "room_not_found")
            
            existing = db.query(RoomMember).filter(
                RoomMember.room_id == room_id,
                RoomMember.user_id == current_user["id"]
            ).first()
            if existing:
                return {"ok": True, "joined": False}
            
            db.add(RoomMember(room_id=room_id, user_id=current_user["id"]))
            db.commit()
            return {"ok": True, "joined": True}
        finally:
            db.close()
    return await run_in_threadpool(_join)

@app.get("/rooms/{room_id}/messages")
async def load_room_messages(
    room_id: str,
    before: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=100),
    current_user = Depends(get_current_user),
):
    """
    Cursor-based pagination for infinite scroll.
    Returns messages ordered newest -> oldest.
    """

    def _load():
        db = SessionLocal()
        try:
            # --- verify room membership ---
            member = db.query(RoomMember).filter(
                RoomMember.room_id == room_id,
                RoomMember.user_id == current_user["id"]
            ).first()
            if not member:
                raise HTTPException(status_code=403, detail="not_in_room")
            
            q = db.query(Message).filter(
                Message.room_id == room_id
            )

            # --- cursor filter ---
            if before:
                try:
                    before_dt = _dt.datetime.fromisoformat(
                        before.replace("Z", "+00:00")
                    )
                except ValueError:
                    raise HTTPException(status_code=400, detail="invalid_before_cursor")
                
                q = q.filter(Message.timestamp < before_dt)

            rows = (
                q.order_by(Message.timestamp.desc())
                    .limit(limit + 1)
                    .all()
            )

            has_more = len(rows) > limit
            rows = rows[:limit]

            # materialize to plain dicts
            out = []
            for m in rows:
                out.append({
                    "type": "message",
                    "id": m.id,
                    "author": m.author_username,
                    "author_id": m.author_id,
                    "timestamp": m.timestamp.isoformat().replace("+00:00", "Z"),
                    "edited_at": m.edited_at.isoformat().replace("+00:00","Z") if m.edited_at else None,
                    "content": m.content,
                    "reply_to": m.reply_to,
                    "deleted": m.deleted,
                    "pinned": m.pinned,
                    "pinned_by": m.pinned_by,
                    "pinned_at": m.pinned_at.isoformat().replace("+00:00","Z") if m.pinned_at else None,
                    "reactions": m.reactions or {},
                })

            next_cursor = out[-1]["timestamp"] if out else None
            return out, has_more, next_cursor

        finally:
            db.close()

    messages, has_more, next_cursor = await run_in_threadpool(_load)
    return {
        "messages": messages,
        "has_more": has_more,
        "next_cursor": next_cursor
    }

@app.get("/rooms/{room_id}/members")
async def room_members(room_id: str, current_user=Depends(get_current_user)):
    def _list():
        db = SessionLocal()
        try:
            # verify membership
            m = db.query(RoomMember).filter(
                RoomMember.room_id == room_id,
                RoomMember.user_id == current_user["id"]
            ).first()
            if not m:
                raise HTTPException(status_code=403, detail="not_in_room")

            rows = (
                db.query(User.username, User.display_name, User.avatar_color, User.status)
                  .join(RoomMember, RoomMember.user_id == User.id)
                  .filter(RoomMember.room_id == room_id)
                  .order_by(User.username.asc())
                  .all()
            )
            return [{
                "username": u,
                "display_name": dn,
                "avatar_color": ac,
                "status": st
            } for (u, dn, ac, st) in rows]
        finally:
            db.close()

    return {"results": await run_in_threadpool(_list)}

@app.post("/rooms/{room_id}/invite")
async def create_room_invite(room_id: str, current_user=Depends(get_current_user)):
    def _make():
        db = SessionLocal()
        try:
            # verify membership
            m = db.query(RoomMember).filter(
                RoomMember.room_id == room_id,
                RoomMember.user_id == current_user["id"]
            ).first()
            if not m:
                raise HTTPException(status_code=403, detail="not_in_room")

            r = db.query(Room).filter(Room.id == room_id).first()
            if not r:
                raise HTTPException(status_code=404, detail="room_not_found")

            if not r.invite_code:
                r.invite_code = make_invite_code()
                db.commit()

            return {"invite_code": r.invite_code}
        finally:
            db.close()

    out = await run_in_threadpool(_make)
    # client can build absolute URL; returning relative keeps it environment-agnostic
    return {"invite_code": out["invite_code"], "invite_path": f"/?invite={out['invite_code']}"}

@app.post("/invites/{code}/join")
async def join_by_invite(code: str, current_user=Depends(get_current_user)):
    code = (code or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="invalid_code")

    def _join():
        db = SessionLocal()
        try:
            r = db.query(Room).filter(Room.invite_code == code).first()
            if not r:
                raise HTTPException(status_code=404, detail="invite_not_found")

            existing = db.query(RoomMember).filter(
                RoomMember.room_id == r.id,
                RoomMember.user_id == current_user["id"]
            ).first()
            if existing:
                return {"room_id": r.id, "joined": False}

            db.add(RoomMember(room_id=r.id, user_id=current_user["id"], role="member"))
            db.commit()
            return {"room_id": r.id, "joined": True}
        finally:
            db.close()

    return await run_in_threadpool(_join)

@app.post("/upload")
async def upload_file(
    file: UploadFile = File(...), 
    current_user=Depends(get_current_user),
):
    chunk0 = await file.read(1024)
    real_type = sniff_image_type(chunk0)
    
    if real_type not in ALLOWED_UPLOAD_TYPES:
        raise HTTPException(status_code=415, detail="Unsupported file type")
    
    filename = f"{uuid.uuid4().hex}_{os.path.basename(file.filename)}"
    dest = UPLOAD_DIR / filename
    total = 0
    async with aiofiles.open(dest, "wb") as out_file:
        await out_file.write(chunk0)
        total = len(chunk0)
        while True:
            chunk = await file.read(1024 * 64)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                await out_file.close()
                # delete partial file
                try: dest.unlink()
                except Exception: pass
                raise HTTPException(status_code=413, detail="File too large")
            await out_file.write(chunk)
    return {"url": f"/uploads/{filename}", "filename": filename}

def is_public_host(hostname: str) -> bool:
    try:
        infos = socket.getaddrinfo(hostname, None)
        for fam, _, _, _, sockaddr in infos:
            ip = sockaddr[0]
            ip_obj = ipaddress.ip_address(ip)
            if (
                ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local
                or ip_obj.is_multicast or ip_obj.is_reserved
            ):
                return False
        return True
    except Exception:
        return False

@app.post("/preview")
def link_preview(url: str):

    u = urlparse(url)
    if u.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="invalid_url_scheme")
    if not u.hostname or not is_public_host(u.hostname):
        raise HTTPException(status_code=400, detail="blocked_host")

    try:
        r = requests.get(
            url,
            timeout=(3.05, 5),
            allow_redirects=False,
            headers={"User-Agent": "ChatterboxPreview/1.0"},
            stream=True,
        )
    except requests.RequestException:
        raise HTTPException(status_code=400, detail="preview_fetch_failed")

    # cap bytes
    max_bytes = 250_000
    data = b""
    for chunk in r.iter_content(16_384):
        if not chunk:
            break
        data += chunk
        if len(data) > max_bytes:
            break

    soup = BeautifulSoup(data, "html.parser")

    def meta(prop):
        tag = soup.find("meta", property=prop)
        return tag["content"] if tag and tag.get("content") else None

    return {
        "title": meta("og:title"),
        "description": meta("og:description"),
        "image": meta("og:image"),
        "url": url,
    }

@app.get("/me", response_model=ProfileOut)
async def get_me(current_user=Depends(get_current_user)):
    def _get(uid: int):
        db = SessionLocal()
        try:
            u = db.query(User).filter(User.id == uid).first()
            if not u:
                raise HTTPException(status_code=404, detail="not_found")
            return {
                "id": u.id,
                "username": u.username,
                "display_name": u.display_name,
                "status": u.status,
                "bio": u.bio,
                "avatar_color": u.avatar_color,
                "avatar": u.avatar,
            }
        finally:
            db.close()
    return await run_in_threadpool(_get, current_user["id"])

@app.patch("/me", response_model=ProfileOut)
async def update_me(payload: ProfileUpdate, current_user=Depends(get_current_user)):
    def _upd(uid: int, p: ProfileUpdate):
        db = SessionLocal()
        try:
            u = db.query(User).filter(User.id == uid).first()
            if not u:
                raise HTTPException(status_code=404, detail="not_found")
            
            if p.display_name is not None:
                u.display_name = p.display_name.strip()[:120] or None
            if p.status is not None:
                u.status = p.status.strip()[:32] or None
            if p.bio is not None:
                u.bio = p.bio.strip()[:200] or None
            if p.avatar_color is not None:
                c = p.avatar_color.strip()
                if c and not c.startswith("#"):
                    c = "#" + c
                u.avatar_color = c[:16] if c else None

            db.commit()
            db.refresh(u)
            return {
                "id": u.id,
                "username": u.username,
                "display_name": u.display_name,
                "status": u.status,
                "bio": u.bio,
                "avatar_color": u.avatar_color,
                "avatar": u.avatar,
            }
        finally:
            db.close()

    return await run_in_threadpool(_upd, current_user["id"], payload)

@app.get("/rooms")
async def list_rooms(current_user=Depends(get_current_user)):
    def _list():
        db = SessionLocal()
        try:
            rooms = (
                db.query(Room)
                    .join(RoomMember, RoomMember.room_id == Room.id)
                    .filter(RoomMember.user_id == current_user["id"])
                    .order_by(Room.created_at.asc())
                    .all())
            return [{"id": r.id, "name": r.name} for r in rooms]
        finally:
            db.close()

    return {"results": await run_in_threadpool(_list)}

@app.get("/gif/search")
def gif_search(q: str, limit: int = 20, offset: int = 0):
    raw = query_giphy_search(q, limit, offset)
    out = []
    for item in raw.get("data", []):
        images = item.get("images", {})
        preview = images.get("fixed_height_small", {}).get("url") or images.get("downsized_small", {}).get("mp4")
        url = images.get("original", {}).get("url") or images.get("downsized", {}).get("url")
        out.append({
            "id": item.get("id"),
            "url": url,
            "preview": preview,
            "title": item.get("title"),
            "source": item.get("source"),
        })
    return {"results": out}

@app.get("/gif/trending")
def gif_trending(limit: int = 20, offset: int = 0):
    raw = query_giphy_trending(limit, offset)
    out = []
    for item in raw.get("data", []):
        images = item.get("images", {})
        preview = images.get("fixed_height_small", {}).get("url") or images.get("downsized_small", {}).get("mp4")
        url = images.get("original", {}).get("url") or images.get("downsized", {}).get("url")
        out.append({
            "id": item.get("id"),
            "url": url,
            "preview": preview,
            "title": item.get("title"),
            "source": item.get("source"),
        })
    return {"results": out}

@app.get("/gif/favorites")
async def gif_favorites(current_user = Depends(get_current_user)):
    def _get(uid):
        db = SessionLocal()
        try:
            rows = db.query(Favorite).filter(Favorite.user_id == uid).order_by(Favorite.created_at.desc()).all()
            return [{
                "id": r.id,
                "gif_id": r.gif_id,
                "url": r.url,
                "preview": r.preview,
                "title": r.title,
                "metadata": r.metadata_json
            } for r in rows]
        finally:
            db.close()
    return {"results": await run_in_threadpool(_get, current_user["id"])}

@app.post("/gif/favorite")
async def add_gif_favorite(payload: Dict[str, Any], current_user = Depends(get_current_user)):
    """
    payload: { gif_id, url, preview?, title?, metadata? }
    """
    #gif_id = payload.get("gif_id") or payload.get("id") or payload.get("url")
    metadata = payload.get("metadata") or {}
    provider_id = None
    if isinstance(metadata, dict):
        provider_id = metadata.get("provider_id") or metadata.get("id")

    gif_id = provider_id or payload.get("gif_id") or payload.get("id") or payload.get("url")

    url = payload.get("url")
    if not gif_id or not url:
        raise HTTPException(status_code=400, detail="gif_id and url required")

    def _add(uid, gif_id, url, preview, title, metadata):
        db = SessionLocal()
        try:
            orig_gid = "" if gif_id is None else str(gif_id)
            if len(orig_gid) >128:
                safe_gid = hashlib.sha256(orig_gid.encode("utf-8")).hexdigest()
                metadata = metadata or {}

                if "original_gif_id" not in metadata:
                    metadata["original_gif_id"] = orig_gid
            else:
                safe_gid = orig_gid

            existing = db.query(Favorite).filter(
                Favorite.user_id == uid, 
                Favorite.gif_id == str(safe_gid)
                ).first()
            
            if existing:
                orig = None
                try:
                    orig = (existing.metadata_json or {}).get("original_gif_id")
                except Exception:
                    orig = None
                return {"ok": True, "id": existing.id, "gif_id": existing.gif_id, "original_gif_id": orig}
            
            safe_url = (url or "")[:2048]
            safe_preview = (preview or "")[:1024]
            safe_title = (title or "")[:256]

            fav = Favorite(
                user_id=uid, 
                gif_id=str(safe_gid),
                url=safe_url,
                preview=safe_preview, 
                title=safe_title, 
                metadata_json=metadata or {}
            )
            db.add(fav)
            db.commit()
            db.refresh(fav)

            orig_return = None
            if metadata and isinstance(metadata, dict):
                orig_return = metadata.get("original_gif_id")

            return {"ok": True, "id": fav.id, "gif_id": fav.gif_id, "original_gif_id": orig_return}

        except DataError:
            db.rollback()
            safe_gid = hashlib.sha256((str(gif_id) or "").encode("utf-8")).hexdigest()
            fav = Favorite(
                user_id=uid,
                gif_id=safe_gid,
                url=(url or "")[:2048],
                preview=(preview or "")[:1024] or None,
                title=(title or "")[:256] or "",
                metadata_json=(metadata or {"original_gif_id": str(gif_id)})
            )
            db.add(fav)
            db.commit()
            db.refresh(fav)
            return {"ok": True, "id": fav.id, "gif_id": fav.gif_id, "original_gif_id": (metadata or {}).get("original_gif_id")}
        finally:
            db.close()
            

    res = await run_in_threadpool(_add, current_user["id"], gif_id, url, payload.get("preview"), payload.get("title"), payload.get("metadata"))
    return res

@app.delete("/gif/favorite")
async def remove_gif_favorite(id: int, current_user = Depends(get_current_user)):
    def _remove(uid, fid):
        db = SessionLocal()
        try:
            row = db.query(Favorite).filter(Favorite.user_id == uid, Favorite.id == fid).first()
            if not row:
                return {"ok": False, "error": "not_found"}
            db.delete(row)
            db.commit()
            return {"ok": True}
        finally:
            db.close()
    res = await run_in_threadpool(_remove, current_user["id"], id)
    if not res.get("ok"):
        raise HTTPException(status_code=404, detail=res.get("error"))
    return res
         
@app.get("/", response_class=HTMLResponse)
def read_index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})




# username = string to track names
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    token = websocket.query_params.get("token")
    room_id = websocket.query_params.get("room_id")

    # --- auth / user lookup ---
    print("[ws] token present:", bool(token))
    if not token:
        print("[ws] closing websocket: no token provided")
        await websocket.close(code=1008, reason="missing token")  # policy violation / auth required
        return

    if not room_id:
        print("[ws] closing websocket: no room id")
        await websocket.close(code=1008, reason="missing room_id")
        return

    peer = getattr(websocket, "client", None)
    print("[ws] incoming connection from peer:", peer)

    try:
        user_id = decode_access_token(token)
    except Exception as e:
        print("[ws] decode_access_token raised:", repr(e))
        user_id = None

    print("[ws] decode_access_token ->", user_id)
    if not user_id:
        print("[ws] closing websocket: token invalid/expired")
        await websocket.close(code=1008)
        return
    
    await websocket.accept()

  

    # fetch user in threadpool (session created inside)
    def get_user_by_id(uid):
        db = SessionLocal()
        try:
            u = db.query(User).filter(User.id == int(uid)).first()
            if not u:
                return None
            return {"id": u.id, "username": u.username}
        finally:
            db.close()

    def get_room_for_user(rid: str, uid: int):
        db = SessionLocal()
        try:
            r = db.query(Room).filter(Room.id == rid).first()
            if not r: return None
            m = db.query(RoomMember).filter(RoomMember.room_id==rid, RoomMember.user_id==uid).first()
            if not m: return None
            return {"id": r.id, "name": r.name}
        finally:
            db.close()


    user_info = await run_in_threadpool(get_user_by_id, user_id)
    if not user_info:
        print("[ws] closing websocket: user not found for id", user_id)
        await websocket.close(code=1008, reason="user not found")
        return
    
    room_info = await run_in_threadpool(
        get_room_for_user, 
        room_id,
        user_info["id"])
    if not room_info:
        await websocket.close(code=1008, reason="room not found")
        return

    client_meta = {
        "user_id": user_info["id"], 
        "username": user_info["username"], 
        "room_id": room_id
    }
    
    try:
        await connectionmanager.connect(websocket, client_meta)
        await connectionmanager.broadcast_presence_snapshot_room(room_id)
        await connectionmanager.broadcast_presence_change_room(room_id, client_meta["username"], "online")

    except Exception as e:
        print("[ws] connectionmanager.connect failed:", repr(e))
        try:
            await websocket.close()
        except Exception:
            pass
        return
    

    # --- load history (threadpool DB access) ---
    def load_history(limit=200):
        db = SessionLocal()
        try:
            rows = (db.query(Message).filter(Message.room_id == room_id).order_by(Message.timestamp.asc()).limit(limit).all())
            out = []
            for m in rows:
                out.append({
                    "type": "message",
                    "id": m.id,
                    "author": m.author_username,
                    "author_id": m.author_id,
                    "timestamp": m.timestamp.isoformat().replace("+00:00", "Z"),
                    "content": m.content,
                    "reply_to": m.reply_to,
                    "deleted": m.deleted,
                    "pinned": m.pinned,
                    "pinned_by": m.pinned_by,
                    "pinned_at": m.pinned_at.isoformat().replace("+00:00","Z") if m.pinned_at else None,
                    "edited_at": m.edited_at.isoformat().replace("+00:00","Z") if m.edited_at else None,
                    "reactions": m.reactions or {}
                })
            return out
        finally:
            db.close()

    def load_user_directory(usernames):
        db = SessionLocal()
        try:
            rows = db.query(User).filter(User.username.in_(list(usernames))).all()
            d = {}
            for u in rows:
                d[u.username] = {
                    "display_name": u.display_name,
                    "avatar_color": u.avatar_color,
                    "status": u.status
                }
            return d
        finally:
            db.close()

    history = await run_in_threadpool(load_history, 400)

    usernames = {m["author"] for m in history if m.get("author")}
    user_dir = await run_in_threadpool(load_user_directory, usernames)
    await connectionmanager.send_personal_message({"type": "user_directory", "users": user_dir}, websocket)


    # ensure connectionmanager has the loaded history in memory (avoid later not_found)
    for m in history:
        # avoid duplicate
        if m["id"] not in connectionmanager.messages_index:
            connectionmanager.store_message(m)

    print(f"[ws] sending history ({len(history)} messages) to {client_meta['username']}")
    await connectionmanager.send_personal_message({"type": "history", "messages": history}, websocket)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except Exception:
                print("[recv] malformed JSON, ignoring:", raw)
                continue

            print("[recv]", client_meta["username"], data.get("type", "message"), data.get("content"))
            msg_type = data.get("type", "message")

            # -------------- MESSAGE --------------
            if msg_type == "message":
                m_id = str(uuid.uuid4())
                timestamp = _dt.datetime.now(_dt.timezone.utc)

                content = data.get("content") or ""
                gif = data.get("gif")
                if (not content.strip()) and isinstance(gif, dict):
                    content = gif.get("url") or gif.get("preview") or ""

                def save_msg():
                    db = SessionLocal()
                    try:
                        msg = Message(
                            id=m_id,
                            author_id=client_meta["user_id"],
                            author_username=client_meta["username"],
                            timestamp=timestamp,
                            content=content,
                            reply_to=data.get("reply_to"),
                            room_id=room_id,
                            deleted=False,
                            pinned=False,
                            reactions={}
                        )
                        db.add(msg)
                        db.commit()
                    finally:
                        db.close()
                    return {
                        "type": "message",
                        "id": m_id,
                        "author": client_meta["username"],
                        "author_id": client_meta["user_id"],
                        "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
                        "content": content,
                        "reply_to": data.get("reply_to"),
                        "room_id": room_id,
                        "deleted": False,
                        "pinned": False,
                        "reactions": {}
                    }

                out_msg = await run_in_threadpool(save_msg)

                connectionmanager.store_message(out_msg)
                await connectionmanager.send_personal_message(out_msg, websocket)

                await connectionmanager.broadcast_room(room_id, out_msg, exclude=websocket)

            # -------------- DELETE --------------
            elif msg_type == "delete":
                target_id = data.get("id")
                if not target_id:
                    continue

                def delete_msg_if_author(tid, author_id):
                    db = SessionLocal()
                    try:
                        target = db.query(Message).filter(Message.id == tid).first()
                        if not target:
                            return {"error":"not_found"}
                        if target.author_id != author_id:
                            return {"error":"not_author"}
                        target.deleted = True
                        target.content = ""
                        db.commit()
                        return {"ok": True}
                    finally:
                        db.close()

                res = await run_in_threadpool(delete_msg_if_author, target_id, client_meta["user_id"])
                if res.get("error") == "not_found":
                    await connectionmanager.send_personal_message({"type":"error","error":"not_found","id":target_id}, websocket)
                    continue
                if res.get("error") == "not_author":
                    await connectionmanager.send_personal_message({"type":"error","error":"not_author","id":target_id}, websocket)
                    continue

                # update in-memory if present
                t = connectionmanager.find_message(target_id)
                if t:
                    t["deleted"] = True
                    t["content"] = ""

                await connectionmanager.broadcast_room(room_id, {"type": "delete", "id": target_id})

            # ------------------ EDIT -------------------
            elif msg_type == "edit":
                # payload: { type: "edit", id: "<message_id>", content: "<new content>" }
                target_id = data.get("id")
                new_content = data.get("content", "")
                if not target_id:
                    continue

                def edit_msg_if_author(tid, author_id, new_content, edited_at_dt):
                    db = SessionLocal()
                    try:
                        target = db.query(Message).filter(Message.id == tid).first()
                        if not target:
                            return {"error": "not_found"}
                        if target.author_id != author_id:
                            return {"error": "not_author"}
                        target.content = new_content
                        target.edited_at = edited_at_dt
                        db.commit()
                        return {"ok": True}
                    finally:
                        db.close()

                edited_at_dt = _dt.datetime.now(_dt.timezone.utc)
                res = await run_in_threadpool(edit_msg_if_author, target_id, client_meta["user_id"], new_content, edited_at_dt)
                if res.get("error") == "not_found":
                    await connectionmanager.send_personal_message({"type":"error","error":"not_found","id":target_id}, websocket)
                    continue
                if res.get("error") == "not_author":
                    await connectionmanager.send_personal_message({"type":"error","error":"not_author","id":target_id}, websocket)
                    continue

                # update in-memory if present
                t = connectionmanager.find_message(target_id)
                if t:
                    t["content"] = new_content
                    t["edited_at"] = edited_at_dt.isoformat().replace("+00:00", "Z")

                # broadcast edit to everyone
                await connectionmanager.broadcast_room(room_id, {
                    "type": "edit",
                    "id": target_id,
                    "content": new_content,
                    "edited_at": edited_at_dt.isoformat().replace("+00:00", "Z")
                })

            # -------------- PIN --------------

            elif msg_type == "pin":
                target_id = data.get("id")
                pin_flag = data.get("pin", True)
                if not target_id:
                    continue

                # load in-memory target (will fallback to DB via find_message)
                t = connectionmanager.find_message(target_id)
                if not t:
                    await connectionmanager.send_personal_message({"type":"error","error":"not_found","id":target_id}, websocket)
                    continue

                # if trying to pin, enforce global limit
                if pin_flag:
                    def count_pins():
                        db = SessionLocal()
                        try:
                            return db.query(Message).filter(Message.room_id == room_id, Message.pinned == True).count()
                        finally:
                            db.close()
                    current_pinned = await run_in_threadpool(count_pins)
                    if current_pinned >= MAX_PINNED:
                        await connectionmanager.send_personal_message({"type":"error","error":"pin_limit","limit": MAX_PINNED}, websocket)
                        continue

                # update in-memory
                if pin_flag:
                    now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")
                    t["pinned"] = True
                    t["pinned_by"] = client_meta["user_id"]
                    t["pinned_at"] = now_iso
                else:
                    t["pinned"] = False
                    t["pinned_by"] = None
                    t["pinned_at"] = None

                # persist to DB: convert pinned_at string back to datetime or None
                def update_pin_db(tid, pinned, pinned_by_id, pinned_at_iso):
                    db = SessionLocal()
                    try:
                        pinned_at_dt = None
                        if pinned_at_iso:
                            # expect ISO like "2023-01-01T12:00:00Z"
                            from datetime import datetime, timezone as _tz
                            pinned_at_dt = datetime.fromisoformat(pinned_at_iso.replace("Z", "+00:00"))
                        db.query(Message).filter(Message.id == tid).update({
                            "pinned": pinned,
                            "pinned_by": pinned_by_id,
                            "pinned_at": pinned_at_dt
                        })
                        db.commit()
                    finally:
                        db.close()

                await run_in_threadpool(update_pin_db, target_id, t["pinned"], t["pinned_by"], t["pinned_at"])
                await connectionmanager.broadcast_room(room_id, {
                    "type": "pin", 
                    "id": target_id, 
                    "pinned": t["pinned"], 
                    "pinned_by": t["pinned_by"], 
                    "pinned_by_username": client_meta["username"] if pin_flag else None,
                    "pinned_at": t.get("pinned_at")
                })

            # -------------- REACT --------------
            elif msg_type == "react":
                target_id = data.get("id")
                emoji_raw = data.get("emoji")
                if not target_id or not emoji_raw:
                    continue

                # normalize same as your existing logic
                emoji_norm = None
                if isinstance(emoji_raw, str):
                    e = emoji_raw.strip()
                    if e.startswith(":") and e.endswith(":"):
                        e = e[1:-1]
                    lower = e.lower()
                    if lower in EMOJI_ALIASES:
                        emoji_norm = EMOJI_ALIASES[lower]
                    elif e in EMOJI_LIST:
                        emoji_norm = e
                    else:
                        await connectionmanager.send_personal_message({"type":"error","error":"invalid_emoji","emoji":emoji_raw}, websocket)
                        continue
                else:
                    await connectionmanager.send_personal_message({"type":"error","error":"invalid_emoji_type"}, websocket)
                    continue

                # get in-memory target
                t = connectionmanager.find_message(target_id)
                if not t:
                    await connectionmanager.send_personal_message({"type":"error","error":"not_found","id":target_id}, websocket)
                    continue

                reactions = t.setdefault("reactions", {})
                user_list = reactions.setdefault(emoji_norm, [])

                user = client_meta["username"]
                if user in user_list:
                    user_list.remove(user)
                else:
                    user_list.append(user)

                if not user_list:
                    reactions.pop(emoji_norm, None)

                # persist reactions to DB (replace with updated dict)
                def update_reactions_db(tid, reactions_dict):
                    db = SessionLocal()
                    try:
                        db.query(Message).filter(Message.id == tid).update({"reactions": reactions_dict})
                        db.commit()
                    finally:
                        db.close()

                await run_in_threadpool(update_reactions_db, target_id, t["reactions"])

                await connectionmanager.broadcast_room(room_id, {
                    "type":"react",
                    "id":target_id,
                    "emoji":emoji_norm,
                    "users": user_list
                })

            # -------------- REPLY PREVIEW --------------
            elif msg_type == "reply_preview_request":
                target_id = data.get("id")
                t = connectionmanager.find_message(target_id)
                if t:
                    await connectionmanager.send_personal_message({"type":"reply_preview","id":target_id,"content": t["content"], "deleted": t["deleted"]}, websocket)

            elif msg_type == "typing":
                is_typing = bool(data.get("is_typing"))
                await connectionmanager.broadcast_room(room_id, {
                    "type": "typing",
                    "user": client_meta["username"],
                    "is_typing": is_typing
                }, exclude=websocket)

            # -------------- VOICE: JOIN --------------
            elif msg_type == "voice_join":
                username = client_meta["username"]

                # add + send snapshot to this user (so client knows who to connect to)
                connectionmanager.add_voice_member(room_id, username)

                # initialize state for joiner
                connectionmanager.set_voice_state(room_id, username, muted=False, speaking=False)

                await connectionmanager.send_personal_message({
                    "type": "voice_snapshot",
                    "users": connectionmanager.voice_members_in_room(room_id),
                    "states": connectionmanager.voice_states_in_room(room_id),
                }, websocket)


                # notify room others
                await connectionmanager.broadcast_room(room_id, {
                    "type": "voice_join",
                    "user": username,
                }, exclude=websocket)

            # -------------- VOICE: LEAVE --------------
            elif msg_type == "voice_leave":
                username = client_meta["username"]
                connectionmanager.remove_voice_member(room_id, username)
                connectionmanager.clear_voice_state(room_id, username)

                await connectionmanager.broadcast_room(room_id, {
                    "type": "voice_leave",
                    "user": username,
                }, exclude=websocket)

            # -------------- VOICE: SIGNAL RELAY --------------
            elif msg_type == "voice_signal":
                """
                payload: {
                  type: "voice_signal",
                  to: "<username>",
                  data: { kind: "offer"|"answer"|"ice", sdp?:..., candidate?:... }
                }
                """
                to_user = (data.get("to") or "").strip()
                sig = data.get("data")

                if not to_user or not isinstance(sig, dict):
                    await connectionmanager.send_personal_message({
                        "type": "error",
                        "error": "invalid_voice_signal",
                    }, websocket)
                    continue

                target_ws = connectionmanager.find_ws_by_username(room_id, to_user)
                if not target_ws:
                    await connectionmanager.send_personal_message({
                        "type": "error",
                        "error": "voice_target_offline",
                        "to": to_user,
                    }, websocket)
                    continue

                # Relay to target
                await connectionmanager.send_personal_message({
                    "type": "voice_signal",
                    "from": client_meta["username"],
                    "data": sig,
                }, target_ws)

            elif msg_type == "voice_state":
                username = client_meta["username"]

                # Only accept state updates from users currently in voice
                if username not in connectionmanager.voice_members.get(room_id, set()):
                    # ignore quietly or return an error
                    continue

                muted = data.get("muted", None)
                speaking = data.get("speaking", None)

                # Normalize to bool if present
                if muted is not None:
                    muted = bool(muted)
                    # If muted, speaking should be false
                    if muted:
                        speaking = False

                if speaking is not None:
                    speaking = bool(speaking)

                connectionmanager.set_voice_state(room_id, username, muted=muted, speaking=speaking)

                await connectionmanager.broadcast_room(room_id, {
                    "type": "voice_state",
                    "user": username,
                    "muted": muted if muted is not None else None,
                    "speaking": speaking if speaking is not None else None,
                }, exclude=None)



            else:
                await connectionmanager.send_personal_message({"type":"error","error":"unknown_type","got": msg_type}, websocket)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print("[ws] unexpected error:", repr(e))
    finally:
        meta = connectionmanager.disconnect(websocket)
        if meta:
            rid = meta.get("room_id")
            username = meta.get("username")

            # remove from voice + tell others
            if rid and username:
                if username in connectionmanager.voice_members.get(rid, set()):
                    connectionmanager.remove_voice_member(rid, username)
                    connectionmanager.clear_voice_state(rid, username)
                    try:
                        await connectionmanager.broadcast_room(rid, {
                            "type": "voice_leave",
                            "user": username,
                        })
                    except Exception as e:
                        print("[ws] failed to broadcast voice_leave:", repr(e))