CREATE INDEX job_history_order ON job(created_at DESC, id DESC);
CREATE INDEX job_history_state ON job(state, created_at DESC, id DESC);
CREATE INDEX job_history_kind ON job(kind, created_at DESC, id DESC);
CREATE INDEX job_event_history ON job_event(job_id, id DESC);
