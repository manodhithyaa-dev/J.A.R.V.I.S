"""
Local conversation memory for the J.A.R.V.I.S. LLM API.

Every session is stored on disk in two forms:

    memory/sessions/<session_id>.json   -> source of truth (used to reload history)
    memory/sessions/<session_id>.txt    -> human-readable transcript:

        # Title: Fixing CORS errors in FastAPI
        # Session: 3f2a...
        # Created: 2026-10-31 14:02:11

        User : How do I fix CORS errors? @2026-10-31 14:02:11
        J.A.R.V.I.S: Add CORSMiddleware to your app... @2026-10-31 14:02:14
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

USER_LABEL = "User :"
ASSISTANT_LABEL = "J.A.R.V.I.S:"
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_TITLE = "New Conversation"

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def now() -> str:
    return datetime.now().strftime(TIMESTAMP_FORMAT)


def fallback_title(text: str, max_words: int = 7, max_chars: int = 60) -> str:
    """Cheap title derived from the first user query (used until/unless an LLM title replaces it)."""
    cleaned = re.sub(r"\s+", " ", text).strip().strip("?!.:;,")
    if not cleaned:
        return DEFAULT_TITLE
    title = " ".join(cleaned.split(" ")[:max_words])
    if len(title) > max_chars:
        title = title[:max_chars].rstrip() + "…"
    return title[:1].upper() + title[1:]


class ConversationMemory:
    """Thread-safe, file-backed storage for chat sessions."""

    def __init__(self, base_dir: str | os.PathLike = "memory"):
        self.sessions_dir = Path(base_dir) / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Paths / helpers
    # ------------------------------------------------------------------

    @staticmethod
    def is_valid_id(session_id: str) -> bool:
        return bool(_SESSION_ID_RE.match(session_id or ""))

    def _json_path(self, session_id: str) -> Path:
        if not self.is_valid_id(session_id):
            raise ValueError("Invalid session id.")
        return self.sessions_dir / f"{session_id}.json"

    def _txt_path(self, session_id: str) -> Path:
        if not self.is_valid_id(session_id):
            raise ValueError("Invalid session id.")
        return self.sessions_dir / f"{session_id}.txt"

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)

    def _save(self, session: dict) -> None:
        sid = session["id"]
        session["updated_at"] = now()
        self._atomic_write(
            self._json_path(sid),
            json.dumps(session, ensure_ascii=False, indent=2),
        )
        self._atomic_write(self._txt_path(sid), self._render_transcript(session))

    @staticmethod
    def _render_transcript(session: dict) -> str:
        lines = [
            f"# Title: {session['title']}",
            f"# Session: {session['id']}",
            f"# Created: {session['created_at']}",
            "",
        ]
        for msg in session["messages"]:
            label = USER_LABEL if msg["role"] == "user" else ASSISTANT_LABEL
            lines.append(f"{label} {msg['content']} @{msg['timestamp']}")
            lines.append("")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def exists(self, session_id: str) -> bool:
        return self.is_valid_id(session_id) and self._json_path(session_id).exists()

    def create_session(
        self, session_id: Optional[str] = None, title: str = DEFAULT_TITLE
    ) -> dict:
        with self._lock:
            session_id = session_id or uuid.uuid4().hex
            session = {
                "id": session_id,
                "title": title,
                "created_at": now(),
                "updated_at": now(),
                "messages": [],
            }
            self._save(session)
            return session

    def get_session(self, session_id: str) -> Optional[dict]:
        with self._lock:
            path = self._json_path(session_id)
            if not path.exists():
                return None
            return json.loads(path.read_text(encoding="utf-8"))

    def get_or_create_session(self, session_id: Optional[str] = None) -> dict:
        with self._lock:
            if session_id:
                session = self.get_session(session_id)  # raises ValueError if invalid
                if session:
                    return session
            return self.create_session(session_id)

    def list_sessions(self) -> list[dict]:
        """Newest-activity first. Returns lightweight summaries (no messages)."""
        summaries = []
        with self._lock:
            for path in self.sessions_dir.glob("*.json"):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                summaries.append(
                    {
                        "id": data["id"],
                        "title": data.get("title", DEFAULT_TITLE),
                        "created_at": data.get("created_at"),
                        "updated_at": data.get("updated_at"),
                        "message_count": len(data.get("messages", [])),
                    }
                )
        summaries.sort(key=lambda s: s["updated_at"] or "", reverse=True)
        return summaries

    def delete_session(self, session_id: str) -> bool:
        with self._lock:
            json_path, txt_path = self._json_path(session_id), self._txt_path(session_id)
            existed = json_path.exists()
            json_path.unlink(missing_ok=True)
            txt_path.unlink(missing_ok=True)
            return existed

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    def get_messages(self, session_id: str) -> list[dict]:
        """History in the {'role', 'content'} shape LiteLLM expects."""
        session = self.get_session(session_id)
        if not session:
            return []
        return [{"role": m["role"], "content": m["content"]} for m in session["messages"]]

    def add_exchange(
        self,
        session_id: str,
        query: str,
        reply: str,
        query_timestamp: Optional[str] = None,
    ) -> dict:
        """Persist one user query + assistant reply pair. Auto-titles untitled sessions."""
        with self._lock:
            session = self.get_or_create_session(session_id)

            session["messages"].append(
                {"role": "user", "content": query, "timestamp": query_timestamp or now()}
            )
            session["messages"].append(
                {"role": "assistant", "content": reply, "timestamp": now()}
            )

            if session["title"] == DEFAULT_TITLE:
                session["title"] = fallback_title(query)

            self._save(session)
            return session

    def set_title(self, session_id: str, title: str) -> None:
        title = re.sub(r"\s+", " ", title).strip().strip("\"'`.")
        if not title:
            return
        with self._lock:
            session = self.get_session(session_id)
            if not session:
                return
            session["title"] = title[:80]
            self._save(session)