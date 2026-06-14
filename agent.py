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
from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
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

    messages = [{"role": "system", "content": system_prompt}]
    for turn in req.history:
        messages.append({"role": turn.role, "content": turn.content})
    messages.append({"role": "user", "content": req.message})

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
