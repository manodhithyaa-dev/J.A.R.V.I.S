# J.A.R.V.I.S. — Personal AI Assistant

<p align="center">
  <strong>Your Personal Intelligence System</strong>
</p>

<p align="center">
  A modular, multi-provider AI assistant powered by FastAPI, LiteLLM, React, and Ollama.
</p>

---

## Overview

**J.A.R.V.I.S.** is a personal AI assistant designed to provide a unified interface for interacting with multiple Large Language Models (LLMs).

It supports local and cloud-based model providers through LiteLLM, allowing users to switch between models without changing the frontend.

The system includes persistent conversation memory, session management, Markdown rendering, voice interaction, and a React-based dashboard.

## Features

- **Multi-Model Support** — Switch between local and cloud LLMs through a unified interface.
- **Multi-Provider Integration** — Connect different providers using LiteLLM.
- **Local AI Inference** — Integrate locally hosted models through Ollama.
- **Persistent Conversation Memory** — Store conversations locally in JSON and human-readable TXT formats.
- **Session Management** — Create, restore, list, and delete conversations.
- **Automatic Session Titles** — Generate descriptive conversation titles.
- **Markdown Rendering** — Display formatted responses, code blocks, tables, lists, and links.
- **Voice Input** — Interact using browser speech recognition.
- **Voice Output** — Listen to assistant responses using browser speech synthesis.
- **Model Selection** — Dynamically retrieve available models from the backend.
- **REST API** — Communicate with the LLM service through FastAPI endpoints.

## Architecture

```text
                  USER
                    |
                    v
          REACT + VITE FRONTEND
                    |
          +---------+---------+
          |                   |
          v                   v
     Chat Interface      Voice Interface
          |
          v
       FASTAPI
          |
          v
       LITELLM
          |
    +-----+----------------+
    |          |           |
    v          v           v
  Ollama    OpenRouter   Gemini / OpenAI
    |
    v
 Local LLM Models

          FASTAPI
             |
             v
    CONVERSATION MEMORY
             |
       +-----+-----+
       |           |
       v           v
    JSON Files   TXT Files
```

## Technology Stack

| Component | Technologies |
|---|---|
| Frontend | React, TypeScript, Vite |
| UI | CSS, Framer Motion, Lucide React |
| Markdown | React Markdown, Remark GFM |
| Backend | Python, FastAPI |
| LLM Routing | LiteLLM |
| Local Inference | Ollama |
| Memory | JSON, TXT, Python |
| Voice | Web Speech API |

## Project Structure

```text
Jarvis/
│
├── frontend/
│   ├── src/
│   │   ├── lib/
│   │   │   └── api.ts
│   │   ├── App.tsx
│   │   ├── main.tsx
│   │   └── styles.css
│   │
│   ├── .env.example
│   ├── index.html
│   ├── package.json
│   ├── package-lock.json
│   └── vite.config.ts
│
├── llm/
│   ├── main.py
│   ├── cli.py
│   ├── models.py
│   ├── memory.py
│   ├── prompt.md
│   └── memory/
│       └── sessions/
│
├── requirements.txt
└── .gitignore
```

## Installation

### Prerequisites

- Python 3.10+
- Node.js and npm
- Ollama (for local model inference)
- Git

### 1. Clone the repository

```bash
git clone https://github.com/manodhithyaa-dev/Jarvis.git

cd Jarvis
```

### 2. Set up the backend

Create and activate a Python virtual environment:

```bash
cd llm

python -m venv .venv

source .venv/bin/activate
```

Install dependencies:

```bash
cd ..

pip install -r requirements.txt
```

Configure your backend environment variables in `llm/.env`.

Example:

```env
DEFAULT_MODEL_GROUP=local
DEFAULT_MODEL_ID=gemma4:e4b

OLLAMA_API_BASE=http://localhost:11434

OPENROUTER_API_KEY=your_openrouter_api_key
GEMINI_API_KEY=your_gemini_api_key
OPENAI_API_KEY=your_openai_api_key

CORS_ORIGINS=http://localhost:5173
```

Only configure credentials for providers you intend to use.

### 3. Set up Ollama

Install Ollama and pull a model available in your model registry.

Example:

```bash
ollama pull gemma3:4b
```

Make sure the Ollama service is running.

### 4. Start the backend

From the repository root:

```bash
cd llm

source .venv/bin/activate

uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Backend API:

```text
http://localhost:8000
```

API documentation:

```text
http://localhost:8000/docs
```

### 5. Set up the frontend

Open another terminal:

```bash
cd Jarvis/frontend

npm install
```

Configure `frontend/.env`:

```env
VITE_API_BASE_URL=http://localhost:8000

VITE_CHAT_ENDPOINT=/api/chat
VITE_MODELS_ENDPOINT=/api/models
VITE_HEALTH_ENDPOINT=/api/health
VITE_SESSIONS_ENDPOINT=/api/sessions
```

Start the development server:

```bash
npm run dev
```

Frontend:

```text
http://localhost:5173
```

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| GET | `/` | Backend service information |
| GET | `/api/health` | Check backend availability |
| GET | `/api/models` | Retrieve available models |
| POST | `/api/chat` | Send a chat request |
| GET | `/api/sessions` | List conversation sessions |
| GET | `/api/sessions/{session_id}` | Retrieve a conversation |
| DELETE | `/api/sessions/{session_id}` | Delete a conversation |

### Chat Request

```json
{
  "message": "Explain neural networks",
  "model": "local::gemma4:e4b",
  "session_id": null
}
```

### Chat Response

```json
{
  "reply": "A neural network is...",
  "model": "local::gemma4:e4b",
  "provider": "ollama",
  "session_id": "generated-session-id",
  "title": "Understanding Neural Networks"
}
```

## Conversation Memory

J.A.R.V.I.S. uses local file-based persistent conversation storage.

Each conversation is stored in two formats:

```text
llm/
└── memory/
    └── sessions/
        ├── session_id.json
        └── session_id.txt
```

- **JSON:** Structured conversation history used to restore sessions.
- **TXT:** Human-readable conversation transcript.
- **Session ID:** Identifies and maintains individual conversations.

The backend automatically restores previous messages when a session ID is provided.

## Security Notes

- Keep API keys and credentials in backend environment files.
- Never expose provider API keys in frontend code.
- Do not commit `.env` files.
- Conversation memory may contain private information; keep it out of public repositories.
- Configure CORS appropriately before exposing the backend to external networks.

## Current Development

J.A.R.V.I.S. is under active development.

The current implementation focuses on:

- Unified LLM access.
- Persistent conversation memory.
- Frontend and backend integration.
- Voice-enabled interaction.
- Model selection and session management.

Additional capabilities and architectural improvements may be introduced as development progresses.

## Contributing

Contributions, suggestions, and improvements are welcome.

1. Fork the repository.
2. Create a feature branch.
3. Implement your changes.
4. Commit your changes with descriptive messages.
5. Open a pull request.

## License

Add a license to the repository before distributing or reusing the project. Until then, no open-source license is granted by this README.

---

<p align="center">
  <strong>J.A.R.V.I.S.</strong><br/>
  Personal Intelligence. Your Command.
</p>