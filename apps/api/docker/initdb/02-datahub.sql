-- ============================================================
-- 02-datahub.sql — Create datahub database, user, and schemas
-- Runs on first postgres container init only
-- ============================================================

-- Create role (idempotent via DO block)
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'datahub_user') THEN
    CREATE ROLE datahub_user WITH LOGIN PASSWORD 'DataHub_123456';
  END IF;
END
$$;

-- Create database (must use \gexec trick since CREATE DATABASE can't be in DO block)
SELECT 'CREATE DATABASE datahub OWNER datahub_user'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'datahub');
\gexec

-- Connect to datahub database
\c datahub

-- ── ops schema ──
CREATE SCHEMA IF NOT EXISTS ops;

CREATE TABLE IF NOT EXISTS ops.dataset_state (
    dataset_id text PRIMARY KEY,
    active_raw_slot text NOT NULL DEFAULT 'raw_1',
    active_ex_slot text NOT NULL DEFAULT 'ex',
    ingest_paused boolean NOT NULL DEFAULT false,
    current_version text NOT NULL DEFAULT 'v0',
    last_success_batch text,
    last_success_ts timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ops.pipeline_run (
    run_id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
    dataset_id text NOT NULL,
    batch_id text,
    stage text NOT NULL,
    status text NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    detail text,
    metrics jsonb
);
CREATE INDEX IF NOT EXISTS ix_pipeline_run_dataset ON ops.pipeline_run (dataset_id, started_at DESC);
CREATE INDEX IF NOT EXISTS ix_pipeline_run_batch ON ops.pipeline_run (batch_id);

CREATE TABLE IF NOT EXISTS ops.audit_log (
    audit_id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
    dataset_id text,
    action text NOT NULL,
    detail text,
    actor text NOT NULL DEFAULT 'system',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_audit_log_dataset ON ops.audit_log (dataset_id, created_at DESC);

-- ── raw_sop schema ──
CREATE SCHEMA IF NOT EXISTS raw_sop;

CREATE TABLE IF NOT EXISTS raw_sop.session (
    raw_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id text,
    audio_id bigint NOT NULL,
    source_uid text,
    start_time_str text,
    end_time_str text,
    start_ts timestamptz,
    end_ts timestamptz,
    fzuer text NOT NULL,
    dzdh text,
    kunr text,
    sop_analyze jsonb,
    consult_analyze jsonb,
    tags_analyze jsonb,
    strategy_analyze jsonb,
    face_analyze jsonb,
    payload jsonb NOT NULL,
    ingest_status text NOT NULL DEFAULT 'NEW',
    error_msg text,
    source_ip text,
    ingested_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_raw_sop_session_audio ON raw_sop.session (audio_id);
CREATE INDEX IF NOT EXISTS idx_raw_sop_session_fzuer ON raw_sop.session (fzuer);
CREATE INDEX IF NOT EXISTS idx_raw_sop_session_batch ON raw_sop.session (batch_id);
CREATE INDEX IF NOT EXISTS idx_raw_sop_session_ingested ON raw_sop.session (ingested_at);

CREATE TABLE IF NOT EXISTS raw_sop.utterance (
    utterance_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    raw_id uuid NOT NULL REFERENCES raw_sop.session(raw_id),
    audio_id bigint NOT NULL,
    utterance_index int NOT NULL,
    begin_ms int,
    end_ms int,
    speaker_role text,
    speaker_role_raw text,
    content_text text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_raw_sop_utt_raw ON raw_sop.utterance (raw_id);
CREATE INDEX IF NOT EXISTS idx_raw_sop_utt_audio ON raw_sop.utterance (audio_id);

CREATE TABLE IF NOT EXISTS raw_sop.egest_log (
    log_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    raw_id uuid REFERENCES raw_sop.session(raw_id),
    batch_id text,
    audio_id bigint,
    target_url text,
    http_code int,
    response_body text,
    attempts int NOT NULL DEFAULT 1,
    status text NOT NULL DEFAULT 'pending',
    created_at timestamptz NOT NULL DEFAULT now(),
    sent_at timestamptz
);
CREATE INDEX IF NOT EXISTS idx_raw_sop_egest_status ON raw_sop.egest_log (status);
CREATE INDEX IF NOT EXISTS idx_raw_sop_egest_batch ON raw_sop.egest_log (batch_id);
CREATE INDEX IF NOT EXISTS idx_raw_sop_egest_audio ON raw_sop.egest_log (audio_id);

CREATE TABLE IF NOT EXISTS raw_sop.ingest_event (
    event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id text,
    audio_id bigint,
    payload_hash text NOT NULL,
    event_type text NOT NULL,
    source_ip text,
    reject_reasons jsonb,
    duplicate_of text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ingest_event_type ON raw_sop.ingest_event (event_type);
CREATE INDEX IF NOT EXISTS idx_ingest_event_audio ON raw_sop.ingest_event (audio_id);
CREATE INDEX IF NOT EXISTS idx_ingest_event_hash ON raw_sop.ingest_event (payload_hash);
CREATE INDEX IF NOT EXISTS idx_ingest_event_created ON raw_sop.ingest_event (created_at);

-- ── mart_sop schema ──
CREATE SCHEMA IF NOT EXISTS mart_sop;

CREATE TABLE IF NOT EXISTS mart_sop.session (
    session_id text PRIMARY KEY,
    raw_id uuid NOT NULL,
    audio_id bigint NOT NULL,
    start_ts timestamptz,
    end_ts timestamptz,
    duration_ms int,
    fzuer text NOT NULL,
    dzdh text,
    kunr text,
    utterance_count int NOT NULL DEFAULT 0,
    staff_utt_count int NOT NULL DEFAULT 0,
    customer_utt_count int NOT NULL DEFAULT 0,
    is_valid boolean NOT NULL DEFAULT true,
    qa_flags text[] DEFAULT '{}',
    batch_id text,
    ingested_at timestamptz,
    loaded_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_mart_sop_sess_fzuer ON mart_sop.session (fzuer);

CREATE TABLE IF NOT EXISTS mart_sop.utterance (
    utterance_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id text NOT NULL,
    audio_id bigint NOT NULL,
    utterance_index int NOT NULL,
    begin_ms int,
    end_ms int,
    speaker_role text,
    content_text text,
    fzuer text,
    dzdh text,
    kunr text,
    is_valid boolean NOT NULL DEFAULT true,
    qa_flags text[] DEFAULT '{}',
    raw_id uuid,
    batch_id text,
    loaded_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_mart_sop_utt_session ON mart_sop.utterance (session_id);
CREATE INDEX IF NOT EXISTS idx_mart_sop_utt_audio ON mart_sop.utterance (audio_id);

-- ── Grants ──
GRANT USAGE ON SCHEMA ops TO datahub_user;
GRANT USAGE ON SCHEMA raw_sop TO datahub_user;
GRANT USAGE ON SCHEMA mart_sop TO datahub_user;
GRANT ALL ON ALL TABLES IN SCHEMA ops TO datahub_user;
GRANT ALL ON ALL TABLES IN SCHEMA raw_sop TO datahub_user;
GRANT ALL ON ALL TABLES IN SCHEMA mart_sop TO datahub_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA ops GRANT ALL ON TABLES TO datahub_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA raw_sop GRANT ALL ON TABLES TO datahub_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA mart_sop GRANT ALL ON TABLES TO datahub_user;
