// Waiting without timers. The Serverless runtime has no setTimeout and its clock
// only advances across I/O (measured: a CPU busy-wait never sees Date.now() move
// and is killed by the CPU timer). So a pause is a loop of tiny awaited I/O
// calls: getMe (~25 ms, no side effects), or a trivial DB query if the Bot API
// is unavailable. Used only on the failure path (webhook retry backoff) and to
// collect album parts.
import { api, db } from 'sdk';
import { sql } from 'sdk/db';

export async function pause(ms) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    try {
      await api.getMe();
    } catch {
      await db.get(sql`SELECT 1 AS x`);
    }
  }
}
