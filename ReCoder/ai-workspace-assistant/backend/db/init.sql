CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE users (
    user_id    VARCHAR PRIMARY KEY,
    email      VARCHAR UNIQUE NOT NULL,
    password   VARCHAR NOT NULL,
    name       VARCHAR,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE sessions (
    session_id       VARCHAR PRIMARY KEY,
    user_id          VARCHAR REFERENCES users(user_id),
    start_time       TIMESTAMP,
    end_time         TIMESTAMP,
    ai_summary       TEXT,
    current_task     VARCHAR,
    has_error        BOOLEAN DEFAULT FALSE,
    importance       INTEGER DEFAULT 0,
    resolved         BOOLEAN DEFAULT FALSE,
    shared           BOOLEAN DEFAULT FALSE,
    embedding        vector(768),
    created_at       TIMESTAMP DEFAULT NOW()
);

CREATE INDEX ON sessions
  USING ivfflat (embedding vector_cosine_ops)
  WITH (lists = 100);
