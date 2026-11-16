// AutoClipper login helper (Cloudflare Worker).
//
// TikTok needs the app's client secret to finish a login and to renew it. A secret can't be
// shipped inside a desktop app that anyone can download, so the desktop app sends the one-time
// login code here, and this helper adds the secret and asks TikTok for the tokens.
//
// It stores nothing and logs nothing: every request is passed straight to TikTok and the answer
// goes straight back to the user's own computer.
//
// Settings (wrangler.toml / Cloudflare dashboard):
//   TIKTOK_CLIENT_KEY     (plain variable)  - the app's client key (public, it's in every login link)
//   TIKTOK_CLIENT_SECRET  (secret)          - set with: npx wrangler secret put TIKTOK_CLIENT_SECRET
//   REDIRECT_URI          (plain variable)  - must match the redirect URI registered with TikTok

const TIKTOK = "https://open.tiktokapis.com/v2/oauth";

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
  });
}

async function tiktok(path, fields, env) {
  const body = new URLSearchParams({
    client_key: env.TIKTOK_CLIENT_KEY,
    client_secret: env.TIKTOK_CLIENT_SECRET,
    ...fields,
  });
  const r = await fetch(`${TIKTOK}/${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded", "Cache-Control": "no-cache" },
    body,
  });
  const text = await r.text();
  return new Response(text, {
    status: r.status,
    headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "GET" && url.pathname === "/") {
      return json({ service: "AutoClipper login helper", stores_data: false,
                    source: "https://github.com/AathifZahir/autoclipper/tree/main/login-helper" });
    }
    if (request.method !== "POST") return json({ error: "Use POST." }, 405);
    if (!env.TIKTOK_CLIENT_KEY || !env.TIKTOK_CLIENT_SECRET) return json({ error: "Helper is not configured." }, 500);

    let data;
    try {
      data = await request.json();
    } catch {
      return json({ error: "Send JSON." }, 400);
    }
    const str = (v) => (typeof v === "string" ? v.trim() : "");

    // 1) Finish a login: one-time code (+ PKCE verifier) -> access & refresh tokens
    if (url.pathname === "/tiktok/token") {
      const code = str(data.code), verifier = str(data.code_verifier);
      if (!code || !verifier) return json({ error: "code and code_verifier are required." }, 400);
      if (str(data.redirect_uri) !== env.REDIRECT_URI) return json({ error: "Unexpected redirect_uri." }, 400);
      return tiktok("token/", {
        code, code_verifier: verifier, grant_type: "authorization_code", redirect_uri: env.REDIRECT_URI,
      }, env);
    }

    // 2) Renew a login (access tokens last 24 hours)
    if (url.pathname === "/tiktok/refresh") {
      const rt = str(data.refresh_token);
      if (!rt) return json({ error: "refresh_token is required." }, 400);
      return tiktok("token/", { grant_type: "refresh_token", refresh_token: rt }, env);
    }

    // 3) Disconnect: revoke the user's access so the app can't be used with it any more
    if (url.pathname === "/tiktok/revoke") {
      const token = str(data.token);
      if (!token) return json({ error: "token is required." }, 400);
      return tiktok("revoke/", { token }, env);
    }

    return json({ error: "Not found." }, 404);
  },
};
