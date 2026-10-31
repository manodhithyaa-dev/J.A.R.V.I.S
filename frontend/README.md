# J.A.R.V.I.S. Web Interface

A static React + TypeScript + Vite frontend for an existing JARVIS agent backend. Includes a hybrid holographic/command-center interface, chat, model selection, browser speech input and speech output.

## Requirements
- Node.js 20+
- An existing backend reachable from the browser
- Backend endpoints (defaults below), or edit `src/lib/api.ts` to match your API contract

## Run
```bash
cp .env.example .env
npm install
npm run dev
```

Open the URL printed by Vite.

## Backend contract expected by this starter

Set `VITE_API_BASE_URL` in `.env`.

### `GET /api/health`
Any successful 2xx response means online.

### `GET /api/models`
Accepted response shapes:
```json
{"models":[{"id":"ollama/qwen3:8b","name":"Qwen 3 8B","provider":"Ollama","mode":"local"},{"id":"openai/gpt-4o-mini","name":"GPT-4o mini","provider":"OpenAI","mode":"cloud"}]}
```
Also accepts a plain array or `{"data":[...]}`.

### `POST /api/chat`
Request:
```json
{
  "message": "Hello",
  "messages": [{"role":"user","content":"Hello"}],
  "model": "ollama/qwen3:8b"
}
```
Accepted response:
```json
{"reply":"Hello. How can I help?"}
```
The client also recognizes `response`, `message`, `content`, and OpenAI-compatible `choices[0].message.content`.

### OpenAI-compatible mode
If your backend exposes `/v1/chat/completions`, set:
```env
VITE_OPENAI_COMPATIBLE=true
```
The frontend sends `{model,messages,stream:false}` to that route.

## Important integration notes
- This is a static frontend; it does not contain the agent, LiteLLM, model credentials, or provider keys.
- Do not put LiteLLM master keys or provider API keys in Vite environment variables. `VITE_*` values are shipped to the browser.
- Browser microphone input uses `SpeechRecognition` / `webkitSpeechRecognition`; support varies by browser and may use a browser-managed remote service. Run on localhost or HTTPS and grant microphone permission.
- Voice output uses the browser's `SpeechSynthesis` API.
- Model selection is passed to your backend as the `model` field. Your backend must validate/allowlist model IDs and route them through LiteLLM.
- The starter sends non-streaming requests. Streaming/SSE can be added once the backend's exact streaming protocol is known.
- The UI falls back to sample model options if `/api/models` is unreachable. This is for preview only; chat still requires a working backend.

## Build static assets
```bash
npm run build
```
Output is generated in `dist/`. Deploy that folder to Nginx, Caddy, or static hosting. Configure your reverse proxy/API CORS so the browser can reach the backend.
