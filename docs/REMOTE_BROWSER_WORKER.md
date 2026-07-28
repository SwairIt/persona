# Persona Remote Browser Worker

## Capability and network-policy guarantees

- The browser uses `PERSONA_BROWSER_WORKER_TOKEN`, generated independently
  from `PERSONA_WORKER_TOKEN`; neither credential authorizes the other queue.
- Every claimed job carries a bounded owner allow/deny snapshot. The PC
  enforces it for every request, redirect, click navigation, subresource and
  WebSocket. A malformed or oversized policy blocks all traffic.
- Cancelled, expired and abandoned pending rows are redacted in the same
  terminal transition. An unconsumed successful result has a 90-second
  recovery grace before maintenance redacts it. Seven-day retention applies
  only to the already-redacted audit shape.

## Назначение

Playwright выполняется на отдельном ПК владельца, а не на production-сервере.
Поэтому сайты видят IP/VPN этого ПК, Chromium использует его системный proxy,
а cookies остаются в локальном persistent profile.

Схема соединений:

```text
web / Telegram tool call
        |
        v
Persona application service -> SQLite durable browser job
        ^
        | HTTPS long-poll + X-Worker-Token (только исходящее соединение)
        |
owner PC: persona_remote_browser_worker.py -> persistent Playwright Chromium
```

На ПК не открывается порт, не нужен devtunnel и не запускается HTTP-сервер.

## Инварианты безопасности

- Создать browser command может только owner application context.
- Worker API принимает секрет исключительно в `X-Worker-Token`; query/cookie
  авторизация не поддерживается.
- Протокол разрешает ровно `open`, `click`, `type`, `read`, `screenshot`,
  `close`, `ping`.
- Не существует action для shell, Python/JavaScript evaluation, загрузки
  произвольного файла или передачи пути на ПК.
- URL разрешены только абсолютные HTTP(S), без credentials. Worker повторно
  проверяет DNS и блокирует loopback, RFC1918, link-local и reserved address
  для navigation, redirect и subresource.
- Browser session привязана к `(owner_user_id, conversation_id)` и одному
  стабильному `worker_id`; другой PC не может продолжить её lease.
- На session исполняется не более одной job одновременно.
- Lease ограничен 90 секундами. Истёкшая lease становится terminal error и не
  повторяется автоматически: повтор mutating click/type небезопасен.
- Timeout/cancellation сохраняются в durable job. Поздний результат
  отменённой job отбрасывается.
- Ответ ограничен 2 MiB, read — 32k символов, screenshot — JPEG до 1.45 MiB.
- Typed text, URL, page text и screenshot очищаются из durable job сразу после
  потребления результата application service; в ledger остаётся redacted shape.
- Screenshot декодируется только внутрь owner workspace; remote worker не
  выбирает server path.
- Correlation id идемпотентен и не может быть повторно использован для другой
  команды.

## Локальное состояние и proxy

Профили по умолчанию:

```text
%LOCALAPPDATA%\Persona\browser-profiles\<sha256 profile key>
```

В них Chromium хранит cookies/local storage. По умолчанию браузер видимый
(`PERSONA_BROWSER_HEADLESS=false`), поэтому владелец может сам пройти login,
CAPTCHA или 2FA.

Если задан `PERSONA_BROWSER_PROXY`, worker передаёт его Chromium явно.
Поддерживаются `http`, `https`, `socks5`. Если переменной нет, Chromium
на Windows использует системный proxy/VPN. HTTP-клиент worker использует
стандартные `HTTPS_PROXY`, `HTTP_PROXY`, `ALL_PROXY`, `NO_PROXY`.

## Конфигурация worker

```dotenv
PERSONA_SERVER=https://persona.getdoday.ru
PERSONA_BROWSER_WORKER_TOKEN=<отдельный browser-only secret>
PERSONA_BROWSER_WORKER_ID=my-owner-pc-browser
PERSONA_BROWSER_HEADLESS=false
# PERSONA_BROWSER_PROXY=socks5://127.0.0.1:1080
```

Зависимости:

```powershell
python -m pip install httpx playwright
python -m playwright install chromium
python persona_remote_browser_worker.py
```

Production bootstrap должен скачать worker с
`/api/llm/worker/browser/agent.py` и зарегистрировать отдельную Scheduled Task.
LLM worker и browser worker — разные процессы: зависший Chromium не должен
останавливать inference.

## Наблюдаемость и эксплуатация

Для каждой job сохраняются correlation id, owner/session, action, worker id,
claim/lease/finish timestamps, terminal status и bounded error. Секрет, typed
text и screenshot не должны попадать в operational log.

При потере ПК:

1. job ждёт максимум пять минут до `remote browser worker unavailable`;
2. claimed job завершается после потери lease;
3. mutating action автоматически не повторяется;
4. после восстановления стабильный worker id продолжает привязанные sessions;
5. `close` освобождает binding, но сохраняет PC-local cookies в profile.
