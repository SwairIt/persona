# Mac agent end-to-end smoke check

После v1.12-v1.17 Mac demon работает, но **никогда не валидировался
end-to-end за >5 минут**. Реальная валидация = шлёт ли он что-то
после ребута Mac, после закрытия крышки, после auth-rotation.

Чеклист на 15 минут. Один прогон — и можно ставить tick на «Mac demon
готов к продакшну».

## 0. Подготовка (на Mac)

```bash
# Убедиться что demon запущен
launchctl print gui/$(id -u)/com.persona.agent | head -5
# Должен показать state = running

# Tail-логи — оставь это окно открытым
tail -f ~/Library/Logs/persona-agent.log
```

В соседнем окне:

```bash
cat ~/.config/persona-agent.toml | grep url
```

→ должен быть актуальный URL (после v1.34 — Tailscale Funnel).

## 1. Hard-kickstart

```bash
launchctl kickstart -k gui/$(id -u)/com.persona.agent
```

В первом окне (tail) должно появиться:

```
agent.starting server=https://... agent_id=2
```

Если 401 — токен устарел, перерегистрируй через
`persona-agent pair --server <URL> --token <NEW>`.

## 2. Status-check через CLI

```bash
persona-agent status
```

Должен вернуть **200** с реальными счётчиками (раньше показывал «—»;
после v1.12 fix — настоящие числа):

```
agent_id        : 2
today (UTC)     : 2026-06-04
screens today   : 0          ← вырастет за 5 мин
audio today     : 0 segments, 0.0 KB
last_seen_at    : 2026-06-04 ...
last_screen_at  : (None пока — должен прийти за минуту)
```

## 3. Подождать 5 минут — проверить что screens_today > 0

Если на Mac включён screen-capture (config `[capture] screen = true`):

```bash
# 5 минут спустя
persona-agent status
```

`screens today` должен быть 30-50 (по 5-6 шотов/минуту).

Если 0 после 5 мин — что-то блокирует. Часто:
- macOS Privacy → Screen Recording → persona-agent не выбран
- launchctl print gui/.../com.persona.agent | grep -E "state|exit" → state should be `running`, last_exit_code = 0

## 4. Подождать 1 минуту с включённым миком — audio_today > 0

```bash
# Скажи вслух «test test test» (15 сек речи)
sleep 30
persona-agent status
```

`audio today` должен быть >=1 segment.

Если 0:
- macOS Privacy → Microphone → persona-agent не выбран
- VAD threshold слишком высокий (попробуй `audio_vad_threshold = 0.4` в config)
- `~/Library/Logs/persona-agent.log` ищи `agent.audio.queue_full_drop` (overload — нормально, дропы есть)

## 5. На сервере — проверить что данные дошли

```powershell
# На сервере, через uvicorn
curl http://127.0.0.1:8000/api/memory/cards.json | jq '.items[] | select(.audio_seconds > 0)'
```

Если в /memory hourly cards появляется `audio_seconds > 0` — Mac->server путь живой.

## 6. Stress: закрой крышку Mac на 5 минут, открой

```bash
# В tail-окне на маке:
# Crash NotificationCenter появится на open lid? - значит agent перезагрузился ОК
persona-agent status
# screens today / audio today продолжают расти?
```

KeepAlive в plist обещает restart на crash. Открытие крышки = wake from sleep.

## 7. Verdict

После всех 6 шагов:

- screens today, audio today, last_*_at — все >0/non-null
- логи без recurring errors  
- /memory на сервере показывает данные с Mac
- Notification Center на маке не показывает crash-уведомлений

→ Mac demon валидирован. Можно ставить в **MVP done**.

Иначе — пришли `~/Library/Logs/persona-agent.log` (последние 100 строк)
+ `persona-agent status --json`, я диагностирую.
