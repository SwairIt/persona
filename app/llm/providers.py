"""Каталог OpenAI-совместимых провайдеров + проверка пользовательского URL.

Зачем отдельный модуль
----------------------
``app/llm/client.py`` уже содержит по классу на каждого «особенного» провайдера
(Anthropic, Gemini, Yandex, GigaChat, Ollama, worker — у всех свой формат
запроса). Но подавляющее большинство сервисов на рынке говорят ОДНИМ и тем же
протоколом: ``POST /chat/completions`` с телом
``{model, messages, temperature, max_tokens}`` и SSE-стримом. Писать по классу
на каждого — это N копий одного кода, которые расходятся при первой же правке.

Поэтому здесь лежат ДАННЫЕ, а не код: таблица пресетов
``slug → (базовый URL, модель по умолчанию, подпись, подсказка по ключу)``.
Клиент собирается одной фабрикой :func:`app.llm.client.make_preset_client`
поверх существующего ``_OpenAICompatibleClient``. Добавить новый сервис =
дописать одну строку в :data:`PRESETS`.

Базовый URL — ВСЕГДА переопределяемый
-------------------------------------
Каждый пресет несёт ``base_url`` лишь как ЗНАЧЕНИЕ ПО УМОЛЧАНИЮ. Реальный URL
читается из настроек (``<slug>_base_url``, kv у владельца / ``user_settings``
у участника) и падает на дефолт, только если пользователь ничего не вписал.
Это сознательное решение: провайдеры переезжают с домена на домен (у половины
списка ниже уже есть по два живых хоста — CN и international), и «зашитая
константа» превращает такой переезд в баг, который чинится только релизом.
Поле ``confidence`` честно говорит, насколько мы уверены в дефолте.

Плюс универсальный ``openai_compatible``: пользователь сам вписывает
base URL + модель + ключ. Он закрывает ВСЕ сервисы — и те, которых тут нет,
и те, которых ещё не существует.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

__all__ = [
    "PRESETS",
    "PRESETS_BY_SLUG",
    "UNIVERSAL_SLUG",
    "InvalidBaseURL",
    "ProviderPreset",
    "normalise_chat_completions_url",
    "validate_base_url",
]

#: Слаг универсального провайдера: base URL + модель вводит сам пользователь.
UNIVERSAL_SLUG: Final[str] = "openai_compatible"


@dataclass(frozen=True, slots=True)
class ProviderPreset:
    """Один OpenAI-совместимый сервис.

    ``base_url`` — полный URL эндпоинта чат-комплишенов (со схемой), а не
    «корень API»: у половины сервисов путь нестандартный
    (``/v1/openai/...``, ``/compatible-mode/v1/...``, вообще без ``/v1``),
    и склейка «корень + /v1/chat/completions» ломалась бы ровно на них.
    """

    slug: str
    label: str
    base_url: str
    default_model: str
    placeholder: str
    key_hint: str
    key_url: str
    #: ``high`` — эндпоинт проверен и стабилен; ``medium`` — у сервиса есть
    #: несколько живых хостов (CN/international) либо он недавно переезжал;
    #: ``low`` — формат подтверждён, точный хост стоит перепроверить в доках.
    #: На поведение НЕ влияет: URL всё равно переопределяем. Это подпись в UI.
    confidence: str = "high"
    #: Дополнительная честная оговорка, показывается в UI под подсказкой.
    note: str = ""


# ---------------------------------------------------------------------------
# Каталог пресетов
# ---------------------------------------------------------------------------
#
# Порядок = порядок показа в /settings/llm внутри группы «ещё сервисы».
# Эмодзи в подписях согласованы с уже существующим списком PROVIDERS
# (app/web/routes/llm_switcher.py): 🌐 агрегатор, 🇷🇺 РФ-шлюз, 🏠 локально,
# ⚡ быстро/бесплатно, 💸 дёшево, 🌍 требует зарубежной карты/VPN.

PRESETS: Final[tuple[ProviderPreset, ...]] = (
    ProviderPreset(
        slug="cerebras",
        label="⚡ Cerebras — самый быстрый инференс, щедрый free tier",
        base_url="https://api.cerebras.ai/v1/chat/completions",
        default_model="llama3.1-8b",
        placeholder="csk-...",
        key_hint="Регистрация по email, ключ в разделе API Keys. Бесплатный "
                 "лимит щедрый — миллионы токенов в сутки, карта не нужна.",
        key_url="https://cloud.cerebras.ai/",
        confidence="high",
    ),
    ProviderPreset(
        slug="github_models",
        label="🐙 GitHub Models — бесплатно по GitHub-токену",
        base_url="https://models.github.ai/inference/chat/completions",
        default_model="openai/gpt-4o-mini",
        placeholder="github_pat_... или ghp_...",
        key_hint="Ключ — обычный Personal Access Token GitHub (Settings → "
                 "Developer settings → Tokens). Отдельной регистрации нет.",
        key_url="https://github.com/marketplace/models",
        confidence="medium",
        note="У GitHub Models два живых хоста: новый models.github.ai "
             "(по умолчанию) и старый models.inference.ai.azure.com — если "
             "получаешь 404, впиши второй в поле «свой URL».",
    ),
    ProviderPreset(
        slug="fireworks",
        label="🎆 Fireworks AI — быстрый хостинг open-weight моделей",
        base_url="https://api.fireworks.ai/inference/v1/chat/completions",
        default_model="accounts/fireworks/models/llama-v3p1-8b-instruct",
        placeholder="fw_...",
        key_hint="Регистрация даёт стартовый кредит; ключ — в разделе API Keys.",
        key_url="https://fireworks.ai/account/api-keys",
        confidence="high",
    ),
    ProviderPreset(
        slug="deepinfra",
        label="💸 DeepInfra — дёшево, оплата за токены",
        base_url="https://api.deepinfra.com/v1/openai/chat/completions",
        default_model="meta-llama/Meta-Llama-3.1-8B-Instruct",
        placeholder="ключ из дашборда DeepInfra",
        key_hint="Вход через GitHub, ключ в Dashboard → API Keys.",
        key_url="https://deepinfra.com/dash/api_keys",
        confidence="high",
    ),
    ProviderPreset(
        slug="hyperbolic",
        label="🌍 Hyperbolic — open-weight модели по низкой цене",
        base_url="https://api.hyperbolic.xyz/v1/chat/completions",
        default_model="meta-llama/Meta-Llama-3.1-8B-Instruct",
        placeholder="ключ из настроек Hyperbolic",
        key_hint="Регистрация по email, ключ в Settings → API Key.",
        key_url="https://app.hyperbolic.xyz/settings",
        confidence="high",
    ),
    ProviderPreset(
        slug="nebius",
        label="🌍 Nebius AI Studio — EU-хостинг open-weight моделей",
        base_url="https://api.studio.nebius.com/v1/chat/completions",
        default_model="meta-llama/Meta-Llama-3.1-8B-Instruct",
        placeholder="ключ из Nebius AI Studio",
        key_hint="Регистрация в Nebius AI Studio, ключ в разделе API keys.",
        key_url="https://studio.nebius.com/",
        confidence="medium",
        note="Nebius переезжал с домена .ai на .com — если дефолт не "
             "отвечает, попробуй api.studio.nebius.ai в поле «свой URL».",
    ),
    ProviderPreset(
        slug="perplexity",
        label="🔎 Perplexity — модели с живым веб-поиском",
        base_url="https://api.perplexity.ai/chat/completions",
        default_model="sonar",
        placeholder="pplx-...",
        key_hint="Ключ в настройках аккаунта (API). Нужен платный баланс — "
                 "бесплатного tier у API нет, подписка Pro даёт кредиты.",
        key_url="https://www.perplexity.ai/settings/api",
        confidence="high",
        note="Путь БЕЗ /v1 — это не опечатка, у Perplexity эндпоинт лежит "
             "прямо в корне.",
    ),
    ProviderPreset(
        slug="moonshot",
        label="🌙 Moonshot (Kimi) — длинный контекст",
        base_url="https://api.moonshot.ai/v1/chat/completions",
        default_model="moonshot-v1-8k",
        placeholder="sk-...",
        key_hint="Кабинет Moonshot → API Keys. Международный вход — .ai, "
                 "китайский — .cn (у них РАЗНЫЕ аккаунты и разные ключи).",
        key_url="https://platform.moonshot.ai/",
        confidence="medium",
        note="Если ключ выпущен в китайском кабинете, впиши "
             "https://api.moonshot.cn/v1/chat/completions в «свой URL».",
    ),
    ProviderPreset(
        slug="zhipu",
        label="🇨🇳 Zhipu GLM — есть по-настоящему бесплатная glm-4-flash",
        base_url="https://open.bigmodel.cn/api/paas/v4/chat/completions",
        default_model="glm-4-flash",
        placeholder="ключ вида id.secret",
        key_hint="Кабинет bigmodel.cn → API Keys. Модель glm-4-flash "
                 "бесплатна, но регистрация обычно требует телефон.",
        key_url="https://open.bigmodel.cn/usercenter/apikeys",
        confidence="high",
    ),
    ProviderPreset(
        slug="dashscope",
        label="🐧 Qwen / DashScope (Alibaba) — родной дом моделей Qwen",
        base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions",
        default_model="qwen-plus",
        placeholder="sk-...",
        key_hint="Alibaba Cloud Model Studio → API-KEY. По умолчанию стоит "
                 "МЕЖДУНАРОДНЫЙ хост (Сингапур).",
        key_url="https://bailian.console.alibabacloud.com/",
        confidence="medium",
        note="Ключ из китайского кабинета работает только на китайском "
             "хосте: https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
    ),
    ProviderPreset(
        slug="minimax",
        label="🌍 MiniMax — сильные модели, длинный контекст",
        base_url="https://api.minimaxi.chat/v1/text/chatcompletion_v2",
        default_model="MiniMax-Text-01",
        placeholder="ключ из кабинета MiniMax",
        key_hint="Кабинет MiniMax → API Keys. Международный хост — minimaxi, "
                 "китайский — minimax.",
        key_url="https://www.minimaxi.com/",
        confidence="low",
        note="У MiniMax нестандартный путь (/text/chatcompletion_v2) и два "
             "домена. Если не отвечает — сверь эндпоинт в их доках и впиши "
             "его в «свой URL», это поле для того и есть.",
    ),
    ProviderPreset(
        slug="novita",
        label="💸 Novita AI — дешёвый хостинг open-weight",
        base_url="https://api.novita.ai/v3/openai/chat/completions",
        default_model="meta-llama/llama-3.1-8b-instruct",
        placeholder="sk_...",
        key_hint="Регистрация по email/GitHub, ключ в Settings → Key Management.",
        key_url="https://novita.ai/settings/key-management",
        confidence="medium",
    ),
    ProviderPreset(
        slug="ionet",
        label="🌍 io.net — инференс на распределённом GPU-облаке",
        base_url="https://api.intelligence.io.solutions/api/v1/chat/completions",
        default_model="meta-llama/Llama-3.3-70B-Instruct",
        placeholder="io-v2-...",
        key_hint="io.net Intelligence → API Keys. Есть бесплатная дневная квота.",
        key_url="https://ai.io.net/",
        confidence="medium",
    ),
    ProviderPreset(
        slug="chutes",
        label="🌍 Chutes — децентрализованный инференс (Bittensor)",
        base_url="https://llm.chutes.ai/v1/chat/completions",
        default_model="deepseek-ai/DeepSeek-V3-0324",
        placeholder="cpk_...",
        key_hint="Регистрация на chutes.ai, ключ в кабинете. Модели крутятся "
                 "на чужих узлах — для приватных данных не лучший выбор.",
        key_url="https://chutes.ai/",
        confidence="medium",
    ),
    ProviderPreset(
        slug="featherless",
        label="🪶 Featherless — подписка вместо оплаты за токены",
        base_url="https://api.featherless.ai/v1/chat/completions",
        default_model="meta-llama/Meta-Llama-3.1-8B-Instruct",
        placeholder="rc_...",
        key_hint="Подписка с фиксированной ценой в месяц, ключ в кабинете. "
                 "Бесплатного tier нет.",
        key_url="https://featherless.ai/account/api-keys",
        confidence="medium",
    ),
    ProviderPreset(
        slug="targon",
        label="🌍 Targon — дешёвый инференс на Bittensor",
        base_url="https://api.targon.com/v1/chat/completions",
        default_model="deepseek-ai/DeepSeek-V3",
        placeholder="sn4_...",
        key_hint="Кабинет Targon → API Keys.",
        key_url="https://targon.com/",
        confidence="low",
        note="Самый молодой сервис в списке — эндпоинт мог измениться. "
             "Не отвечает → сверь URL в их доках и впиши в «свой URL».",
    ),
)

PRESETS_BY_SLUG: Final[dict[str, ProviderPreset]] = {p.slug: p for p in PRESETS}


# ---------------------------------------------------------------------------
# Проверка пользовательского base URL (анти-SSRF)
# ---------------------------------------------------------------------------


class InvalidBaseURL(ValueError):
    """Пользовательский base URL не прошёл проверку."""


#: Хосты облачных metadata-сервисов. Запрещены ВСЕГДА и всем, включая
#: владельца: попадание сюда — это кража IAM-креденшелов инстанса, а не
#: «необычная конфигурация». Имена (а не только IP) нужны потому, что
#: GCP/Azure отдают метаданные и по DNS-имени.
_METADATA_HOSTS: Final[frozenset[str]] = frozenset({
    "metadata",
    "metadata.google.internal",
    "metadata.goog",
    "instance-data",
    "instance-data.ec2.internal",
})

#: Суффиксы имён, которые считаем «домашняя сеть/железо пользователя»: им
#: разрешён http без TLS, потому что у домашнего роутера/NAS сертификата нет.
_LAN_SUFFIXES: Final[tuple[str, ...]] = (".local", ".lan", ".home.arpa", ".internal")


def _host_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """IP-литерал из хоста или ``None``, если это доменное имя."""
    candidate = host
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        return None


def _is_lan_name(host: str) -> bool:
    return host == "localhost" or any(host.endswith(sfx) for sfx in _LAN_SUFFIXES)


def validate_base_url(raw: str, *, owner: bool) -> str:
    """Проверить и нормализовать пользовательский base URL. Иначе — исключение.

    ПРАВИЛО (одно, целиком, чтобы его можно было держать в голове):

    1. Схема только ``http``/``https``, логин-пароль в URL запрещены
       (``https://user:pass@host`` — классический способ спрятать реальный хост).
    2. **Всегда запрещено, всем**: link-local ``169.254.0.0/16`` (сюда входит
       метадата-адрес ``169.254.169.254``), IPv6 link-local ``fe80::/10``,
       неуказанные адреса (``0.0.0.0``, ``::``) и известные metadata-имена.
       Это не «настройка», это единственный способ, которым чужой base URL
       превращается в кражу креденшелов сервера.
    3. **Петля сервера** (``127.0.0.0/8``, ``::1``, ``localhost``) запрещена
       НЕ-владельцу: для участника «localhost» — это не его машина, а машина,
       где крутится Persona (и Ollama владельца на ней же). Владельцу петля
       разрешена: сервер его, и он уже ходит на свой localhost:11434.
    4. HTTPS обязателен, КРОМЕ приватных/LAN-адресов
       (``10/8``, ``172.16/12``, ``192.168/16``, ``fc00::/7``, ``*.local``,
       ``*.lan``, ``*.home.arpa``, ``*.internal``) — у домашнего сервера или
       self-hosted шлюза в LAN сертификата обычно нет, и требовать TLS там
       значило бы просто запретить самый честный сценарий.

    Чего это правило НЕ ловит (честно): DNS-rebinding — имя, которое СЕЙЧАС
    резолвится в публичный IP, а в момент запроса в 127.0.0.1. Резолвить имя
    здесь бессмысленно (проверка и запрос разнесены во времени), а гоняться за
    этим по-настоящему нужно на уровне сетевой политики исходящих соединений,
    не в валидаторе строки.

    Возвращает URL без хвостовых пробелов и слэша.
    """
    text = (raw or "").strip()
    if not text:
        msg = "Пустой URL. Нужен полный адрес эндпоинта, например https://api.example.com/v1/chat/completions"
        raise InvalidBaseURL(msg)

    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        msg = f"Схема «{parts.scheme or '?'}» не поддерживается — нужен http:// или https://"
        raise InvalidBaseURL(msg)
    if parts.username or parts.password:
        msg = "Логин/пароль прямо в URL запрещены — вставь ключ в поле ключа."
        raise InvalidBaseURL(msg)

    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host:
        msg = "В URL нет хоста."
        raise InvalidBaseURL(msg)

    if host in _METADATA_HOSTS:
        msg = "Это адрес metadata-сервиса облака — он запрещён."
        raise InvalidBaseURL(msg)

    ip = _host_ip(host)
    if ip is not None:
        # Петлю проверяем ПЕРВОЙ и отдельно: ``::1`` попадает в зарезервированный
        # ``::/8``, поэтому общая проверка «служебных диапазонов» ниже съела бы
        # его раньше — и владелец, которому петля разрешена, получал бы отказ с
        # неверной причиной («metadata-сервис») на собственном localhost.
        if ip.is_loopback:
            if not owner:
                msg = (
                    "127.0.0.1 / ::1 — это машина сервера Persona, а не твоя. "
                    "Укажи адрес, доступный извне (или свой белый IP / туннель)."
                )
                raise InvalidBaseURL(msg)
        elif ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved:
            msg = (
                f"Адрес {ip} запрещён (link-local / служебный диапазон). "
                "169.254.169.254 — это metadata-сервис облака, туда нельзя."
            )
            raise InvalidBaseURL(msg)
        private = ip.is_loopback or ip.is_private
    else:
        if host == "localhost" and not owner:
            msg = (
                "localhost — это машина сервера Persona, а не твоя. "
                "Укажи адрес, доступный извне."
            )
            raise InvalidBaseURL(msg)
        private = _is_lan_name(host)

    if scheme != "https" and not private:
        msg = (
            "Для публичного адреса обязателен https:// — по http ключ уедет "
            "открытым текстом. http допустим только для локальной сети "
            "(192.168.*, 10.*, *.local и т.п.)."
        )
        raise InvalidBaseURL(msg)

    return text.rstrip("/")


def normalise_chat_completions_url(raw: str) -> str:
    """Дописать ``/chat/completions``, если пользователь дал только корень API.

    Люди копируют из доков ``https://api.example.com/v1`` (именно это значение
    просят вставить в ``OPENAI_BASE_URL`` у большинства SDK) — и получают 404,
    потому что мы шлём POST ровно по указанному адресу. Достраиваем сами:
    хвост ``/chat/completions`` добавляется, только если его ещё нет и путь
    выглядит как корень (``/``, ``/v1``, ``/openai/v1`` и т.п.).
    """
    text = (raw or "").strip().rstrip("/")
    if not text:
        return text
    parts = urlsplit(text)
    path = parts.path.rstrip("/")
    if path.endswith("/chat/completions") or path.endswith("/completions"):
        return text
    return text + "/chat/completions"
