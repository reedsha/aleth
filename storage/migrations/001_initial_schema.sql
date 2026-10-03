-- Phase 45: the baseline state schema, as of Phase 44.
--
-- This is the *whole* state database in one place. Before Phase 45 it was created by four modules
-- (``storage.db``, ``storage.intents``, ``storage.telemetry`` and the guarded ``ALTER``s each of
-- them carried), which meant a column added to one ``CREATE`` text never reached a database that
-- already existed. The versioned runner (``storage.migrations``) now owns creation and evolution.
--
-- Every statement is idempotent. A database that predates the runner has no ``schema_version``, so
-- it is at version 0 and this file is applied to it -- and it must therefore do nothing to the
-- tables that are already there. Column order matches what the old code produced (columns the old
-- ``ALTER``s appended sit after the columns the old ``CREATE`` text declared), so a fresh database
-- and an upgraded one are the same shape.

CREATE TABLE IF NOT EXISTS plans (
    plan_id       TEXT PRIMARY KEY,
    plan_file     TEXT NOT NULL DEFAULT 'PLAN.md',
    title         TEXT NOT NULL DEFAULT '',
    goal          TEXT NOT NULL DEFAULT '',
    version       TEXT NOT NULL DEFAULT '1.0',
    state_summary TEXT,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    plan_id        TEXT NOT NULL,
    id             TEXT NOT NULL,
    title          TEXT NOT NULL DEFAULT '',
    description    TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending','planned','in_progress','completed','failed')),
    section        TEXT NOT NULL DEFAULT '',
    section_id     TEXT NOT NULL DEFAULT '',
    tag            TEXT,
    assigned_agent TEXT,
    ast_targets    TEXT NOT NULL DEFAULT '[]',
    required_capabilities TEXT NOT NULL DEFAULT '[]',
    verified       INTEGER NOT NULL DEFAULT 0,
    details        TEXT NOT NULL DEFAULT '[]',
    files          TEXT NOT NULL DEFAULT '[]',
    behavioral_log TEXT NOT NULL DEFAULT '[]',
    sub_steps      TEXT NOT NULL DEFAULT '[]',
    order_index    INTEGER NOT NULL DEFAULT 0,
    rejection_attempts INTEGER NOT NULL DEFAULT 0,
    rejection_feedback TEXT NOT NULL DEFAULT '[]',
    complexity_score   INTEGER NOT NULL DEFAULT 1,
    system_failures    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (plan_id, id),
    FOREIGN KEY (plan_id) REFERENCES plans(plan_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_tasks_order ON tasks(plan_id, order_index);

CREATE TABLE IF NOT EXISTS task_dependencies (
    plan_id    TEXT NOT NULL,
    task_id    TEXT NOT NULL,
    depends_on TEXT NOT NULL,
    PRIMARY KEY (plan_id, task_id, depends_on),
    FOREIGN KEY (plan_id, task_id) REFERENCES tasks(plan_id, id) ON DELETE CASCADE,
    FOREIGN KEY (plan_id, depends_on) REFERENCES tasks(plan_id, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_deps_task ON task_dependencies(plan_id, task_id);

CREATE TABLE IF NOT EXISTS artifacts (
    plan_id    TEXT NOT NULL,
    task_id    TEXT NOT NULL,
    payload    TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (plan_id, task_id),
    FOREIGN KEY (plan_id) REFERENCES plans(plan_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS knowledge_entities (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    type          TEXT NOT NULL
                  CHECK (type IN ('symbol','rule','task','file')),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS knowledge_synapses (
    source_id     TEXT NOT NULL,
    target_id     TEXT NOT NULL,
    relation_type TEXT NOT NULL
                  CHECK (relation_type IN ('calls','modifies','enforces','invalidates')),
    weight        REAL NOT NULL DEFAULT 1.0,
    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (source_id, target_id, relation_type),
    FOREIGN KEY (source_id) REFERENCES knowledge_entities(id) ON DELETE CASCADE,
    FOREIGN KEY (target_id) REFERENCES knowledge_entities(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_synapses_source ON knowledge_synapses(source_id);
CREATE INDEX IF NOT EXISTS idx_synapses_target ON knowledge_synapses(target_id);

CREATE TABLE IF NOT EXISTS skills (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    target_capabilities TEXT NOT NULL DEFAULT '[]',
    markdown_content    TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_skills_capabilities ON skills(id);

CREATE TABLE IF NOT EXISTS intent_ledger (
    intent_id       TEXT PRIMARY KEY,
    action_type     TEXT NOT NULL DEFAULT '',
    message         TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'queued',
    error           TEXT NOT NULL DEFAULT '',
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    acknowledged_at REAL NOT NULL DEFAULT 0,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    pending_input     TEXT NOT NULL DEFAULT '[]',
    rollback_step     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS intent_step_spend (
    intent_id         TEXT NOT NULL,
    step              INTEGER NOT NULL,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    context_blob      TEXT NOT NULL DEFAULT '',
    recorded_at       REAL NOT NULL,
    PRIMARY KEY (intent_id, step)
);

CREATE INDEX IF NOT EXISTS idx_intent_status ON intent_ledger(status);
CREATE INDEX IF NOT EXISTS idx_intent_created ON intent_ledger(created_at);
CREATE INDEX IF NOT EXISTS idx_intent_step ON intent_step_spend(intent_id, step);

CREATE TABLE IF NOT EXISTS execution_telemetry (
    execution_id TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL DEFAULT '',
    target_tool  TEXT NOT NULL DEFAULT '',
    exit_code    INTEGER NOT NULL DEFAULT 0,
    duration_ms  INTEGER NOT NULL DEFAULT 0,
    stdout_hash  TEXT NOT NULL DEFAULT '',
    stderr_hash  TEXT NOT NULL DEFAULT '',
    outcome      TEXT NOT NULL DEFAULT '',
    recorded_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_telemetry_session ON execution_telemetry(session_id);
CREATE INDEX IF NOT EXISTS idx_telemetry_tool ON execution_telemetry(target_tool);
CREATE INDEX IF NOT EXISTS idx_telemetry_recorded ON execution_telemetry(recorded_at);

CREATE TABLE IF NOT EXISTS routing_decisions (
    decision_id  TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL DEFAULT '',
    plan_id      TEXT NOT NULL DEFAULT '',
    task_id      TEXT NOT NULL DEFAULT '',
    intent       TEXT NOT NULL DEFAULT '',
    domain       TEXT NOT NULL DEFAULT '',
    complexity   TEXT NOT NULL DEFAULT '',
    route        TEXT NOT NULL DEFAULT '',
    confidence   REAL NOT NULL DEFAULT 0.0,
    fault        TEXT NOT NULL DEFAULT '',
    engine       TEXT NOT NULL DEFAULT '',
    evidence     TEXT NOT NULL DEFAULT '[]',
    decided_at   REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_routing_session ON routing_decisions(session_id);
CREATE INDEX IF NOT EXISTS idx_routing_task ON routing_decisions(plan_id, task_id);
CREATE INDEX IF NOT EXISTS idx_routing_recorded ON routing_decisions(decided_at);

CREATE TABLE IF NOT EXISTS agent_faults (
    fault_id    TEXT PRIMARY KEY,
    intent_id   TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL DEFAULT '',
    role        TEXT NOT NULL DEFAULT '',
    detail      TEXT NOT NULL DEFAULT '',
    steps       INTEGER NOT NULL DEFAULT 0,
    recorded_at REAL NOT NULL
);

-- ---------------------------------------------------------------------------
-- Legacy repair: bring a database written before the runner up to this baseline.
--
-- The ``CREATE TABLE IF NOT EXISTS`` statements above do nothing to a table that is already there,
-- so a table that predates a column keeps missing it. SQLite has no ``ADD COLUMN IF NOT EXISTS``,
-- so these are plain ``ALTER``s and the runner treats "duplicate column name" (and "no such
-- column" on a DROP) as "already in the wanted shape". Every other error rolls the patch back.
-- ---------------------------------------------------------------------------

ALTER TABLE tasks ADD COLUMN rejection_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN rejection_feedback TEXT NOT NULL DEFAULT '[]';
ALTER TABLE tasks ADD COLUMN complexity_score INTEGER NOT NULL DEFAULT 1;
ALTER TABLE tasks ADD COLUMN required_capabilities TEXT NOT NULL DEFAULT '[]';
ALTER TABLE tasks ADD COLUMN system_failures INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN verified INTEGER NOT NULL DEFAULT 0;

ALTER TABLE execution_telemetry ADD COLUMN outcome TEXT NOT NULL DEFAULT '';

-- ``intent_id`` replaces the "session" this ledger shipped with; the index goes first, because
-- SQLite refuses to drop a column an index still names.
ALTER TABLE agent_faults ADD COLUMN intent_id TEXT NOT NULL DEFAULT '';
DROP INDEX IF EXISTS idx_agent_faults_session;
ALTER TABLE agent_faults DROP COLUMN session_id;

ALTER TABLE intent_ledger ADD COLUMN prompt_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE intent_ledger ADD COLUMN completion_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE intent_ledger ADD COLUMN pending_input TEXT NOT NULL DEFAULT '[]';
ALTER TABLE intent_ledger ADD COLUMN rollback_step INTEGER NOT NULL DEFAULT 0;

ALTER TABLE intent_step_spend ADD COLUMN context_blob TEXT NOT NULL DEFAULT '';

-- Indexes last: an index may name a column the repair above just added -- ``agent_faults.intent_id``
-- is the case that matters, because a table written before Phase 27 does not have it until the
-- ``ALTER`` above.
CREATE INDEX IF NOT EXISTS idx_agent_faults_intent ON agent_faults(intent_id);
CREATE INDEX IF NOT EXISTS idx_agent_faults_recorded ON agent_faults(recorded_at);
