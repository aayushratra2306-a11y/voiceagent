# Voice Agent (Lite) — Build Plan

Scoped-down rebuild of a production voice-agent platform: **one frontend**, **bot settings**, **real-time voice chat**, **basic RAG**. No multi-tenancy, no WhatsApp/telephony, no billing, no safety/SLO layer — those exist in the full version but are cut here on purpose to keep this buildable solo and explainable in an interview.

## 1. Pitch (one-liner)

A configurable voice AI agent: create a bot, give it a name/personality/system prompt, upload a knowledge-base doc, then talk to it live in the browser — it answers grounded in your doc via RAG.

## 2. Scope

**In:**
- Single bot per user (or simple bot list, no tenant isolation)
- Bot settings page: name, system prompt, voice selection, LLM model, STT/TTS config
- Voice chat page: WebRTC mic in → live transcript → LLM response → TTS audio out
- Text chat as a secondary/fallback mode (easier to demo without mic)
- Basic RAG: upload PDF/doc → chunk → embed → store in vector DB → retrieve top-k on each query → inject into LLM context
- Auth: simple JWT login (single-user or basic multi-user, no roles/scopes)

**Out (cut from full project, mention as "future work" in interview):**
- Multi-tenancy, per-tenant isolation, credit/billing system
- WhatsApp/telephony integration
- Circuit breakers, SLO watcher, auto-rollback flags
- Input/output safety guardrails, grounding audit
- Two separate admin portals
- Graph RAG (Neo4j/Graphiti), reranking, semantic cache, hybrid BM25

## 3. Tech stack

| Layer | Choice | Why |
|---|---|---|
| Backend | FastAPI (Python) | async, good WS/streaming support |
| Voice pipeline | Pipecat | handles STT→LLM→TTS orchestration, WebRTC transport built-in |
| DB | MongoDB (Beanie ODM) or Postgres | pick one you're faster in; Mongo if copying doc-shaped bot config |
| Vector store | Pinecone (free tier) or Chroma (local, zero infra) | Chroma = simpler for solo demo, no external dependency |
| STT | Deepgram (WSS) | low-latency streaming, generous free tier |
| LLM | OpenRouter or direct OpenAI | OpenRouter = model flexibility for demo |
| TTS | Cartesia or ElevenLabs | low-latency streaming TTS |
| Frontend | React + Vite + TypeScript + Tailwind + shadcn/ui | fast to build, matches original |
| Voice client | @pipecat-ai/client-js + SmallWebRTC | official client, handles the WebRTC plumbing |

## 4. Architecture (basic)

```
Browser (React)
  ├── Settings page → REST → FastAPI → Mongo (bot config, KB doc metadata)
  ├── Upload doc → FastAPI → chunk → embed → Chroma/Pinecone
  └── Voice chat page → WebRTC → Pipecat pipeline:
        mic in → Deepgram STT → [inject RAG context] → LLM (OpenRouter) → Cartesia TTS → speaker out
```

RAG injection point: before each LLM turn, embed the user's transcript, query vector store top-k, prepend retrieved chunks to system/context message. Same pattern as the full project's `inject_rag_context()`, just without dialogue-pinning/entity-routing complexity.

## 5. Data models (minimal)

```
User        { id, email, password_hash }
Bot         { id, user_id, name, system_prompt, voice_id, llm_model, stt_config, tts_config }
Document    { id, bot_id, filename, status (processing/ready/failed) }
Chunk       { id, doc_id, bot_id, text, embedding_id }   # or just store in vector DB w/ metadata
CallSession { id, bot_id, started_at, transcript[] }      # optional, for a "history" feature
```

## 6. Core API endpoints

```
POST   /auth/login
POST   /auth/register

GET    /bots
POST   /bots
PATCH  /bots/{id}
DELETE /bots/{id}

POST   /bots/{id}/documents         # upload + trigger ingestion
GET    /bots/{id}/documents
DELETE /documents/{id}

POST   /connect                     # WebRTC offer/answer for voice session
POST   /chat                        # text-mode chat (non-voice, easier to demo/debug)
```

## 7. Build phases

1. **Skeleton** — FastAPI app + Mongo connection + JWT auth + bot CRUD + React shell with routing (Settings, Voice Chat pages)
2. **Basic RAG** — doc upload → chunk (simple fixed-size or langchain splitter) → embed (OpenAI/OpenRouter embeddings) → store in Chroma → retrieval function → wire into a plain `/chat` text endpoint first (no voice yet — easiest to verify RAG works)
3. **Voice pipeline** — Pipecat pipeline: Deepgram STT → LLM (with RAG injection reused from step 2) → Cartesia TTS, WebRTC transport, `/connect` endpoint
4. **Frontend voice UI** — mic button, live transcript display, audio playback, connection status
5. **Settings UI** — bot config form (system prompt, voice picker, model picker), doc upload with processing-status indicator
6. **Polish** — error states (mic denied, STT/LLM/TTS failures), loading states, basic responsive layout

## 8. Interview talking points (why this design)

- **Why Pipecat over hand-rolled WebRTC+streaming**: avoids reimplementing audio frame buffering/VAD/turn-taking; lets you focus on the RAG/product layer.
- **Why RAG injection happens pre-LLM-call, shared between text and voice paths**: one code path to test/debug, voice pipeline just calls the same function text chat does.
- **Fail-open RAG**: if vector store times out or errors, don't block the conversation — return empty context, let LLM answer from general knowledge or say it doesn't know. (Cheap to add, shows you think about degradation, not just happy path.)
- **What you'd add next**: multi-tenancy (per-user data isolation), telephony (Twilio), safety guardrails (output hallucination/PII check before TTS), observability (latency per pipeline stage) — name these as the "known scope cuts," it signals you understand production concerns even in a scoped demo.

## 9. Suggested repo structure

```
voice-agent-lite/
  backend/
    app/
      api/          # auth.py, bots.py, documents.py, connect.py, chat.py
      core/         # config.py, auth.py, security.py
      db/           # mongo.py
      models/       # bot.py, user.py, document.py
      rag/          # ingest.py, retriever.py, embedder.py
      pipeline/      # pipecat pipeline builder
      main.py
  frontend/
    src/
      pages/        # SettingsPage.tsx, VoiceChatPage.tsx, LoginPage.tsx
      components/
      services/api.ts
```
