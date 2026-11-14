from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

STUDIO_ROOT = Path(__file__).resolve().parent.parent

# Local dev: studio/.env, then the box secret file if the key is still missing.
load_dotenv(STUDIO_ROOT / ".env")
_secret = Path("/home/box/agent-data/secrets/maas-xyb2b.env")
if _secret.is_file() and not os.getenv("MAAS_KEY"):
    load_dotenv(_secret)
_oss_secret = Path("/home/box/agent-data/secrets/aliyun-oss-learn.env")
if _oss_secret.is_file() and not os.getenv("ALIYUN_ACCESS_KEY_ID") and not os.getenv("OSS_ACCESS_KEY_ID"):
    load_dotenv(_oss_secret)
_decision_secret = Path("/home/box/agent-data/secrets/openrouter-jev.env")
if _decision_secret.is_file() and not os.getenv("DECISION_API_KEY") and not os.getenv("OPENROUTER_API_KEY"):
    load_dotenv(_decision_secret)

MAAS_BASE = os.getenv("MAAS_BASE", "https://api.xytoken.xyb2b.com/v1").rstrip("/")
MAAS_KEY = os.getenv("MAAS_KEY", "")

ACCESS_USERNAME = os.getenv("ACCESS_USERNAME", "")
ACCESS_PASSWORD = os.getenv("ACCESS_PASSWORD", "")
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
SESSION_SECURE = os.getenv("SESSION_SECURE", "0").strip().lower() in {"1", "true", "yes"}
SESSION_COOKIE = "cad_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 14

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8780"))
CAD_DATA_DIR = os.getenv("CAD_DATA_DIR", "").strip()
CAD_LIBRARY_INDEX = os.getenv("CAD_LIBRARY_INDEX", "").strip()

WEB_DIR = STUDIO_ROOT / "web"
VAR_DIR = STUDIO_ROOT / "var"
CACHE_DIR = VAR_DIR / "preview-cache"
SCENE_DIR = VAR_DIR / "scene-cache"
PROJECTS_FILE = Path(__file__).resolve().parent / "projects.json"

_LINUX_CJK_FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
_LINUX_CJK_FONT_BOLD = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
_WINDOWS_CJK_FONTS = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
)


def _pick_cjk_font(env_name: str, linux_default: str, *, skip: str = "") -> str:
    """Env override first, then the first existing platform font, else ""."""
    override = os.getenv(env_name, "").strip()
    if override:
        return override
    candidates = [linux_default]
    if os.name == "nt":
        candidates = [p for p in _WINDOWS_CJK_FONTS if p != skip]
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    return ""


CJK_FONT = _pick_cjk_font("CAD_CJK_FONT", _LINUX_CJK_FONT)
CJK_FONT_BOLD = _pick_cjk_font("CAD_CJK_FONT_BOLD", _LINUX_CJK_FONT_BOLD, skip=CJK_FONT)

DEFAULT_MODEL = "Tifa-6.1-sol"
MODELS = (
    {
        "id": "Tifa-6.1-sol",
        "label": "Tifa-6.1-sol",
        "image": "fallback",
        "note": "默认。若拒绝图片则改为纯文字。",
    },
    {
        "id": "Tifa-6-astra",
        "label": "Tifa-6-astra",
        "image": "yes",
        "note": "支持文字加图片。",
    },
)
ALLOWED_MODELS = {m["id"] for m in MODELS}


def maas_configured() -> bool:
    return bool(MAAS_BASE and MAAS_KEY)


def auth_configured() -> bool:
    return bool(ACCESS_USERNAME and ACCESS_PASSWORD and SESSION_SECRET)


# OpenRouter Decisions API (Jev) for off-topic routing. Key stays on the server.
DECISION_API_KEY = os.getenv("DECISION_API_KEY") or os.getenv("OPENROUTER_API_KEY") or ""
DECISION_BASE_URL = (os.getenv("DECISION_BASE_URL") or "https://openrouter.ai/api/alpha/decisions").rstrip("/")
DECISION_MODEL = os.getenv("DECISION_MODEL") or "typesafe/jev-1.14"
DECISION_API_PROTOCOL = os.getenv("DECISION_API_PROTOCOL") or "openrouter-decisions"
try:
    DECISION_REJECT_THRESHOLD = float(os.getenv("DECISION_REJECT_THRESHOLD") or "0.65")
except ValueError:
    DECISION_REJECT_THRESHOLD = 0.65


def decision_configured() -> bool:
    return bool(DECISION_API_KEY and DECISION_BASE_URL and DECISION_MODEL)



# Object storage (optional). Prefer CAD_OSS_* then ALIYUN_* / OSS_*.
OSS_ACCESS_KEY_ID = os.getenv("CAD_OSS_ACCESS_KEY_ID") or os.getenv("ALIYUN_ACCESS_KEY_ID") or os.getenv("OSS_ACCESS_KEY_ID") or ""
OSS_ACCESS_KEY_SECRET = os.getenv("CAD_OSS_ACCESS_KEY_SECRET") or os.getenv("ALIYUN_ACCESS_KEY_SECRET") or os.getenv("OSS_ACCESS_KEY_SECRET") or ""
OSS_REGION = os.getenv("CAD_OSS_REGION") or os.getenv("ALIYUN_OSS_REGION") or os.getenv("OSS_REGION") or "oss-cn-hangzhou"
OSS_BUCKET = os.getenv("CAD_OSS_BUCKET") or os.getenv("ALIYUN_OSS_BUCKET") or os.getenv("OSS_BUCKET") or ""
OSS_PREFIX = (os.getenv("CAD_OSS_PREFIX") or os.getenv("ALIYUN_OSS_PREFIX") or os.getenv("OSS_PREFIX") or "chathub/chat-uploads").strip().strip("/")
OSS_ENDPOINT = os.getenv("CAD_OSS_ENDPOINT") or os.getenv("OSS_ENDPOINT") or f"https://{OSS_REGION}.aliyuncs.com"
STUDIO_OSS_PREFIX = f"{OSS_PREFIX}/dailywork/cad-studio"


def oss_configured() -> bool:
    return bool(OSS_ACCESS_KEY_ID and OSS_ACCESS_KEY_SECRET and OSS_BUCKET)


def db_path() -> Path:
    base = Path(CAD_DATA_DIR) if CAD_DATA_DIR else (STUDIO_ROOT / "data")
    base.mkdir(parents=True, exist_ok=True)
    return base / "tutan.sqlite"
