// Test-only ESM loader: maps the platform modules 'sdk' and 'sdk/db' to in-memory fakes.
export async function resolve(spec, ctx, next) {
  if (spec === 'sdk') return { url: new URL('./fake-sdk.mjs', import.meta.url).href, shortCircuit: true };
  if (spec === 'sdk/db') return { url: new URL('./fake-sdk-db.mjs', import.meta.url).href, shortCircuit: true };
  return next(spec, ctx);
}
