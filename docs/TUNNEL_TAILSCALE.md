# Tailscale Funnel — стабильный туннель вместо devtunnel

`*.devtunnels.ms` отваливается каждые несколько часов, URL меняется
при пересоздании, плюс Microsoft может в любой момент refuse-уровень
поднять. **Tailscale Funnel** — рекомендуемая замена:

- **Бесплатно** для личного использования (Free plan)
- **Стабильный URL**: `<machine>.tailXXX.ts.net` навсегда
- **Через 443 outbound** к Tailscale DERP relay — пройдёт через
  тот же firewall что devtunnel пускает; Cloudflare Tunnel (7844)
  блокируется на этом сервере, Tailscale нет
- **Один процесс** — `tailscaled` крутится как Windows service

## Установка на Windows Server 2019

### 1. Скачать установщик

<https://pkgs.tailscale.com/stable/tailscale-setup-latest.exe>

Запустить, install, перезапуститься (или не надо — `tailscaled`
зарегистрируется как Windows service сразу).

### 2. Логин

```powershell
tailscale up
```

Откроется браузер → залогиниться через Google/GitHub/email → готово.
На сервере без браузера: команда выведет ссылку, копируешь её на свой
Mac, открываешь, авторизуешь.

### 3. Включить Funnel для порта 8000

```powershell
# Один раз — разрешить Funnel
tailscale serve --bg https+insecure://localhost:8000

# Перевести в публичный режим (Funnel)
tailscale funnel --bg 8000
```

После `tailscale funnel --bg 8000` команда выведет публичный URL
вида `https://persona-server.tailXXX.ts.net`. Открой на маке —
должен отдать тот же ответ что `http://127.0.0.1:8000`.

### 4. Killнуть devtunnel

```powershell
Get-Process devtunnel -ErrorAction SilentlyContinue | Stop-Process -Force
```

### 5. Обновить Mac agent config

`~/.config/persona-agent.toml`:

```toml
[server]
url = "https://persona-server.tailXXX.ts.net"
token = "..."
```

Перезапустить агент: `launchctl kickstart -k gui/$(id -u)/com.persona.agent`.

## Сравнение

| | devtunnel | Tailscale Funnel | Cloudflare Tunnel |
|---|---|---|---|
| Cost | free | free | free |
| Stable URL | no (random на restart) | **yes** (`<name>.tail*.ts.net`) | yes (с своим доменом) |
| Outbound port | 443 | 443 | **7844 — БЛОКИРУЕТСЯ** на этом сервере |
| Setup | 2 мин | 5 мин | 10 мин + auth |
| Auto-restart | manually | Windows service | Windows service |

## Что если Tailscale тоже не пройдёт

Маловероятно (Tailscale использует DERP через 443 + STUN, оба
обычно открыты у самых злобных провайдеров), но если так — варианты
в порядке убывания удобства:

1. **Сергей-хостинг VPS** + SSH reverse tunnel: `ssh -R 8000:localhost:8000 user@vps.example.com`
2. **ngrok free** (`ngrok http 8000`) — URL меняется, но HTTPS работает почти везде
3. **Купить домен + Caddy reverse proxy** ($10/год)

## Откат на devtunnel

Если Tailscale Funnel почему-то не подойдёт:

```powershell
"C:/Users/Yaroslav/.local/devtunnel/devtunnel.exe" host
```

— старый кейс. URL `https://jswvbzgl-8000.euw.devtunnels.ms` пересоздаётся.
