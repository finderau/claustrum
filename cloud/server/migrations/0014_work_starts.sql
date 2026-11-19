CREATE TABLE IF NOT EXISTS work_starts (
    uid          text NOT NULL,
    opening_id   text NOT NULL,
    user_email   text NOT NULL,
    machine      text NOT NULL,
    label        text,
    prompt_hash  char(64) NOT NULL,
    plan_key     char(64),
    repo         text,
    branch       text,
    acknowledged boolean NOT NULL DEFAULT true,
    first_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (uid, opening_id)
);

CREATE INDEX IF NOT EXISTS idx_work_starts_prompt
    ON work_starts (prompt_hash, first_at DESC) WHERE acknowledged;
CREATE INDEX IF NOT EXISTS idx_work_starts_plan
    ON work_starts (plan_key, first_at DESC) WHERE acknowledged AND plan_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_work_starts_user
    ON work_starts (user_email);
