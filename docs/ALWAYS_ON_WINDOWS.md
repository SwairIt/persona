# Persona always-on на Windows

Как сайт остаётся поднятым **навсегда**: переживает выход владельца из
Windows-сессии, перезагрузку без логина и краш процесса.

Источник правды — `ops/install_persona_autostart_windows.ps1`.
Python-обёртка `ops/install_watchdog_windows.py` просто зовёт его.

---

## 1. Что за задачи

| Задача | Скрипт | Период | Что делает |
|---|---|---|---|
| `PersonaWatchdog` | `ops/persona_watchdog.py` | 1 мин + AtStartup | Пробит `http://127.0.0.1:8000/landing`. Если сервер лежит `_FAIL_THRESHOLD` прогонов подряд — убивает залипший uvicorn и поднимает новый. |
| `PersonaMemproc` | `ops/memory_processor.py` | 10 мин + AtStartup | Часовые карточки памяти + ограниченные пачки OCR/эмбеддингов. |

**Watchdog — короткоживущая проба, а не супервизор.** Каждый прогон:
две пробы по 20 с с паузой 8 с (~48 с худший случай), и только при
восстановлении — kill + start + до 32 с ожидания. Процесс выходит сам.
Долгоживущий процесс здесь НЕ крутится — поэтому лимит выполнения короткий
(`PT10M`), а не `PT0S`/«без лимита»: зависшая проба должна быть прибита, иначе
`IgnoreNew` навсегда заблокирует все следующие прогоны.

`PersonaMemproc` — тоже конечный прогон, но тяжелее (OCR/эмбеддинги), плюс
собственный лок `~/.persona/memproc.lock` со сроком протухания 30 мин.
Лимит выполнения `PT30M` выставлен под этот же срок.

---

## 2. Настройки и зачем именно они

```
Principal : UserId=<владелец>  LogonType=S4U  RunLevel=Highest
Triggers  : BootTrigger (delay PT30S / PT5M) + TimeTrigger repeat PT1M / PT10M, без Duration
Settings  : MultipleInstancesPolicy = IgnoreNew
            StartWhenAvailable      = true
            DisallowStartIfOnBatteries = false
            StopIfGoingOnBatteries     = false
            ExecutionTimeLimit      = PT10M (watchdog) / PT30M (memproc)
            RestartOnFailure        = 3 попытки с интервалом PT1M
            IdleSettings.StopOnIdleEnd = false
```

* **`LogonType=S4U`** — «выполнять независимо от того, вошёл ли пользователь».
  S4U (*Service-for-User*) даёт токен пользователя **без хранения пароля**.
  Это и есть исправление главного бага: было `InteractiveToken` («только для
  вошедшего пользователя») — при логауте Windows останавливала задачу, watchdog
  умирал, а вместе с ним и порождённый им uvicorn.
* **`BootTrigger`** — сайт возвращается после ребута, когда в систему никто не
  входил. Без него после перезагрузки всё ждёт логина владельца.
  Задержки разные: watchdog `PT30S` (владеет окном загрузки), memproc `PT5M`
  (тяжёлая работа не должна конкурировать с холодным стартом веба).
* **`StartWhenAvailable`** — догнать пропущенный прогон, если машина была
  выключена/спала.
* **Батарейные флаги выключены** — сервер не должен пропускать или обрывать
  прогон из-за «питания от батареи».
* **`IgnoreNew`** — никогда не накладывать вторую пробу на работающую.
* **`RestartOnFailure`** — перезапуск прогона, упавшего целиком (типично на
  загрузке, когда сетевой стек ещё не готов).
* **`StopOnIdleEnd=false`** — не убивать прогон из-за того, что машина
  перестала простаивать.
* **`RunLevel=Highest`** — запас прав на `taskkill /T` по чужим процессам.
  Функционально не обязателен (порт 8000 > 1024, процессы — того же
  пользователя), но безопаснее при восстановлении.

---

## 3. Установка / починка (ТРЕБУЕТ АДМИНА)

Открыть PowerShell **от имени администратора**:

```powershell
powershell -ExecutionPolicy Bypass -File C:\www-Yaroslav\Persona\ops\install_persona_autostart_windows.ps1
```

или через привычную обёртку:

```powershell
C:\www-Yaroslav\Persona\.venv\Scripts\python.exe C:\www-Yaroslav\Persona\ops\install_watchdog_windows.py
```

Посмотреть план без изменений — добавить `-DryRun` (`--dry-run` у python).

### Почему нужен именно админ

Неповышенный шелл владельца может менять **settings** задачи, но получает
`Access is denied` (**0x80070005**) ровно на трёх вещах — и это проверено
экспериментально на этой машине:

| Операция | Неповышенный шелл |
|---|---|
| `Set-ScheduledTask -Settings ...` | **разрешено** |
| `Principal LogonType=Interactive, RunLevel=Limited` | **разрешено** |
| `Principal LogonType=S4U` | **Access is denied** |
| `Principal RunLevel=Highest` | **Access is denied** |
| `Trigger AtStartup` (BootTrigger) | **Access is denied** |

Владелец `Yaroslav` состоит в `BUILTIN\Administrators`, но токен отфильтрован
UAC (`Group used for deny only`), поэтому нужен именно **Run as administrator**,
а не просто вход этим пользователем.

### Обязательный шаг после установки

Задачи начинают работать в **сессии 0**. Любой uvicorn, поднятый раньше из
интерактивной сессии, **продолжит держать порт 8000 и отдавать СТАРЫЙ код**.
Сделать один чистый цикл:

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like '*app.web.main*' } |
    ForEach-Object { taskkill /F /PID $_.ProcessId /T }
Start-ScheduledTask -TaskName PersonaWatchdog
```

---

## 4. Проверка

```powershell
# принципал и триггеры
$t = Get-ScheduledTask -TaskName PersonaWatchdog
"$($t.Principal.LogonType) / $($t.Principal.RunLevel)"          # ждём: S4U / Highest
$t.Triggers | ForEach-Object { $_.CimClass.CimClassName }        # ждём: BootTrigger + TimeTrigger
$t.Settings | Format-List StartWhenAvailable, ExecutionTimeLimit, MultipleInstances

# принудительный прогон
Start-ScheduledTask -TaskName PersonaWatchdog
Get-ScheduledTaskInfo -TaskName PersonaWatchdog | Format-List LastRunTime, LastTaskResult   # ждём 0

# ГЛАВНОЕ: живой ли сайт и СВЕЖИЙ ли код (порт сам по себе ничего не доказывает)
$r = Invoke-WebRequest http://127.0.0.1:8000/landing -UseBasicParsing
$r.StatusCode                                                    # 200
($r.Content | Select-String '\?v=([0-9.]+)' -AllMatches).Matches |
    ForEach-Object { $_.Groups[1].Value } | Select-Object -Unique # должно совпасть с app/__init__.py __version__

# доказательство «переживает логаут»: процессы в сессии 0
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like '*app.web.main*' } |
    Select-Object ProcessId, SessionId, CommandLine               # SessionId ждём 0
```

### После перезагрузки

Зайти по RDP **не входя** под владельцем (или просто подождать) и с любой
машины дёрнуть сайт по HTTP. Если отвечает 200 — BootTrigger + S4U работают.
Дополнительно: `Get-ScheduledTaskInfo -TaskName PersonaWatchdog` покажет
`LastRunTime` вскоре после времени загрузки.

---

## 5. Откат

Определения задач до правки лежат в XML-бэкапах (делать перед КАЖДОЙ правкой):

```powershell
schtasks /Query /TN PersonaWatchdog /XML > PersonaWatchdog.before.xml
schtasks /Query /TN PersonaMemproc  /XML > PersonaMemproc.before.xml
```

Восстановление (от админа):

```powershell
schtasks /Create /TN PersonaWatchdog /XML PersonaWatchdog.before.xml /F
schtasks /Create /TN PersonaMemproc  /XML PersonaMemproc.before.xml  /F
```

Снести обе задачи целиком:

```powershell
powershell -ExecutionPolicy Bypass -File ops\install_persona_autostart_windows.ps1 -Uninstall
```

---

## 6. Известные грабли

**Осиротевший uvicorn на унаследованном сокете.** Самая частая беда. Старый
процесс держит `:8000` и отдаёт код прошлой версии; порт «живой», сайт
«работает», но версия не та. Порт НИКОГДА не доказывает свежесть кода —
проверять `?v=` в HTML против `app/__init__.py __version__`. Лечится чистым
циклом из раздела 3.

**Процессы сессии 0 не видны в Диспетчере задач.** По умолчанию вкладка
«Процессы» показывает только текущую сессию. Нужно смотреть вкладку
«Подробности» с колонкой *ИД сеанса*, либо через PowerShell
(`Get-CimInstance Win32_Process | Select ProcessId, SessionId, CommandLine`).
Иначе легко решить, что сервер не запущен, и наплодить дублей.

**S4U и сетевые ресурсы.** S4U-токен — локальный: он НЕ несёт сетевых
учётных данных. Задача под S4U не сможет достучаться до сетевых шар и
UNC-путей от имени пользователя. Persona пишет в локальный
`PERSONA_DATA_DIR` (в `.env` он задан абсолютным путём
`C:/Users/Yaroslav/.persona`), поэтому проблемы нет — но если данные когда-то
переедут на сетевую шару, S4U сломается, и придётся либо `LogonType=Password`
(хранение пароля), либо gMSA, либо локальный путь.

**Профиль пользователя при logon-типе без интерактива.** `memory_processor.py`
берёт путь лока через `Path.home()`. Пока `USERPROFILE` резолвится в профиль
владельца — всё нормально. Данные приложения от этого не зависят: watchdog
явно прокидывает `PERSONA_DATA_DIR`/`PERSONA_DB_PATH` в окружение uvicorn,
так что БД всегда настоящая, а не пустая свежесозданная.

**Окно простоя ~3 минуты при краше.** `_FAIL_THRESHOLD = 3` в
`ops/persona_watchdog.py` при триггере раз в минуту = сайт лежит ~3 минуты,
прежде чем watchdog решится на рестарт. Это осознанный компромисс против
«флаппинга» (одна медленная проба под нагрузкой убивала здоровый сервер и
вызывала холодный старт-стадо). Если 3 минуты много — менять `_FAIL_THRESHOLD`
на `2` (даёт ~2 мин, риск флаппинга выше) и/или период триггера на 30 с.

**Не запускать установщик из неповышенного шелла и не «чинить» это
через `schtasks /RU /RP`.** Пароль нигде не хранится и храниться не должен.
Если S4U падает по правам — проверить право *«Вход в качестве пакетного
задания»* (`secpol.msc` → Локальные политики → Назначение прав пользователя →
`Log on as a batch job`).
