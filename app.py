from typing import Dict, Any
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from fastapi import FastAPI, WebSocket, Request, WebSocketDisconnect, UploadFile, File
import json
import uuid
from datetime import datetime, timezone
from fastapi.staticfiles import StaticFiles
import pathlib
import requests
from bs4 import BeautifulSoup
import aiofiles
import os
from cachetools import TTLCache

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")





EMOJI_FILE = pathlib.Path("static/emojis.json")
EMOJI_LIST = []
EMOJI_ALIASES = {}

def load_emojis():
    global EMOJI_LIST, EMOJI_ALIASES
    if not EMOJI_FILE.exists():
        print("[emoji] static/emojis.json not found - EMOJI_LIST empty")
        EMOJI_LIST = []
        EMOJI_ALIASES = {}
        return
    data = json.loads(EMOJI_FILE.read_text(encoding="UTF-8"))
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
        # websocket -> metadata dict {"client_id": "123"}
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
        await websocket.send_text(json.dumps(payload))

    async def broadcast(self, payload: Dict[str, Any], exclude: WebSocket = None):
        text = json.dumps(payload)
        # remove broken sockets
        for conn in list(self.active_connections.keys()):
            if conn == exclude:
                continue
            try:
                await conn.send_text(text)
            except Exception as e:
                print(f"[broadcast] removing broken socket: {e}")
                self.disconnect(conn)

    def store_message(self, msg: Dict[str, Any]):
        self.messages.append(msg)
        N = 1000
        if len(self.messages) > N:
            self.messages.pop(0)

    def find_message(self, message_id: str):
        for m in self.messages:
            if m["id"] == message_id:
                return m
        return None
    
def query_giphy_search(q: str, limit: int = 20, offset: int = 0):
    if not GIPHY_KEY:
        print("[gif] Warning: GIPHY_API_KEY not set")
        return {"data": []}
    cache_key = f"giphy:search:{q}:{limit}:{offset}"
    if cache_key in gif_search_cache:
        return gif_search_cache[cache_key]
    url = "https://api.giphy.com/v1/gifs/search"
    params = {"api_key": GIPHY_KEY, "q": q, "limit": limit, "offset": offset, "rating":"pg-13", "lang":"en"}
    r = requests.get(url, params=params, timeout=5)
    result = r.json() if r.status_code == 200 else {"data": []}
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

connectionmanager = ConnectionManager()

@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    filename = f"{uuid.uuid4().hex}_{os.path.basename(file.filename)}" #sanitize
    dest = UPLOAD_DIR / filename

    #write asynchronously
    async with aiofiles.open(dest, "wb") as out_file:
        content = await file.read()          #read uploaded contents
        await out_file.write(content)        #write to disk

    #return a URL clients can use
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
         

@app.get("/", response_class=HTMLResponse)
def read_index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

# client_id = string to track names
@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    client_meta = {"client_id": str(client_id)}
    await connectionmanager.connect(websocket, client_meta)

    # send history when first connected
    await connectionmanager.send_personal_message(
        {"type": "history", "messages": connectionmanager.messages},
        websocket
    )

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except Exception:
                print("[recv] malformed JSON, ignoring:", raw)
                continue

            print("[recv]", client_meta["client_id"], data.get("type", "message"), data.get("content"))

            msg_type = data.get("type", "message")

            if msg_type == "message":
                new_msg = {
                    "type": "message",
                    "id": str(uuid.uuid4()),
                    "author": client_meta["client_id"],
                    "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "content": data.get("content", ""),
                    "reply_to": data.get("reply_to"),
                    "deleted": False,

                    "pinned": False,
                    "pinned_by": None,
                    "pinned_at": None,

                    "reactions": {} #dict: emojis -> usernames

                }

                connectionmanager.store_message(new_msg)
                await connectionmanager.send_personal_message(new_msg, websocket)
                await connectionmanager.broadcast(new_msg, exclude=websocket)

            elif msg_type == "delete":
                target_id = data.get("id")
                if not target_id:
                    continue
                target = connectionmanager.find_message(target_id)
                if not target:
                    await connectionmanager.send_personal_message({"type": "error", "error": "not_found", "id": target_id}, websocket)
                    continue
                if target["author"] != client_meta["client_id"]:
                    await connectionmanager.send_personal_message({"type": "error", "error": "not_author", "id": target_id}, websocket)
                    continue
                target["deleted"] = True
                target["content"] = ""
                await connectionmanager.broadcast({"type": "delete", "id": target_id})

            elif msg_type == "pin":
                #payload: { "type": "pin", "id": "<message_id>", "pin": true/false }
                target_id = data.get("id")
                pin_flag = data.get("pin", True)
                if not target_id:
                    continue
                target = connectionmanager.find_message(target_id)
                if not target_id:
                    continue
                target = connectionmanager.find_message(target_id)
                if not target:
                    await connectionmanager.send_personal_message({"type":"error", "error":"not_found","id":target_id}, websocket)
                    continue

                if pin_flag:
                    target["pinned"] = True
                    target["pinned_by"] = client_meta["client_id"]
                    target["pinned_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                else:
                    target["pinned"] = False
                    target["pinned_by"] = None
                    target["pinned_at"] = None

                await connectionmanager.broadcast({"type": "pin", "id": target_id, "pinned": target["pinned"], "pinned_by": target["pinned_by"], "pinned_at": target["pinned_at"]})

            elif msg_type == "react":
                # payload: { "type":"react", "id":"<message_id>", "emoji":"👍" }
                target_id = data.get("id")
                emoji_raw = data.get("emoji")
                if not target_id or not emoji_raw:
                    continue

                #normalize emoji:
                emoji_norm = None
                if isinstance(emoji_raw, str):
                    e = emoji_raw.strip()
                    if e.startswith(":") and e.endswith(":"):
                        e = e[1: -1]
                    lower = e.lower()
                    if lower in EMOJI_ALIASES:
                        emoji_norm = EMOJI_ALIASES[lower]
                    elif e in EMOJI_LIST:
                        emoji_norm = e
                    else:
                        # not recognized
                        await connectionmanager.send_personal_message({"type": "error", "error": "invalid_emoji", "emoji": emoji_raw}, websocket)
                        continue
                else:
                    await connectionmanager.send_personal_message({"type": "error", "error": "invalid_emoji_type"}, websocket)
                    continue

                target = connectionmanager.find_message(target_id)
                if not target:
                    await connectionmanager.send_personal_message({"type": "error", "error": "not_found", "id": target_id}, websocket)
                    continue

                reactions = target.setdefault("reactions", {}) #emoji -> list
                user_list = reactions.setdefault(emoji_norm, [])

                #user toggle function
                user = client_meta["client_id"]
                if user in user_list:
                    user_list.remove(user)
                else:
                    user_list.append(user)

                if not user_list:
                    reactions.pop(emoji_norm, None)

                await connectionmanager.broadcast({"type": "react", "id": target_id, "emoji": emoji_norm, "users": user_list})

            elif msg_type == "reply_preview_request":
                target_id = data.get("id")
                target = connectionmanager.find_message(target_id)
                if target:
                    await connectionmanager.send_personal_message({"type": "reply_preview", "id": target_id, "content": target["content"], "deleted": target["deleted"]}, websocket)

            else:
                await connectionmanager.send_personal_message({"type": "error", "error":"unknown_type","got": msg_type}, websocket)

    except WebSocketDisconnect:
        connectionmanager.disconnect(websocket)
        await connectionmanager.broadcast({"type": "notice", "text": f"Client #{client_id} left the chat"})