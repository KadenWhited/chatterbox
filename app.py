from typing import Dict, Any
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from fastapi import FastAPI, WebSocket, Request, WebSocketDisconnect, UploadFile, File, Header
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
from models import User, Message, Favorite
from auth_utils import hash_password, verify_password, create_access_token, decode_access_token
from starlette.status import HTTP_401_UNAUTHORIZED
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError, DataError
from fastapi import Depends, HTTPException, BackgroundTasks
from fastapi.concurrency import run_in_threadpool

#Security/Webhosting
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi import Response
import hashlib
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
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

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


UPLOAD_DIR = pathlib.Path("static/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

GIPHY_KEY = os.environ.get("GIPHY_API_KEY")
gif_search_cache = TTLCache(maxsize= 256, ttl= 60)

class ConnectionManager:
    def __init__(self):
        # websocket -> metadata dict {"username": "123"}
        self.active_connections: Dict[WebSocket, Dict[str, Any]] = {}
        self.messages = []  # stores logs

    async def connect(self, websocket: WebSocket, client_meta: Dict[str, Any]):
        await websocket.accept()
        self.active_connections[websocket] = client_meta
        print(f"[connect] {client_meta}")

    def disconnect(self, websocket: WebSocket):
        meta = self.active_connections.pop(websocket, None)
        print(f"[disconnect] {meta}")

    async def send_personal_message(self, payload: Dict[str, Any], websocket: WebSocket):
        try:
            await websocket.send_text(json.dumps(payload))
        except Exception as e:
            print("[send_personal_message] failed to send to", self.active_connections.get(websocket), "error:", repr(e))
            raise

    async def broadcast(self, payload: Dict[str, Any], exclude: WebSocket = None):
        text = json.dumps(payload)
        # remove broken sockets
        for conn in list(self.active_connections.keys()):
            if conn == exclude:
                continue
            try:
                await conn.send_text(text)
            except Exception as e:
                print(f"[broadcast] removing broken socket {self.active_connections.get(conn)}: {e}")
                self.disconnect(conn)

    def store_message(self, msg: Dict[str, Any]):
        self.messages.append(msg)
        N = 1000
        if len(self.messages) > N:
            self.messages.pop(0)

    def find_message(self, message_id: str):
        # check in-memory first
        for m in self.messages:
            if m["id"] == message_id:
                return m

        # fallback to DB: try to load and materialize into same dict shape
        try:
            db = SessionLocal()
            row = db.query(Message).filter(Message.id == message_id).first()
            if not row:
                return None
            msg = {
                "type": "message",
                "id": row.id,
                "author": row.author_username,
                "author_id": row.author_id,
                "timestamp": row.timestamp.isoformat().replace("+00:00","Z"),
                "content": row.content,
                "reply_to": row.reply_to,
                "deleted": row.deleted,
                "pinned": row.pinned,
                "pinned_by": row.pinned_by,
                "pinned_at": row.pinned_at.isoformat().replace("+00:00","Z") if row.pinned_at else None,
                "reactions": row.reactions or {}
            }
            # keep in memory to prevent repeated DB hits
            self.store_message(msg)
            return msg
        finally:
            try:
                db.close()
            except Exception:
                pass
    
def query_giphy_search(q: str, limit: int = 20, offset: int = 0):
    if not GIPHY_KEY:
        print("[gif] Warning: GIPHY_API_KEY not set")
        return {"data": []}
    
    cache_key = f"giphy:search:{q}:{limit}:{offset}"
    if cache_key in gif_search_cache:
        return gif_search_cache[cache_key]
    
    url = "https://api.giphy.com/v1/gifs/search"
    params = {"api_key": GIPHY_KEY, "q": q, "limit": limit, "offset": offset, "rating":"r", "lang":"en"}

    try:
        r = requests.get(url, params=params, timeout=5)
        result = r.json() if r.status_code == 200 else {"data": []}
    except Exception:
        result = {"data": []}

    gif_search_cache[cache_key] = result
    return result

def query_giphy_trending(limit: int= 20, offset: int= 0):
    cache_key = f"giphy:trending:{limit}:{offset}"
    if cache_key in gif_search_cache:
        return gif_search_cache[cache_key]
    url = "https://api.giphy.com/v1/gifs/trending"
    params = {"api_key": GIPHY_KEY, "limit": limit, "offset": offset}
    r = requests.get(url, params=params, timeout=5)
    result = r.json() if r.status_code == 200 else {"data": []}
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

async def get_current_user(token: str = Header(None)):
    if not token:
        raise HTTPException(status_code=401, detail="missing token")
    user = await run_in_threadpool(get_user_from_token_sync, token)
    if not user:
        raise HTTPException(status_code=401, detail="invalid token")
    return user


connectionmanager = ConnectionManager()

class RegisterRequest(BaseModel):
    username: str
    password: str

@app.middleware("http")
async def add_security_headers(request, call_next):
    resp: Response = await call_next(request)
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer-when-downgrade"
    return resp

@app.post("/admin/emojis/reload")
def reload_emojis(background: BackgroundTasks):
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
        if len(password) < 6:
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

@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    filename = f"{uuid.uuid4().hex}_{os.path.basename(file.filename)}"
    dest = UPLOAD_DIR / filename
    total = 0
    async with aiofiles.open(dest, "wb") as out_file:
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
    return {"url": f"/static/uploads/{filename}", "filename": filename}

@app.post("/preview")
def link_preview(url: str):
    r = requests.get(url, timeout=3)
    soup = BeautifulSoup(r.text, "html.parser")

    def meta(prop):
        tag = soup.find("meta", property=prop)
        return tag["content"] if tag else None
    
    return {
        "title": meta("og:title"),
        "description": meta("og:description"),
        "image": meta("og:image"),
        "url": url
    }

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
def gid_trending(limit: int = 20, offset: int = 0):
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
    gif_id = payload.get("gif_id") or payload.get("id") or payload.get("url")
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

            existing = db.query(Favorite).filter(Favorite.user_id==uid, Favorite.gif_id==safe_gid).first()
            if existing:
                return {"ok": True, "id": existing.id}
            
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
            return {"ok": True, "id": fav.id}

        except DataError:
            db.rollback()
            safe_gid = hashlib.sha256((str(gif_id) or "").encode("utf-8")).hexdigest()
            fav = Favorite(
                user_id=uid,
                gif_id=safe_gid,
                url=(url or "")[:2048] or None,
                preview=(preview or "")[:1024] or None,
                title=(title or "")[:256] or "",
                metadata_json=(metadata or {"original_gif_id": str(gif_id)})
            )
            db.add(fav)
            db.commit()
            db.refresh(fav)
            return {"ok": True, "id": fav.id}
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

    try:
        peer = websocket.client
    except Exception:
        peer = None
    print("[ws] incoming connection from peer:", peer)

    # --- auth / user lookup ---
    token = websocket.query_params.get("token")
    print("[ws] token present:", bool(token))

    if not token:
        print("[ws] closing websocket: no token provided")
        await websocket.close(code=1008)  # policy violation / auth required
        return

    user_id = None
    try:
        user_id = decode_access_token(token)
    except Exception as e:
        print("[ws] decode_access_token raised:", repr(e))
    print("[ws] decode_access_token ->", user_id)

    if not user_id:
        print("[ws] closing websockey: token invalid/expired")
        await websocket.close(code=1008)
        return

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

    user_info = None
    try:
        user_info = await run_in_threadpool(get_user_by_id, user_id)
    except Exception as e:
        print("[ws] get_user_by_id error:", repr(e))
    print("[ws] get_user_by_id ->", user_info)

    if not user_info:
        print("[ws] closing websocket: user not found for id", user_id)
        await websocket.close(code=1008)
        return

    client_meta = {"user_id": user_info["id"], "username": user_info["username"]}
    try:
        await connectionmanager.connect(websocket, client_meta)
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
            rows = db.query(Message).order_by(Message.timestamp.asc()).limit(limit).all()
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

    history = await run_in_threadpool(load_history, 200)
    # ensure connectionmanager has the loaded history in memory (avoid later not_found)
    for m in history:
        # avoid duplicate
        if not connectionmanager.find_message(m["id"]):
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

                def save_msg():
                    db = SessionLocal()
                    try:
                        msg = Message(
                            id=m_id,
                            author_id=client_meta["user_id"],
                            author_username=client_meta["username"],
                            timestamp=timestamp,
                            content=data.get("content",""),
                            reply_to=data.get("reply_to"),
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
                        "content": data.get("content",""),
                        "reply_to": data.get("reply_to"),
                        "deleted": False,
                        "pinned": False,
                        "reactions": {}
                    }

                out_msg = await run_in_threadpool(save_msg)

                connectionmanager.store_message(out_msg)
                await connectionmanager.send_personal_message(out_msg, websocket)
                await connectionmanager.broadcast(out_msg, exclude=websocket)

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

                await connectionmanager.broadcast({"type": "delete", "id": target_id})

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
                await connectionmanager.broadcast({
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
                            return db.query(Message).filter(Message.pinned == True).count()
                        finally:
                            db.close()
                    current_pinned = await run_in_threadpool(count_pins)
                    if current_pinned >= MAX_PINNED:
                        await connectionmanager.send_personal_message({"type":"error","error":"pin_limit","limit": MAX_PINNED}, websocket)
                        continue

                # update in-memory
                if pin_flag:
                    t["pinned"] = True
                    t["pinned_by"] = client_meta["username"]
                    t["pinned_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00","Z")
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

                await run_in_threadpool(update_pin_db, target_id, t["pinned"], client_meta["user_id"] if t["pinned"] else None, t["pinned_at"])
                await connectionmanager.broadcast({"type": "pin", "id": target_id, "pinned": t["pinned"], "pinned_by": t["pinned_by"], "pinned_at": t["pinned_at"]})

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

                await connectionmanager.broadcast({"type":"react","id":target_id,"emoji":emoji_norm,"users": user_list})

            # -------------- REPLY PREVIEW --------------
            elif msg_type == "reply_preview_request":
                target_id = data.get("id")
                t = connectionmanager.find_message(target_id)
                if t:
                    await connectionmanager.send_personal_message({"type":"reply_preview","id":target_id,"content": t["content"], "deleted": t["deleted"]}, websocket)

            else:
                await connectionmanager.send_personal_message({"type":"error","error":"unknown_type","got": msg_type}, websocket)

    except WebSocketDisconnect:
        connectionmanager.disconnect(websocket)
        await connectionmanager.broadcast({"type":"notice","text": f"User {client_meta['username']} left the chat"})