# Webhook Signing — Receiver-Side Verification

Every outbound HTTP POST that Persona sends to a user-configured webhook URL
is signed with **HMAC-SHA256**. The signature lets your receiver prove that
the request really came from your Persona instance and that the body was not
tampered with in transit.

## Headers

Persona attaches two custom headers to every delivery:

| Header                | Value                                                                 |
| --------------------- | --------------------------------------------------------------------- |
| `Content-Type`        | `application/json`                                                    |
| `X-Persona-Signature` | `sha256=<hexdigest>` — HMAC-SHA256 of the **raw request body bytes**  |
| `X-Persona-Timestamp` | ISO-8601 UTC time the event was queued (also mirrored in body `ts`)   |

The HMAC key is the per-webhook `secret`. You can find / regenerate it in the
Persona UI at `/webhooks`. The first time a webhook fires Persona generates a
cryptographically-random 32-byte URL-safe token via `secrets.token_urlsafe(32)`
and persists it; the secret never changes on its own afterwards.

## Body shape

```json
{
  "event": "capture.saved",
  "ts": "2026-06-01T12:34:56.789012+00:00",
  "payload": { "...": "event-specific fields" }
}
```

The signature is computed over the **exact bytes** of the HTTP body, not over
any re-serialised form. Always read the raw body before parsing JSON —
re-encoding will change byte-for-byte output (key ordering, whitespace,
Unicode escaping) and your HMAC will not match.

## Replay protection

Reject any request whose `X-Persona-Timestamp` is more than ~5 minutes away
from your server's clock. Together with HMAC this prevents an attacker from
re-playing a captured request.

## Verifying in Python

```python
import hmac
import hashlib
from datetime import datetime, timezone, timedelta

SECRET = "<paste-the-secret-from-/webhooks>"
MAX_SKEW = timedelta(minutes=5)


def verify(raw_body: bytes, signature_header: str, ts_header: str) -> bool:
    # 1. Reject stale / future timestamps.
    try:
        ts = datetime.fromisoformat(ts_header)
    except ValueError:
        return False
    if abs(datetime.now(timezone.utc) - ts) > MAX_SKEW:
        return False

    # 2. Constant-time HMAC compare.
    expected = "sha256=" + hmac.new(
        SECRET.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


# --- FastAPI example -------------------------------------------------------
# from fastapi import FastAPI, Request, HTTPException
# app = FastAPI()
#
# @app.post("/persona-webhook")
# async def receive(request: Request) -> dict[str, bool]:
#     body = await request.body()
#     sig = request.headers.get("X-Persona-Signature", "")
#     ts  = request.headers.get("X-Persona-Timestamp", "")
#     if not verify(body, sig, ts):
#         raise HTTPException(status_code=401, detail="bad signature")
#     return {"ok": True}
```

## Verifying in Node.js

```javascript
import crypto from "node:crypto";

const SECRET = "<paste-the-secret-from-/webhooks>";
const MAX_SKEW_MS = 5 * 60 * 1000;

export function verify(rawBody, signatureHeader, tsHeader) {
  // 1. Reject stale / future timestamps.
  const ts = Date.parse(tsHeader);
  if (Number.isNaN(ts) || Math.abs(Date.now() - ts) > MAX_SKEW_MS) {
    return false;
  }

  // 2. Constant-time HMAC compare.
  const expected =
    "sha256=" +
    crypto.createHmac("sha256", SECRET).update(rawBody).digest("hex");

  const a = Buffer.from(expected);
  const b = Buffer.from(signatureHeader || "");
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

// --- Express example -----------------------------------------------------
// import express from "express";
// const app = express();
//
// app.post(
//   "/persona-webhook",
//   express.raw({ type: "application/json" }),
//   (req, res) => {
//     const ok = verify(
//       req.body,                              // Buffer of raw bytes
//       req.header("x-persona-signature"),
//       req.header("x-persona-timestamp"),
//     );
//     if (!ok) return res.status(401).send("bad signature");
//     res.json({ ok: true });
//   },
// );
```

## Debugging from the UI

The Webhooks settings page (`/webhooks`) has a **Test** button. It hits
`POST /api/webhooks/{id}/test`, which calls
`app.webhooks.dispatcher.dispatch_test()` and fires a synthetic
`capture.saved` event at your endpoint with the **exact same** signed
envelope as a real production event — so once your receiver accepts the
test fire it will accept real events too.

## Common pitfalls

- **Re-parsing the body before verifying.** Frameworks that helpfully
  JSON-decode the body lose the original bytes. Always grab the raw body
  first; verify; *then* parse.
- **String compare instead of `compare_digest` / `timingSafeEqual`.**
  Naive `==` leaks the prefix length of the correct signature via timing
  side-channels.
- **Empty `secret` in DB.** A freshly-created webhook starts with an
  empty secret; Persona backfills it on the first POST and logs
  `persona.webhook.sign.secret_generated`. Read the secret from the UI
  *after* the first delivery (or fire one manually with the Test button).
- **Comparing without the `sha256=` prefix.** Persona always sends the
  prefix; the receiver-side helper in this repo accepts both forms, but
  third-party verifiers may not. Match exactly what you send.
