from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # MongoDB
    mongodb_url: str
    db_name: str = "voiceagent"

    # JWT
    secret_key: str
    algorithm: str = "HS256"
    # Task 2.5 — shortened from 60. A leaked/stolen access token used to be
    # live for an hour with no way to cut it off; now it's live for 15
    # minutes, and the refresh token that replaces it (below) can be
    # revoked instantly on logout.
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7

    # OpenAI
    openai_api_key: str

    # Groq — optional, faster/cheaper LLM alternative to OpenAI.
    # If set, the voice pipeline uses Groq for the LLM step instead of
    # OpenAI. Leave blank to keep using OpenAI (no other change needed —
    # this is the "switch back" the manual's task 1.2 asked for).
    groq_api_key: str = ""
    # Llama 3.3 70B (pipecat's built-in default) has been retired from Groq's
    # catalog — verified 2026-08-30 via /v1/models. gpt-oss-120b is the
    # current best tool-capable replacement (needed for function calling,
    # task 1.3): 131k context, cheap, and it's a reasoning model — see the
    # reasoning_effort="low" setting in voice_pipeline.py, which keeps it
    # fast for voice rather than letting it "think" before every reply.
    groq_model: str = "openai/gpt-oss-120b"

    # Task 1.6 — a separate, smaller/faster Groq model just for cleaning up
    # spoken RAG queries before search. Deliberately not the 120b model used
    # for actual replies: this call sits directly in the latency path of
    # every RAG lookup, so it needs to be as fast as possible, not as smart
    # as possible. Verified live against Groq's catalog 2026-08-31 (same
    # /v1/models check that caught llama-3.3-70b-versatile's retirement) —
    # ~0.5-0.7s round trip, correctly strips filler words and converts
    # spoken numbers ("page fifty" -> "page 50").
    groq_rewrite_model: str = "openai/gpt-oss-20b"

    # Task 2.1 — provider switching layer. One setting each flips the whole
    # pipeline between cloud and free/local, no code changes anywhere else
    # (see app/pipeline/providers.py). "auto" for llm_provider preserves the
    # pre-2.1 behavior exactly: Groq if a key is configured, else OpenAI.
    stt_provider: str = "deepgram"  # deepgram | whisper
    tts_provider: str = "cartesia"  # cartesia | piper
    llm_provider: str = "auto"      # auto | groq | openai

    # Task 2.2 — local speech recognition. "small" is faster-whisper's own
    # multilingual small model — verified 2026-08-31 it transcribes both
    # English and Hindi correctly on CPU (this bot's two confirmed working
    # languages), unlike the pipecat default (DISTIL_MEDIUM_EN), which is
    # English-only and would silently break Hindi callers.
    whisper_model: str = "small"

    # Deepgram
    deepgram_api_key: str

    # Cartesia
    cartesia_api_key: str

    # Pinecone
    pinecone_api_key: str
    pinecone_index_name: str = "voiceagent"
    # Task 1.8 — hybrid search. Pinecone serverless indexes are single-type
    # (dense OR sparse, never both in one index — confirmed 2026-08-31 via
    # describe_index() on the existing index), so keyword matching needs its
    # own separate index, queried alongside the dense one and merged.
    pinecone_sparse_index_name: str = "voiceagent-sparse"

    # Task 2.3 — WebRTC connectivity.
    #
    # STUN lets each side discover its own public address; it's enough for
    # most home/office networks. Found 2026-08-31: the BACKEND was passing
    # no ICE servers at all (SmallWebRTCRequestHandler() with no arguments)
    # — only the frontend had STUN configured, so the two sides weren't
    # symmetric. These defaults fix that at no cost.
    stun_servers: str = "stun:stun.l.google.com:19302,stun:stun1.l.google.com:19302"
    #
    # TURN is the actual relay — the piece that makes calls work from
    # genuinely restrictive networks (corporate firewalls, some mobile
    # carriers, symmetric NAT) where STUN alone fails. This is what task
    # 2.3 exists to solve. Blank by default; fill these in once a TURN
    # server exists (self-hosted coturn, or a managed provider) and it
    # takes effect with no code change.
    turn_url: str = ""          # e.g. "turn:203.0.113.10:3478"
    turn_username: str = ""
    turn_credential: str = ""

    # Latency (2026-09-03) — how many warm call workers to keep idle.
    # Each holds an imported pipecat stack (roughly 300MB) but has NOT yet
    # loaded the VAD or turn-detection models, which happens per call. Two
    # fits comfortably on the 4GB VM alongside the API, Caddy and coturn.
    # Raise it only with `free -h` in hand: an OOM kill during a live call
    # costs far more than the startup latency this saves.
    call_worker_pool_size: int = 2

    # Phase 3 hardening — whether this server may send a request to a
    # private, loopback, or link-local address when a customer configures
    # one (a webhook subscription URL, task 3.8; a bot tool URL, task 3.1).
    # Off by default because on a cloud VM the link-local range is the
    # metadata service, which hands out the instance's own credentials to
    # anything that asks it — see app/core/url_safety.py. Turn this on for
    # local development, where pointing a tool at http://localhost:9000 is
    # the normal thing to do, and leave it off anywhere reachable from the
    # internet.
    allow_private_outbound_urls: bool = False

    # Task 2.7 — error tracking. Blank by default: sentry_sdk.init(dsn="")
    # is a confirmed-safe no-op (verified live 2026-08-31), so this stays
    # completely dormant — zero behavior change — until a real DSN is set.
    # Needs a Sentry account (sentry.io free tier, or self-hosted) to get
    # one; that account creation isn't something this session can do.
    sentry_dsn: str = ""

    # Task 2.7 — self-hosted Langfuse (AI call cost/latency dashboard).
    # Consumed by app/core/tracing.py, which exports pipecat's per-stage
    # spans over OTLP. All three must be set for tracing to switch on;
    # blank (the default) means no exporter is built and pipecat's tracing
    # stays off entirely, so this is dormant with zero overhead.
    # Requires Langfuse v3 or Langfuse Cloud — v2 has no OTLP endpoint.
    # See deploy/docker-compose.langfuse.yml, including its warning about
    # the RAM this needs relative to the current VM.
    langfuse_host: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""

    # ---------------------------------------------------------------------
    # Phase 4 — scale foundations.
    #
    # Everything below is written so that leaving it alone keeps the server
    # behaving exactly as it did before Phase 4 existed. The one genuinely
    # new default is the concurrency cap, and it is set well above what this
    # VM can serve rather than as a limit anyone would hit by accident.
    # ---------------------------------------------------------------------

    # Task 4.1/4.2/4.5 — shared state across API replicas. BLANK BY DEFAULT,
    # and blank means "one server, keep state in this process", which is
    # exactly what the code did before and is completely correct while only
    # one API process is running. Set it (redis://host:6379/0) at the point
    # a SECOND API replica exists — from then on the in-process call
    # registry, the concurrency counter and the rate limiter would each be
    # counting only their own replica's share, which is worse than useless.
    redis_url: str = ""

    # Task 4.5 — the hard ceiling on simultaneous calls.
    #
    # This number is a memory figure, not a CPU one. Each live call is its
    # own process (task 2.4) holding roughly 300MB of pipecat, and the
    # deployed VM has 4GB total with the API, Caddy and coturn already in
    # it. Six is comfortable; the real number for a given machine comes out
    # of task 4.8's load test, and this is the setting that test exists to
    # inform. 0 disables the cap entirely, which is not recommended
    # anywhere the machine can run out of memory.
    max_concurrent_calls: int = 6

    # Task 4.3 — how far the warm pool is allowed to grow and shrink on its
    # own. The floor is what is kept ready when nothing is happening; the
    # ceiling exists because a pool that grows without one will happily fill
    # the machine's memory with idle workers. Setting max equal to min turns
    # autoscaling off and pins the pool at call_worker_pool_size, which is
    # the pre-Phase-4 behaviour.
    call_worker_pool_min: int = 2
    call_worker_pool_max: int = 4
    # Below this much free memory, the pool refuses to grow no matter how
    # long the queue is. An out-of-memory kill during live calls costs far
    # more than a caller waiting a few extra seconds for a cold worker.
    pool_min_free_memory_mb: int = 700

    # Task 4.4 — Motor's connection pool. 0 means "work it out from
    # max_concurrent_calls" (see app/db/mongo.py), which is the right answer
    # almost always; set a number only to override that.
    mongo_max_pool_size: int = 0
    # Send read-only queries to a replica instead of the primary. Off by
    # default and deliberately so: replicas lag, and code that writes and
    # then immediately reads gets the old value back — a genuinely
    # confusing bug. See app/db/mongo.py for which reads are safe.
    mongo_read_from_secondary: bool = False

    # Task 4.6 — switch to the local backup provider when a cloud one has
    # failed repeatedly. Safe to leave on: the switch only happens once a
    # breaker has actually tripped, and it is skipped automatically when the
    # local model is not on this machine (which would make a call slower to
    # START, the one thing worse than a slow call). Run
    # scripts/prefetch_local_models.py to make the backups usable.
    provider_fallback_enabled: bool = True

    # Task 4.9 — the Prometheus scrape endpoint at /metrics. Reports counts
    # and timings only: no transcripts, no caller data, no secrets.
    metrics_enabled: bool = True
    # ...but it DOES report which customer hostnames have tripped a circuit
    # breaker, and how many calls are live right now, so it is never served
    # to an anonymous request. A logged-in user always passes; setting this
    # additionally lets a Prometheus scraper authenticate with a static
    # bearer token, which is what Prometheus actually supports
    # (`authorization: credentials:` in a scrape config). Generate one with
    # `openssl rand -hex 32`.
    metrics_token: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
