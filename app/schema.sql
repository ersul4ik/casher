-- Spend tracker schema. Applied on every start, so every statement is idempotent.

CREATE TABLE IF NOT EXISTS users (
    id           BIGSERIAL PRIMARY KEY,
    tg_id        BIGINT      NOT NULL UNIQUE,
    username     TEXT,
    first_name   TEXT,
    api_token    TEXT        NOT NULL UNIQUE,
    currency     TEXT        NOT NULL DEFAULT 'KGS',
    tz_minutes   INTEGER     NOT NULL DEFAULT 360,
    is_active    BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS categories (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT      NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    name        TEXT        NOT NULL,
    pos         INTEGER     NOT NULL DEFAULT 100,
    is_archived BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS categories_user_name_key
    ON categories (user_id, lower(name));
CREATE INDEX IF NOT EXISTS categories_user_idx
    ON categories (user_id, is_archived, pos, id);

CREATE TABLE IF NOT EXISTS spends (
    id             BIGSERIAL PRIMARY KEY,
    user_id        BIGINT        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    category_id    BIGINT        REFERENCES categories (id) ON DELETE SET NULL,
    amount         NUMERIC(14, 2) NOT NULL CHECK (amount > 0),
    currency       TEXT          NOT NULL,
    status         TEXT          NOT NULL DEFAULT 'pending'
                                 CHECK (status IN ('pending', 'done', 'ignored')),
    source         TEXT          NOT NULL DEFAULT 'shortcut',
    raw            TEXT,
    note           TEXT,
    occurred_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    created_at     TIMESTAMPTZ   NOT NULL DEFAULT now(),
    categorized_at TIMESTAMPTZ,
    tg_message_id  BIGINT
);

CREATE INDEX IF NOT EXISTS spends_user_time_idx
    ON spends (user_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS spends_user_status_idx
    ON spends (user_id, status, occurred_at DESC);
-- Serves deduplication: same user, amount and currency within the last N seconds.
CREATE INDEX IF NOT EXISTS spends_dedup_idx
    ON spends (user_id, currency, amount, occurred_at DESC);
-- Serves reports: grouping by category within a period.
CREATE INDEX IF NOT EXISTS spends_report_idx
    ON spends (user_id, status, currency, occurred_at);
