"""
SO Task Intelligence Agent
==========================
A small FastAPI service that wraps an OpenAI-compatible LLM and uses the
contents of CLAUDE.md as its system prompt. Built for the Sales Operations
(SO) team to turn raw task-tracking data into Vietnamese daily/weekly briefings.

Endpoints
---------
- GET  /health  -> {"status": "ok"}
- POST /chat    -> send a message (optionally with history) and get the reply
"""

from __future__ import annotations

import logging
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Tuple

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from openai import OpenAI, OpenAIError
from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
logger = logging.getLogger("so-agent")

# Path to the system prompt file. Defaults to CLAUDE.md next to this script.
SYSTEM_PROMPT_PATH = Path(
    os.getenv("SYSTEM_PROMPT_PATH", Path(__file__).parent / "CLAUDE.md")
)

# OpenAI-compatible client settings.
# Primary names target GreenNode MaaS; the OPENAI_* names are kept as fallbacks
# so the service still works against a plain OpenAI endpoint.
LLM_API_KEY = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL")  # GreenNode MaaS base URL
MODEL = os.getenv("MODEL_NAME") or os.getenv("MODEL", "minimax/minimax-m2.5")
TEMPERATURE = float(os.getenv("TEMPERATURE", "0.3"))
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "2048"))

# Google Sheets auto-ingest settings.
GSHEET_TIMEOUT = float(os.getenv("GSHEET_TIMEOUT", "20"))
GSHEET_MAX_CHARS = int(os.getenv("GSHEET_MAX_CHARS", "60000"))  # cap to protect context window
GSHEET_MAX_LINKS = int(os.getenv("GSHEET_MAX_LINKS", "3"))      # max sheets read per message

# Matches a Google Sheets URL and captures the identifier. This is either a
# normal spreadsheet ID (share link: /d/{ID}/edit) or a published-to-web token
# (/d/e/{TOKEN}/pub...). The captured group keeps the "e/" prefix for the latter.
_GSHEET_RE = re.compile(
    r"https?://docs\.google\.com/spreadsheets/d/(e/[A-Za-z0-9_-]+|[A-Za-z0-9_-]+)[^\s)]*",
    re.IGNORECASE,
)


@lru_cache(maxsize=1)
def load_system_prompt() -> str:
    """Read CLAUDE.md once and cache it as the system prompt."""
    try:
        text = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()
        if not text:
            raise ValueError("System prompt file is empty")
        logger.info("Loaded system prompt from %s (%d chars)", SYSTEM_PROMPT_PATH, len(text))
        return text
    except FileNotFoundError:
        logger.error("System prompt file not found at %s", SYSTEM_PROMPT_PATH)
        raise


@lru_cache(maxsize=1)
def get_client() -> OpenAI:
    """Build a single shared OpenAI-compatible client."""
    if not LLM_API_KEY:
        logger.warning("LLM_API_KEY is not set — /chat calls will fail until it is configured.")
    kwargs = {"api_key": LLM_API_KEY or "missing-key"}
    if OPENAI_BASE_URL:
        kwargs["base_url"] = OPENAI_BASE_URL
    return OpenAI(**kwargs)


# --------------------------------------------------------------------------- #
# Google Sheets auto-ingest
# --------------------------------------------------------------------------- #

def _csv_export_url(sheet_id: str, gid: Optional[str]) -> str:
    """Build the primary CSV-export URL for a public Google Sheet (optionally a tab)."""
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
    if gid:
        url += f"&gid={gid}"
    return url


def _csv_candidate_urls(sheet_id: str, gid: Optional[str]) -> List[str]:
    """CSV URLs to try in order. Different public-sharing configurations expose
    CSV on different endpoints, so we try several before giving up."""
    # Published-to-web link (/d/e/{TOKEN}/pub...): use the /pub?output=csv form.
    if sheet_id.startswith("e/"):
        pub = f"https://docs.google.com/spreadsheets/d/{sheet_id}/pub?output=csv"
        if gid:
            pub += f"&gid={gid}"
        return [pub]

    # Normal share link: try /export?format=csv, then the gviz endpoint.
    urls = [_csv_export_url(sheet_id, gid)]
    gviz = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv"
    if gid:
        gviz += f"&gid={gid}"
    urls.append(gviz)
    return urls


def _find_sheet_links(text: str) -> List[Tuple[str, str, Optional[str]]]:
    """Find Google Sheets links in text.

    Returns a list of (original_url, sheet_id, gid) tuples, de-duplicated by
    (sheet_id, gid) and capped at GSHEET_MAX_LINKS.
    """
    found: List[Tuple[str, str, Optional[str]]] = []
    seen = set()
    for match in _GSHEET_RE.finditer(text):
        original = match.group(0)
        sheet_id = match.group(1)
        gid_match = re.search(r"[#?&]gid=(\d+)", original)
        gid = gid_match.group(1) if gid_match else None
        key = (sheet_id, gid)
        if key in seen:
            continue
        seen.add(key)
        found.append((original, sheet_id, gid))
        if len(found) >= GSHEET_MAX_LINKS:
            break
    return found


def _fetch_sheet_csv(sheet_id: str, gid: Optional[str]) -> str:
    """Fetch a public Google Sheet as CSV text.

    Raises ValueError with a Vietnamese, user-facing reason when the sheet
    cannot be read (private, not shared, deleted, etc.).
    """
    last_error = "không đọc được"
    for url in _csv_candidate_urls(sheet_id, gid):
        try:
            resp = httpx.get(url, follow_redirects=True, timeout=GSHEET_TIMEOUT)
        except httpx.HTTPError as exc:
            last_error = f"không kết nối được tới Google ({exc})"
            continue

        # A private/unshared sheet redirects to a Google login/HTML page instead of CSV.
        content_type = resp.headers.get("content-type", "")
        final_host = resp.url.host or ""
        if resp.status_code != 200 or "text/html" in content_type or "accounts.google.com" in final_host:
            last_error = (
                'sheet chưa được chia sẻ công khai — hãy đặt quyền "Bất kỳ ai có đường '
                'liên kết đều có thể xem" (Anyone with the link can view)'
            )
            continue

        if not resp.text.strip():
            last_error = "sheet rỗng hoặc không có dữ liệu"
            continue

        return resp.text

    raise ValueError(last_error)


def _truncate_csv(csv_text: str) -> Tuple[str, int, bool]:
    """Trim CSV to GSHEET_MAX_CHARS on a row boundary.

    Returns (possibly_trimmed_text, total_row_count, was_truncated).
    """
    lines = csv_text.splitlines()
    total_rows = max(len(lines) - 1, 0)  # exclude header from the "rows" count
    if len(csv_text) <= GSHEET_MAX_CHARS:
        return csv_text, total_rows, False

    kept: List[str] = []
    size = 0
    for line in lines:
        size += len(line) + 1
        if size > GSHEET_MAX_CHARS:
            break
        kept.append(line)
    return "\n".join(kept), total_rows, True


def augment_message_with_sheets(message: str) -> str:
    """If the message contains Google Sheets links, fetch each sheet and append
    its CSV data so the LLM can analyze it. Failures are surfaced inline as
    short Vietnamese notes rather than raising."""
    links = _find_sheet_links(message)
    if not links:
        return message

    blocks: List[str] = []
    for original, sheet_id, gid in links:
        try:
            csv_text = _fetch_sheet_csv(sheet_id, gid)
            data, total_rows, truncated = _truncate_csv(csv_text)
            note = (
                f" (đã rút gọn, hiển thị phần đầu trong tổng {total_rows} dòng)"
                if truncated
                else f" ({total_rows} dòng)"
            )
            logger.info("Read Google Sheet %s gid=%s — %d rows%s", sheet_id, gid,
                        total_rows, " [truncated]" if truncated else "")
            blocks.append(
                f"--- Dữ liệu đọc tự động từ Google Sheets{note}\n"
                f"Nguồn: {original}\n"
                f"```csv\n{data}\n```\n"
                f"--- Hết dữ liệu Google Sheets ---"
            )
        except ValueError as exc:
            logger.warning("Could not read Google Sheet %s: %s", sheet_id, exc)
            blocks.append(
                f"[Không đọc được Google Sheets: {original} — {exc}.]"
            )

    return message + "\n\n" + "\n\n".join(blocks)


# --------------------------------------------------------------------------- #
# Request / response models
# --------------------------------------------------------------------------- #

class Message(BaseModel):
    role: str = Field(..., description="One of: user, assistant")
    content: str


class ChatRequest(BaseModel):
    message: str = Field(..., description="The user's latest message / pasted data")
    history: List[Message] = Field(
        default_factory=list,
        description="Optional prior turns to keep conversational context",
    )
    model: Optional[str] = Field(default=None, description="Override the default model")
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)


class ChatResponse(BaseModel):
    reply: str
    model: str
    usage: Optional[dict] = None


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #

app = FastAPI(
    title="SO Task Intelligence Agent",
    description="AI assistant for the Sales Operations team. Produces Vietnamese "
    "daily/weekly task briefings from raw task-tracking data.",
    version="1.0.0",
)

# Allow browser-based chat clients (e.g. the single-file chat page) to call /chat
# from any origin. CORS_ALLOW_ORIGINS can narrow this to specific origins (comma-separated).
_allow_origins = [
    o.strip() for o in os.getenv("CORS_ALLOW_ORIGINS", "*").split(",") if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allow_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    # Fail fast if the system prompt is missing.
    load_system_prompt()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    system_prompt = load_system_prompt()

    # If the user pasted a public Google Sheets link, read its data and embed it.
    user_message = augment_message_with_sheets(req.message)

    messages = [{"role": "system", "content": system_prompt}]
    for turn in req.history:
        messages.append({"role": turn.role, "content": turn.content})
    messages.append({"role": "user", "content": user_message})

    client = get_client()
    model = req.model or MODEL
    temperature = req.temperature if req.temperature is not None else TEMPERATURE

    try:
        completion = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=MAX_TOKENS,
        )
    except OpenAIError as exc:
        logger.exception("LLM call failed")
        raise HTTPException(status_code=502, detail=f"LLM call failed: {exc}") from exc
    except UnicodeEncodeError as exc:
        # Almost always a non-ASCII API key (e.g. the placeholder still in .env)
        logger.exception("Invalid API key — contains non-ASCII characters")
        raise HTTPException(
            status_code=500,
            detail="LLM_API_KEY không hợp lệ (chứa ký tự không phải ASCII). "
            "Hãy thay bằng API key thật từ email BTC trong file .env.",
        ) from exc
    except Exception as exc:  # noqa: BLE001 - surface any other failure cleanly
        logger.exception("Unexpected error during LLM call")
        raise HTTPException(status_code=502, detail=f"LLM call failed: {exc}") from exc

    reply = completion.choices[0].message.content or ""
    usage = completion.usage.model_dump() if completion.usage else None
    return ChatResponse(reply=reply, model=model, usage=usage)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "agent:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=bool(os.getenv("RELOAD", "")),
    )
