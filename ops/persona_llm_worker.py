"""Persona LLM Worker — исходящий агент на ПК (без devtunnel).

Идея архитектуры «Persona LLM Worker»: ПК больше НЕ принимает входящие
соединения (никаких туннелей, дружит с FastPanel-прокси). Вместо этого ПК
делает ИСХОДЯЩИЕ запросы к серверу (``persona.getdoday.ru``):

  1. long-poll ``GET /api/llm/worker/next`` — забрать задачу из очереди в БД;
  2. посчитать её на ЛОКАЛЬНОЙ Ollama (chat — NDJSON-стрим ``/api/chat``;
     embed — ``/api/embeddings``);
  3. отправить результат ОБРАТНО серверу по HTTP
     (``POST /api/llm/worker/{id}/chunk`` для токенов чата и
     ``POST /api/llm/worker/{id}/done`` в конце / для эмбеддинга / для ошибки).

Всё — чистый HTTP. Авторизация воркера: заголовок ``X-Worker-Token`` (плейн-
текст; на сервере сверяется sha256). Скрипт самодостаточный: stdlib + httpx
(httpx уже в зависимостях проекта). Запускается и БЕЗ сервера — просто будет
ретраить с бэкоффом, пока сервер не появится. Ctrl+C — мягкая остановка.

Конфиг (env, либо ``.env`` рядом с репо):
  * ``PERSONA_SERVER``        — деф. ``https://persona.getdoday.ru``
  * ``PERSONA_WORKER_TOKEN``  — ОБЯЗАТЕЛЕН (получить через owner-кабинет
                                «ротация токена воркера»);
  * ``OLLAMA_URL``            — деф. ``http://localhost:11434``;
  * ``PERSONA_WORKER_MODEL``  — опц. модель, которую анонсируем серверу
                                (информативно; реальную модель задаёт задача).

Запуск (просто): ``ops\\persona_llm_worker.ps1`` — лаунчер сам поставит httpx
и будет авто-рестартить этот скрипт.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sys
import time
from pathlib import Path

try:
    import httpx
except ImportError:  # pragma: no cover — лаунчер ставит httpx до запуска
    sys.stderr.write(
        "[persona-worker] нет httpx. Установи: pip install httpx "
        "(или запускай через ops\\persona_llm_worker.ps1)\n"
    )
    raise SystemExit(2) from None


# ---------------------------------------------------------------------------
# Конфиг + .env
# ---------------------------------------------------------------------------

#: Серверный таймаут long-poll (сек). Сервер держит соединение до wait сек,
#: затем отдаёт 204. Наш read-таймаут должен быть ЗАМЕТНО больше, чтобы клиент
#: не рвал соединение раньше сервера.
_POLL_WAIT = 25
_POLL_READ_TIMEOUT = _POLL_WAIT + 15.0

#: Ollama на слабом железе грузит модель в VRAM 60-180с до первого токена —
#: держим жирный read-таймаут, как и основной OllamaClient в приложении.
_OLLAMA_CONNECT_TIMEOUT = 30.0
_OLLAMA_READ_TIMEOUT = 600.0

#: Батчинг токенов чата перед отправкой на сервер: копим ~50мс, чтобы не
#: спамить сервер одним HTTP-запросом на токен (но и не задерживать UX).
_CHUNK_FLUSH_INTERVAL = 0.05

#: Бэкофф при сетевых ошибках (сервер лежит / нет связи): экспоненциальный
#: рост до потолка, чтобы не долбить недоступный сервер.
_BACKOFF_START = 1.0
_BACKOFF_MAX = 30.0


def _detect_repo() -> Path:
    """Корень репозитория: ops/persona_llm_worker.py → parent.parent."""
    env = os.environ.get("PERSONA_REPO")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent


def _read_dotenv(repo: Path) -> dict[str, str]:
    """Распарсить ``<repo>/.env`` в dict (best-effort, без зависимостей).

    НЕ пишем в ``os.environ`` напрямую — возвращаем dict, а явный env имеет
    приоритет над ``.env`` (см. :func:`_cfg`). Формат — простые ``KEY=VALUE``
    строки; ``#``-комментарии и пустые строки игнорируются; кавычки снимаются.
    """
    path = repo / ".env"
    result: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return result
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            result[key] = value
    return result


def _cfg(dotenv: dict[str, str], name: str, default: str = "") -> str:
    """Значение конфига: сначала реальный env, затем ``.env``, затем дефолт."""
    env_val = os.environ.get(name)
    if env_val is not None and env_val.strip():
        return env_val.strip()
    file_val = dotenv.get(name)
    if file_val is not None and file_val.strip():
        return file_val.strip()
    return default


class Config:
    """Разобранная конфигурация воркера."""

    def __init__(self) -> None:
        repo = _detect_repo()
        dotenv = _read_dotenv(repo)
        self.server = _cfg(dotenv, "PERSONA_SERVER", "https://persona.getdoday.ru").rstrip("/")
        self.token = _cfg(dotenv, "PERSONA_WORKER_TOKEN", "")
        self.ollama = _cfg(dotenv, "OLLAMA_URL", "http://localhost:11434").rstrip("/")
        # Имя воркера = хостнейм ПК (для логов сервера и touch_worker).
        self.worker_id = _cfg(dotenv, "PERSONA_WORKER_ID", "") or socket.gethostname()
        # Анонсируемая модель — чисто информативно (реальную задаёт задача).
        self.model = _cfg(dotenv, "PERSONA_WORKER_MODEL", "")


# ---------------------------------------------------------------------------
# Логирование (простое, в stdout, с временной меткой)
# ---------------------------------------------------------------------------


def log(msg: str) -> None:
    """Печать в stdout с меткой времени и немедленным flush."""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Graceful Ctrl+C
# ---------------------------------------------------------------------------


class _Stopper:
    """Кооперативный флаг остановки по SIGINT/SIGTERM."""

    def __init__(self) -> None:
        self.stop = False

    def request(self, *_args: object) -> None:
        self.stop = True
        log("Получен сигнал остановки — завершаюсь после текущей задачи...")


# ---------------------------------------------------------------------------
# Отправка результата на сервер
# ---------------------------------------------------------------------------


def _send_chunk(client: httpx.Client, cfg: Config, job_id: int, seq: int, content: str) -> None:
    """POST /api/llm/worker/{id}/chunk — отправить пачку токенов чата."""
    client.post(
        f"{cfg.server}/api/llm/worker/{job_id}/chunk",
        headers={"X-Worker-Token": cfg.token},
        json={"seq": seq, "content": content},
        timeout=30.0,
    )


def _send_done(
    client: httpx.Client,
    cfg: Config,
    job_id: int,
    *,
    error: str | None = None,
    result: str | None = None,
) -> None:
    """POST /api/llm/worker/{id}/done — финализировать задачу (или ошибку)."""
    body: dict[str, str] = {}
    if error is not None:
        body["error"] = error
    if result is not None:
        body["result"] = result
    client.post(
        f"{cfg.server}/api/llm/worker/{job_id}/done",
        headers={"X-Worker-Token": cfg.token},
        json=body,
        timeout=30.0,
    )


# ---------------------------------------------------------------------------
# Обработчики задач
# ---------------------------------------------------------------------------


def _handle_chat(client: httpx.Client, cfg: Config, job: dict, stopper: _Stopper) -> None:
    """kind='chat': стримим NDJSON из Ollama /api/chat → чанки на сервер.

    Токены батчатся по ~50мс (см. :data:`_CHUNK_FLUSH_INTERVAL`), чтобы не
    плодить по HTTP-запросу на токен. ``seq`` — монотонный счётчик пачек,
    сервер пишет их в ``llm_job_chunk`` и отдаёт приложению по возрастанию.
    """
    job_id = int(job["job_id"])
    model = job.get("model") or cfg.model or ""
    payload = job.get("payload") or {}
    messages = payload.get("messages") or []
    options = payload.get("options") or {}
    fmt = payload.get("format")

    # Структурный вывод (knowledge_graph-триплеты / user_memory-реконсиляция):
    # format + stream=false → готовый JSON одним ответом, шлём в result (не чанки).
    if fmt is not None:
        body: dict[str, object] = {"model": model, "messages": messages, "format": fmt, "stream": False}
        if options:
            body["options"] = options
        timeout = httpx.Timeout(_OLLAMA_READ_TIMEOUT, connect=_OLLAMA_CONNECT_TIMEOUT)
        with httpx.Client(timeout=timeout) as ollama:
            resp = ollama.post(f"{cfg.ollama}/api/chat", json=body)
            resp.raise_for_status()
            data = resp.json()
        content = (data.get("message") or {}).get("content", "")
        _send_done(client, cfg, job_id, result=content)
        log(f"chat-json job #{job_id} готов (model={model}, {len(content)} симв)")
        return

    ollama_body: dict[str, object] = {
        "model": model,
        "messages": messages,
        "stream": True,
    }
    if options:
        ollama_body["options"] = options

    seq = 0
    buf: list[str] = []
    last_flush = time.monotonic()
    timeout = httpx.Timeout(_OLLAMA_READ_TIMEOUT, connect=_OLLAMA_CONNECT_TIMEOUT)

    def flush() -> None:
        nonlocal seq, buf, last_flush
        if not buf:
            return
        content = "".join(buf)
        buf = []
        last_flush = time.monotonic()
        _send_chunk(client, cfg, job_id, seq, content)
        seq += 1

    # Отдельный httpx-клиент для Ollama (локальный, без worker-токена).
    with httpx.Client(timeout=timeout) as ollama:
        with ollama.stream("POST", f"{cfg.ollama}/api/chat", json=ollama_body) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if stopper.stop:
                    break
                if not line or not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                piece = (obj.get("message") or {}).get("content", "")
                if piece:
                    buf.append(piece)
                # Сбрасываем буфер по таймеру (батчинг), не на каждый токен.
                if buf and (time.monotonic() - last_flush) >= _CHUNK_FLUSH_INTERVAL:
                    flush()
                if obj.get("done"):
                    break
    flush()  # добить хвост
    _send_done(client, cfg, job_id)
    log(f"chat job #{job_id} готов (model={model}, чанков={seq})")


def _handle_embed(client: httpx.Client, cfg: Config, job: dict) -> None:
    """kind='embed': Ollama /api/embeddings → result = JSON-вектор."""
    job_id = int(job["job_id"])
    model = job.get("model") or cfg.model or ""
    payload = job.get("payload") or {}
    prompt = payload.get("prompt") or ""

    timeout = httpx.Timeout(_OLLAMA_READ_TIMEOUT, connect=_OLLAMA_CONNECT_TIMEOUT)
    with httpx.Client(timeout=timeout) as ollama:
        resp = ollama.post(
            f"{cfg.ollama}/api/embeddings",
            json={"model": model, "prompt": prompt},
        )
        resp.raise_for_status()
        data = resp.json()
    # Ollama отдаёт ``{"embedding": [...]}``; на всякий случай поддержим и
    # OpenAI-подобный ``{"data":[{"embedding":[...]}]}``.
    vector = data.get("embedding")
    if vector is None:
        items = data.get("data") or []
        if items:
            vector = (items[0] or {}).get("embedding")
    if vector is None:
        raise ValueError("ответ Ollama не содержит embedding")
    _send_done(client, cfg, job_id, result=json.dumps(vector))
    log(f"embed job #{job_id} готов (model={model}, dim={len(vector)})")


def _process_job(client: httpx.Client, cfg: Config, job: dict, stopper: _Stopper) -> None:
    """Выполнить одну задачу; любая ошибка → POST /done {error}."""
    job_id = int(job["job_id"])
    kind = job.get("kind") or "chat"
    log(f"Взял job #{job_id} (kind={kind}, model={job.get('model')})")
    try:
        if kind == "embed":
            _handle_embed(client, cfg, job)
        else:
            _handle_chat(client, cfg, job, stopper)
    except Exception as exc:  # noqa: BLE001 — любую ошибку репортим серверу
        msg = f"{type(exc).__name__}: {exc}"
        log(f"job #{job_id} ОШИБКА: {msg}")
        try:
            _send_done(client, cfg, job_id, error=msg)
        except Exception as report_exc:  # noqa: BLE001
            log(f"не смог отрепортить ошибку job #{job_id}: {report_exc}")


# ---------------------------------------------------------------------------
# Главный цикл (long-poll + реконнект с бэкоффом)
# ---------------------------------------------------------------------------


def _poll_once(client: httpx.Client, cfg: Config) -> dict | None:
    """Один long-poll к /api/llm/worker/next. 200 → задача, 204 → None."""
    params = {"wait": _POLL_WAIT, "worker_id": cfg.worker_id}
    if cfg.model:
        params["model"] = cfg.model
    resp = client.get(
        f"{cfg.server}/api/llm/worker/next",
        headers={"X-Worker-Token": cfg.token},
        params=params,
        timeout=httpx.Timeout(_POLL_READ_TIMEOUT, connect=10.0),
    )
    if resp.status_code == 204:
        return None
    if resp.status_code == 401:
        # Неверный токен — это конфиг-ошибка, ретраи не помогут; явный сигнал.
        raise PermissionError(
            "сервер вернул 401 — неверный/пустой PERSONA_WORKER_TOKEN. "
            "Сгенерируй новый токен в owner-кабинете и обнови env/.env."
        )
    resp.raise_for_status()
    return resp.json()


def run(cfg: Config) -> int:
    """Бесконечный цикл воркера. Возврат — код выхода процесса."""
    if not cfg.token:
        log(
            "ОШИБКА: не задан PERSONA_WORKER_TOKEN. Получи токен в owner-"
            "кабинете Persona и положи в env или в <repo>/.env. Останавливаюсь."
        )
        return 2

    log("Persona LLM Worker запускается:")
    log(f"  server   = {cfg.server}")
    log(f"  ollama   = {cfg.ollama}")
    log(f"  worker_id= {cfg.worker_id}")
    log(f"  model    = {cfg.model or '(задаёт задача)'}")

    stopper = _Stopper()
    try:
        signal.signal(signal.SIGINT, stopper.request)
    except (ValueError, OSError):  # не главный поток / нет сигнала
        pass
    try:
        signal.signal(signal.SIGTERM, stopper.request)
    except (ValueError, OSError, AttributeError):
        pass

    backoff = _BACKOFF_START
    idle_polls = 0
    # Один httpx-клиент на весь цикл для запросов К СЕРВЕРУ (keep-alive).
    with httpx.Client() as client:
        while not stopper.stop:
            try:
                job = _poll_once(client, cfg)
                backoff = _BACKOFF_START  # успешный контакт — сбрасываем бэкофф
                if job is None:
                    # 204 — задач нет. Тихий long-poll, но раз в ~N опросов
                    # пишем heartbeat, чтобы было видно «жив, жду» (а не «завис»).
                    idle_polls += 1
                    if idle_polls == 1 or idle_polls % 5 == 0:
                        log(f"подключён, жду задачи (long-poll активен, опросов: {idle_polls})")
                    continue
                idle_polls = 0
                _process_job(client, cfg, job, stopper)
            except PermissionError as exc:
                # 401 — фатальная конфиг-ошибка, нет смысла крутиться вечно.
                log(f"ФАТАЛЬНО: {exc}")
                return 3
            except KeyboardInterrupt:
                stopper.stop = True
            except (httpx.HTTPError, OSError) as exc:
                # Сеть/сервер недоступны — ретраим с бэкоффом (скрипт обязан
                # работать и без сервера).
                log(f"нет связи с сервером ({type(exc).__name__}: {exc}); "
                    f"повтор через {backoff:.0f}с")
                _interruptible_sleep(backoff, stopper)
                backoff = min(backoff * 2, _BACKOFF_MAX)
            except Exception as exc:  # noqa: BLE001 — не падаем на неожиданном
                log(f"неожиданная ошибка цикла: {type(exc).__name__}: {exc}; "
                    f"повтор через {backoff:.0f}с")
                _interruptible_sleep(backoff, stopper)
                backoff = min(backoff * 2, _BACKOFF_MAX)

    log("Остановлен. Пока.")
    return 0


def _interruptible_sleep(seconds: float, stopper: _Stopper) -> None:
    """Сон с дроблением на 0.2с, чтобы Ctrl+C срабатывал быстро."""
    end = time.monotonic() + seconds
    while not stopper.stop and time.monotonic() < end:
        time.sleep(min(0.2, max(0.0, end - time.monotonic())))


def main() -> int:
    return run(Config())


if __name__ == "__main__":
    raise SystemExit(main())
