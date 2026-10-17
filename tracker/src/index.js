// Outreach tracker: signed open pixel, click redirect and unsubscribe page.
// The local app signs every URL with SIGNING_SECRET (HMAC-SHA256), so this worker only redirects to links
// that were in our own emails. The local app pulls events from /events with API_KEY.

const GIF = Uint8Array.from(atob("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"), (c) => c.charCodeAt(0));
const NO_STORE = { "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", Pragma: "no-cache" };
const SID = /^[A-Za-z0-9_-]{6,32}$/;
const enc = new TextEncoder();

function b64url(bytes) {
  let s = "";
  for (const b of new Uint8Array(bytes)) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function fromB64url(s) {
  s = s.replace(/-/g, "+").replace(/_/g, "/");
  while (s.length % 4) s += "=";
  return new TextDecoder().decode(Uint8Array.from(atob(s), (c) => c.charCodeAt(0)));
}

let key;
async function sign(env, msg) {
  key ??= crypto.subtle.importKey("raw", enc.encode(env.SIGNING_SECRET), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return b64url(await crypto.subtle.sign("HMAC", await key, enc.encode(msg))).slice(0, 22);
}

async function valid(env, msg, sig) {
  const want = await sign(env, msg);
  if (!sig || want.length !== sig.length) return false;
  let diff = 0;
  for (let i = 0; i < want.length; i++) diff |= want.charCodeAt(i) ^ sig.charCodeAt(i);
  return diff === 0;
}

function record(env, req, type, sid, url = null) {
  return env.DB.prepare("INSERT INTO events (type, sid, url, ua, country, head, ts) VALUES (?, ?, ?, ?, ?, ?, ?)")
    .bind(type, sid, url, (req.headers.get("user-agent") || "").slice(0, 300), req.cf?.country || null,
      req.method === "HEAD" ? 1 : 0, Date.now())
    .run();
}

function page(body, status = 200) {
  return new Response(
    `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Email preferences</title>
<style>body{font:16px/1.5 system-ui,sans-serif;max-width:420px;margin:15vh auto;padding:0 16px;color:#222}
button{font:inherit;padding:8px 18px;border-radius:6px;border:1px solid #888;background:#fff;cursor:pointer}</style>
${body}`,
    { status, headers: { "Content-Type": "text/html; charset=utf-8", ...NO_STORE, "X-Robots-Tag": "noindex" } },
  );
}

export default {
  async fetch(req, env, ctx) {
    const url = new URL(req.url);
    const p = url.pathname.split("/").filter(Boolean);

    // Open pixel: /o/{sid}/{sig}.gif. Always returns the image; only valid signatures are recorded.
    if (p[0] === "o" && p.length === 3 && SID.test(p[1])) {
      if (await valid(env, `o:${p[1]}`, p[2].replace(/\.gif$/, ""))) ctx.waitUntil(record(env, req, "open", p[1]));
      return new Response(req.method === "HEAD" ? null : GIF, { headers: { "Content-Type": "image/gif", ...NO_STORE } });
    }

    // Click: /c/{sid}/{base64url(target)}/{sig}
    if (p[0] === "c" && p.length === 4 && SID.test(p[1])) {
      let target = "";
      try { target = fromB64url(p[2]); } catch { /* invalid */ }
      if (!/^https?:\/\//i.test(target) || !(await valid(env, `c:${p[1]}:${target}`, p[3]))) {
        return new Response("This link is invalid.", { status: 400 });
      }
      ctx.waitUntil(record(env, req, "click", p[1], target));
      return new Response(null, { status: 302, headers: { Location: target, ...NO_STORE } });
    }

    // Unsubscribe: GET shows a button, POST records it. Scanners follow GETs; only a person clicks the button.
    if (p[0] === "u" && p.length === 3 && SID.test(p[1])) {
      if (!(await valid(env, `u:${p[1]}`, p[2]))) return page("<p>This link is invalid.</p>", 400);
      if (req.method === "POST") {
        await record(env, req, "unsubscribe", p[1]);
        return page("<p>You're unsubscribed. You won't receive further emails in this sequence.</p>");
      }
      return page('<form method="post"><p>Stop receiving these emails?</p><button type="submit">Unsubscribe</button></form>');
    }

    // Event feed for the local app: /events?since={id}
    if (p[0] === "events" && p.length === 1) {
      if (!env.API_KEY || req.headers.get("authorization") !== `Bearer ${env.API_KEY}`) {
        return new Response("unauthorized", { status: 401 });
      }
      const since = Math.max(0, Math.floor(Number(url.searchParams.get("since")) || 0));
      const { results } = await env.DB.prepare(
        "SELECT id, type, sid, url, ua, country, head, ts FROM events WHERE id > ? ORDER BY id LIMIT 1000",
      ).bind(since).all();
      return Response.json({ events: results });
    }

    if (p[0] === "health") return Response.json({ ok: true });
    return new Response("Not found", { status: 404 });
  },

  async scheduled(_event, env) {
    await env.DB.prepare("DELETE FROM events WHERE ts < ?").bind(Date.now() - 180 * 86400000).run();
  },
};
