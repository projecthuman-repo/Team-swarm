-- v4.1 T5.1 — idempotency hardening: redelivered handling can't
-- double-insert an outbox row (claims are already compare-and-set;
-- Nats-Msg-Id already dedupes at the server; this closes the DB side).
CREATE UNIQUE INDEX IF NOT EXISTS outbox_msg_id_unique ON outbox(msg_id);
