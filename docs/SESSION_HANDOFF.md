# Persona — handoff документ для следующей сессии

> Этот файл — память между сессиями. Юзер — Ярослав (SwairIt), инди-разработчик,
> делает Persona как **личный AI с памятью** + строит путь к **собственной
> fine-tuned модели PersonaAI**. Прочитай ВЕСЬ файл прежде чем продолжать.

## Контекст и роль

Сессия длилась долго, было сделано T9-T28. Юзер хочет:
1. Тёплый, прямой, по-человечески общающийся AI (не робот-секретарша)
2. **Свою модель** PersonaAI через LoRA fine-tune на личных Q&A парах
3. Полный контроль: открытый локальный стек, без зависимости от чужих API
4. Удобный UX как у Claude/ChatGPT

**Tone правила**:
- Английский для кода/комментариев/docstrings/коммитов; русский ТОЛЬКО для чата с юзером
- Tough-mentor mode: без сюсюканья, без "great question!". Лидируй с несогласием когда юзер не прав
- Git commits в past tense на русском, author всегда `112168281+SwairIt@users.noreply.github.com`
- Push: токен из `C:/www-Yaroslav/SchoolProject/.env` (TOKEN=), URL `https://SwairIt:${TOKEN}@github.com/SwairIt/persona.git`, через `sed "s|${TOKEN}|<TOKEN>|g"` чтобы не светить в логе
- НЕ трогай `C:\www-Yaroslav\SchoolProject` — это отдельный проект Doday, токен только оттуда

## Архитектура текущего состояния

```
Persona-server (Windows server, C:\www-Yaroslav\Persona)
  ├── FastAPI + uvicorn на 127.0.0.1:8000
  ├── devtunnel host -t kind-ocean-5s32cxv → https://jqktqlvt-8000.euw.devtunnels.ms
  │   (старый jswvbzgl-8000... умер, пересоздан новый T18-fix; токен принадлежит SwairIt GitHub auth)
  ├── РЕЗЕРВНЫЙ публичный URL (T29): Tailscale Funnel →
  │   https://bsql.tail318a09.ts.net  (проксирует на 127.0.0.1:8000, другой
  │   провайдер чем devtunnel, переживает ребут). CLI: C:\www-Yaroslav\Instruments\tailscale.exe
  │   Управление: `tailscale funnel status` / `tailscale funnel --bg 8000` / `tailscale funnel --https=443 off`
  │   Funnel включён в админке Tailscale (nodeAttr funnel). Auth-gate защищает так же как devtunnel.
  ├── SQLite в ~/.persona/persona.db
  └── Workspace files в ~/.persona/data/workspaces/{user_id}/

Tailscale-сеть юзера
  ├── Сервер Persona: 100.106.21.121 (Windows, 127 GB RAM, без GPU)
  └── Игровой PC юзера: 100.88.154.4 (Windows, 1050 Ti 4GB VRAM, i7-3770, 16 GB RAM)
       └── Ollama на :11434 (через OLLAMA_HOST=0.0.0.0:11434)
            Установлены: qwen2.5:3b, qwen2.5:7b, qwen2.5vl:3b, qwen2.5vl:7b,
            moondream:latest, gemma3:4b

Mac юзера (a1@MacBook-Air--Yaroslav)
  ├── persona-agent установлен через T18 one-click installer (curl ... | bash)
  ├── launchd plist com.swairit.persona-agent.plist
  ├── config.json: server=https://jqktqlvt-8000.euw.devtunnels.ms, agent_token
  └── screenshots/audio пишутся на сервер (не локально)

iPhone — только web viewer через Safari (Apple запрещает background capture для PWA)
```

## Что сделано (T9 → T28)

### T9-T10: LLM провайдеры расширены
- Provider literal: anthropic/openai/groq/gemini + yandex/gigachat/deepseek + **ollama**
- `_OpenAICompatibleClient` base class — наследуется OpenRouter/Mistral/Together/xAI/ProxyAPI/AITunnel
- OllamaClient endpoint поле = URL (по умолч. localhost:11434)
- `/settings/llm` показывает 14 провайдеров с описаниями и группировкой
- Migration кв `{provider}_model` для overrides модели

### T11: /chat — постоянные беседы с памятью
- Tables `chat_session` + `chat_message` (migration 158)
- Sidebar со списком сессий, новый чат, переименовать, удалить
- История 50 turns скармливается LLM каждый запрос
- T20: `_DEFAULT_HISTORY_TURNS = 50`
- T21: auto-summary через `maybe_summarise(session_id)` когда >60 messages → roll oldest into chat_session.summary (incremental через summary_up_to_id watermark)
- Streaming через SSE (T22.3): `/api/chat/sessions/{id}/send-stream`
- T22.5: keepalive каждые 0.5s через `data: {"type":"keepalive"}` чтобы devtunnel не резал
- T22.6: read_timeout 600s для Ollama (cold start 90+ sec)
- T22.10: thread.model реально используется через monkey-patch `client._inner._model`

### T13-T15: Multi-device storage
- Per-device storage policy (квота, retention, role primary/archive/viewer/passive)
- Selective sync filter (какие kinds событий получает device)
- `/storage` dashboard + retention policy + nightly cleanup worker
- Server-side cleanup_log audit trail

### T16-T17: Welcome + iOS ingest
- `/welcome` — pipeline страница с per-device инструкциями (User-Agent sniff)
- POST `/api/ingest/photo` для iOS Shortcuts (single token, Pillow normalisation)
- Bulk ZIP export `/storage/export.zip`
- T17: SW v1.66→1.67 bump, /help → /help/shortcuts (collision fix)

### T18: One-click Mac installer
- `/welcome/install/mac` — UI с кнопкой "Сгенерировать команду"
- POST `/welcome/install/mac/mint` создаёт agent_token, stash под install_id
- GET `/api/install/mac.sh?t=...` — single-use, выдаёт bash-installer с baked AGENT_TOKEN + SERVER_URL
- Скрипт: git clone, venv, requirements, config.json, launchd plist
- T18-fix: ZSH quote URL (`'...'`) + auto-detect public URL через X-Forwarded-Host

### T22: Model picker + ratings + vision
- `/api/llm/models` — собирает installed Ollama models + cloud defaults с descriptions
- Picker в /chat composer'е снизу, vision-модели помечены 👁
- T22.4: WebP→PNG конверсия через Pillow (Ollama не принимает WebP)
- T22.9: downscale до 1280px (vision encoder limit)
- T22.7: vision-friendly system prompt когда attached image

### T23: Training dataset collection
- Migration 162: `training_dataset` table
- Каждый /chat Q&A пишется ПОСЛЕ assistant turn (record_qa_pair)
- `/admin/dataset` — статы + экспорт JSONL (HuggingFace SFTTrainer format)
- 👍/👎 rating system (T26 fix через index вместо object ref)
- Когда юзер соберёт 1000+ rated пар — следующий тик: Kaggle Notebook + LoRA скрипт

### T24-T25: MCP + built-in tools
- Migration 163: chat_session.custom_system_prompt + chat_session.auto_switch_on_image + mcp_server table
- Migration 164: 5 built-in tools (builtin:read_file, list_dir, write_file, run_shell, git_status)
- `/admin/mcp` — CRUD страница серверов с описаниями
- `app/mcp/builtin_tools.py` — Python-native tools (без Node.js)
- `app/mcp/__init__.py` — enabled_builtin_tool_names() + parse_tool_calls() + call_tool() + build_tools_prompt()
- Tool-use loop в /send-stream: parse `<tool>name(args)</tool>` → call_tool → yield result → follow-up to LLM → up to 5 rounds
- Real MCP-протокол (subprocess + JSON-RPC) — TODO, не начат

### T26: UX полировка
- Stop button (AbortController + cancel stream)
- Type while thinking + Queue (Ctrl+Enter добавляет в очередь)
- Auto-process очереди после finally{}
- Markdown CSS как у Claude: h1/h2/h3, blockquote, table, code blocks, GFM checkboxes emerald galkы
- Thinking badge скрывается на первом delta event
- 👍/👎 кнопки через index в template (не stale object ref)
- max_tokens 1024 → 4096

### T27: Per-user workspace
- `data/workspaces/{user_id}/` для каждого юзера
- `app/workspace/dirs.py`: ensure_user_workspace, resolve_user_path (sandbox), list_user_files
- built-in tools теперь работают ВНУТРИ workspace через resolve_user_path
- `/workspace` — таблица файлов + download через `/workspace/file/{path}`
- LLM prompt сообщает что paths относительные к workspace, не abs

### T28: ЗАВЕРШЕНО (2026-06-08)
Юзер хотел: «выбрать в моём акке КУДА писать код — на Mac или на Windows. Каждый акк свой target».

Что **сделано** (всё проверено smoke-тестами на живом сервере):
- Migration 165: `device.is_code_write_target` column + table `workspace_file_event`
- Helper functions в `app/devices/storage_policy.py`: `set_code_write_target` (atomic
  unset all + set one), `clear_code_write_target`, `get_code_write_target`
- `is_code_write_target` добавлен в `DeviceRow` + `_row_to_device` projection
  (`app/devices/core.py`) — guard на `row.keys()` для окна до миграции
- `app/workspace/sync.py` (НОВЫЙ): `record_file_event`, `list_file_events_since`,
  `build_sync_payload` (dedup до latest-op-per-path, content читается с диска при pull,
  cursor = max event id)
- `write_file()` в `app/mcp/builtin_tools.py` эмитит workspace_file_event после записи
  (best-effort, content_bytes = utf-8 len)
- `POST /devices/{id}/code-target` (devices.py) — `enabled=1` ставит target, `enabled=0`
  чистит. UI: ☆/★ toggle + badge на каждой карточке (devices.html)
- `GET /api/workspace/sync` (workspace_admin.py) — X-Device-Token auth, 403 если device
  не target, 401 без/с битым токеном. Возвращает `{device_id, cursor, files, count}`,
  files = `[{relative_path, operation, content}]`. Агент сам держит cursor (?since=N).
- **ВАЖНО**: добавил `/api/workspace/sync` в allowlist `app/web/middleware/auth_gate.py`
  (иначе middleware 401-ит до хендлера — как `/api/sync/`, `/api/ingest/`)
- `/workspace` баннер "Файлы синхронизируются на: {device.name}" (workspace_admin.html)
- Mac agent: `sync_workspace_loop` + `_apply_workspace_file` (sandbox внутри base) в
  `mac-agent/persona_agent.py`, `pull_workspace()` в `sync_client.py`. Пишет в
  `~/persona-workspace/`, cursor в `.persona_sync_cursor`, self-disable без device_token,
  тихий backoff на 403.

Что **ОСТАЛОСЬ** (зона юзера):
- Переустановить Mac agent через `/welcome/install/mac` чтобы подхватить
  `sync_workspace_loop`. Существующие инсталлы не ломаются (loop self-disable без токена).

## Архитектурные принципы (соблюдай)

1. **Workspace на сервере — каноничен**. Device — это sync target, не источник правды.
2. **Sandbox** через `resolve_user_path` — ничто не пишется вне `data/workspaces/{user_id}/`.
3. **Один write target per user** — поддерживается атомарным UPDATE'ом (clear all + set one).
4. **Agent token = X-Device-Token** — единственный auth для /api/workspace/sync. Никаких cookie sessions для агентов.
5. **Soft защита**: shell, write_file disabled by default в `/admin/mcp`. Read-only включены.
6. **Migration runner** в `app/storage/db.py` — `_IDEMPOTENT_ALTER_ERRORS = {'already exists', ...}`. Любая новая миграция должна повторно запускаться без ошибок (`CREATE INDEX IF NOT EXISTS`, etc).

## Известные проблемы / pitfalls

- **qwen2.5:7b на 1050 Ti** спилится в CPU = медленно + смешивает русский с китайским. Рекомендация юзеру: gemma3:4b или qwen2.5:3b для текста, qwen2.5vl:3b для vision.
- **Cold-start Ollama** 90+ sec для VL моделей. Решено через keepalive SSE + read_timeout=600s.
- **WebP → Ollama 400**. Решено через `_normalise_image_for_ollama()` который конвертит в PNG через Pillow + downscale до 1280px.
- **Database is locked** при concurrent writers. Решено через `PRAGMA busy_timeout = 5000` в `get_connection()`.
- **Devtunnel session expiry**. Если 504 на публичный URL: kill devtunnel.exe processes + `devtunnel user login -d -g` (device code через GitHub) + `devtunnel host -t kind-ocean-5s32cxv`. Подробности в этом же handoff.
- **Alpine x-data quoting trap** (`feedback_alpine_xdata_quotes` в моей memory): `|tojson` внутри `x-data="..."` ломает HTML attribute. Всегда `| tojson | forceescape` или `&quot;`.

## Цель долгосрочная

Через 2-3 месяца юзер соберёт 1000+ rated Q&A в /chat. Тогда:
1. Скачать `/admin/dataset/export.jsonl?min_rating=1`
2. Kaggle Notebook (бесплатно 30ч/неделю T4 GPU) или Vast.ai ($18 за полный прогон)
3. LoRA fine-tune base = Saiga 7B / Qwen 2.5 7B (Apache 2.0)
4. Адаптер ~10-50 MB → твоя модель
5. Add to Ollama → hosting на 1050 Ti через Ollama provider в Persona
6. Возможно несколько sub-моделей: PersonaAI-Coder, PersonaAI-Memory, PersonaAI-General

Это путь к "**своя модель**" — не от-scratch (нереально для одного человека), но через fine-tune (реально).

## Что НЕ делать

- Не пихай план/чек-лист в каждый ответ AI. Только для реально многошаговых задач (>3 явных шагов).
- Не смешивай языки в одном ответе. Один язык = тот же что у юзера.
- Не вводи новые провайдеры без описаний — добавляй в `_PROVIDER_DEFAULTS` и `_OLLAMA_DESCRIPTIONS` сразу.
- Не делай blocking calls внутри SSE generator — обернуть в asyncio.Queue producer pattern.
- Не делай `from app.X import` на module-top если X импортирует обратно — циклические импорты. Используй lazy `# noqa: PLC0415` внутри функций.

## Команды для запуска

Restart uvicorn (стандартная процедура):
```powershell
Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.Id -ne 130112 } | ForEach-Object { taskkill /F /PID $_.Id /T 2>&1 | Out-Null }
Start-Sleep -Seconds 4
Set-Location C:\www-Yaroslav\Persona
Start-Process -FilePath ".venv\Scripts\python.exe" -ArgumentList "-m","uvicorn","app.web.main:create_app","--factory","--host","127.0.0.1","--port","8000" -RedirectStandardOutput "C:\Users\Yaroslav\.persona\uvicorn.out.log" -RedirectStandardError "C:\Users\Yaroslav\.persona\uvicorn.err.log" -WindowStyle Hidden | Out-Null
Start-Sleep -Seconds 14
```

PID 130112 — древний orphan python (С 04.06), оставляй жить.

### T29 — стабильность сервера (важно)

Сайт «постоянно ложился». Причина: **сервер захватывал СВОЙ экран** (capture_loop
каждые 6с) на headless Windows Server без консольной сессии → синхронные WinAPI
вызовы (`seconds_since_last_input`, session-lock detect, `WTSGetActiveConsoleSessionId`)
зависали и **блокировали event loop** → uvicorn жив, но не отвечает. Симптом в логе:
`session.detect_failed reason=bind_failed` каждые 6с.

Два фикса (оба применены):
1. **Отключён self-capture сервера** через kv: `capture_screens_disabled=1` +
   `audio_capture_paused_live=1` (в `kv_settings`). Захват с Mac-агента это НЕ трогает —
   у него свой путь (`/api/agent/*`). Снять — через `/settings/capture` или kv. НЕ
   включай self-capture обратно пока блокирующие WinAPI вызовы не обёрнуты в
   `asyncio.to_thread` + timeout (иначе снова повесит loop на этой headless-коробке).
2. **Watchdog** `ops/persona_watchdog.py` + Scheduled Task `PersonaWatchdog` (каждую
   минуту, от текущего юзера): пробит `/landing`, если down/hung — убивает stale uvicorn
   и поднимает свежий с ЯВНЫМ `PERSONA_DB_PATH`/`PERSONA_DATA_DIR` (никогда не пустая БД).
   Лог: `~/.persona/watchdog.log`. Управление: `schtasks /Query|/Run|/Delete /TN PersonaWatchdog`.
   Ограничение: задача от юзера = «только когда залогинен» (RDP-disconnect сессию держит,
   logoff — останавливает). Для переживания ребута нужна SYSTEM-задача (нужны админ-права,
   которых у текущей сессии нет).

Smoke endpoints:
```powershell
$sess = New-Object Microsoft.PowerShell.Commands.WebRequestSession
Invoke-WebRequest -Uri "http://127.0.0.1:8000/auth/login" -Method POST -Body "email=test%40persona.local&password=password123" -ContentType "application/x-www-form-urlencoded" -WebSession $sess -UseBasicParsing | Out-Null
```

Test creds: `test@persona.local` / `password123` (это тестовый аккаунт, реальный юзер логинится своим email).

## Последние коммиты (top of master)

```
5c7fd68  fix(T27): per-user workspace + 👍/👎 через index + max_tokens 4096
ba7a03a  fix(T26): план только для сложных задач + "думает" исчезает
d86dc75  feat(T26): Stop + Queue + Type-while-thinking + Task lists
318cef8  feat(T25): MCP runtime — built-in Python tools
86c60d6  feat(T24): mega-апгрейд /chat — Claude-style UI, MCP, ratings, compare
5c52334  fix(T22.10): session-pinned модели + vision picker
20bb292  feat(T23): сбор Q&A в датасет для PersonaAI fine-tune
```

T28 закоммичен (см. feat(T28) на top of master). Следующий тик — T29.

## Tone в чате с юзером

- Короткие предложения. Без bullshit. Без "great question!" / "конечно я с радостью".
- Когда ошибаешься — признавай сразу.
- Когда юзер не прав — лидируй с несогласием, потом объясняй.
- Юзер просит "доделай всё" — реально не останавливайся пока не сделано.
- Юзер просит "не спрашивай меня" — не задавай вопросов, принимай решения сам.
- Markdown с заголовками | таблицами | code-блоками — обычное дело.
