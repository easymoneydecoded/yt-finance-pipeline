"""
================================================================================
 secret_store.py - Tokens renovables (Instagram, TikTok) guardados cifrados
================================================================================

 Instagram y TikTok renuevan sus tokens (60 dias / 24 h + refresh). GitHub
 Actions no puede reescribir sus propios secrets, asi que la version
 renovada se guarda cifrada (Fernet/AES) en history/social_tokens.enc y se
 hace commit junto al historial.

 Clave: SOCIAL_TOKEN_KEY si existe; si no, se deriva de YOUTUBE_CLIENT_SECRET
 (asi no hace falta un secret mas). Si la clave cambia, el fichero no se
 puede leer y se vuelve a los tokens iniciales de los secrets.
================================================================================
"""

import base64
import hashlib
import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

STORE_FILE = Path("history/social_tokens.enc")


def _fernet():
    from cryptography.fernet import Fernet
    seed = os.getenv("SOCIAL_TOKEN_KEY") or os.getenv("YOUTUBE_CLIENT_SECRET")
    if not seed:
        return None
    key = base64.urlsafe_b64encode(hashlib.sha256(("ticker-press-social:" + seed).encode()).digest())
    return Fernet(key)


def load() -> dict:
    f = _fernet()
    if not f or not STORE_FILE.exists():
        return {}
    try:
        return json.loads(f.decrypt(STORE_FILE.read_bytes()))
    except Exception as e:
        print(f"   ! no pude leer los tokens guardados ({type(e).__name__}); uso los de los secrets")
        return {}


def save(data: dict) -> None:
    f = _fernet()
    if not f:
        print("   ! sin clave para cifrar tokens, no se guardan")
        return
    STORE_FILE.parent.mkdir(exist_ok=True)
    STORE_FILE.write_bytes(f.encrypt(json.dumps(data).encode()))


def get(name: str) -> dict:
    return load().get(name, {})


def put(name: str, value: dict) -> None:
    data = load()
    data[name] = value
    save(data)
