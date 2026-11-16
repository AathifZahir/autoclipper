"""Built-in app IDs, so people can use AutoClipper without registering their own developer apps.

None of these are secrets:
- The Twitch client ID is a "Public" client (device login, no secret).
- The TikTok client key appears in every TikTok login link. TikTok's client SECRET is never shipped:
  logins are finished by the AutoClipper login helper (see login-helper/), which holds the secret.

Advanced users can still use their own apps by filling in config.toml (client_id,
tiktok_client_key + tiktok_client_secret) - those always win over these defaults.
"""

TWITCH_CLIENT_ID = "vg2e6zxkqpkmwani6j7a3iobzch8o2"

# "sbaw..." = TikTok Sandbox key. Swap for the Production key (and update login-helper/wrangler.toml)
# once TikTok approves the app.
TIKTOK_CLIENT_KEY = "sbawtbye7p9ei56b4c"

LOGIN_HELPER_URL = "https://autoclipper-login.aathifzahir.workers.dev"   # source: login-helper/
