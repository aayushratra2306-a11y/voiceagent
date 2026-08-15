# Voice Agent (Lite) — Architecture

## 1. System overview

```
                         ┌─────────────────────────┐
                         │        Browser           │
                         │  React SPA (single app)  │
                         │  - Settings page          │
                         │  - Voice Chat page        │
                         │  - Login page             │
                         └───────────┬───────────────┘
                                     │ REST (JSON) + WebRTC
                                     ▼
                         ┌─────────────────────────┐
                         │      FastAPI backend      │
                         │  (single process, async)  │
                         └──────┬──────────┬─────────┘
                                │          │
                 ┌──────────────┘          └──────────────┐
                 ▼                                          ▼
        ┌────────────────┐                        ┌──────────────────┐
        │   MongoDB       │                        │  Vector DB        │
        │  users, bots,   │                        │  (Chroma/Pinecone)│
        │  documents      │                        │  doc chunks +     │
        │                 │                        │  embeddings        │
        └────────────────┘                        └──────────────────┘
                                     │
                                     ▼
                  ┌─────────────────────────────────────┐
                  │        External providers             │
                  │  Deepgram (STT) · OpenRouter (LLM/     │
                  │  embeddings) · Cartesia (TTS)          │
                  └─────────────────────────────────────┘
```

One backend process, one frontend app, one DB, one vector store. No service mesh, no multi-region, no message queue — everything talks directly.

## 2. Components

| Component | Responsibility |
|---|---|
| **React SPA** | Login, bot settings form, doc upload, voice chat UI (mic button, live transcript, audio playback) |
| **FastAPI backend** | Auth, bot CRUD, doc ingestion trigger, RAG retrieval, voice pipeline host, WebRTC signaling |
| **MongoDB** | `users`, `bots` (config), `documents` (upload metadata/status) |
| **Vector DB** | Chunked doc embeddings, queried by similarity at answer-time |
| **Deepgram** | Streaming speech-to-text over WebSocket |
| **OpenRouter (or OpenAI direct)** | LLM completions + text embeddings |
| **Cartesia (or ElevenLabs)** | Streaming text-to-speech |
| **Pipecat** | Orchestrates the STT→LLM→TTS frame pipeline + WebRTC transport, runs inside the FastAPI process |

## 3. Request/data flows

### 3.1 Auth
```
Browser --POST /auth/login--> FastAPI --verify--> Mongo(users)
FastAPI --JWT--> Browser (stored in memory/localStorage, sent as Bearer token)
```

### 3.2 Bot settings
```
Browser --GET/PATCH /bots/{id}--> FastAPI --> Mongo(bots)
Fields: name, system_prompt, voice_id, llm_model, stt_config, tts_config
```

### 3.3 Document ingestion (RAG setup)
```
Browser --POST /bots/{id}/documents (file)--> FastAPI
  → save file, create Document{status: processing}
  → background task:
      extract text → chunk (fixed-size, ~500 tokens, overlap ~50)
      → embed each chunk (OpenRouter embeddings API)
      → upsert into vector DB with metadata {bot_id, doc_id, chunk_text}
      → Document{status: ready}  (or failed, with error)
Browser polls GET /bots/{id}/documents for status
```

### 3.4 Text chat (RAG path, no voice — build/debug this first)
```
Browser --POST /chat {bot_id, message}--> FastAPI
  → embed(message)
  → vector DB query top-k (filtered by bot_id) → retrieved chunks
  → build prompt: system_prompt + retrieved chunks + user message
  → LLM call (OpenRouter) → response
  → return {response} to browser
```

### 3.5 Voice chat (full pipeline)
```
Browser --POST /connect (WebRTC offer)--> FastAPI
  → creates Pipecat pipeline instance for this session:

  mic audio (WebRTC) → Deepgram STT (streaming)
       → on final transcript: run RAG retrieval (same function as 3.4)
       → inject retrieved chunks into LLM context
       → OpenRouter LLM (streaming completion)
       → Cartesia TTS (streaming synthesis)
       → speaker audio (WebRTC) → Browser

  FastAPI --WebRTC answer--> Browser (SmallWebRTC transport, via @pipecat-ai/client-js)
```

Session lifecycle: pipeline instance lives for the duration of the WebRTC connection, torn down on disconnect.

## 4. Backend module layout

```
backend/app/
  api/
    auth.py         # /auth/login, /auth/register
    bots.py         # /bots CRUD
    documents.py    # upload, list, delete, ingestion trigger
    chat.py         # /chat (text mode)
    connect.py      # /connect (WebRTC offer/answer)
  core/
    config.py       # env/settings
    auth.py         # JWT create/verify, get_current_user dependency
    security.py     # password hashing
  db/
    mongo.py        # Motor client + Beanie init
  models/
    user.py
    bot.py
    document.py
  rag/
    ingest.py       # chunk + embed + upsert
    retriever.py     # top-k query
    embedder.py      # embedding client wrapper
  pipeline/
    voice_pipeline.py   # Pipecat pipeline builder (STT→RAG→LLM→TTS)
  main.py            # FastAPI app, router registration, startup (mongo init)
```

## 5. Frontend module layout

```
frontend/src/
  pages/
    LoginPage.tsx
    SettingsPage.tsx     # bot config form + doc upload
    VoiceChatPage.tsx     # mic button, transcript, connection state
  components/
    DocUploadStatus.tsx
    TranscriptView.tsx
  services/
    api.ts               # REST calls
    voiceClient.ts        # @pipecat-ai/client-js wrapper
```

## 6. Key design decisions (and why)

- **Single backend process, no microservices** — solo project, one deploy target, simplest to reason about and demo.
- **RAG retrieval is one function, called by both `/chat` and the voice pipeline** — avoids duplicating retrieval logic, and lets you verify RAG correctness via the easier-to-debug text endpoint before wiring it into voice.
- **Background task for ingestion, not sync** — file processing (chunk+embed) can take seconds; don't block the upload request.
- **Fail-open retrieval** — if the vector DB call errors/times out, return empty context rather than failing the whole chat/voice turn; the LLM answers without grounding rather than the user hitting a dead end.
- **JWT, not sessions** — stateless, simplest auth for a single-service app with no shared session store needed.

## 7. What's deliberately not here (name these if asked)

Multi-tenancy/isolation, telephony, WhatsApp, billing, circuit breakers/SLO auto-rollback, safety guardrails (hallucination/PII checks), reranking, hybrid BM25+dense search, graph RAG. All present in the full production version this is scoped down from — cut here to keep it buildable and explainable end-to-end.
