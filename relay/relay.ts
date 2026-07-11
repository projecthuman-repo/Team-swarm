/**
 * Outbox relay — the ONLY publisher into JetStream (Hard Rule 2).
 *
 * Polls the Postgres outbox for unpublished rows, publishes each to its
 * subject with a Nats-Msg-Id header (server-side dedupe, --dupe-window),
 * and marks it published in the same transaction. FOR UPDATE SKIP LOCKED
 * makes it safe to run more than one relay instance: no double-publish.
 *
 * Higher-throughput option (see runbook §7): replace this poll loop with
 * Postgres logical-replication CDC (wal_level=logical is already set in
 * deploy/compose.yaml) and a slot-tailing relay.
 */
import { connect, headers, type NatsConnection } from "nats";
import pg from "pg";

const NATS_URL = process.env.NATS_URL ?? "nats://localhost:4222";
const DATABASE_URL =
  process.env.DATABASE_URL ?? "postgres://postgres:dev@localhost:5432/swarm";
const POLL_MS = Number(process.env.RELAY_POLL_MS ?? 250);
const BATCH = Number(process.env.RELAY_BATCH ?? 100);

const pool = new pg.Pool({ connectionString: DATABASE_URL });

async function relayOnce(nc: NatsConnection): Promise<number> {
  const js = nc.jetstream();
  const client = await pool.connect();
  let published = 0;
  try {
    await client.query("BEGIN");
    const { rows } = await client.query(
      `SELECT id, subject, msg_id, payload FROM outbox
       WHERE published_at IS NULL ORDER BY id
       FOR UPDATE SKIP LOCKED LIMIT $1`,
      [BATCH],
    );
    for (const r of rows) {
      const h = headers();
      h.set("Nats-Msg-Id", r.msg_id); // dedupe key
      await js.publish(r.subject, r.payload, { headers: h });
      await client.query(
        `UPDATE outbox SET published_at = now() WHERE id = $1`,
        [r.id],
      );
      published++;
    }
    await client.query("COMMIT");
  } catch (err) {
    await client.query("ROLLBACK").catch(() => {});
    throw err;
  } finally {
    client.release();
  }
  return published;
}

async function main() {
  const nc = await connect({ servers: NATS_URL });
  console.log(`relay: connected to ${NATS_URL}, polling every ${POLL_MS}ms`);

  let stopping = false;
  const stop = async () => {
    stopping = true;
    await nc.drain();
    await pool.end();
    process.exit(0);
  };
  process.on("SIGINT", stop);
  process.on("SIGTERM", stop);

  while (!stopping) {
    try {
      const n = await relayOnce(nc);
      if (n > 0) console.log(`relay: published ${n} message(s)`);
    } catch (err) {
      console.error("relay: tick failed, retrying:", err);
    }
    await new Promise((r) => setTimeout(r, POLL_MS));
  }
}

main().catch((err) => {
  console.error("relay: fatal:", err);
  process.exit(1);
});
