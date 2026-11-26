-- TalkOps v2.0 Operational Store Canonical PostgreSQL Schema
-- Requires: PostgreSQL >= 15 and pgvector extension (`CREATE EXTENSION IF NOT EXISTS vector;`)

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS talkops_topology_nodes (
    tenant_id TEXT NOT NULL DEFAULT 'default',
    node_id TEXT NOT NULL,
    node_type TEXT NOT NULL,
    service_name TEXT NOT NULL DEFAULT '',
    environment TEXT NOT NULL DEFAULT 'production',
    correlation_tags JSONB NOT NULL DEFAULT '{}'::jsonb,
    attributes JSONB NOT NULL DEFAULT '{}'::jsonb,
    baseline_stats JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (tenant_id, node_id)
);

CREATE INDEX IF NOT EXISTS idx_talkops_topology_nodes_tags
    ON talkops_topology_nodes USING GIN (correlation_tags);
CREATE INDEX IF NOT EXISTS idx_talkops_topology_nodes_service
    ON talkops_topology_nodes (tenant_id, service_name);

CREATE TABLE IF NOT EXISTS talkops_topology_edges (
    tenant_id TEXT NOT NULL DEFAULT 'default',
    src_id TEXT NOT NULL,
    dst_id TEXT NOT NULL,
    rel_type TEXT NOT NULL,
    lineage_source TEXT NOT NULL DEFAULT 'manual',
    last_seen_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (tenant_id, src_id, dst_id, rel_type)
);

CREATE INDEX IF NOT EXISTS idx_talkops_topology_edges_dst
    ON talkops_topology_edges (tenant_id, dst_id);

CREATE TABLE IF NOT EXISTS talkops_change_events (
    event_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    service_name TEXT NOT NULL,
    environment TEXT NOT NULL DEFAULT 'production',
    source TEXT NOT NULL,
    commit_sha TEXT NOT NULL DEFAULT '',
    pr_url TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    diff_summary TEXT NOT NULL DEFAULT '',
    git_tree_hash TEXT NOT NULL DEFAULT '',
    occurred_at DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_talkops_change_events_service_time
    ON talkops_change_events (tenant_id, service_name, occurred_at DESC);

CREATE TABLE IF NOT EXISTS talkops_incidents (
    incident_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    thread_id TEXT NOT NULL,
    service_name TEXT NOT NULL,
    environment TEXT NOT NULL DEFAULT 'production',
    alert_fingerprint TEXT NOT NULL DEFAULT '',
    symptoms_text TEXT NOT NULL DEFAULT '',
    primary_root_cause TEXT NOT NULL,
    contributing_factors JSONB NOT NULL DEFAULT '[]'::jsonb,
    ruled_out_hypotheses JSONB NOT NULL DEFAULT '[]'::jsonb,
    remediation_action JSONB NOT NULL DEFAULT '{}'::jsonb,
    verification_status TEXT NOT NULL DEFAULT 'unverified',
    confidence_weight DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    git_tree_hash TEXT NOT NULL DEFAULT '',
    tsv tsvector GENERATED ALWAYS AS (
        to_tsvector(
            'english',
            coalesce(service_name, '') || ' ' ||
            coalesce(alert_fingerprint, '') || ' ' ||
            coalesce(symptoms_text, '') || ' ' ||
            coalesce(primary_root_cause, '')
        )
    ) STORED,
    embedding vector(1536),
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_talkops_incidents_tsv
    ON talkops_incidents USING GIN (tsv);
CREATE INDEX IF NOT EXISTS idx_talkops_incidents_embedding_hnsw
    ON talkops_incidents USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_talkops_incidents_service
    ON talkops_incidents (tenant_id, service_name, verification_status);

CREATE TABLE IF NOT EXISTS talkops_memory_rules (
    rule_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    scope_service TEXT NOT NULL DEFAULT '*',
    memory_type TEXT NOT NULL DEFAULT 'episodic_constraint',
    rule_text TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'reflection_observer',
    confirmation_count INTEGER NOT NULL DEFAULT 1,
    hit_count INTEGER NOT NULL DEFAULT 0,
    helped_count INTEGER NOT NULL DEFAULT 0,
    misled_count INTEGER NOT NULL DEFAULT 0,
    confidence_weight DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    expires_at DOUBLE PRECISION,
    updated_at DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_talkops_memory_rules_scope
    ON talkops_memory_rules (tenant_id, scope_service, active);

CREATE TABLE IF NOT EXISTS talkops_correlated_alerts (
    alert_row_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    provider TEXT NOT NULL,
    dedup_key TEXT NOT NULL,
    service_name TEXT NOT NULL DEFAULT '',
    environment TEXT NOT NULL DEFAULT 'production',
    normalized_event JSONB NOT NULL,
    consumed BOOLEAN NOT NULL DEFAULT FALSE,
    arrived_at DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_talkops_correlated_alerts_inbox
    ON talkops_correlated_alerts (thread_id, consumed, arrived_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_talkops_correlated_alerts_dedup
    ON talkops_correlated_alerts (thread_id, dedup_key);

CREATE TABLE IF NOT EXISTS talkops_durable_workflows (
    workflow_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    workflow_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    phase TEXT NOT NULL DEFAULT 'grace_crash',
    poll_interval_sec DOUBLE PRECISION NOT NULL DEFAULT 15.0,
    next_poll_at DOUBLE PRECISION NOT NULL,
    started_at DOUBLE PRECISION NOT NULL,
    deadline_at DOUBLE PRECISION NOT NULL,
    consecutive_passes INTEGER NOT NULL DEFAULT 0,
    spec_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    tnr_undo_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    sink_refs JSONB NOT NULL DEFAULT '{}'::jsonb,
    lease_owner TEXT,
    lease_expires_at DOUBLE PRECISION,
    result_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_talkops_durable_workflows_due
    ON talkops_durable_workflows (status, next_poll_at);

CREATE TABLE IF NOT EXISTS talkops_autoqa_scores (
    thread_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    plugin_name TEXT NOT NULL DEFAULT 'core',
    plugin_version TEXT NOT NULL DEFAULT 'default',
    stage0_passed BOOLEAN NOT NULL,
    stage0_violations JSONB NOT NULL DEFAULT '[]'::jsonb,
    judge_tier TEXT NOT NULL DEFAULT 'fast',
    self_confidence DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    overall_score DOUBLE PRECISION NOT NULL,
    criterion_citations JSONB NOT NULL DEFAULT '{}'::jsonb,
    resolution_mode TEXT NOT NULL DEFAULT 'agent_assisted',
    dispute_status TEXT NOT NULL DEFAULT 'none',
    feedback_notes TEXT NOT NULL DEFAULT '',
    created_at DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_talkops_autoqa_scores_plugin
    ON talkops_autoqa_scores (tenant_id, plugin_name, created_at DESC);
