import asyncio
import aiohttp
import json
import re
import sqlite3
import time
import struct
import hashlib
import traceback
import logging
from copy import deepcopy
from html import escape
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

try:
    import brotli
    HAS_BROTLI = True
except ImportError:
    HAS_BROTLI = False

import zlib
import base64

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    Message, BotCommand,
)

# ═══════════════════════════════════════════
#  ⚙️  CONFIG
# ═══════════════════════════════════════════

BOT_TOKEN = "8225223905:AAEc_OzKG2ecjHjnAXgVJZSOme5ss3hbZLM"
OWNER_ID  = 6095762919

RATE_LIMIT_ACTIONS = 10
RATE_LIMIT_SECONDS = 60
BULKADD_TIMEOUT_SECONDS = 180

FK       = "AIzaSyAe_aOVT1gSfmHKBrorFvX4fRwN5nODXVA"
LOAD_URL = "https://europe-west1-cp-multiplayer.cloudfunctions.net/GetPlayerRecords3"
SAVE_URL = "https://europe-west1-cp-multiplayer.cloudfunctions.net/SavePlayerRecordsPartially8"
RANK_URL = "https://us-central1-cp-multiplayer.cloudfunctions.net/SetUserRating4"

MAX_MONEY = 50_000_000
MAX_COIN  = 500_000

# ═══════════════════════════════════════════
#  📊 LOGGING
# ═══════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)s │ %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("CPM")

# ═══════════════════════════════════════════
#  🗄️  STORE
# ═══════════════════════════════════════════

STORE_PATH = Path("cpm_store.json")

DEFAULT_STORE: Dict[str, Any] = {
    "allowed_users": [], "vip_users": [], "admins": {},
    "pending": {}, "banned": [], "expiry": {},
    "stats": {"total_logins": 0, "total_actions": 0, "total_unlocks": 0},
    "admin_log": [], "users": {}, "daily_stats": {},
    "notes": {}, "warnings": {},
    "maintenance": False, "broadcast_history": [],
    "bot_photo": "",
}


def load_store() -> Dict[str, Any]:
    try:
        if STORE_PATH.exists():
            with STORE_PATH.open("r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in DEFAULT_STORE.items():
                if k not in data:
                    data[k] = deepcopy(v)
            data["admins"]        = {str(k): v for k, v in data.get("admins", {}).items()}
            data["allowed_users"] = list({int(x) for x in data.get("allowed_users", [])})
            data["vip_users"]     = list({int(x) for x in data.get("vip_users", [])})
            data["banned"]        = list({int(x) for x in data.get("banned", [])})
            return data
        save_store(DEFAULT_STORE)
        return deepcopy(DEFAULT_STORE)
    except Exception:
        save_store(DEFAULT_STORE)
        return deepcopy(DEFAULT_STORE)


def save_store(data: Dict[str, Any]) -> bool:
    try:
        tmp = STORE_PATH.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        tmp.replace(STORE_PATH)
        return True
    except Exception as e:
        log.error(f"Save error: {e}")
        return False


STORE = load_store()

if OWNER_ID not in STORE["allowed_users"]:
    STORE["allowed_users"].append(OWNER_ID)
if str(OWNER_ID) not in STORE["admins"]:
    STORE["admins"][str(OWNER_ID)] = "owner"
save_store(STORE)

ALLOWED_USERS: List[int]      = list(STORE.get("allowed_users", []))
VIP_USERS:     List[int]      = list(STORE.get("vip_users", []))
ADMINS:        Dict[int, str] = {int(k): v for k, v in STORE.get("admins", {}).items()}
BANNED:        List[int]      = list(STORE.get("banned", []))
PENDING:       Dict[str, Any] = STORE.get("pending", {})
EXPIRY:        Dict[str, Any] = STORE.get("expiry", {})
RATE_DATA:     Dict[str, Any] = {}

ADMIN_LEVELS = {"owner": 100, "superadmin": 50, "admin": 10, "moderator": 5}


def is_allowed(uid):   return uid in ALLOWED_USERS
def is_banned(uid):    return uid in BANNED
def is_pending(uid):   return str(uid) in PENDING
def is_vip(uid):       return uid in VIP_USERS
def is_maintenance():  return STORE.get("maintenance", False)
def admin_level(uid):  return ADMIN_LEVELS.get(ADMINS.get(uid, ""), 0)
def admin_role(uid):   return ADMINS.get(uid, "")
def has_admin(uid, required="admin"):
    return admin_level(uid) >= ADMIN_LEVELS.get(required, 10)

def get_bot_photo():
    return STORE.get("bot_photo", "")

def set_bot_photo(file_id: str):
    STORE["bot_photo"] = file_id
    save_store(STORE)

def is_expired(uid):
    exp = EXPIRY.get(str(uid))
    if not exp: return False
    try: return datetime.now() > datetime.fromisoformat(exp)
    except: return False

def check_rate_limit(uid):
    now  = time.time()
    key  = str(uid)
    data = RATE_DATA.get(key, {"count": 0, "reset": now + RATE_LIMIT_SECONDS})
    if now > data["reset"]:
        data = {"count": 0, "reset": now + RATE_LIMIT_SECONDS}
    if data["count"] >= RATE_LIMIT_ACTIONS:
        return False, int(data["reset"] - now)
    data["count"] += 1
    RATE_DATA[key] = data
    return True, 0


def store_allow(uid, name="", save=True):
    global ALLOWED_USERS, STORE
    uid = int(uid)
    if uid in ALLOWED_USERS: return False
    ALLOWED_USERS.append(uid)
    STORE["allowed_users"] = list(ALLOWED_USERS)
    if save:
        save_store(STORE)
    return True

def store_ban(uid):
    global BANNED, ALLOWED_USERS, STORE
    uid = int(uid)
    if uid in BANNED: return False
    BANNED.append(uid)
    if uid in ALLOWED_USERS: ALLOWED_USERS.remove(uid)
    STORE["banned"] = list(BANNED)
    STORE["allowed_users"] = list(ALLOWED_USERS)
    save_store(STORE); return True

def store_unban(uid):
    global BANNED, STORE
    uid = int(uid)
    if uid not in BANNED: return False
    BANNED.remove(uid)
    STORE["banned"] = list(BANNED)
    save_store(STORE); return True

def store_remove_user(uid):
    global ALLOWED_USERS, STORE
    uid = int(uid)
    if uid not in ALLOWED_USERS: return False
    ALLOWED_USERS = [x for x in ALLOWED_USERS if x != uid]
    STORE["allowed_users"] = list(ALLOWED_USERS)
    save_store(STORE); return True

def store_add_admin(uid, role="admin"):
    global ADMINS, STORE
    uid = int(uid)
    role = role if role in ADMIN_LEVELS else "admin"
    store_allow(uid)
    ADMINS[uid] = role
    STORE["admins"] = {str(k): v for k, v in ADMINS.items()}
    save_store(STORE); return True

def store_remove_admin(uid):
    global ADMINS, STORE
    uid = int(uid)
    if uid not in ADMINS: return False
    ADMINS.pop(uid, None)
    STORE["admins"] = {str(k): v for k, v in ADMINS.items()}
    save_store(STORE); return True

def store_add_pending(uid, name, username=""):
    global PENDING, STORE
    if str(uid) in PENDING: return False
    PENDING[str(uid)] = {"name": name, "username": username, "time": datetime.now().isoformat()}
    STORE["pending"] = PENDING
    save_store(STORE); return True

def store_remove_pending(uid):
    global PENDING, STORE
    PENDING.pop(str(uid), None)
    STORE["pending"] = PENDING
    save_store(STORE)

def store_add_vip(uid):
    global VIP_USERS, STORE
    uid = int(uid)
    if uid in VIP_USERS: return False
    VIP_USERS.append(uid); store_allow(uid)
    STORE["vip_users"] = list(VIP_USERS)
    save_store(STORE); return True

def store_remove_vip(uid):
    global VIP_USERS, STORE
    uid = int(uid)
    if uid not in VIP_USERS: return False
    VIP_USERS.remove(uid)
    STORE["vip_users"] = list(VIP_USERS)
    save_store(STORE); return True

def store_set_expiry(uid, days):
    global EXPIRY, STORE
    EXPIRY[str(uid)] = (datetime.now() + timedelta(days=days)).isoformat()
    STORE["expiry"] = EXPIRY; save_store(STORE)

def store_remove_expiry(uid):
    global EXPIRY, STORE
    EXPIRY.pop(str(uid), None)
    STORE["expiry"] = EXPIRY; save_store(STORE)

def store_get_warnings(uid):
    return STORE.get("warnings", {}).get(str(uid), [])

def store_set_note(uid, note):
    STORE.setdefault("notes", {})[str(uid)] = note
    save_store(STORE)

def store_get_note(uid):
    return STORE.get("notes", {}).get(str(uid), "")

def admin_log(actor_id, action, target=""):
    STORE.setdefault("admin_log", []).insert(0, {
        "time": datetime.now().isoformat(), "actor": actor_id,
        "action": action, "target": target,
    })
    STORE["admin_log"] = STORE["admin_log"][:200]
    save_store(STORE)

def add_broadcast_history(actor, msg_type, text, sent, failed):
    bh = STORE.setdefault("broadcast_history", [])
    bh.insert(0, {"time": datetime.now().isoformat(), "actor": actor,
                  "type": msg_type, "text": text[:50], "sent": sent, "failed": failed})
    STORE["broadcast_history"] = bh[:20]; save_store(STORE)

def update_daily_stats(key="actions"):
    today = datetime.now().strftime("%Y-%m-%d")
    ds = STORE.setdefault("daily_stats", {})
    td = ds.setdefault(today, {"actions": 0, "logins": 0, "unlocks": 0})
    td[key] = td.get(key, 0) + 1; save_store(STORE)


# ═══════════════════════════════════════════
#  🔐 CRYPTO
# ═══════════════════════════════════════════

def make_xor_key(uid: str) -> bytes:
    chars = list(uid)
    if len(chars) >= 9: chars[1], chars[8] = chars[8], chars[1]
    if len(chars) >= 3: chars.pop(2)
    if len(chars) >= 5: chars.append(chars[4])
    return "".join(chars).encode("utf-8")

def xor_bytes(data: bytes, key: bytes) -> bytes:
    return bytes(data[i] ^ key[i % len(key)] for i in range(len(data)))

def decompress(data: bytes) -> Optional[bytes]:
    if HAS_BROTLI:
        try: return brotli.decompress(data)
        except: pass
    try: return zlib.decompress(data, zlib.MAX_WBITS | 16)
    except: pass
    try: return zlib.decompress(data)
    except: pass
    return None

def decrypt_aes(data: bytes, key: bytes) -> Optional[bytes]:
    if not HAS_CRYPTO: return None
    try:
        cipher = AES.new(key[:16], AES.MODE_CBC, b"\x00" * 16)
        return unpad(cipher.decrypt(data), 16)
    except: return None

def _md5(t): return hashlib.md5(t.encode()).digest()
def _sha1(t): return hashlib.sha1(t.encode()).digest()[:16]

def build_aes_keys(uid, password=None, email=None):
    keys = [_md5("olzhas_carparking")]
    if password: keys += [_md5(password), _sha1(password)]
    if uid:      keys += [_md5(uid), _sha1(uid)]
    if email:    keys.append(_md5(email))
    return keys


class Reader:
    def __init__(self, data):
        self.buf = data; self.pos = 0

    def has_bytes(self, n): return self.pos + n <= len(self.buf)

    def read_byte(self):
        if not self.has_bytes(1): return 0
        v = self.buf[self.pos]; self.pos += 1; return v

    def read_int(self):
        if not self.has_bytes(4): self.pos = len(self.buf); return 0
        v = struct.unpack_from("<i", self.buf, self.pos)[0]; self.pos += 4; return v

    def read_float(self):
        if not self.has_bytes(4): self.pos = len(self.buf); return 0.0
        v = struct.unpack_from("<f", self.buf, self.pos)[0]; self.pos += 4; return v

    def read_string(self):
        marker = self.read_int()
        if marker in (0, -1): return ""
        length = (-marker) - 1 if marker < -1 else marker
        if marker < -1: self.read_int()
        if length > 1_000_000: length = 1_000_000
        if not self.has_bytes(length): return ""
        text = self.buf[self.pos:self.pos + length].decode("utf-8", errors="replace")
        self.pos += length
        return text.replace("\x00", "").strip()

    def read_list(self, item_fn):
        count = self.read_int()
        if count <= 0 or count > 1_000_000: return []
        result = []
        for _ in range(count):
            if self.pos >= len(self.buf): break
            v = item_fn()
            if v is not None: result.append(v)
        return result

    def read_dict(self):
        count = self.read_int()
        if count <= 0 or count > 1_000_000: return {}
        d = {}
        for _ in range(count):
            if self.pos >= len(self.buf): break
            d[self.read_int()] = self.read_int()
        return d

    def read_equipment(self):
        if self.read_byte() == 0: return None
        return {
            "hair": self.read_list(self.read_int),
            "face": self.read_list(self.read_int),
            "beard": self.read_list(self.read_int),
            "cap": self.read_list(self.read_int),
            "mask": self.read_list(self.read_int),
            "top": self.read_list(self.read_int),
            "gloves": self.read_list(self.read_int),
            "bag": self.read_list(self.read_int),
            "pants": self.read_list(self.read_int),
            "shoes": self.read_list(self.read_int),
            "glasses": self.read_list(self.read_int),
            "SelectedEquipments": self.read_list(self.read_int),
            "Gender": self.read_int(),
        }


def parse_player(buf):
    r = Reader(buf)
    if r.read_byte() == 0: return None
    p = {}
    p["Name"] = r.read_string(); p["money"] = r.read_int()
    p["coin"] = r.read_int(); p["localID"] = r.read_string()
    p["boughtFsos"] = r.read_list(r.read_int)

    def read_friend():
        r.read_byte()
        return {"id": r.read_string(), "Name": r.read_string(), "accountID": r.read_string()}

    p["FriendsID"] = r.read_list(read_friend)
    p["LevelsDoneTime"] = r.read_list(r.read_float)
    p["floats"] = r.read_list(r.read_float)
    p["integers"] = r.read_list(r.read_int)
    p["fcar"] = r.read_list(r.read_int)
    p["favouriteWheels"] = r.read_list(r.read_int)
    p["favouriteVinyls"] = r.read_list(r.read_int)
    p["favouriteEmojis"] = r.read_list(r.read_int)
    p["personEquipmentsMale"] = r.read_equipment()
    p["personEquipmentsFemale"] = r.read_equipment()

    if r.read_byte() == 0:
        p["platesData"] = None
    else:
        def read_vinyl():
            r.read_byte()
            def rv(): return {"x": r.read_float(), "y": r.read_float(), "z": r.read_float()}
            return {"vectors": r.read_list(rv), "v": r.read_list(r.read_string),
                    "floats": r.read_list(r.read_float), "text": r.read_string()}
        def read_plate():
            r.read_byte()
            return {"plateId": r.read_int(), "frontCarId": r.read_int(),
                    "rearCarId": r.read_int(), "vinyls": r.read_list(read_vinyl)}
        p["platesData"] = {"allPlates": r.read_list(read_plate)}

    if r.read_byte() == 0:
        p["carIDnStatus"] = None
    else:
        p["carIDnStatus"] = {
            "carGeneratedIDs": r.read_list(r.read_string),
            "carStatus": r.read_list(r.read_int),
        }

    p["allData"] = r.read_string()
    p["flags"] = r.read_dict()
    p["animations"] = r.read_list(r.read_int)
    p["emojiPacks"] = r.read_list(r.read_int)
    p["wheels"] = r.read_list(r.read_int)
    p["boughtPoliceLights"] = r.read_list(r.read_int)
    p["boughtPoliceSirens"] = r.read_list(r.read_int)
    return p


def try_parse(buf):
    candidates = [buf]
    d1 = decompress(buf)
    if d1:
        candidates.append(d1)
        d2 = decompress(d1)
        if d2: candidates.append(d2)
    for c in candidates:
        if not c: continue
        if len(c) > 0 and c[0] in (17, 23, 24):
            try:
                p = parse_player(c)
                if p and p.get("Name") is not None: return p
            except: pass
        try:
            clean = c[3:] if (len(c) >= 3 and c[0] == 0xef and c[1] == 0xbb) else c
            if clean[0] == 123: return json.loads(clean.decode("utf-8"))
        except: pass
    return None


def decrypt_player_record(base64_text, uid, password=None, email=None):
    try: buf = base64.b64decode(base64_text)
    except: return {"success": False, "message": "Bad base64"}
    if len(buf) < 10: return {"success": False, "message": "Too small"}

    direct = try_parse(buf)
    if direct: return {"success": True, "record": direct}

    if uid:
        try:
            xp = xor_bytes(buf, make_xor_key(uid))
            d  = decompress(xp)
            if d:
                p = try_parse(d)
                if p: return {"success": True, "record": p}
        except: pass

    for key in build_aes_keys(uid or "", password, email):
        plain = decrypt_aes(buf, key)
        if not plain: continue
        p = try_parse(plain)
        if p: return {"success": True, "record": p}

    return {"success": False, "message": "Could not decrypt"}


# ── Writer ────────────────────────────────

class Writer:
    def __init__(self): self._p: List[bytes] = []
    def write_byte(self, v): self._p.append(bytes([v & 0xFF]))
    def write_int(self, v):  self._p.append(struct.pack("<i", int(v or 0)))
    def write_float(self, v): self._p.append(struct.pack("<f", float(v or 0.0)))

    def write_string(self, s):
        if s is None: self._p.append(struct.pack("<i", -1)); return
        s = str(s)
        if s == "": self._p.append(struct.pack("<i", 0)); return
        enc = s.encode("utf-8")
        self._p.append(struct.pack("<ii", -(len(enc)) - 1, len(s)) + enc)

    def write_list(self, lst, fn):
        if lst is None: self._p.append(struct.pack("<i", -1)); return
        self._p.append(struct.pack("<i", len(lst)))
        for item in lst: fn(item)

    def write_equipment(self, data):
        if not data: self.write_byte(0); return
        self.write_byte(13)
        for k in ["hair","face","beard","cap","mask","top","gloves","bag","pants","shoes","glasses","SelectedEquipments"]:
            self.write_list(data.get(k, []), self.write_int)
        self.write_int(data.get("Gender", 0))

    def write_plates(self, data):
        if not data: self.write_byte(0); return
        self.write_byte(1)
        plates = data.get("allPlates", [])
        self._p.append(struct.pack("<i", len(plates)))
        for plate in plates:
            self.write_byte(4)
            self.write_int(plate.get("plateId", 0))
            self.write_int(plate.get("frontCarId", 0))
            self.write_int(plate.get("rearCarId", 0))
            vinyls = plate.get("vinyls", [])
            self._p.append(struct.pack("<i", len(vinyls)))
            for vinyl in vinyls:
                self.write_byte(4)
                vecs = vinyl.get("vectors", [])
                self._p.append(struct.pack("<i", len(vecs)))
                for vec in vecs:
                    self._p.append(struct.pack("<fff", vec.get("x",0), vec.get("y",0), vec.get("z",0)))
                self.write_list(vinyl.get("v", []), self.write_string)
                self.write_list(vinyl.get("floats", []), self.write_float)
                self.write_string(vinyl.get("text", ""))

    def write_car_id_status(self, data):
        if not data: self.write_byte(0); return
        self.write_byte(2)
        self.write_list(data.get("carGeneratedIDs", []), self.write_string)
        self.write_list(data.get("carStatus", []), self.write_int)

    def to_bytes(self): return b"".join(self._p)


FIELD_MAPPING = [
    (1,"localID"),(2,"money"),(3,"Name"),(4,"coin"),(5,"allData"),
    (6,"boughtFsos"),(7,"boughtPoliceLights"),(8,"boughtPoliceSirens"),
    (9,"FriendsID"),(10,"LevelsDoneTime"),(11,"floats"),(12,"integers"),
    (13,"fcar"),(14,"favouriteWheels"),(15,"favouriteVinyls"),
    (16,"favouriteEmojis"),(18,"emojiPacks"),
    (41,"personEquipmentsMale"),(42,"personEquipmentsFemale"),
    (43,"platesData"),(44,"carIDnStatus"),(45,"flags"),
    (46,"animations"),(48,"wheels"),
]

INT_LIST_FIELDS   = {6,7,8,12,13,14,15,16,18,46,48}
FLOAT_LIST_FIELDS = {10,11}
ALWAYS_SEND       = {"allData"}


def _field_modified(nv, ov):
    if nv is None and ov is None: return False
    if nv is None or ov is None: return True
    if type(nv) != type(ov): return True
    if isinstance(nv, (dict,list)):
        return json.dumps(nv,sort_keys=True) != json.dumps(ov,sort_keys=True)
    return nv != ov


def serialize_field(fid, value):
    w = Writer()
    if fid in (1,3,5): w.write_string(value); return w.to_bytes()
    if fid in (2,4): w.write_int(value or 0); return w.to_bytes()
    if fid == 9:
        friends = value or []
        w._p.append(struct.pack("<i", len(friends)))
        for f in friends:
            w.write_byte(3)
            w.write_string((f or {}).get("id",""))
            w.write_string((f or {}).get("Name",""))
            w.write_string((f or {}).get("accountID",""))
        return w.to_bytes()
    if fid in INT_LIST_FIELDS: w.write_list(value or [], w.write_int); return w.to_bytes()
    if fid in FLOAT_LIST_FIELDS: w.write_list(value or [], w.write_float); return w.to_bytes()
    if fid in (41,42): w.write_equipment(value); return w.to_bytes()
    if fid == 43: w.write_plates(value); return w.to_bytes()
    if fid == 44: w.write_car_id_status(value); return w.to_bytes()
    if fid == 45:
        flags = value or {}
        w._p.append(struct.pack("<i", len(flags)))
        for k, v in flags.items():
            w.write_int(int(k)); w.write_int(int(v))
        return w.to_bytes()
    return None


def build_payload(record, uid, original=None):
    fields = []
    for fid, key in FIELD_MAPPING:
        value = record.get(key)
        if value is None: continue
        if key in ALWAYS_SEND:
            should = isinstance(value, str) and len(value) > 0
        elif original is not None:
            should = _field_modified(value, original.get(key))
        else:
            should = True
        if not should: continue
        raw = serialize_field(fid, value)
        if raw is not None: fields.append((fid, raw))

    parts = [struct.pack("<i", len(fields))]
    for fid, raw in fields:
        parts.append(struct.pack("<hi", fid, len(raw)))
        parts.append(raw)
    combined   = b"".join(parts)
    compressed = brotli.compress(combined) if HAS_BROTLI else zlib.compress(combined)
    encrypted  = xor_bytes(compressed, make_xor_key(uid))
    return base64.b64encode(encrypted).decode("ascii")


# ═══════════════════════════════════════════
#  🎮 CPM NUKER
# ═══════════════════════════════════════════

GAME_HEADERS = {
    "Accept": "*/*", "Accept-Encoding": "gzip",
    "Content-Type": "application/json",
    "User-Agent": "UnityPlayer/2022.3.62f2 (UnityWebRequest/1.0, libcurl/8.10.1-DEV)",
    "X-Unity-Version": "2022.3.62f2",
}


class CPMNuker:
    def __init__(self):
        self.db_path = "cpm_tokens.db"
        self.cache: Dict[str, Dict] = {}
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as c:
            c.execute("""CREATE TABLE IF NOT EXISTS tokens (
                user_id INTEGER PRIMARY KEY, auth_token TEXT, email TEXT,
                password TEXT, refresh_token TEXT, firebase_uid TEXT,
                token_expires_at REAL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS user_data (
                cache_key TEXT PRIMARY KEY, email TEXT, data_json TEXT)""")
            try: c.execute("ALTER TABLE tokens ADD COLUMN firebase_uid TEXT")
            except: pass
            c.commit()

    def _ck(self, uid, email=None):
        if email: return f"{uid}_{email}"
        td = self.get_token_data(uid)
        return f"{uid}_{td['email']}" if td and td.get("email") else str(uid)

    def save_token(self, uid, auth, email, pw=None, rt=None, fuid=None):
        with sqlite3.connect(self.db_path) as c:
            c.execute("""INSERT OR REPLACE INTO tokens
                (user_id,auth_token,email,password,refresh_token,firebase_uid,token_expires_at)
                VALUES (?,?,?,?,?,?,?)""",
                (uid, auth, email, pw, rt, fuid, time.time()+3600))
            c.commit()

    def get_token_data(self, uid):
        with sqlite3.connect(self.db_path) as c:
            row = c.execute("""SELECT auth_token,email,password,refresh_token,
                firebase_uid,token_expires_at FROM tokens WHERE user_id=?""", (uid,)).fetchone()
        if row:
            return {"auth_token":row[0],"email":row[1],"password":row[2],
                    "refresh_token":row[3],"firebase_uid":row[4],"token_expires_at":row[5]}
        return None

    def get_token(self, uid):
        td = self.get_token_data(uid)
        return {"auth_token":td["auth_token"],"email":td["email"]} if td else None

    def update_token(self, uid, auth, rt=None):
        exp = time.time()+3600
        with sqlite3.connect(self.db_path) as c:
            if rt: c.execute("UPDATE tokens SET auth_token=?,refresh_token=?,token_expires_at=? WHERE user_id=?",(auth,rt,exp,uid))
            else:  c.execute("UPDATE tokens SET auth_token=?,token_expires_at=? WHERE user_id=?",(auth,exp,uid))
            c.commit()

    def delete_token(self, uid):
        with sqlite3.connect(self.db_path) as c:
            c.execute("DELETE FROM tokens WHERE user_id=?",(uid,)); c.commit()
        for k in [k for k in self.cache if k.startswith(str(uid))]:
            del self.cache[k]

    def is_expired(self, uid):
        td = self.get_token_data(uid)
        return not td or not td.get("token_expires_at") or td["token_expires_at"] < time.time()

    def get_record(self, uid, email=None):
        ck = self._ck(uid, email)
        if ck not in self.cache:
            with sqlite3.connect(self.db_path) as c:
                row = c.execute("SELECT data_json FROM user_data WHERE cache_key=?",(ck,)).fetchone()
            if row:
                try: self.cache[ck] = json.loads(row[0])
                except: pass
        return self.cache.get(ck, {})

    def set_record(self, uid, data, email=None):
        ck = self._ck(uid, email)
        self.cache[ck] = data
        with sqlite3.connect(self.db_path) as c:
            c.execute("INSERT OR REPLACE INTO user_data (cache_key,email,data_json) VALUES (?,?,?)",
                      (ck, email, json.dumps(data))); c.commit()

    async def _post(self, url, payload, headers):
        try:
            h = {k:v for k,v in headers.items() if k.lower() != "host"}
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout, connector=aiohttp.TCPConnector(ssl=False)) as s:
                async with s.post(url, json=payload, headers=h) as r:
                    text = await r.text()
                    try: return json.loads(text)
                    except: return {"raw": text, "status": r.status}
        except Exception as e:
            log.error(f"HTTP: {e}"); return None

    async def login(self, email, password):
        url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={FK}"
        h = {"Accept":"*/*","Accept-Encoding":"gzip","Content-Type":"application/json",
             "User-Agent":"UnityPlayer/2022.3.62f2 (UnityWebRequest/1.0, libcurl/8.10.1-DEV)",
             "X-Unity-Version":"2022.3.62f2"}
        p = {"email":email,"password":password,"returnSecureToken":True,"clientType":"CLIENT_TYPE_ANDROID"}
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout, connector=aiohttp.TCPConnector(ssl=False)) as s:
                async with s.post(url, json=p, headers=h) as resp:
                    text = await resp.text()
                    log.info(f"Login [{resp.status}] {email}: {text[:200]}")
                    try: r = json.loads(text)
                    except: return {"ok":False,"message":"NETWORK_ERROR"}
        except Exception as e:
            log.error(f"Login: {e}"); return {"ok":False,"message":"NETWORK_ERROR"}

        if "idToken" in r:
            return {"ok":True,"auth":r["idToken"],"refresh_token":r.get("refreshToken",""),"firebase_uid":r.get("localId","")}
        err = str(r.get("error",{}).get("message","")).upper()
        for k in ["EMAIL_NOT_FOUND","INVALID_PASSWORD","INVALID_LOGIN_CREDENTIALS","TOO_MANY_ATTEMPTS","USER_DISABLED","INVALID_EMAIL"]:
            if k in err: return {"ok":False,"message":k}
        return {"ok":False,"message":f"LOGIN_FAILED: {err[:60]}"}

    async def _refresh(self, uid):
        td = self.get_token_data(uid)
        if not td: return False,"NO_TOKEN"
        rt,em,pw = td.get("refresh_token"),td.get("email"),td.get("password")
        if rt:
            try:
                timeout = aiohttp.ClientTimeout(total=30)
                async with aiohttp.ClientSession(timeout=timeout, connector=aiohttp.TCPConnector(ssl=False)) as s:
                    async with s.post(f"https://securetoken.googleapis.com/v1/token?key={FK}",
                        json={"grant_type":"refresh_token","refresh_token":rt},
                        headers={"Content-Type":"application/json"}) as resp:
                        r = await resp.json(content_type=None)
                        if r and r.get("id_token"):
                            self.update_token(uid,r["id_token"],r.get("refresh_token",rt))
                            return True,"OK"
            except: pass
        if em and pw:
            res = await self.login(em,pw)
            if res.get("ok"):
                self.save_token(uid,res["auth"],em,pw,res.get("refresh_token",""),res.get("firebase_uid",""))
                return True,"OK"
        return False,"REFRESH_FAILED"

    async def get_auth(self, uid):
        if self.is_expired(uid):
            ok,msg = await self._refresh(uid)
            if not ok: return False,msg,""
        td = self.get_token_data(uid)
        if td and td.get("auth_token"): return True,"OK",td["auth_token"]
        return False,"NO_TOKEN",""

    async def load(self, uid, force=False):
        td = self.get_token_data(uid)
        if not td: return False
        ck = self._ck(uid)
        if not force and ck in self.cache: return True
        ok,msg,auth = await self.get_auth(uid)
        if not ok: return False
        try:
            r = await self._post(LOAD_URL,{"data":None},{**GAME_HEADERS,"Authorization":f"Bearer {auth}"})
            if not r or not r.get("result"): return False
            dec = decrypt_player_record(r["result"],td.get("firebase_uid",""),td.get("password",""),td.get("email",""))
            if dec.get("success") and dec.get("record"):
                self.set_record(uid,dec["record"],td.get("email",""))
                log.info(f"✅ Loaded {uid}: {dec['record'].get('Name')} ${dec['record'].get('money')}")
                return True
            return False
        except Exception as e:
            log.error(f"Load error: {e}"); return False

    def _ok(self, v):
        if v in (1,True): return True
        if v in (0,False): return False
        if isinstance(v,str):
            t=v.strip()
            if t=="1": return True
            if t=="0": return False
            try: return self._ok(json.loads(t))
            except: return False
        if isinstance(v,dict):
            for k in ("result","ok","success"):
                if k in v: return self._ok(v[k])
        return False

    async def _send(self, auth, record, fuid, original=None):
        if not fuid: return False,"NO_UID"
        try:
            payload = build_payload(record, fuid, original)
            r = await self._post(SAVE_URL,
                {"data":{"data":payload,"deviceId":fuid[:8]}},
                {**GAME_HEADERS,"Authorization":f"Bearer {auth}","Connection":"Keep-Alive",
                 "User-Agent":"Dalvik/2.1.0 (Linux; U; Android 12; Pixel 6 Build/SD1A.210817.036)"})
            if r and self._ok(r): return True,"OK"
            return False,f"SAVE_FAILED: {str(r)[:100]}"
        except Exception as e: return False,str(e)

    async def _save(self, uid, data):
        ok,msg,auth = await self.get_auth(uid)
        if not ok: return {"ok":False,"message":msg}
        td    = self.get_token_data(uid)
        fuid  = td.get("firebase_uid","") if td else ""
        email = td.get("email","") if td else ""
        orig  = self.get_record(uid,email) or None
        ok2,msg2 = await self._send(auth,data,fuid,orig)
        if ok2:
            self.set_record(uid,data,email)
            STORE["stats"]["total_actions"] = STORE["stats"].get("total_actions",0)+1
            save_store(STORE); update_daily_stats("actions")
            return {"ok":True}
        return {"ok":False,"message":msg2}

    async def _modify(self, uid, mods):
        await self.load(uid)
        td    = self.get_token_data(uid)
        email = td.get("email") if td else None
        d     = deepcopy(self.get_record(uid,email))
        if not d or not d.get("Name"):
            return {"ok":False,"message":"Could not load account data. Try Refresh first."}
        for k,v in mods.items():
            if k=="money": v=min(v,MAX_MONEY)
            if k=="coin":  v=min(v,MAX_COIN)
            d[k]=v
        return await self._save(uid,d)

    async def _set_floats(self, uid, indices_values):
        await self.load(uid)
        td    = self.get_token_data(uid)
        email = td.get("email") if td else None
        d     = deepcopy(self.get_record(uid,email))
        if not d or not d.get("Name"):
            return {"ok":False,"message":"Could not load account data. Try Refresh first."}
        fl = d.get("floats",[])
        max_idx = max(idx for idx,_ in indices_values)
        while len(fl) <= max_idx: fl.append(0.0)
        for idx,val in indices_values: fl[idx]=float(val)
        d["floats"]=fl
        return await self._save(uid,d)

    async def _set_integers(self, uid, indices_values):
        await self.load(uid)
        td    = self.get_token_data(uid)
        email = td.get("email") if td else None
        d     = deepcopy(self.get_record(uid,email))
        if not d or not d.get("Name"):
            return {"ok":False,"message":"Could not load account data. Try Refresh first."}
        it = d.get("integers",[])
        max_idx = max(idx for idx,_ in indices_values)
        while len(it) <= max_idx: it.append(0)
        for idx,val in indices_values: it[idx]=int(val)
        d["integers"]=it
        return await self._save(uid,d)

    # ── Game operations ───────────────────
    async def set_money(self, uid, amount):
        return await self._modify(uid, {"money": min(amount, MAX_MONEY)})

    async def set_coin(self, uid, amount):
        return await self._modify(uid, {"coin": min(amount, MAX_COIN)})

    async def set_player_name(self, uid, name):
        return await self._modify(uid, {"Name": name})

    async def set_player_id(self, uid, pid):
        return await self._modify(uid, {"localID": pid.upper()})

    async def set_race_wins(self, uid, amount):
        return await self._set_floats(uid, [(8, float(amount))])

    async def set_race_loses(self, uid, amount):
        return await self._set_floats(uid, [(9, float(amount))])

    async def unlock_w16(self, uid):
        return await self._set_floats(uid, [(32, 1.0)])

    async def unlock_horns(self, uid):
        return await self._set_floats(uid, [(27,1.0),(28,1.0),(29,1.0),(30,1.0),(31,1.0)])

    async def disable_damage(self, uid):
        return await self._set_floats(uid, [(34, 1.0)])

    async def unlimited_fuel(self, uid):
        return await self._set_floats(uid, [(3, 1.0)])

    async def unlock_smoke(self, uid):
        return await self._set_floats(uid, [(33, 1.0)])

    async def unlock_animations(self, uid):
        await self.load(uid)
        td    = self.get_token_data(uid)
        email = td.get("email") if td else None
        d     = deepcopy(self.get_record(uid,email))
        if not d or not d.get("Name"):
            return {"ok":False,"message":"Could not load account data."}
        d["animations"] = list(set(d.get("animations",[]) + list(range(301))))
        return await self._save(uid,d)

    async def unlock_wheels(self, uid):
        await self.load(uid)
        td    = self.get_token_data(uid)
        email = td.get("email") if td else None
        d     = deepcopy(self.get_record(uid,email))
        if not d or not d.get("Name"):
            return {"ok":False,"message":"Could not load account data."}
        d["wheels"] = list(set(d.get("wheels",[]) + list(range(73,221))))
        it = d.get("integers",[])
        while len(it) < 113: it.append(0)
        for i in [0,1,2,3,4,5,110,111,112]: it[i]=1
        d["integers"]=it
        return await self._save(uid,d)

    async def unlock_houses(self, uid):
        return await self._set_integers(uid, [(8,1),(110,1),(111,1),(112,1)])

    async def complete_all_levels(self, uid):
        lvl = [0] + [120 if i==43 else 1 for i in range(1,110)]
        return await self._modify(uid, {"LevelsDoneTime": lvl})

    async def set_rank(self, uid):
        await self.load(uid)
        ok,msg,auth = await self.get_auth(uid)
        if not ok: return {"ok":False,"message":msg}
        rd = {"RatingData":{"time":1e22,"cars":1e16,"car_fix":1e13,"car_collided":1e12,
            "car_exchange":1e13,"car_trade":1e13,"car_wash":1e13,"slicer_cut":1e13,
            "drift_max":1e14,"drift":1e14,"cargo":1e5,"delivery":1e5,"race_win":3e20,
            "taxi":1e10,"levels":10000990000,"gifts":1e9,"fuel":1e10,"offroad":1e10,
            "speed_banner":1e9,"reactions":1e17,"run":1e9,"real_estate":1e9,
            "t_distance":1e10,"treasure":1e10,"block_post":1e10,"push_ups":1e12,
            "burnt_tire":1e10,"passanger_distance":1e8}}
        r = await self._post(RANK_URL,{"data":json.dumps(rd)},{**GAME_HEADERS,"Authorization":f"Bearer {auth}"})
        if r and self._ok(r):
            STORE["stats"]["total_unlocks"]=STORE["stats"].get("total_unlocks",0)+1
            save_store(STORE); return {"ok":True}
        return {"ok":False,"message":"RANK_FAILED"}

    async def fix_account(self, uid):
        await self.load(uid)
        td    = self.get_token_data(uid)
        email = td.get("email") if td else None
        d     = deepcopy(self.get_record(uid,email))
        if not d or not d.get("Name"):
            return {"ok":False,"message":"Could not load account data."}
        bugs=0
        fl = (d.get("floats",[]))[:54]
        while len(fl)<54: fl.append(0.0)
        fixed_fl=[]
        for v in fl:
            if v in (1,1.0): fixed_fl.append(1.0)
            elif isinstance(v,(int,float)) and v>1: bugs+=1; fixed_fl.append(0.0)
            else: fixed_fl.append(float(v) if v else 0.0)
        it = (d.get("integers",[]))[:120]
        while len(it)<120: it.append(0)
        fixed_it=[]
        for v in it:
            if v==1: fixed_it.append(1)
            elif isinstance(v,(int,float)) and v>1: bugs+=1; fixed_it.append(0)
            else: fixed_it.append(int(v) if v else 0)
        d["floats"]=fixed_fl; d["integers"]=fixed_it
        result = await self._save(uid,d)
        return {"ok":True,"bugs_fixed":bugs} if result.get("ok") else {"ok":False,"message":"FIX_FAILED"}


nuker = CPMNuker()


# ═══════════════════════════════════════════
#  🎨 UI
# ═══════════════════════════════════════════

B = "┅┅┅┅┅┅┅┅┅┅┅┅┅┅┅┅┅┅┅┅┅┅┅┅┅"

def hdr(icon, title): return f"{B}\n  {icon}  {title}\n{B}"

def fmt(n): return f"{int(n):,}"


class T:
    @staticmethod
    def welcome(name, username, uid):
        now = datetime.now()
        return (
            f"{B}\n🔥 AWIMEDANCPM TOOLS 🔥\n{B}\n\n"
            f"  ╭──── 𝗪𝗘𝗟𝗖𝗢𝗠𝗘 ────╮\n"
            f"  │ 👤 {name}\n"
            f"  │ 📱 @{username or 'N/A'}\n"
            f"  │ 🆔 <code>{uid}</code>\n"
            f"  │ 📅 {now.strftime('%d %b %Y • %I:%M %p')}\n"
            f"  ╰─────────────────╯\n\n"
            f"  ▸ Sign in with your CPM credentials"
        )

    @staticmethod
    def no_access():
        return (
            f"{B}\n  🔒  𝗔𝗖𝗖𝗘𝗦𝗦 𝗥𝗘𝗤𝗨𝗜𝗥𝗘𝗗\n{B}\n\n"
            "  You don't have access yet.\n"
            "  Tap below to request access."
        )

    @staticmethod
    def banned():
        return f"{B}\n  🚫  𝗕𝗔𝗡𝗡𝗘𝗗\n{B}\n\n  Your access has been revoked."

    @staticmethod
    def maintenance():
        return f"{B}\n  🔧  𝗠𝗔𝗜𝗡𝗧𝗘𝗡𝗔𝗡𝗖𝗘\n{B}\n\n  Bot under maintenance. Try later."

    @staticmethod
    def login_fail(reason):
        err_map = {
            "EMAIL_NOT_FOUND":           "Email not found",
            "INVALID_PASSWORD":          "Wrong password",
            "INVALID_LOGIN_CREDENTIALS": "Invalid credentials",
            "TOO_MANY_ATTEMPTS":         "Too many attempts, wait",
            "USER_DISABLED":             "Account disabled",
            "INVALID_EMAIL":             "Invalid email format",
            "NETWORK_ERROR":             "Network error, try again",
        }
        clean   = reason.replace("LOGIN_FAILED: ","") if "LOGIN_FAILED:" in reason else reason
        display = err_map.get(reason, clean)
        return f"{B}\n  ❌  𝗟𝗢𝗚𝗜𝗡 𝗙𝗔𝗜𝗟𝗘𝗗\n{B}\n\n  ✗ {display}\n\n  Tap Sign In to retry."

    @staticmethod
    def dashboard(record, email, uid):
        name   = record.get("Name","Unknown")
        pid    = record.get("localID","—")
        money  = record.get("money",0)
        coin   = record.get("coin",0)
        floats = record.get("floats",[])
        wheels = record.get("wheels",[])
        anims  = record.get("animations",[])
        wins   = int(floats[8]) if len(floats)>8 else 0
        loses  = int(floats[9]) if len(floats)>9 else 0
        levels = record.get("LevelsDoneTime",[])
        done   = sum(1 for x in levels if x and x>0) if levels else 0
        friends= len(record.get("FriendsID",[]))
        badge  = " 💎" if is_vip(uid) else ""
        exp_txt= ""
        if str(uid) in EXPIRY:
            try:
                days = (datetime.fromisoformat(EXPIRY[str(uid)]) - datetime.now()).days
                exp_txt = f"\n  ⏰ Expires in {days} days"
            except: pass
        return (
            f"{B}\n  🏠  𝗗𝗔𝗦𝗛𝗕𝗢𝗔𝗥𝗗{badge}\n{B}\n\n"
            f"  ╭──── 𝗔𝗖𝗖𝗢𝗨𝗡𝗧 ────╮\n"
            f"  │ 📧 {email}\n"
            f"  │ 👤 {name}\n"
            f"  │ 🆔 {pid}\n"
            f"  ╰─────────────────╯\n\n"
            f"  ╭──── 𝗦𝗧𝗔𝗧𝗦 ──────╮\n"
            f"  │ 💰 ${money:,}\n"
            f"  │ 🪙 {coin:,} coins\n"
            f"  │ 🏆 {wins:,}W / {loses:,}L\n"
            f"  │ 🎮 {done} levels done\n"
            f"  │ 🛞 {len(wheels)} wheels\n"
            f"  │ 🎭 {len(anims)} animations\n"
            f"  │ 👥 {friends} friends\n"
            f"  ╰─────────────────╯{exp_txt}\n\n"
            f"  ▸ Select an option below:"
        )

    @staticmethod
    def admin_panel(uid):
        role  = admin_role(uid)
        badge = {"owner":"👑 Owner","superadmin":"⭐ Super Admin",
                 "admin":"🛡 Admin","moderator":"👮 Moderator"}.get(role,"❓")
        maint = "🔴 ON" if is_maintenance() else "🟢 OFF"
        return (
            f"{B}\n  👑  𝗔𝗗𝗠𝗜𝗡 𝗣𝗔𝗡𝗘𝗟\n{B}\n\n"
            f"  ◆ Role:        {badge}\n"
            f"  ◆ Maintenance: {maint}\n\n"
            f"  👥 Users    {len(ALLOWED_USERS)}\n"
            f"  ⏳ Pending  {len(PENDING)}\n"
            f"  🚫 Banned   {len(BANNED)}\n"
            f"  💎 VIP      {len(VIP_USERS)}\n"
            f"  🛡 Admins   {len(ADMINS)}"
        )

    @staticmethod
    def stats():
        s  = STORE.get("stats",{})
        ds = STORE.get("daily_stats",{})
        td = ds.get(datetime.now().strftime("%Y-%m-%d"),{})
        return (
            f"{B}\n  📊  𝗦𝗧𝗔𝗧𝗜𝗦𝗧𝗜𝗖𝗦\n{B}\n\n"
            f"  👥 Users     {len(ALLOWED_USERS)}\n"
            f"  💎 VIP       {len(VIP_USERS)}\n"
            f"  ⏳ Pending   {len(PENDING)}\n"
            f"  🚫 Banned    {len(BANNED)}\n\n"
            f"  ⚡ Actions   {s.get('total_actions',0)}\n"
            f"  🔓 Unlocks   {s.get('total_unlocks',0)}\n"
            f"  🔐 Logins    {s.get('total_logins',0)}\n\n"
            f"  📅 Today     {td.get('actions',0)} actions"
        )

    @staticmethod
    def request_to_admin(name, username, uid):
        return (
            f"{B}\n  🔔  𝗡𝗘𝗪 𝗥𝗘𝗤𝗨𝗘𝗦𝗧\n{B}\n\n"
            f"  👤 Name:     {name}\n"
            f"  📱 Username: @{username or 'N/A'}\n"
            f"  🆔 ID:       <code>{uid}</code>\n"
            f"  📅 {datetime.now().strftime('%d %b %Y • %I:%M %p')}\n\n"
            f"  ▸ Select action:"
        )


# ═══════════════════════════════════════════
#  ⌨️  KEYBOARDS
# ═══════════════════════════════════════════

class K:
    @staticmethod
    def no_access():
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔑 Request Access", callback_data="send_request")],
            [InlineKeyboardButton(text="💬 Contact Admin",  callback_data="msg_admin")],
        ])

    @staticmethod
    def after_request():
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Check Status",  callback_data="check_status")],
            [InlineKeyboardButton(text="💬 Contact Admin", callback_data="msg_admin")],
        ])

    @staticmethod
    def login():
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔐 Sign In", callback_data="login")],
        ])

    @staticmethod
    def cancel():
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✗ Cancel", callback_data="cancel")],
        ])

    @staticmethod
    def home(uid=0):
        rows = [
            [InlineKeyboardButton(text="💰 Money",   callback_data="menu_money"),
             InlineKeyboardButton(text="🪙 Coins",   callback_data="menu_coins")],
            [InlineKeyboardButton(text="⚡ Features",callback_data="menu_feat"),
             InlineKeyboardButton(text="🔧 Settings",callback_data="menu_set")],
            [InlineKeyboardButton(text="🔄 Refresh Account", callback_data="refresh")],
        ]
        if has_admin(uid,"moderator"):
            rows.append([InlineKeyboardButton(text="👑 Admin Panel", callback_data="admin_menu")])
        rows.append([InlineKeyboardButton(text="🚪 Sign Out", callback_data="logout")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @staticmethod
    def money():
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="$1M",   callback_data="m_1000000"),
             InlineKeyboardButton(text="$5M",   callback_data="m_5000000"),
             InlineKeyboardButton(text="$10M",  callback_data="m_10000000")],
            [InlineKeyboardButton(text="$25M",  callback_data="m_25000000"),
             InlineKeyboardButton(text="$50M ★",callback_data="m_50000000")],
            [InlineKeyboardButton(text="✏ Custom Amount", callback_data="m_custom")],
            [InlineKeyboardButton(text="◂ Back", callback_data="back_home")],
        ])

    @staticmethod
    def coins():
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="100K",   callback_data="c_100000"),
             InlineKeyboardButton(text="250K",   callback_data="c_250000"),
             InlineKeyboardButton(text="500K ★", callback_data="c_500000")],
            [InlineKeyboardButton(text="✏ Custom Amount", callback_data="c_custom")],
            [InlineKeyboardButton(text="◂ Back", callback_data="back_home")],
        ])

    @staticmethod
    def feat():
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚗 W16",     callback_data="f_w16"),
             InlineKeyboardButton(text="🔊 Horns",   callback_data="f_horns")],
            [InlineKeyboardButton(text="🛡 No Dmg",  callback_data="f_damage"),
             InlineKeyboardButton(text="⛽ Fuel",    callback_data="f_fuel")],
            [InlineKeyboardButton(text="💨 Smoke",   callback_data="f_smoke"),
             InlineKeyboardButton(text="🎭 Anims",   callback_data="f_anims")],
            [InlineKeyboardButton(text="🛞 Wheels",  callback_data="f_wheels"),
             InlineKeyboardButton(text="🏠 Houses",  callback_data="f_houses")],
            [InlineKeyboardButton(text="🎮 Levels",  callback_data="f_levels"),
             InlineKeyboardButton(text="🏅 Rank",    callback_data="f_rank")],
            [InlineKeyboardButton(text="🚀 ★ UNLOCK ALL ★", callback_data="f_all")],
            [InlineKeyboardButton(text="◂ Back", callback_data="back_home")],
        ])

    @staticmethod
    def sett():
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏ Name",     callback_data="s_name"),
             InlineKeyboardButton(text="🆔 Player ID",callback_data="s_pid")],
            [InlineKeyboardButton(text="🏆 Wins",    callback_data="s_wins"),
             InlineKeyboardButton(text="😞 Loses",   callback_data="s_loses")],
            [InlineKeyboardButton(text="🔧 Fix Account Bugs", callback_data="s_fix")],
            [InlineKeyboardButton(text="◂ Back", callback_data="back_home")],
        ])

    @staticmethod
    def admin(uid):
        lvl = admin_level(uid)
        b   = []
        if lvl >= 5:
            b.append([
                InlineKeyboardButton(text="📊 Stats", callback_data="a_stats"),
                InlineKeyboardButton(text="👥 Users", callback_data="a_users"),
            ])
            b.append([InlineKeyboardButton(text="📋 Activity Log", callback_data="a_log")])
        if lvl >= 10:
            b.append([InlineKeyboardButton(text="⏳ Pending Requests", callback_data="a_pend")])
            b.append([
                InlineKeyboardButton(text="➕ Add",   callback_data="a_adduser"),
                InlineKeyboardButton(text="📥 Bulk Add", callback_data="a_bulkadd"),
                InlineKeyboardButton(text="🚫 Ban",   callback_data="a_ban"),
                InlineKeyboardButton(text="🔓 Unban", callback_data="a_unban"),
            ])
            b.append([
                InlineKeyboardButton(text="👢 Kick",    callback_data="a_kick"),
                InlineKeyboardButton(text="⏰ Expiry",  callback_data="a_expiry"),
                InlineKeyboardButton(text="ℹ Profile", callback_data="a_profile"),
            ])
        if lvl >= 50:
            b.append([
                InlineKeyboardButton(text="💎 +VIP", callback_data="a_addvip"),
                InlineKeyboardButton(text="💎 -VIP", callback_data="a_rmvip"),
            ])
            b.append([InlineKeyboardButton(text="📢 Broadcast", callback_data="a_bcast_menu")])
        if lvl >= 100:
            b.append([
                InlineKeyboardButton(text="➕ Add Admin", callback_data="a_addadm"),
                InlineKeyboardButton(text="➖ Rem Admin", callback_data="a_rmadm"),
            ])
            b.append([
                InlineKeyboardButton(text="🖼 Update Photo",    callback_data="a_photo"),
                InlineKeyboardButton(text="🔧 Maintenance",    callback_data="a_maint"),
            ])
            b.append([InlineKeyboardButton(text="🔄 Reset Stats", callback_data="a_reset")])
        b.append([InlineKeyboardButton(text="◂ Home", callback_data="back_home")])
        return InlineKeyboardMarkup(inline_keyboard=b)

    @staticmethod
    def request_actions(uid):
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Accept", callback_data=f"rq_accept_{uid}"),
            InlineKeyboardButton(text="❌ Reject", callback_data=f"rq_reject_{uid}"),
            InlineKeyboardButton(text="🚫 Ban",    callback_data=f"rq_ban_{uid}"),
        ]])

    @staticmethod
    def broadcast_menu():
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📝 Text Message",    callback_data="bcast_text")],
            [InlineKeyboardButton(text="🖼 Photo + Caption", callback_data="bcast_photo")],
            [InlineKeyboardButton(text="💎 VIP Only",        callback_data="bcast_vip")],
            [InlineKeyboardButton(text="◂ Back",             callback_data="admin_menu")],
        ])

    @staticmethod
    def back_admin():
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◂ Admin Panel", callback_data="admin_menu")],
        ])

    @staticmethod
    def back_home():
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◂ Home", callback_data="back_home")],
        ])

    @staticmethod
    def confirm_logout():
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✔ Yes", callback_data="do_logout"),
            InlineKeyboardButton(text="✗ No",  callback_data="back_home"),
        ]])

    @staticmethod
    def pending_list():
        b = []
        for uid_str, info in list(PENDING.items())[:15]:
            uid_int = int(uid_str)
            name    = info.get("name",f"User {uid_int}")[:16]
            b.append([
                InlineKeyboardButton(text=f"✅ {name}", callback_data=f"rq_accept_{uid_int}"),
                InlineKeyboardButton(text="❌",          callback_data=f"rq_reject_{uid_int}"),
                InlineKeyboardButton(text="🚫",          callback_data=f"rq_ban_{uid_int}"),
            ])
        b.append([InlineKeyboardButton(text="◂ Back", callback_data="admin_menu")])
        return InlineKeyboardMarkup(inline_keyboard=b)


# ═══════════════════════════════════════════
#  📋 FSM STATES
# ═══════════════════════════════════════════

class SLogin(StatesGroup):
    email    = State()
    password = State()

class SMoney(StatesGroup):
    amount = State()

class SCoins(StatesGroup):
    amount = State()

class SName(StatesGroup):
    name = State()

class SPID(StatesGroup):
    pid = State()

class SWins(StatesGroup):
    val = State()

class SLoses(StatesGroup):
    val = State()

class SAdmin(StatesGroup):
    ban             = State()
    unban           = State()
    adduser         = State()
    bulkadd         = State()
    addadm_id       = State()
    addadm_lv       = State()
    rmadm           = State()
    kick            = State()
    expiry_id       = State()
    expiry_dy       = State()
    addvip          = State()
    rmvip           = State()
    profile_id      = State()
    bcast_text      = State()
    bcast_photo     = State()
    bcast_photo_cap = State()
    upload_photo    = State()


# ═══════════════════════════════════════════
#  🤖 BOT
# ═══════════════════════════════════════════

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp  = Dispatcher(storage=MemoryStorage())
rt  = Router()
dp.include_router(rt)
START_TIME = time.time()


async def notify_admins(text, markup=None):
    for uid, role in ADMINS.items():
        if ADMIN_LEVELS.get(role,0) >= 10:
            try: await bot.send_message(uid, text, reply_markup=markup)
            except: pass


async def result_msg(msg, ok, title, detail="", kb=None):
    icon  = "✅" if ok else "❌"
    final = f"{B}\n  {icon}  {title}\n{B}"
    if detail: final += f"\n\n  {detail}"
    try: await msg.edit_text(final, reply_markup=kb)
    except: pass


async def show_home(target, uid):
    td = nuker.get_token_data(uid)
    if not td:
        txt = T.welcome("","",uid); kb = K.login()
    else:
        email  = td.get("email","")
        record = nuker.get_record(uid, email)
        if record and record.get("Name"):
            txt = T.dashboard(record, email, uid)
        else:
            txt = f"{B}\n  🏠  𝗗𝗔𝗦𝗛𝗕𝗢𝗔𝗥𝗗\n{B}\n\n  📧 {email}\n\n  ▸ Tap Refresh to load data"
        kb = K.home(uid)
    if isinstance(target, CallbackQuery):
        try: await target.message.edit_text(txt, reply_markup=kb)
        except: pass
    elif isinstance(target, Message):
        await target.answer(txt, reply_markup=kb)


# ═══════════════════════════════════════════
#  🚀 /start
# ═══════════════════════════════════════════

@rt.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    uid  = msg.from_user.id
    name = msg.from_user.full_name
    un   = msg.from_user.username or ""

    STORE.setdefault("users",{})[str(uid)] = {
        "name": name, "username": un, "last_seen": datetime.now().isoformat()
    }
    save_store(STORE)

    if is_banned(uid):    await msg.answer(T.banned()); return
    if is_maintenance() and not has_admin(uid,"moderator"):
        await msg.answer(T.maintenance()); return
    if is_expired(uid):
        store_remove_user(uid)
        await msg.answer(f"{B}\n  ⏰  𝗘𝗫𝗣𝗜𝗥𝗘𝗗\n{B}\n\n  Access expired.", reply_markup=K.no_access()); return
    if not is_allowed(uid):
        await msg.answer(T.no_access(), reply_markup=K.no_access()); return

    td = nuker.get_token_data(uid)
    if td:
        email  = td.get("email","")
        record = nuker.get_record(uid, email)
        if record and record.get("Name"):
            await msg.answer(T.dashboard(record, email, uid), reply_markup=K.home(uid))
        else:
            await msg.answer(T.welcome(name,un,uid), reply_markup=K.home(uid))
    else:
        await msg.answer(T.welcome(name,un,uid), reply_markup=K.login())


# ═══════════════════════════════════════════
#  🔑 ACCESS
# ═══════════════════════════════════════════

@rt.callback_query(F.data == "send_request")
async def cb_send_request(cb: CallbackQuery):
    uid  = cb.from_user.id
    name = cb.from_user.full_name
    un   = cb.from_user.username or ""
    if is_banned(uid):
        await cb.message.edit_text(T.banned()); await cb.answer(); return
    if is_allowed(uid):
        await cb.answer("✅ Already approved!", show_alert=True); return
    if is_pending(uid):
        await cb.message.edit_text(
            f"{B}\n  ⏳  𝗣𝗘𝗡𝗗𝗜𝗡𝗚\n{B}\n\n  Request pending. Wait for admin.",
            reply_markup=K.after_request()); await cb.answer(); return

    store_add_pending(uid, name, un)
    await notify_admins(T.request_to_admin(name, un, uid), markup=K.request_actions(uid))
    await cb.message.edit_text(
        f"{B}\n  📩  𝗦𝗘𝗡𝗧\n{B}\n\n  ✔ Request sent!\n  You'll be notified.",
        reply_markup=K.after_request())
    await cb.answer("📩 Sent!")


@rt.callback_query(F.data.startswith("rq_accept_"))
async def cb_rq_accept(cb: CallbackQuery):
    if not has_admin(cb.from_user.id,"admin"):
        await cb.answer("✗ No permission", show_alert=True); return
    uid  = int(cb.data.split("_")[2])
    info = PENDING.get(str(uid),{})
    name = info.get("name",f"User {uid}")
    un   = info.get("username","")
    store_allow(uid, name); store_remove_pending(uid)
    admin_log(cb.from_user.id,"APPROVED",str(uid))
    try: await bot.send_message(uid, T.welcome(name,un,uid), reply_markup=K.login())
    except: pass
    try:
        await cb.message.edit_text(
            f"  ✅ <b>ACCEPTED</b>\n\n  👤 {name}\n  🆔 <code>{uid}</code>\n  ✔ By {cb.from_user.full_name}")
    except: pass
    await cb.answer("✅ Approved!")


@rt.callback_query(F.data.startswith("rq_reject_"))
async def cb_rq_reject(cb: CallbackQuery):
    if not has_admin(cb.from_user.id,"admin"):
        await cb.answer("✗ No permission", show_alert=True); return
    uid  = int(cb.data.split("_")[2])
    name = PENDING.get(str(uid),{}).get("name",f"User {uid}")
    store_remove_pending(uid)
    admin_log(cb.from_user.id,"REJECTED",str(uid))
    try: await bot.send_message(uid, f"{B}\n  ❌  𝗥𝗘𝗝𝗘𝗖𝗧𝗘𝗗\n{B}\n\n  Request declined.")
    except: pass
    try: await cb.message.edit_text(f"  ❌ <b>REJECTED</b>\n\n  👤 {name}\n  🆔 <code>{uid}</code>")
    except: pass
    await cb.answer("❌ Rejected")


@rt.callback_query(F.data.startswith("rq_ban_"))
async def cb_rq_ban(cb: CallbackQuery):
    if not has_admin(cb.from_user.id,"admin"):
        await cb.answer("✗ No permission", show_alert=True); return
    uid  = int(cb.data.split("_")[2])
    name = PENDING.get(str(uid),{}).get("name",f"User {uid}")
    store_ban(uid); store_remove_pending(uid)
    admin_log(cb.from_user.id,"BANNED_REQUEST",str(uid))
    try: await bot.send_message(uid, T.banned())
    except: pass
    try: await cb.message.edit_text(f"  🚫 <b>BANNED</b>\n\n  👤 {name}\n  🆔 <code>{uid}</code>")
    except: pass
    await cb.answer("🚫 Banned")


@rt.callback_query(F.data == "msg_admin")
async def cb_msg_admin(cb: CallbackQuery):
    try:
        owner = await bot.get_chat(OWNER_ID)
        txt   = f"{B}\n  💬  𝗖𝗢𝗡𝗧𝗔𝗖𝗧\n{B}\n\n  ▸ @{owner.username}\n  ▸ Your ID: <code>{cb.from_user.id}</code>"
    except:
        txt = f"  Your ID: <code>{cb.from_user.id}</code>"
    await cb.message.edit_text(txt, reply_markup=K.after_request())
    await cb.answer()


@rt.callback_query(F.data == "check_status")
async def cb_check_status(cb: CallbackQuery):
    uid = cb.from_user.id
    if is_allowed(uid):   await show_home(cb, uid); await cb.answer("✅ Approved!")
    elif is_banned(uid):  await cb.message.edit_text(T.banned()); await cb.answer("🚫")
    else:
        await cb.message.edit_text(
            f"{B}\n  ⏳  𝗣𝗘𝗡𝗗𝗜𝗡𝗚\n{B}\n\n  Still pending.",
            reply_markup=K.after_request())
        await cb.answer("⏳")


# ═══════════════════════════════════════════
#  🔐 LOGIN
# ═══════════════════════════════════════════

@rt.callback_query(F.data == "login")
async def cb_login(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    if is_banned(uid):   await cb.message.edit_text(T.banned()); await cb.answer(); return
    if not is_allowed(uid): await cb.message.edit_text(T.no_access(), reply_markup=K.no_access()); await cb.answer(); return
    if is_maintenance() and not has_admin(uid,"moderator"): await cb.message.edit_text(T.maintenance()); await cb.answer(); return
    await state.set_state(SLogin.email)
    await cb.message.edit_text(f"{B}\n  📧  𝗘𝗡𝗧𝗘𝗥 𝗘𝗠𝗔𝗜𝗟\n{B}\n\n  Type your CPM email:", reply_markup=K.cancel())
    await cb.answer()


@rt.message(SLogin.email)
async def p_email(msg: Message, state: FSMContext):
    em = msg.text.strip()
    if "@" not in em or "." not in em:
        await msg.answer("  ✗ Invalid email.", reply_markup=K.cancel()); return
    await state.update_data(email=em)
    await state.set_state(SLogin.password)
    await msg.answer(f"{B}\n  🔑  𝗣𝗔𝗦𝗦𝗪𝗢𝗥𝗗\n{B}\n\n  Type your password:\n  🔒 Auto-deleted", reply_markup=K.cancel())


@rt.message(SLogin.password)
async def p_pass(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    pw  = msg.text.strip()
    try: await msg.delete()
    except: pass
    data = await state.get_data()
    em   = data.get("email","")
    ld   = await msg.answer("  ⏳ Signing in...")
    try:
        r = await nuker.login(em, pw)
        if r.get("ok"):
            nuker.save_token(uid, r["auth"], em, pw, r.get("refresh_token",""), r.get("firebase_uid",""))
            await ld.edit_text("  ⏳ Loading account data...")
            loaded = await nuker.load(uid, force=True)
            STORE["stats"]["total_logins"] = STORE["stats"].get("total_logins",0)+1
            save_store(STORE); update_daily_stats("logins")
            await state.clear()
            if loaded:
                record = nuker.get_record(uid, em)
                await ld.edit_text(T.dashboard(record, em, uid), reply_markup=K.home(uid))
            else:
                await ld.edit_text(
                    f"{B}\n  ✅  𝗟𝗢𝗚𝗜𝗡 𝗦𝗨𝗖𝗖𝗘𝗦𝗦\n{B}\n\n  📧 {em}\n\n  ⚠ Tap Refresh to load data.",
                    reply_markup=K.home(uid))
        else:
            await state.clear()
            await ld.edit_text(T.login_fail(r.get("message","LOGIN_FAILED")), reply_markup=K.login())
    except Exception as e:
        log.error(f"Login handler: {e}\n{traceback.format_exc()}")
        await state.clear()
        await ld.edit_text(T.login_fail("NETWORK_ERROR"), reply_markup=K.login())


# ═══════════════════════════════════════════
#  🏠 NAV
# ═══════════════════════════════════════════

@rt.callback_query(F.data == "back_home")
async def cb_back_home(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_home(cb, cb.from_user.id)
    await cb.answer()


@rt.callback_query(F.data == "refresh")
async def cb_refresh(cb: CallbackQuery):
    uid = cb.from_user.id
    td  = nuker.get_token_data(uid)
    if not td: await show_home(cb, uid); await cb.answer(); return
    try: await cb.message.edit_text("  ⏳ Refreshing...")
    except: pass
    await nuker.load(uid, force=True)
    email  = td.get("email","")
    record = nuker.get_record(uid, email)
    if record and record.get("Name"):
        try: await cb.message.edit_text(T.dashboard(record, email, uid), reply_markup=K.home(uid))
        except: pass
    else:
        try: await cb.message.edit_text(f"{B}\n  ⚠  𝗖𝗢𝗨𝗟𝗗 𝗡𝗢𝗧 𝗟𝗢𝗔𝗗\n{B}\n\n  Try again.", reply_markup=K.home(uid))
        except: pass
    await cb.answer("🔄 Refreshed!")


@rt.callback_query(F.data == "cancel")
async def cb_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_home(cb, cb.from_user.id)
    await cb.answer("✗ Cancelled")


@rt.callback_query(F.data == "logout")
async def cb_logout(cb: CallbackQuery):
    await cb.message.edit_text(hdr("🚪","𝗦𝗜𝗚𝗡 𝗢𝗨𝗧")+"\n\n  Are you sure?", reply_markup=K.confirm_logout())
    await cb.answer()


@rt.callback_query(F.data == "do_logout")
async def cb_do_logout(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    nuker.delete_token(cb.from_user.id)
    await cb.message.edit_text(hdr("✅","𝗦𝗜𝗚𝗡𝗘𝗗 𝗢𝗨𝗧")+"\n\n  Successfully signed out.", reply_markup=K.login())
    await cb.answer("✅")


# ═══════════════════════════════════════════
#  💰 MONEY
# ═══════════════════════════════════════════

@rt.callback_query(F.data == "menu_money")
async def cb_money_menu(cb: CallbackQuery):
    if not nuker.get_token(cb.from_user.id): await cb.answer("✗ Sign in first!", show_alert=True); return
    ok,w = check_rate_limit(cb.from_user.id)
    if not ok: await cb.answer(f"⏳ Wait {w}s", show_alert=True); return
    await cb.message.edit_text(f"{B}\n  💰  𝗠𝗢𝗡𝗘𝗬\n{B}\n\n  Max: $50,000,000", reply_markup=K.money())
    await cb.answer()


@rt.callback_query(F.data.startswith("m_"))
async def cb_money(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    v   = cb.data[2:]
    if v == "custom":
        await state.set_state(SMoney.amount)
        await cb.message.edit_text(hdr("💰","𝗖𝗨𝗦𝗧𝗢𝗠")+f"\n\n  Enter amount (1 — {fmt(MAX_MONEY)}):", reply_markup=K.cancel())
        await cb.answer(); return
    m = await cb.message.edit_text(f"  ⏳ Setting ${fmt(int(v))}...")
    await cb.answer()
    r = await nuker.set_money(uid, int(v))
    await result_msg(m, r.get("ok"), "𝗠𝗢𝗡𝗘𝗬 𝗦𝗘𝗧" if r.get("ok") else "𝗙𝗔𝗜𝗟𝗘𝗗",
        f"💰 ${fmt(int(v))}" if r.get("ok") else r.get("message",""), K.back_home())


@rt.message(SMoney.amount)
async def p_money(msg: Message, state: FSMContext):
    try:
        a = int(msg.text.strip().replace(",","").replace(" ",""))
        assert 1 <= a <= MAX_MONEY
    except:
        await msg.answer(f"  ✗ Enter 1 — {fmt(MAX_MONEY)}", reply_markup=K.cancel()); return
    await state.clear()
    ld = await msg.answer(f"  ⏳ Setting ${fmt(a)}...")
    r  = await nuker.set_money(msg.from_user.id, a)
    await result_msg(ld, r.get("ok"), "𝗠𝗢𝗡𝗘𝗬 𝗦𝗘𝗧" if r.get("ok") else "𝗙𝗔𝗜𝗟𝗘𝗗",
        f"💰 ${fmt(a)}" if r.get("ok") else "", K.back_home())


# ═══════════════════════════════════════════
#  🪙 COINS
# ═══════════════════════════════════════════

@rt.callback_query(F.data == "menu_coins")
async def cb_coins_menu(cb: CallbackQuery):
    if not nuker.get_token(cb.from_user.id): await cb.answer("✗ Sign in first!", show_alert=True); return
    await cb.message.edit_text(f"{B}\n  🪙  𝗖𝗢𝗜𝗡𝗦\n{B}\n\n  Max: 500,000", reply_markup=K.coins())
    await cb.answer()


@rt.callback_query(F.data.startswith("c_"))
async def cb_coins(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    v   = cb.data[2:]
    if v == "custom":
        await state.set_state(SCoins.amount)
        await cb.message.edit_text(hdr("🪙","𝗖𝗨𝗦𝗧𝗢𝗠")+f"\n\n  Enter amount (1 — {fmt(MAX_COIN)}):", reply_markup=K.cancel())
        await cb.answer(); return
    m = await cb.message.edit_text(f"  ⏳ Setting {fmt(int(v))} coins...")
    await cb.answer()
    r = await nuker.set_coin(uid, int(v))
    await result_msg(m, r.get("ok"), "𝗖𝗢𝗜𝗡𝗦 𝗦𝗘𝗧" if r.get("ok") else "𝗙𝗔𝗜𝗟𝗘𝗗",
        f"🪙 {fmt(int(v))} coins" if r.get("ok") else "", K.back_home())


@rt.message(SCoins.amount)
async def p_coins(msg: Message, state: FSMContext):
    try:
        a = int(msg.text.strip().replace(",","").replace(" ",""))
        assert 1 <= a <= MAX_COIN
    except:
        await msg.answer(f"  ✗ Enter 1 — {fmt(MAX_COIN)}", reply_markup=K.cancel()); return
    await state.clear()
    ld = await msg.answer(f"  ⏳ Setting {fmt(a)} coins...")
    r  = await nuker.set_coin(msg.from_user.id, a)
    await result_msg(ld, r.get("ok"), "𝗖𝗢𝗜𝗡𝗦 𝗦𝗘𝗧" if r.get("ok") else "𝗙𝗔𝗜𝗟𝗘𝗗",
        f"🪙 {fmt(a)} coins" if r.get("ok") else "", K.back_home())


# ═══════════════════════════════════════════
#  ⚡ FEATURES
# ═══════════════════════════════════════════

FEAT_MAP = {
    "f_w16":    ("🚗 W16 Engine",    nuker.unlock_w16),
    "f_horns":  ("🔊 Horns",         nuker.unlock_horns),
    "f_damage": ("🛡 No Damage",     nuker.disable_damage),
    "f_fuel":   ("⛽ Unlimited Fuel", nuker.unlimited_fuel),
    "f_smoke":  ("💨 Smoke",         nuker.unlock_smoke),
    "f_anims":  ("🎭 Animations",    nuker.unlock_animations),
    "f_wheels": ("🛞 Wheels",        nuker.unlock_wheels),
    "f_houses": ("🏠 Houses",        nuker.unlock_houses),
    "f_levels": ("🎮 All Levels",    nuker.complete_all_levels),
    "f_rank":   ("🏅 Max Rank",      nuker.set_rank),
}


@rt.callback_query(F.data == "menu_feat")
async def cb_feat_menu(cb: CallbackQuery):
    if not nuker.get_token(cb.from_user.id): await cb.answer("✗ Sign in first!", show_alert=True); return
    await cb.message.edit_text(f"{B}\n  ⚡  𝗙𝗘𝗔𝗧𝗨𝗥𝗘𝗦\n{B}\n\n  Select a feature or UNLOCK ALL:", reply_markup=K.feat())
    await cb.answer()


@rt.callback_query(F.data.in_(set(FEAT_MAP.keys())))
async def cb_feat(cb: CallbackQuery):
    uid       = cb.from_user.id
    fname, fn = FEAT_MAP[cb.data]
    m = await cb.message.edit_text(f"  ⏳ Loading account & applying {fname}...")
    await cb.answer()
    r = await fn(uid)
    if r.get("ok"):
        STORE["stats"]["total_unlocks"] = STORE["stats"].get("total_unlocks",0)+1
        save_store(STORE); update_daily_stats("unlocks")
    await result_msg(m, r.get("ok"),
        f"{fname} ✔" if r.get("ok") else f"{fname} ✗",
        "" if r.get("ok") else r.get("message",""), K.feat())


@rt.callback_query(F.data == "f_all")
async def cb_feat_all(cb: CallbackQuery):
    uid = cb.from_user.id
    if not nuker.get_token(uid): await cb.answer("✗ Sign in first!", show_alert=True); return
    await cb.answer()
    m = await cb.message.edit_text(f"{B}\n  🚀  𝗨𝗡𝗟𝗢𝗖𝗞𝗜𝗡𝗚 𝗔𝗟𝗟\n{B}\n\n  ⏳ Loading account...")
    await nuker.load(uid, force=True)
    ALL    = list(FEAT_MAP.values())
    total  = len(ALL)
    done   = 0; failed = 0; results = []
    for i,(name,fn) in enumerate(ALL):
        pct    = int(((i+1)/total)*100)
        bar    = "▰"*int(pct/7) + "▱"*(15-int(pct/7))
        try:
            await m.edit_text(
                f"{B}\n  🚀  𝗨𝗡𝗟𝗢𝗖𝗞𝗜𝗡𝗚 𝗔𝗟𝗟\n{B}\n\n"
                f"  [{bar}] {pct}%\n"
                f"  ✔ {done}  ✗ {failed}  ▸ {i+1}/{total}\n\n"
                f"  ⏳ {name}")
        except: pass
        r = await fn(uid)
        if r.get("ok"): done+=1; results.append(f"  ✔ {name}")
        else: failed+=1; results.append(f"  ✗ {name}")
        await asyncio.sleep(0.3)
    STORE["stats"]["total_unlocks"] = STORE["stats"].get("total_unlocks",0)+done
    save_store(STORE)
    try:
        await m.edit_text(
            f"{B}\n  🎉  𝗖𝗢𝗠𝗣𝗟𝗘𝗧𝗘\n{B}\n\n"
            f"  [▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰] 100%\n\n"
            f"  ✔ {done}/{total}  ✗ {failed}/{total}\n\n"
            + "\n".join(results), reply_markup=K.back_home())
    except: pass


# ═══════════════════════════════════════════
#  🔧 SETTINGS
# ═══════════════════════════════════════════

@rt.callback_query(F.data == "menu_set")
async def cb_set_menu(cb: CallbackQuery):
    if not nuker.get_token(cb.from_user.id): await cb.answer("✗ Sign in first!", show_alert=True); return
    await cb.message.edit_text(f"{B}\n  🔧  𝗦𝗘𝗧𝗧𝗜𝗡𝗚𝗦\n{B}\n\n  Modify your account:", reply_markup=K.sett())
    await cb.answer()


@rt.callback_query(F.data == "s_name")
async def cb_s_name(cb: CallbackQuery, state: FSMContext):
    await state.set_state(SName.name)
    await cb.message.edit_text(hdr("✏","𝗖𝗛𝗔𝗡𝗚𝗘 𝗡𝗔𝗠𝗘")+"\n\n  Enter new name:", reply_markup=K.cancel())
    await cb.answer()


@rt.message(SName.name)
async def p_name(msg: Message, state: FSMContext):
    name = msg.text.strip()
    if not name or len(name) > 100:
        await msg.answer("  ✗ 1-100 characters.", reply_markup=K.cancel()); return
    await state.clear()
    ld = await msg.answer("  ⏳ Setting name...")
    r  = await nuker.set_player_name(msg.from_user.id, name)
    await result_msg(ld, r.get("ok"), "𝗡𝗔𝗠𝗘 𝗨𝗣𝗗𝗔𝗧𝗘𝗗" if r.get("ok") else "𝗙𝗔𝗜𝗟𝗘𝗗",
        f"✔ {name}" if r.get("ok") else r.get("message",""), K.back_home())


@rt.callback_query(F.data == "s_pid")
async def cb_s_pid(cb: CallbackQuery, state: FSMContext):
    await state.set_state(SPID.pid)
    await cb.message.edit_text(hdr("🆔","𝗣𝗟𝗔𝗬𝗘𝗥 𝗜𝗗")+"\n\n  Enter new Player ID:", reply_markup=K.cancel())
    await cb.answer()


@rt.message(SPID.pid)
async def p_pid(msg: Message, state: FSMContext):
    pid   = msg.text.strip()
    clean = re.sub(r'\[\w+\]','',pid)
    if not clean or len(clean) < 4 or len(clean) > 100:
        await msg.answer("  ✗ 4-100 characters.", reply_markup=K.cancel()); return
    await state.clear()
    ld = await msg.answer("  ⏳ Setting ID...")
    r  = await nuker.set_player_id(msg.from_user.id, pid)
    await result_msg(ld, r.get("ok"), "𝗜𝗗 𝗨𝗣𝗗𝗔𝗧𝗘𝗗" if r.get("ok") else "𝗙𝗔𝗜𝗟𝗘𝗗",
        f"✔ {pid.upper()}" if r.get("ok") else r.get("message",""), K.back_home())


@rt.callback_query(F.data == "s_wins")
async def cb_s_wins(cb: CallbackQuery, state: FSMContext):
    await state.set_state(SWins.val)
    await cb.message.edit_text(hdr("🏆","𝗦𝗘𝗧 𝗪𝗜𝗡𝗦")+"\n\n  Enter win count:", reply_markup=K.cancel())
    await cb.answer()


@rt.message(SWins.val)
async def p_wins(msg: Message, state: FSMContext):
    try: v = int(msg.text.strip()); assert v >= 0
    except: await msg.answer("  ✗ Invalid number.", reply_markup=K.cancel()); return
    await state.clear()
    ld = await msg.answer("  ⏳ Setting wins...")
    r  = await nuker.set_race_wins(msg.from_user.id, v)
    await result_msg(ld, r.get("ok"), "𝗪𝗜𝗡𝗦 𝗨𝗣𝗗𝗔𝗧𝗘𝗗" if r.get("ok") else "𝗙𝗔𝗜𝗟𝗘𝗗",
        f"🏆 {fmt(v)} wins" if r.get("ok") else r.get("message",""), K.back_home())


@rt.callback_query(F.data == "s_loses")
async def cb_s_loses(cb: CallbackQuery, state: FSMContext):
    await state.set_state(SLoses.val)
    await cb.message.edit_text(hdr("😞","𝗦𝗘𝗧 𝗟𝗢𝗦𝗘𝗦")+"\n\n  Enter loss count:", reply_markup=K.cancel())
    await cb.answer()


@rt.message(SLoses.val)
async def p_loses(msg: Message, state: FSMContext):
    try: v = int(msg.text.strip()); assert v >= 0
    except: await msg.answer("  ✗ Invalid number.", reply_markup=K.cancel()); return
    await state.clear()
    ld = await msg.answer("  ⏳ Setting loses...")
    r  = await nuker.set_race_loses(msg.from_user.id, v)
    await result_msg(ld, r.get("ok"), "𝗟𝗢𝗦𝗘𝗦 𝗨𝗣𝗗𝗔𝗧𝗘𝗗" if r.get("ok") else "𝗙𝗔𝗜𝗟𝗘𝗗",
        f"😞 {fmt(v)} loses" if r.get("ok") else r.get("message",""), K.back_home())


@rt.callback_query(F.data == "s_fix")
async def cb_s_fix(cb: CallbackQuery):
    uid = cb.from_user.id
    m   = await cb.message.edit_text("  ⏳ Loading & fixing account...")
    await cb.answer()
    r = await nuker.fix_account(uid)
    await result_msg(m, r.get("ok"),
        "𝗔𝗖𝗖𝗢𝗨𝗡𝗧 𝗙𝗜𝗫𝗘𝗗" if r.get("ok") else "𝗙𝗔𝗜𝗟𝗘𝗗",
        f"✔ {r.get('bugs_fixed',0)} bugs fixed" if r.get("ok") else r.get("message",""),
        K.back_home())


# ═══════════════════════════════════════════
#  👑 ADMIN PANEL
# ═══════════════════════════════════════════

@rt.message(Command("admin"))
async def cmd_admin(msg: Message, state: FSMContext):
    await state.clear()
    uid = msg.from_user.id
    if not has_admin(uid,"moderator"): await msg.answer("  ✗ No admin access."); return
    await msg.answer(T.admin_panel(uid), reply_markup=K.admin(uid))


async def expire_bulkadd_prompt(user_id: int, chat_id: int, state: FSMContext):
    await asyncio.sleep(BULKADD_TIMEOUT_SECONDS)
    try:
        if await state.get_state() == SAdmin.bulkadd.state:
            data = await state.get_data()
            if data.get("bulkadd_user") == user_id:
                await state.clear()
                await bot.send_message(chat_id, "  ⌛ Bulk add cancelled: timed out waiting for user IDs.", reply_markup=K.back_admin())
    except Exception as e:
        log.error(f"Bulk add timeout error: {e}")


@rt.message(Command("bulkadd"))
async def cmd_bulkadd(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    if not has_admin(uid,"admin"):
        await msg.answer("  ✗ No admin access.")
        return
    await state.clear()
    await state.set_state(SAdmin.bulkadd)
    await state.update_data(bulkadd_user=uid)
    await msg.answer(
        hdr("📥","𝗕𝗨𝗟𝗞 𝗔𝗗𝗗") +
        "\n\n  Send the user IDs (one per line)."
        "\n\n  Send /cancel to cancel.",
        reply_markup=K.back_admin()
    )
    asyncio.create_task(expire_bulkadd_prompt(uid, msg.chat.id, state))


@rt.callback_query(F.data == "admin_menu")
async def cb_admin_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    uid = cb.from_user.id
    if not has_admin(uid,"moderator"): await cb.answer("✗ No access!", show_alert=True); return
    await cb.message.edit_text(T.admin_panel(uid), reply_markup=K.admin(uid))
    await cb.answer()


@rt.callback_query(F.data == "a_stats")
async def cb_a_stats(cb: CallbackQuery):
    if not has_admin(cb.from_user.id,"moderator"): await cb.answer("✗",show_alert=True); return
    await cb.message.edit_text(T.stats(), reply_markup=K.back_admin())
    await cb.answer()


@rt.callback_query(F.data == "a_log")
async def cb_a_log(cb: CallbackQuery):
    if not has_admin(cb.from_user.id,"moderator"): await cb.answer("✗",show_alert=True); return
    logs = STORE.get("admin_log",[])[:10]
    txt  = f"{B}\n  📋  𝗔𝗖𝗧𝗜𝗩𝗜𝗧𝗬 𝗟𝗢𝗚\n{B}\n\n"
    if not logs: txt += "  No activity yet."
    else:
        for e in logs:
            t = e.get("time","")[:16].replace("T"," ")
            txt += f"  ◆ {t}\n    {e.get('action','')} → {e.get('target','')}\n\n"
    await cb.message.edit_text(txt, reply_markup=K.back_admin())
    await cb.answer()


@rt.callback_query(F.data == "a_users")
async def cb_a_users(cb: CallbackQuery):
    if not has_admin(cb.from_user.id,"moderator"): await cb.answer("✗",show_alert=True); return
    users = STORE.get("users",{})
    lb = {"owner":"👑","superadmin":"⭐","admin":"🛡","moderator":"👮"}
    txt = f"{B}\n  👥  𝗨𝗦𝗘𝗥𝗦 ({len(ALLOWED_USERS)})\n{B}\n\n"
    for uid in sorted(ALLOWED_USERS)[:25]:
        info  = users.get(str(uid),{})
        name  = info.get("name",f"User {uid}")[:16]
        role  = ADMINS.get(uid,"")
        badge = lb.get(role,"")
        vip   = "💎" if uid in VIP_USERS else ""
        txt  += f"  {badge}{vip} {name}  <code>{uid}</code>\n"
    if len(ALLOWED_USERS)>25: txt+=f"\n  ... +{len(ALLOWED_USERS)-25} more"
    await cb.message.edit_text(txt, reply_markup=K.back_admin())
    await cb.answer()


@rt.callback_query(F.data == "a_pend")
async def cb_a_pend(cb: CallbackQuery):
    if not has_admin(cb.from_user.id,"admin"): await cb.answer("✗",show_alert=True); return
    if not PENDING:
        await cb.message.edit_text(hdr("✅","𝗡𝗢 𝗣𝗘𝗡𝗗𝗜𝗡𝗚")+"\n\n  No pending requests.", reply_markup=K.back_admin())
    else:
        await cb.message.edit_text(hdr("⏳","𝗣𝗘𝗡𝗗𝗜𝗡𝗚")+f"\n\n  {len(PENDING)} pending:", reply_markup=K.pending_list())
    await cb.answer()


# ── User management ───────────────────────

@rt.callback_query(F.data == "a_bulkadd")
async def cb_a_bulkadd(cb: CallbackQuery, state: FSMContext):
    if not has_admin(cb.from_user.id,"admin"): await cb.answer("✗",show_alert=True); return
    await state.set_state(SAdmin.bulkadd)
    await state.update_data(bulkadd_user=cb.from_user.id)
    await cb.message.edit_text(
        hdr("📥","𝗕𝗨𝗟𝗞 𝗔𝗗𝗗") +
        "\n\n  Send the user IDs (one per line)."
        "\n\n  Send /cancel to cancel.",
        reply_markup=K.back_admin()
    )
    asyncio.create_task(expire_bulkadd_prompt(cb.from_user.id, cb.message.chat.id, state))
    await cb.answer()


@rt.callback_query(F.data == "a_adduser")
async def cb_a_adduser(cb: CallbackQuery, state: FSMContext):
    if not has_admin(cb.from_user.id,"admin"): await cb.answer("✗",show_alert=True); return
    await state.set_state(SAdmin.adduser)
    await cb.message.edit_text(hdr("➕","𝗔𝗗𝗗 𝗨𝗦𝗘𝗥")+"\n\n  Enter user ID:", reply_markup=K.back_admin())
    await cb.answer()


@rt.message(SAdmin.adduser)
async def p_adduser(msg: Message, state: FSMContext):
    try: uid = int(msg.text.strip())
    except: await msg.answer("  ✗ Invalid ID."); return
    await state.clear()
    store_allow(uid); store_remove_pending(uid)
    admin_log(msg.from_user.id,"ADDED",str(uid))
    name = STORE.get("users",{}).get(str(uid),{}).get("name","User")
    un   = STORE.get("users",{}).get(str(uid),{}).get("username","")
    try: await bot.send_message(uid, T.welcome(name,un,uid), reply_markup=K.login())
    except: pass
    await msg.answer(f"  ✔ <code>{uid}</code> added!", reply_markup=K.back_admin())


@rt.message(SAdmin.bulkadd)
async def p_bulkadd(msg: Message, state: FSMContext):
    actor_id = msg.from_user.id
    if not has_admin(actor_id,"admin"):
        await state.clear()
        await msg.answer("  ✗ No admin access.")
        return

    text = (msg.text or "").strip()
    if text.lower() in {"/cancel", "cancel"}:
        await state.clear()
        await msg.answer("  ✗ Bulk add cancelled.", reply_markup=K.back_admin())
        return

    raw_lines = [line.strip() for line in (msg.text or "").splitlines() if line.strip()]
    if not raw_lines:
        await msg.answer("  ✗ Empty submission. Send one user ID per line, or /cancel to cancel.", reply_markup=K.back_admin())
        return

    seen = set()
    added_ids = []
    already_ids = []
    invalid_ids = []
    duplicate_count = 0

    for raw in raw_lines:
        if raw in seen:
            duplicate_count += 1
            continue
        seen.add(raw)
        if not raw.isdigit():
            invalid_ids.append(raw)
            continue
        uid = int(raw)
        if uid in ALLOWED_USERS:
            already_ids.append(uid)
            continue
        if store_allow(uid, save=False):
            added_ids.append(uid)
            PENDING.pop(str(uid), None)
        else:
            already_ids.append(uid)

    STORE["pending"] = PENDING
    save_ok = True
    if added_ids:
        save_ok = save_store(STORE)
        if save_ok:
            admin_log(actor_id,"BULK_ADDED",f"{len(added_ids)} users")

    await state.clear()

    total_processed = len(seen)
    detail = (
        f"Added: {len(added_ids)}\n"
        f"Already existed: {len(already_ids)}\n"
        f"Invalid IDs: {len(invalid_ids)}\n"
        f"Total processed: {total_processed}"
    )
    if duplicate_count:
        detail += f"\nDuplicates ignored: {duplicate_count}"
    if not save_ok:
        detail += "\n\n⚠ Storage write failed. The IDs were added in memory, but saving to disk failed. Check the bot logs."
    if invalid_ids:
        invalid_preview = "\n".join(escape(x[:80]) for x in invalid_ids[:50])
        if len(invalid_ids) > 50:
            invalid_preview += f"\n... and {len(invalid_ids)-50} more"
        detail += f"\n\nInvalid IDs:\n<pre>{invalid_preview}</pre>"

    await msg.answer(
        f"{B}\n  📥  Bulk Add Complete\n{B}\n\n  {detail}",
        reply_markup=K.back_admin()
    )


@rt.callback_query(F.data == "a_ban")
async def cb_a_ban(cb: CallbackQuery, state: FSMContext):
    if not has_admin(cb.from_user.id,"admin"): await cb.answer("✗",show_alert=True); return
    await state.set_state(SAdmin.ban)
    await cb.message.edit_text(hdr("🚫","𝗕𝗔𝗡")+"\n\n  Enter user ID:", reply_markup=K.back_admin())
    await cb.answer()


@rt.message(SAdmin.ban)
async def p_ban(msg: Message, state: FSMContext):
    try: uid = int(msg.text.strip())
    except: await msg.answer("  ✗ Invalid ID."); return
    if uid == OWNER_ID: await msg.answer("  ✗ Cannot ban owner!", reply_markup=K.back_admin()); await state.clear(); return
    await state.clear()
    store_ban(uid); nuker.delete_token(uid)
    admin_log(msg.from_user.id,"BANNED",str(uid))
    try: await bot.send_message(uid, T.banned())
    except: pass
    await msg.answer(f"  🚫 <code>{uid}</code> banned!", reply_markup=K.back_admin())


@rt.callback_query(F.data == "a_unban")
async def cb_a_unban(cb: CallbackQuery, state: FSMContext):
    if not has_admin(cb.from_user.id,"admin"): await cb.answer("✗",show_alert=True); return
    await state.set_state(SAdmin.unban)
    await cb.message.edit_text(hdr("🔓","𝗨𝗡𝗕𝗔𝗡")+"\n\n  Enter user ID:", reply_markup=K.back_admin())
    await cb.answer()


@rt.message(SAdmin.unban)
async def p_unban(msg: Message, state: FSMContext):
    try: uid = int(msg.text.strip())
    except: await msg.answer("  ✗ Invalid ID."); return
    await state.clear()
    store_unban(uid); admin_log(msg.from_user.id,"UNBANNED",str(uid))
    await msg.answer(f"  🔓 <code>{uid}</code> unbanned!", reply_markup=K.back_admin())


@rt.callback_query(F.data == "a_kick")
async def cb_a_kick(cb: CallbackQuery, state: FSMContext):
    if not has_admin(cb.from_user.id,"admin"): await cb.answer("✗",show_alert=True); return
    await state.set_state(SAdmin.kick)
    await cb.message.edit_text(hdr("👢","𝗞𝗜𝗖𝗞")+"\n\n  Enter user ID:", reply_markup=K.back_admin())
    await cb.answer()


@rt.message(SAdmin.kick)
async def p_kick(msg: Message, state: FSMContext):
    try: uid = int(msg.text.strip())
    except: await msg.answer("  ✗ Invalid ID."); return
    if uid == OWNER_ID: await msg.answer("  ✗ Cannot kick owner!", reply_markup=K.back_admin()); await state.clear(); return
    await state.clear()
    store_remove_user(uid); nuker.delete_token(uid)
    admin_log(msg.from_user.id,"KICKED",str(uid))
    try: await bot.send_message(uid, hdr("👢","𝗞𝗜𝗖𝗞𝗘𝗗")+"\n\n  Access removed.")
    except: pass
    await msg.answer(f"  👢 <code>{uid}</code> kicked!", reply_markup=K.back_admin())


@rt.callback_query(F.data == "a_expiry")
async def cb_a_expiry(cb: CallbackQuery, state: FSMContext):
    if not has_admin(cb.from_user.id,"admin"): await cb.answer("✗",show_alert=True); return
    await state.set_state(SAdmin.expiry_id)
    await cb.message.edit_text(hdr("⏰","𝗦𝗘𝗧 𝗘𝗫𝗣𝗜𝗥𝗬")+"\n\n  Enter user ID:", reply_markup=K.back_admin())
    await cb.answer()


@rt.message(SAdmin.expiry_id)
async def p_expiry_id(msg: Message, state: FSMContext):
    try: uid = int(msg.text.strip())
    except: await msg.answer("  ✗ Invalid ID."); return
    await state.update_data(target=uid)
    await state.set_state(SAdmin.expiry_dy)
    await msg.answer(f"  Enter days for <code>{uid}</code> (0 = remove):", reply_markup=K.back_admin())


@rt.message(SAdmin.expiry_dy)
async def p_expiry_dy(msg: Message, state: FSMContext):
    try: days = int(msg.text.strip()); assert days >= 0
    except: await msg.answer("  ✗ Invalid."); return
    d   = await state.get_data(); uid = d.get("target")
    await state.clear()
    if days == 0:
        store_remove_expiry(uid)
        await msg.answer(f"  ✔ Expiry removed for <code>{uid}</code>", reply_markup=K.back_admin())
    else:
        store_set_expiry(uid, days)
        admin_log(msg.from_user.id,f"EXPIRY_{days}d",str(uid))
        try: await bot.send_message(uid, f"  ⏰ Access expires in {days} days.")
        except: pass
        await msg.answer(f"  ✔ <code>{uid}</code> expires in {days} days", reply_markup=K.back_admin())


@rt.callback_query(F.data == "a_profile")
async def cb_a_profile(cb: CallbackQuery, state: FSMContext):
    if not has_admin(cb.from_user.id,"admin"): await cb.answer("✗",show_alert=True); return
    await state.set_state(SAdmin.profile_id)
    await cb.message.edit_text(hdr("ℹ","𝗨𝗦𝗘𝗥 𝗣𝗥𝗢𝗙𝗜𝗟𝗘")+"\n\n  Enter user ID:", reply_markup=K.back_admin())
    await cb.answer()


@rt.message(SAdmin.profile_id)
async def p_profile_id(msg: Message, state: FSMContext):
    try: uid = int(msg.text.strip())
    except: await msg.answer("  ✗ Invalid ID."); return
    await state.clear()
    info  = STORE.get("users",{}).get(str(uid),{})
    name  = info.get("name","Unknown")
    un    = info.get("username","N/A")
    last  = info.get("last_seen","N/A")[:16].replace("T"," ")
    role  = ADMINS.get(uid,"user")
    vip   = "💎 Yes" if uid in VIP_USERS else "No"
    st    = "🚫 Banned" if uid in BANNED else ("✔ Allowed" if uid in ALLOWED_USERS else "⏳ Pending")
    exp   = EXPIRY.get(str(uid),"None")
    if exp != "None":
        try: exp = datetime.fromisoformat(exp).strftime("%d %b %Y")
        except: pass
    warns = store_get_warnings(uid)
    note  = store_get_note(uid)
    txt   = (
        f"{B}\n  ℹ  𝗣𝗥𝗢𝗙𝗜𝗟𝗘\n{B}\n\n"
        f"  👤 Name:     {name}\n"
        f"  📱 Username: @{un}\n"
        f"  🆔 ID:       <code>{uid}</code>\n"
        f"  📊 Status:   {st}\n"
        f"  🛡 Role:     {role}\n"
        f"  💎 VIP:      {vip}\n"
        f"  ⏰ Expiry:   {exp}\n"
        f"  📅 Last:     {last}\n"
        f"  ⚠ Warns:    {len(warns)}/3"
    )
    if note: txt += f"\n  📝 Note: {note}"
    await msg.answer(txt, reply_markup=K.back_admin())


# ── VIP ───────────────────────────────────

@rt.callback_query(F.data == "a_addvip")
async def cb_a_addvip(cb: CallbackQuery, state: FSMContext):
    if not has_admin(cb.from_user.id,"superadmin"): await cb.answer("✗",show_alert=True); return
    await state.set_state(SAdmin.addvip)
    await cb.message.edit_text(hdr("💎","𝗔𝗗𝗗 𝗩𝗜𝗣")+"\n\n  Enter user ID:", reply_markup=K.back_admin())
    await cb.answer()


@rt.message(SAdmin.addvip)
async def p_addvip(msg: Message, state: FSMContext):
    try: uid = int(msg.text.strip())
    except: await msg.answer("  ✗ Invalid ID."); return
    await state.clear()
    store_add_vip(uid); admin_log(msg.from_user.id,"ADD_VIP",str(uid))
    try: await bot.send_message(uid, "  💎 You are now VIP!")
    except: pass
    await msg.answer(f"  💎 <code>{uid}</code> is now VIP!", reply_markup=K.back_admin())


@rt.callback_query(F.data == "a_rmvip")
async def cb_a_rmvip(cb: CallbackQuery, state: FSMContext):
    if not has_admin(cb.from_user.id,"superadmin"): await cb.answer("✗",show_alert=True); return
    await state.set_state(SAdmin.rmvip)
    await cb.message.edit_text(hdr("💎","𝗥𝗘𝗠 𝗩𝗜𝗣")+"\n\n  Enter user ID:", reply_markup=K.back_admin())
    await cb.answer()


@rt.message(SAdmin.rmvip)
async def p_rmvip(msg: Message, state: FSMContext):
    try: uid = int(msg.text.strip())
    except: await msg.answer("  ✗ Invalid ID."); return
    await state.clear()
    store_remove_vip(uid)
    await msg.answer(f"  ✔ VIP removed for <code>{uid}</code>", reply_markup=K.back_admin())


# ═══════════════════════════════════════════
#  🖼 UPLOAD PHOTO (Owner only)
# ═══════════════════════════════════════════

@rt.callback_query(F.data == "a_photo")
async def cb_a_photo(cb: CallbackQuery, state: FSMContext):
    if not has_admin(cb.from_user.id,"owner"):
        await cb.answer("✗ Owner only!", show_alert=True); return
    await state.set_state(SAdmin.upload_photo)
    current = get_bot_photo()
    txt = f"{B}\n  🖼  𝗨𝗣𝗗𝗔𝗧𝗘 𝗕𝗢𝗧 𝗣𝗛𝗢𝗧𝗢\n{B}\n\n"
    if current:
        txt += "  ◆ Current photo is set.\n\n"
    txt += "  Send a new photo to update it.\n  This photo shows on welcome screen."
    await cb.message.edit_text(txt, reply_markup=K.back_admin())
    await cb.answer()


@rt.message(SAdmin.upload_photo, F.photo)
async def p_upload_photo(msg: Message, state: FSMContext):
    if not has_admin(msg.from_user.id,"owner"):
        await msg.answer("  ✗ Owner only!"); await state.clear(); return
    file_id = msg.photo[-1].file_id
    set_bot_photo(file_id)
    admin_log(msg.from_user.id,"UPDATE_PHOTO")
    await state.clear()
    await msg.answer(
        f"{B}\n  ✅  𝗣𝗛𝗢𝗧𝗢 𝗨𝗣𝗗𝗔𝗧𝗘𝗗\n{B}\n\n"
        "  ✔ Bot welcome photo updated!\n"
        "  ▸ It will show for new users.",
        reply_markup=K.back_admin()
    )


# ═══════════════════════════════════════════
#  📢 BROADCAST
# ═══════════════════════════════════════════

@rt.callback_query(F.data == "a_bcast_menu")
async def cb_bcast_menu(cb: CallbackQuery):
    if not has_admin(cb.from_user.id,"superadmin"): await cb.answer("✗",show_alert=True); return
    await cb.message.edit_text(
        f"{B}\n  📢  𝗕𝗥𝗢𝗔𝗗𝗖𝗔𝗦𝗧\n{B}\n\n"
        f"  👥 All: {len(ALLOWED_USERS)}  💎 VIP: {len(VIP_USERS)}",
        reply_markup=K.broadcast_menu())
    await cb.answer()


@rt.callback_query(F.data == "bcast_text")
async def cb_bcast_text(cb: CallbackQuery, state: FSMContext):
    if not has_admin(cb.from_user.id,"superadmin"): await cb.answer("✗",show_alert=True); return
    await state.update_data(bcast_target="all")
    await state.set_state(SAdmin.bcast_text)
    await cb.message.edit_text(hdr("📢","𝗧𝗘𝗫𝗧 𝗕𝗥𝗢𝗔𝗗𝗖𝗔𝗦𝗧")+f"\n\n  Message to all {len(ALLOWED_USERS)} users:", reply_markup=K.back_admin())
    await cb.answer()


@rt.callback_query(F.data == "bcast_vip")
async def cb_bcast_vip(cb: CallbackQuery, state: FSMContext):
    if not has_admin(cb.from_user.id,"superadmin"): await cb.answer("✗",show_alert=True); return
    await state.update_data(bcast_target="vip")
    await state.set_state(SAdmin.bcast_text)
    await cb.message.edit_text(hdr("💎","𝗩𝗜𝗣 𝗕𝗥𝗢𝗔𝗗𝗖𝗔𝗦𝗧")+f"\n\n  Message to {len(VIP_USERS)} VIPs:", reply_markup=K.back_admin())
    await cb.answer()


@rt.callback_query(F.data == "bcast_photo")
async def cb_bcast_photo(cb: CallbackQuery, state: FSMContext):
    if not has_admin(cb.from_user.id,"superadmin"): await cb.answer("✗",show_alert=True); return
    await state.update_data(bcast_target="all")
    await state.set_state(SAdmin.bcast_photo)
    await cb.message.edit_text(hdr("🖼","𝗣𝗛𝗢𝗧𝗢 𝗕𝗥𝗢𝗔𝗗𝗖𝗔𝗦𝗧")+"\n\n  Send a photo:", reply_markup=K.back_admin())
    await cb.answer()


@rt.message(SAdmin.bcast_photo, F.photo)
async def p_bcast_photo_file(msg: Message, state: FSMContext):
    await state.update_data(bcast_photo_id=msg.photo[-1].file_id)
    await state.set_state(SAdmin.bcast_photo_cap)
    await msg.answer("  ✔ Photo received!\n  Type caption:", reply_markup=K.back_admin())


@rt.message(SAdmin.bcast_photo, F.text)
async def p_bcast_photo_url(msg: Message, state: FSMContext):
    await state.update_data(bcast_photo_id=msg.text.strip())
    await state.set_state(SAdmin.bcast_photo_cap)
    await msg.answer("  ✔ URL set!\n  Type caption:", reply_markup=K.back_admin())


@rt.message(SAdmin.bcast_photo_cap)
async def p_bcast_photo_cap(msg: Message, state: FSMContext):
    d = await state.get_data(); await state.clear()
    caption = msg.text.strip()
    photo   = d.get("bcast_photo_id","")
    target  = d.get("bcast_target","all")
    targets = VIP_USERS if target=="vip" else ALLOWED_USERS
    bc_cap  = f"{B}\n  📢  𝗕𝗥𝗢𝗔𝗗𝗖𝗔𝗦𝗧\n{B}\n\n  {caption}"
    s=f=0
    for uid in list(targets):
        try: await bot.send_photo(uid, photo=photo, caption=bc_cap); s+=1
        except:
            try: await bot.send_message(uid, bc_cap); s+=1
            except: f+=1
        await asyncio.sleep(0.05)
    admin_log(msg.from_user.id,f"BCAST_PHOTO s={s} f={f}")
    add_broadcast_history(msg.from_user.id,"photo",caption,s,f)
    await msg.answer(f"  📢 Done!\n  ✔ {s} sent  ✗ {f} failed", reply_markup=K.back_admin())


@rt.message(SAdmin.bcast_text)
async def p_bcast_text(msg: Message, state: FSMContext):
    d = await state.get_data(); await state.clear()
    txt     = msg.text.strip()
    target  = d.get("bcast_target","all")
    targets = VIP_USERS if target=="vip" else ALLOWED_USERS
    bc = f"{B}\n  📢  𝗕𝗥𝗢𝗔𝗗𝗖𝗔𝗦𝗧\n{B}\n\n  {txt}"
    s=f=0
    for uid in list(targets):
        try: await bot.send_message(uid, bc); s+=1
        except: f+=1
        await asyncio.sleep(0.05)
    admin_log(msg.from_user.id,f"BCAST_TEXT s={s} f={f}")
    add_broadcast_history(msg.from_user.id,"text",txt,s,f)
    await msg.answer(f"  📢 Done!\n  ✔ {s} sent  ✗ {f} failed", reply_markup=K.back_admin())


# ── Owner: Add/Remove Admin, Maintenance, Reset ───

@rt.callback_query(F.data == "a_addadm")
async def cb_a_addadm(cb: CallbackQuery, state: FSMContext):
    if not has_admin(cb.from_user.id,"owner"): await cb.answer("✗ Owner only!",show_alert=True); return
    await state.set_state(SAdmin.addadm_id)
    await cb.message.edit_text(hdr("➕","𝗔𝗗𝗗 𝗔𝗗𝗠𝗜𝗡")+"\n\n  Enter user ID:", reply_markup=K.back_admin())
    await cb.answer()


@rt.message(SAdmin.addadm_id)
async def p_addadm_id(msg: Message, state: FSMContext):
    try: uid = int(msg.text.strip())
    except: await msg.answer("  ✗ Invalid ID."); return
    await state.update_data(target=uid)
    await state.set_state(SAdmin.addadm_lv)
    await msg.answer(
        f"  Select role for <code>{uid}</code>:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👮 Moderator",  callback_data="sl_moderator")],
            [InlineKeyboardButton(text="🛡 Admin",      callback_data="sl_admin")],
            [InlineKeyboardButton(text="⭐ Super Admin", callback_data="sl_superadmin")],
            [InlineKeyboardButton(text="◂ Cancel",      callback_data="admin_menu")],
        ]))


@rt.callback_query(F.data.startswith("sl_"))
async def cb_sl(cb: CallbackQuery, state: FSMContext):
    role = cb.data[3:]
    d    = await state.get_data(); t = d.get("target")
    if not t: await cb.answer("✗"); await state.clear(); return
    await state.clear()
    store_add_admin(t, role)
    admin_log(cb.from_user.id,f"ADD_ADMIN_{role}",str(t))
    try: await cb.message.edit_text(f"  ✔ <code>{t}</code> → {role}", reply_markup=K.back_admin())
    except: pass
    try: await bot.send_message(t, f"  🎉 You are now {role}!\n  Use /admin")
    except: pass
    await cb.answer()


@rt.callback_query(F.data == "a_rmadm")
async def cb_a_rmadm(cb: CallbackQuery, state: FSMContext):
    if not has_admin(cb.from_user.id,"owner"): await cb.answer("✗ Owner only!",show_alert=True); return
    await state.set_state(SAdmin.rmadm)
    await cb.message.edit_text(hdr("➖","𝗥𝗘𝗠 𝗔𝗗𝗠𝗜𝗡")+"\n\n  Enter user ID:", reply_markup=K.back_admin())
    await cb.answer()


@rt.message(SAdmin.rmadm)
async def p_rmadm(msg: Message, state: FSMContext):
    try: uid = int(msg.text.strip())
    except: await msg.answer("  ✗ Invalid ID."); return
    if uid == OWNER_ID: await msg.answer("  ✗ Cannot remove owner!", reply_markup=K.back_admin()); await state.clear(); return
    await state.clear()
    store_remove_admin(uid); admin_log(msg.from_user.id,"REM_ADMIN",str(uid))
    await msg.answer(f"  ✔ <code>{uid}</code> demoted!", reply_markup=K.back_admin())


@rt.callback_query(F.data == "a_maint")
async def cb_a_maint(cb: CallbackQuery):
    if not has_admin(cb.from_user.id,"owner"): await cb.answer("✗ Owner only!",show_alert=True); return
    STORE["maintenance"] = not STORE.get("maintenance",False)
    save_store(STORE)
    st = "🔴 ON" if STORE["maintenance"] else "🟢 OFF"
    admin_log(cb.from_user.id,f"MAINTENANCE_{st}")
    await cb.message.edit_text(f"  🔧 Maintenance: {st}", reply_markup=K.back_admin())
    await cb.answer()


@rt.callback_query(F.data == "a_reset")
async def cb_a_reset(cb: CallbackQuery):
    if not has_admin(cb.from_user.id,"owner"): await cb.answer("✗ Owner only!",show_alert=True); return
    STORE["stats"] = {"total_logins":0,"total_actions":0,"total_unlocks":0}
    STORE["daily_stats"] = {}
    save_store(STORE); admin_log(cb.from_user.id,"RESET_STATS")
    await cb.message.edit_text("  ✔ Stats reset!", reply_markup=K.back_admin())
    await cb.answer()


# ═══════════════════════════════════════════
#  📋 COMMANDS
# ═══════════════════════════════════════════

@rt.message(Command("help"))
async def cmd_help(msg: Message):
    await msg.answer(
        f"{B}\n  ❓  𝗛𝗘𝗟𝗣\n{B}\n\n"
        "  /start  — Start bot\n"
        "  /admin  — Admin panel\n"
        "  /bulkadd — Bulk add users (admin)\n"
        "  /help   — Help\n"
        "  /status — Status\n"
        "  /ping   — Ping\n\n"
    )


@rt.message(Command("status"))
async def cmd_status(msg: Message):
    uid   = msg.from_user.id
    td    = nuker.get_token_data(uid)
    up    = time.strftime('%H:%M:%S', time.gmtime(time.time()-START_TIME))
    maint = "🔴" if is_maintenance() else "🟢"
    txt   = f"{B}\n  🤖  𝗦𝗧𝗔𝗧𝗨𝗦\n{B}\n\n"
    txt  += f"  Logged:  {'✔' if td else '✗'}\n"
    if td: txt += f"  Email:   {td.get('email','—')}\n"
    txt  += f"  Users:   {len(ALLOWED_USERS)}\n  Maint:   {maint}\n  Uptime:  {up}"
    await msg.answer(txt)


@rt.message(Command("ping"))
async def cmd_ping(msg: Message):
    t = time.time()
    m = await msg.answer("  🏓 ...")
    await m.edit_text(f"  🏓 Pong! {(time.time()-t)*1000:.0f}ms")


# ═══════════════════════════════════════════
#  🚀 MAIN
# ═══════════════════════════════════════════

async def main():
    global START_TIME
    START_TIME = time.time()

    log.info("━"*40)
    log.info("  🔥 AWIMEDANCPM TOOLS  🔥")
    log.info(f"  Owner:  {OWNER_ID}")
    log.info(f"  Users:  {len(ALLOWED_USERS)}")
    log.info(f"  Brotli: {'✔' if HAS_BROTLI else '✗ pip install brotli'}")
    log.info(f"  Crypto: {'✔' if HAS_CRYPTO else '✗ pip install pycryptodome'}")
    log.info("━"*40)

    await bot.set_my_commands([
        BotCommand(command="start",  description="🎮 Start"),
        BotCommand(command="admin",  description="👑 Admin"),
        BotCommand(command="bulkadd", description="📥 Bulk add users"),
        BotCommand(command="help",   description="❓ Help"),
        BotCommand(command="status", description="📊 Status"),
        BotCommand(command="ping",   description="🏓 Ping"),
    ])

    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Stopped.")
    except Exception as e:
        log.error(f"Fatal: {e}\n{traceback.format_exc()}")
