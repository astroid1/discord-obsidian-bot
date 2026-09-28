/**
 * Dead-man's switch for the bot, running on Cloudflare so it still works when the bot's host
 * is off. The bot POSTs /beat every few minutes; a cron checks the last beat and posts to a
 * Discord webhook when it goes quiet, again every REMIND_HOURS while it stays quiet, and once
 * more when it comes back.
 *
 * Bindings: KV `STATE`. Secrets: HEARTBEAT_TOKEN, DISCORD_WEBHOOK_URL.
 * Vars: GRACE_MINUTES (default 15), REMIND_HOURS (default 12).
 */

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

function authorized(request, env) {
  const got = (request.headers.get("authorization") || "").replace(/^Bearer\s+/i, "");
  const want = env.HEARTBEAT_TOKEN || "";
  if (!want || got.length !== want.length) return false;
  let diff = 0;
  for (let i = 0; i < want.length; i++) diff |= got.charCodeAt(i) ^ want.charCodeAt(i);
  return diff === 0;
}

function minutes(ms) {
  const m = Math.round(ms / 60000);
  if (m < 90) return `${m} min`;
  const h = Math.floor(m / 60);
  return h < 48 ? `${h} h ${m % 60} min` : `${Math.floor(h / 24)} days`;
}

async function notify(env, content) {
  if (!env.DISCORD_WEBHOOK_URL) return;
  await fetch(env.DISCORD_WEBHOOK_URL, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ username: "Watchdog", content, allowed_mentions: { parse: [] } }),
  });
}

async function beat(request, env) {
  if (!authorized(request, env)) return json({ error: "unauthorized" }, 401);
  let body = {};
  try {
    body = await request.json();
  } catch {
    return json({ error: "invalid json" }, 400);
  }
  const service = String(body.service || "").slice(0, 60);
  if (!/^[a-z0-9-]+$/.test(service)) return json({ error: "bad service" }, 400);
  const now = Date.now();
  const key = `beat:${service}`;
  const prev = await env.STATE.get(key, "json");
  const rec = {
    service,
    label: String(body.label || service).slice(0, 80),
    at: now,
    intervalSec: Math.min(Math.max(Number(body.intervalSec) || 300, 60), 86400),
    info: body.info && typeof body.info === "object" ? body.info : {},
    downSince: null,
    lastAlertAt: null,
  };
  if (prev && prev.downSince) {
    await notify(env, `🟢 **${rec.label} is back** after ${minutes(now - prev.downSince)} offline.`);
  }
  await env.STATE.put(key, JSON.stringify(rec));
  return json({ ok: true });
}

async function check(env) {
  const grace = (Number(env.GRACE_MINUTES) || 15) * 60000;
  const remind = (Number(env.REMIND_HOURS) || 12) * 3600000;
  const now = Date.now();
  const { keys } = await env.STATE.list({ prefix: "beat:" });
  for (const { name } of keys) {
    const rec = await env.STATE.get(name, "json");
    if (!rec) continue;
    const limit = Math.max(grace, rec.intervalSec * 3000);
    const silent = now - rec.at;
    if (silent <= limit) continue;
    if (rec.lastAlertAt && now - rec.lastAlertAt < remind) continue;
    const last = new Date(rec.at).toISOString().replace("T", " ").slice(0, 16);
    const first = !rec.downSince;
    await notify(
      env,
      first
        ? `🔴 **${rec.label} has been silent for ${minutes(silent)}** (last heartbeat ${last} UTC). ` +
            "Usually the host PC restarted and nobody has signed in, so Docker is not running."
        : `🔴 Still down: **${rec.label}** silent for ${minutes(silent)}.`,
    );
    rec.downSince = rec.downSince || rec.at;
    rec.lastAlertAt = now;
    await env.STATE.put(name, JSON.stringify(rec));
  }
}

async function status(request, env) {
  if (!authorized(request, env)) return json({ error: "unauthorized" }, 401);
  const { keys } = await env.STATE.list({ prefix: "beat:" });
  const out = [];
  for (const { name } of keys) {
    const rec = await env.STATE.get(name, "json");
    if (rec) out.push({ ...rec, silentSec: Math.round((Date.now() - rec.at) / 1000) });
  }
  return json(out);
}

export default {
  async fetch(request, env) {
    const { pathname } = new URL(request.url);
    if (request.method === "POST" && pathname === "/beat") return beat(request, env);
    if (request.method === "GET" && pathname === "/status") return status(request, env);
    return json({ ok: true, service: "watchdog" });
  },
  async scheduled(_event, env, ctx) {
    ctx.waitUntil(check(env));
  },
};
