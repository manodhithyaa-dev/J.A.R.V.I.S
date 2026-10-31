import os
from pathlib import Path
from typing import Literal

import litellm

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from memory import ConversationMemory, DEFAULT_TITLE, now
from models import MODEL_REGISTRY


# =====================================================
# INITIALIZATION
# =====================================================

BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")

PROMPT_FILE = BASE_DIR / "prompt.md"

if not PROMPT_FILE.exists():
    raise FileNotFoundError("prompt.md not found!")

SYSTEM_PROMPT = PROMPT_FILE.read_text(encoding="utf-8")

DEFAULT_GROUP = os.getenv("DEFAULT_MODEL_GROUP", "local")
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL_ID", "gemma4:e4b")

# Local conversation memory (stored in ./memory/sessions by default).
memory = ConversationMemory(os.getenv("MEMORY_DIR", BASE_DIR / "memory"))

app = FastAPI(
    title="J.A.R.V.I.S. LLM API",
    description="LLM inference service powered by LiteLLM",
    version="1.1.0"
)

# CORS
origins = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS",
        "http://localhost:5173"
    ).split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)


# =====================================================
# REQUEST / RESPONSE SCHEMAS
# =====================================================

class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    message: str | None = None
    messages: list[ChatMessage] = Field(default_factory=list)
    model: str | None = None
    # Omit to start a new session; send it back to continue one.
    session_id: str | None = None


class ChatResponse(BaseModel):
    reply: str
    model: str
    provider: str
    session_id: str
    title: str


# =====================================================
# MODEL REGISTRY
# =====================================================

def get_available_models():

    available = []

    for group, provider_data in MODEL_REGISTRY.items():

        provider = provider_data["provider"]

        for model in provider_data["models"]:

            # Exclude specialized models from text chat.
            if model.get("type") in {
                "embedding",
                "audio",
                "vision"
            }:
                continue

            available.append({
                "id": f"{group}::{model['id']}",
                "name": model["name"],
                "provider": provider,
                "mode": (
                    "local" if provider == "ollama"
                    else "cloud"
                ),
                "group": group,
                "description": model.get("description", ""),
                "size": model.get("size")
            })

    return available


AVAILABLE_MODELS = get_available_models()


def find_model(model_reference):

    for model in AVAILABLE_MODELS:

        if model["id"] == model_reference:
            return model

    return None


# =====================================================
# LITELLM CONFIGURATION
# =====================================================

def get_litellm_config(model_reference):

    model_data = find_model(model_reference)

    if not model_data:
        raise ValueError("Model not found in registry.")

    provider = model_data["provider"]

    # Remove the registry group prefix.
    raw_model_id = model_data["id"].split("::", 1)[1]

    config = {}

    # ---------------------------------------------
    # OLLAMA
    # ---------------------------------------------

    if provider == "ollama":

        config["model"] = f"ollama_chat/{raw_model_id}"

        config["api_base"] = os.getenv(
            "OLLAMA_API_BASE",
            "http://localhost:11434"
        )

    # ---------------------------------------------
    # OPENROUTER
    # ---------------------------------------------

    elif provider == "openrouter":

        config["model"] = (
            raw_model_id
            if raw_model_id.startswith("openrouter/")
            else f"openrouter/{raw_model_id}"
        )

        config["api_key"] = os.getenv("OPENROUTER_API_KEY")

        config["api_base"] = os.getenv(
            "OPENROUTER_API_BASE",
            "https://openrouter.ai/api/v1"
        )

    # ---------------------------------------------
    # GEMINI
    # ---------------------------------------------

    elif provider == "gemini":

        config["model"] = (
            raw_model_id
            if raw_model_id.startswith("gemini/")
            else f"gemini/{raw_model_id}"
        )

        config["api_key"] = os.getenv("GEMINI_API_KEY")

        api_base = os.getenv("GEMINI_API_BASE")

        if api_base:
            config["api_base"] = api_base

    # ---------------------------------------------
    # OPENAI
    # ---------------------------------------------

    elif provider == "openai":

        config["model"] = (
            raw_model_id
            if raw_model_id.startswith("openai/")
            else f"openai/{raw_model_id}"
        )

        config["api_key"] = os.getenv("OPENAI_API_KEY")

        api_base = os.getenv("OPENAI_API_BASE")

        if api_base:
            config["api_base"] = api_base

    else:

        raise ValueError(
            f"Unsupported provider: {provider}"
        )

    # Remove empty configuration values.
    return {
        key: value
        for key, value in config.items()
        if value is not None and value != ""
    }


# =====================================================
# SESSION TITLE GENERATION (runs after the response)
# =====================================================

async def generate_session_title(
    session_id: str,
    model_reference: str,
    query: str,
    reply: str
):
    """Ask the model for a short title. Keeps the fallback title on failure."""

    try:

        config = get_litellm_config(model_reference)

        response = await litellm.acompletion(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You write short conversation titles. "
                        "Reply with ONLY a title of 3 to 6 words. "
                        "No quotes, no punctuation at the end."
                    )
                },
                {
                    "role": "user",
                    "content": (
                        f"User message:\n{query[:500]}\n\n"
                        f"Assistant reply:\n{reply[:500]}\n\n"
                        "Title:"
                    )
                }
            ],
            stream=False,
            max_tokens=24,
            **config
        )

        title = response.choices[0].message.content

        if isinstance(title, str) and title.strip():
            memory.set_title(session_id, title.splitlines()[0])

    except Exception as error:

        print(f"Title generation skipped: {error}")


# =====================================================
# HEALTH API
# =====================================================

@app.get("/api/health")
async def health():

    return {
        "status": "online",
        "service": "JARVIS LLM",
        "version": "1.1.0"
    }


# =====================================================
# MODELS API
# =====================================================

@app.get("/api/models")
async def models():

    return {
        "models": AVAILABLE_MODELS,
        "default": f"{DEFAULT_GROUP}::{DEFAULT_MODEL}"
    }


# =====================================================
# SESSIONS API
# =====================================================

@app.get("/api/sessions")
async def list_sessions():

    return {"sessions": memory.list_sessions()}


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str):

    try:
        session = memory.get_session(session_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))

    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")

    return session


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):

    try:
        deleted = memory.delete_session(session_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))

    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found.")

    return {"deleted": session_id}


# =====================================================
# CHAT API
# =====================================================

@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, background_tasks: BackgroundTasks):

    # Select model.
    model_reference = (
        request.model
        or f"{DEFAULT_GROUP}::{DEFAULT_MODEL}"
    )

    model_data = find_model(model_reference)

    if not model_data:

        raise HTTPException(
            status_code=400,
            detail="Invalid model selection."
        )

    # Load or create the session.
    try:
        session = memory.get_or_create_session(request.session_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))

    session_id = session["id"]

    # Build conversation.
    messages = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT
        }
    ]

    if request.messages:

        # Frontend supplied the full history.
        # Ignore client-supplied system prompts.
        for message in request.messages:

            if message.role == "system":
                continue

            messages.append({
                "role": message.role,
                "content": message.content
            })

    else:

        # Otherwise use the history stored on the server
        # plus the new message (if any).
        messages.extend(memory.get_messages(session_id))

        if request.message:

            messages.append({
                "role": "user",
                "content": request.message
            })

    user_messages = [
        message for message in messages
        if message["role"] == "user"
    ]

    if not user_messages:

        raise HTTPException(
            status_code=400,
            detail="Message cannot be empty."
        )

    query = user_messages[-1]["content"]
    query_timestamp = now()

    try:

        config = get_litellm_config(model_reference)

        response = await litellm.acompletion(
            messages=messages,
            stream=False,
            **config
        )

        reply = response.choices[0].message.content

        if not isinstance(reply, str):

            raise RuntimeError(
                "Model returned an unsupported response format."
            )

        # Persist the exchange locally.
        was_untitled = session["title"] == DEFAULT_TITLE

        session = memory.add_exchange(
            session_id,
            query,
            reply,
            query_timestamp
        )

        # Replace the fallback title with an LLM-written one (first exchange only).
        if was_untitled:

            background_tasks.add_task(
                generate_session_title,
                session_id,
                model_reference,
                query,
                reply
            )

        return ChatResponse(
            reply=reply,
            model=model_reference,
            provider=model_data["provider"],
            session_id=session_id,
            title=session["title"]
        )

    except ValueError as error:

        raise HTTPException(
            status_code=400,
            detail=str(error)
        )

    except Exception as error:

        print(f"LLM Error: {error}")

        raise HTTPException(
            status_code=502,
            detail="LLM provider failed to process the request."
        )


# =====================================================
# ROOT
# =====================================================

@app.get("/")
async def root():

    return {
        "message": "J.A.R.V.I.S. LLM Service",
        "status": "running",
        "docs": "/docs"
    }