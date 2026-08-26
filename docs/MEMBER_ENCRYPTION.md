# Member data encryption at rest — design decision

Status: implemented (v2.33.x). Scope owner: `app/member_crypto.py`.

This document exists to stop us over-promising. Read the **Boundary** section
before writing a single word of marketing copy about it.

---

## The one-sentence guarantee

> **The database file on its own is useless: copy `persona.db` off the server
> — by backup, snapshot, stray export or a stolen disk image — and the
> members' API keys, private messages and memory facts inside it are
> ciphertext.**

A non-engineer can check that claim in thirty seconds:

```sh
sqlite3 ~/.persona/persona.db "SELECT value FROM user_settings WHERE key LIKE 'byo_api_key%'"
sqlite3 ~/.persona/persona.db "SELECT body FROM dm_message ORDER BY id DESC LIMIT 5"
```

Every row must come back as `pcenc1:…`. If any row comes back readable, the
guarantee is broken for that row and you should treat it as a bug.

## The boundary — what is *not* true

**"The owner can never read a member's data" is false, and we will not say
it.** The server must have the plaintext at request time: it builds the
prompt, calls the model, renders the chat, extracts memory facts. The
decryption key sits on the same machine, in `~/.persona/member_keyring.key`.
So:

| Who | Can they read member content? |
|---|---|
| Someone who has only the DB file / a backup | **No.** |
| Someone who has the DB file **and** the key file | Yes — with a small script. |
| The instance owner, root on the box | **Yes.** They can read the key file, or just add three lines to the code and print everything. |
| A second member with full DB access | **No** — keys are per-user/per-thread and wrapped under the master key they do not have. |
| Us, after account deletion | **No** — deleting the account deletes the key (crypto-shredding). |

This is *encryption at rest against file-level exposure*, not end-to-end
encryption, and not zero-access hosting. Anyone who tells you otherwise is
describing a product that cannot also read your messages to answer them.

---

## What is encrypted

| Data | Where | Key | Status |
|---|---|---|---|
| LLM API keys, bot tokens, SMTP passwords | `user_settings.value` where the key name matches `api_key/apikey/token/password/passwd/secret/credential` | per-user | **encrypted** |
| Private messages | `dm_message.body` | per-thread | **encrypted** |
| AI reply drafts | `dm_ai_draft.body` | per-thread | **encrypted** |
| Browser notification bodies (they quote a DM) | `social_notif_item.body` | per-user (recipient) | **encrypted** |
| Memory facts of non-owner users | `user_memory.text` | per-user | **encrypted** |

## What is deliberately left in cleartext — and why

* **`chat_message.content` — the chat itself.** This is the honest one.
  In-chat search and keyword recall are the product's core feature and they
  run on an **FTS5 index** (`chat_message_fts`, bm25) plus `LIKE` fallback.
  FTS5 cannot index ciphertext. Encrypting the bodies would mean either
  keeping a plaintext search index (encrypting nothing in practice) or
  shipping a chat where recall silently stops working. A half-broken chat is
  worse than an honest gap, so the bodies stay readable and we say so.
  Same reasoning for `chat_session.summary` / `title`.
* **The owner's own `user_memory` rows.** The owner *is* the person who holds
  the database; encrypting their data from themselves buys nothing. It would
  cost a lot: `user_memory.text` is read by raw SQL joins in the dream,
  projection and knowledge-graph subsystems (`app/adapters/memory/*`,
  `app/adapters/projection/*`, `app/knowledge_graph.py`), all of which are
  owner-only. Leaving owner rows in cleartext means none of those paths ever
  meet a ciphertext, and members' rows never appear in them.
* **E-mail addresses, display names, timestamps, counters, the social graph.**
  Metadata. Encrypting it would break every listing, sort and join in the
  product and protect nothing that the graph itself does not already reveal.
* **Notification *titles*** ("Message from Anna") — no content, and they are
  used for de-duplication.
* **E-mail that leaves the box.** A notification e-mail carries the quoted
  message in the clear, because that is what e-mail is. Encryption at rest
  does not follow data out the door.
* **The owner's own global `kv_settings` secrets.** Unchanged — that is what
  `app/vault.py` is for, and it needs a human-typed master password. Out of
  scope here: this work is about the data of people who are *not* the owner.

---

## Where the key lives

```
$PERSONA_DATA_DIR/member_keyring.key        # default: ~/.persona/member_keyring.key
```

32 random bytes, base64url, `chmod 600`, created on first use, **outside the
database and outside the repo** (the same place `billing_secrets.json` already
lives). `PERSONA_MEMBER_KEYRING_KEY` (base64url, 32 bytes) overrides the file
for containers with an ephemeral filesystem.

Two levels (envelope encryption):

1. the master key never encrypts data — it derives a wrapping key per scope
   (`HMAC-SHA256(master, "persona.member_crypto.v1|<scope>|<id>")`);
2. each user and each DM thread has its own random 32-byte DEK, stored in the
   DB *wrapped* (`user_encryption_key`, `dm_thread_key`, migration 237).

Why bother with the second level: deleting an account cascades the DEK away,
which makes every row that user ever wrote permanently unreadable — including
rows that survived in an old backup. And one leaked DEK does not open anyone
else's data.

> ⚠ **Losing `member_keyring.key` loses all encrypted data.** A DB backup
> without the key is unreadable; a backup *with* the key next to it is a
> backup with no encryption. Back the key up separately, once, somewhere the
> DB backups are not. This is the trade the design makes on purpose.

## The cipher, and why it is not `cryptography`

`cryptography` (the Fernet library `app/vault.py` uses) is **not a required
dependency** of this project — it lives in the optional `backup` extra and is
**not installed** in the running environment. An encryption feature that
silently degrades to a no-op on the production box is worse than no feature:
it produces a false promise. So the primitive is stdlib-only, standard
composition, and versioned in the envelope so a future AES-GCM can be added
without a data migration:

```
enc_key, mac_key = HMAC-SHA256(dek, "E"|nonce), HMAC-SHA256(dek, "M"|nonce)
keystream        = HMAC-SHA256(enc_key, nonce || counter) blocks   # CTR
ciphertext       = plaintext XOR keystream
tag              = HMAC-SHA256(mac_key, nonce || ciphertext)[:16]  # encrypt-then-MAC
stored           = "pcenc1:" || base64url(nonce(16) || tag(16) || ciphertext)
```

Nonce is 16 fresh random bytes per write, so the same plaintext stores
differently every time (which is exactly why SQL equality/`LIKE` matching on
these columns is gone — see below). Tag comparison is `hmac.compare_digest`.

The `pcenc1:` marker is not decoration: it is how anyone — code, a test, or a
person with `sqlite3` — tells an encrypted row from a legacy plaintext one.
A half-encrypted table with no marker was the outcome we were told to avoid.

---

## Key derivation: why *not* the user's password

The obvious design is Argon2id/scrypt over the member's password, unwrapped at
login, held for the session only. We rejected it, deliberately:

1. **No recovery.** A forgotten password would mean permanently lost content.
   Mail on this box is currently broken, so we cannot even run a normal reset
   flow — a member locked out would simply lose everything, with no way for us
   to help. A recovery code at signup only moves the problem: a code written
   down and lost is the same outcome, and a code we store server-side *is*
   the server-side key copy we were trying to avoid.
2. **Background work has no key.** Nightly memory consolidation, dream/
   reflection cycles, summarisation, embeddings, the DM auto-reply that fires
   while the member is offline, and notification delivery all run with nobody
   logged in. Under a password-derived key those features would have to either
   stop for encrypted fields or keep a server-side wrapped copy of the key —
   and the moment you keep that copy, the "only the user can decrypt" claim is
   gone anyway.
3. **We were asked to pick one and be explicit.** We picked the server-held
   key. The guarantee we actually deliver is therefore
   *"not readable from the database file alone"*, not *"not readable by us"*.

**Consequence for the recovery story: a forgotten password loses nothing.**
Password reset (once mail works) is a normal reset; data survives, because
data was never tied to the password. What *is* unrecoverable is the loss of
`member_keyring.key`. That is the risk we chose to carry, and it is a risk an
operator can mitigate with a one-line backup.

## What breaks for background features

Nothing. That is the direct consequence of the choice above: the key is
available to the process at all times, so nightly consolidation, dream cycles,
DM auto-reply, notification delivery and export all work exactly as before.

Two real behavioural changes, both internal:

* **`user_memory` de-duplication on insert** no longer compares text in SQL
  (`lower(text) = lower(?)`) for encrypted users — the same fact encrypts to a
  different string every time. It now compares decrypted text in Python, with
  `casefold()`, which is *more* correct for Cyrillic than SQLite's ASCII-only
  `lower()`. Search over facts (`search_memory`, `forget`, `_candidates`,
  `consolidate_memories`) already ran in Python over `list_memory`, so it was
  unaffected.
* **Vector memory (`hybrid`/`vector` recall)** remains owner-only and untouched
  — it indexes `chat_message`, which is not encrypted.

## Export and deletion

* **Export** (`/settings/my-data/export.json`, `app/auth/data_export.py`)
  decrypts everything it returns: memory facts, DM bodies, drafts,
  notification bodies. An export the member cannot read would not satisfy a
  right of access. Secrets stay redacted exactly as before — but `length` is
  now measured on the *decrypted* value, not the envelope.
  The Markdown memory export (`/settings/privacy`) decrypts too.
* **Deletion** (`app/auth/account_delete.py`) removes the user's DEK by
  `ON DELETE CASCADE` (`user_encryption_key`), and a deleted DM thread removes
  `dm_thread_key`. After deletion the data is not merely gone from the live
  DB — surviving copies elsewhere cannot be decrypted by anyone, including us.

## Legacy rows

Rows written before this shipped stay plaintext until the one-time backfill
(`app/member_crypto_backfill.py`, run from the lifespan right after
migrations) rewrites them. It is idempotent, marks itself in `kv_settings`
(`member_encryption_backfill_v1`, `member_encryption_memory_backfill_v1`), and
never touches a row that already carries the `pcenc1:` marker. The memory
stage refuses to run while the owner cannot be resolved (a brand-new install)
and retries on the next boot, so the owner's own facts are never encrypted by
accident.

**Rewriting a row is not enough, and this nearly shipped wrong.** An `UPDATE`
leaves the old page — with the plaintext on it — in the file's free list and
in the WAL, so `grep persona.db` still finds the "already encrypted" key. The
backfill therefore finishes with `wal_checkpoint(TRUNCATE)` → `VACUUM` →
`wal_checkpoint(TRUNCATE)` (in that order: in WAL mode the VACUUM output
itself lands in the WAL, so it needs a checkpoint after it too). A test
asserts the canary is absent from `persona.db` *and* `persona.db-wal`.

What that still does **not** do: it does not wipe disk sectors, and it does
not reach backups taken before the upgrade. **A DB backup made before this
release contains plaintext keys and messages forever — delete those backups.**

## Failure behaviour

Never a 500:

* no key / unwritable data dir → **writes store plaintext** and log
  `member_crypto.encrypt.unavailable` at WARNING. Data is never lost; the
  promise is simply not kept, visibly, in the logs.
* ciphertext present but the key is wrong or gone → **reads return an empty
  string** and log `member_crypto.decrypt.no_key` / `.failed` at ERROR. An
  empty field is more honest than a crash or mojibake.

---

## Copy for the site — use this wording, not better-sounding wording

Russian (what goes on the page):

> **Шифрование данных**
> Ключи ваших LLM-провайдеров, личные сообщения и факты вашей памяти хранятся
> в базе в зашифрованном виде. Копия базы данных — резервная, украденная или
> случайно выгруженная — без ключа шифрования бесполезна, а ключ лежит вне
> базы. При удалении аккаунта удаляется и ваш ключ: после этого расшифровать
> ваши данные нельзя уже никак, даже из старой резервной копии.
>
> **Чего это не значит.** Persona — не сервис с нулевым доступом. Чтобы
> ответить вам, сервер читает ваш текст в открытом виде в момент запроса, и
> тот, кто управляет сервером, технически может его прочитать. Мы шифруем
> данные «в покое», а не прячем их от самих себя. Тексты чатов с ассистентом
> шифрованием не покрыты: по ним работает поиск и «вспоминание» — без этого
> ассистент перестал бы вас помнить.

English gloss:

> Your provider API keys, direct messages and memory facts are stored
> encrypted. A copy of the database — a backup, a stolen one, an accidental
> export — is useless without the encryption key, and the key is not in the
> database. Deleting your account deletes your key: after that nobody can
> decrypt your data, not even from an old backup.
>
> **What this does not mean.** Persona is not zero-access hosting. To answer
> you the server reads your text in the clear at request time, and whoever
> runs the server can technically read it. We encrypt data at rest; we do not
> pretend to hide it from ourselves. Assistant chat transcripts are not
> covered: search and recall run over them, and without that the assistant
> would stop remembering you.

Phrases that must **not** appear anywhere: "end-to-end", "zero-knowledge",
"zero-access", "even we cannot read it", "only you hold the key".
