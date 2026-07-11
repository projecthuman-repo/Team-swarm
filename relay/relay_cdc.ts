/**
 * CDC outbox relay — the higher-throughput option from runbook §7.
 *
 * Instead of polling, tails Postgres logical replication (wal_level=logical
 * is set in deploy/compose.yaml; db/schema.sql creates PUBLICATION
 * outbox_pub) and publishes each outbox INSERT to JetStream with its
 * Nats-Msg-Id. A startup sweep drains any backlog written while the relay
 * was down; the server-side dupe window makes overlap with the sweep safe.
 *
 * Run:  npm run start:cdc      (baseline poll relay: npm start)
 * Only one CDC relay per slot; run the poll relay OR the CDC relay.
 */
import { connect, headers, type NatsConnection } from "nats";
import pg from "pg";
import {
  LogicalReplicationService,
  PgoutputPlugin,
  type Pgoutput,
} from "pg-logical-replication";

const NATS_URL = process.env.NATS_URL ?? "nats://localhost:4222";
const DATABASE_URL =
  process.env.DATABASE_URL ?? "postgres://postgres:dev@localhost:5432/swarm";
const SLOT = process.env.CDC_SLOT ?? "outbox_slot";
const PUBLICATION = process.env.CDC_PUBLICATION ?? "outbox_pub";

const pool = new pg.Pool({ connectionString: DATABASE_URL });

async function publishRow(
  nc: NatsConnection,
  row: { id: string | number; subject: string; msg_id: string; payload: Buffer },
) {
  const js = nc.jetstream();
  const h = headers();
  h.set("Nats-Msg-Id", row.msg_id); // dedupe key
  await js.publish(row.subject, row.payload, { headers: h });
  await pool.query(`UPDATE outbox SET published_at = now() WHERE id = $1`, [
    row.id,
  ]);
}

async function sweepBacklog(nc: NatsConnection) {
  const { rows } = await pool.query(
    `SELECT id, subject, msg_id, payload FROM outbox
     WHERE published_at IS NULL ORDER BY id`,
  );
  for (const r of rows) await publishRow(nc, r);
  if (rows.length) console.log(`cdc-relay: swept ${rows.length} backlog row(s)`);
}

async function ensureSlot() {
  const { rows } = await pool.query(
    `SELECT 1 FROM pg_replication_slots WHERE slot_name = $1`,
    [SLOT],
  );
  if (rows.length === 0) {
    await pool.query(
      `SELECT pg_create_logical_replication_slot($1, 'pgoutput')`,
      [SLOT],
    );
    console.log(`cdc-relay: created replication slot ${SLOT}`);
  }
}

async function main() {
  const nc = await connect({ servers: NATS_URL });
  await ensureSlot();
  await sweepBacklog(nc);

  const service = new LogicalReplicationService(
    { connectionString: DATABASE_URL },
    { acknowledge: { auto: true, timeoutSeconds: 10 } },
  );
  const plugin = new PgoutputPlugin({
    protoVersion: 1,
    publicationNames: [PUBLICATION],
  });

  service.on("data", async (_lsn: string, log: Pgoutput.Message) => {
    if (log.tag !== "insert") return;
    const msg = log as Pgoutput.MessageInsert;
    if (msg.relation.name !== "outbox") return;
    const row = msg.new as {
      id: string | number;
      subject: string;
      msg_id: string;
      payload: Buffer;
      published_at: Date | null;
    };
    if (row.published_at) return; // already handled by the startup sweep
    try {
      await publishRow(nc, row);
    } catch (err) {
      console.error("cdc-relay: publish failed (will re-sweep):", err);
    }
  });

  service.on("error", (err: Error) =>
    console.error("cdc-relay: replication error:", err),
  );

  process.on("SIGINT", async () => {
    await service.stop();
    await nc.drain();
    await pool.end();
    process.exit(0);
  });

  console.log(`cdc-relay: tailing ${PUBLICATION} via slot ${SLOT}`);
  // subscribe() resolves when the stream ends; loop to reconnect.
  for (;;) {
    try {
      await service.subscribe(plugin, SLOT);
    } catch (err) {
      console.error("cdc-relay: subscribe failed, retrying in 2s:", err);
    }
    await new Promise((r) => setTimeout(r, 2000));
  }
}

main().catch((err) => {
  console.error("cdc-relay: fatal:", err);
  process.exit(1);
});
