# Persona always-on на Windows

Как сайт остаётся поднятым **навсегда**: переживает выход владельца из
Windows-сессии, перезагрузку без логина и краш процесса.

Источник правды — `ops/install_persona_autostart_windows.ps1`.
Python-обёртка `ops/install_watchdog_windows.py` просто зовёт его.

---

## 1. Что за задачи

| Задача | Скрипт | Период | Что делает |
|---|---|---|---|
| `PersonaWatchdog` | `ops/persona_watchdog.py` | 1 мин + AtStartup | Сначала смотрит маркер рестарта (раздел 3). Затем пробит `http://127.0.0.1:8000/landing`: если сервер лежит `_FAIL_THRESHOLD` прогонов подряд — убивает залипший uvicorn и поднимает новый. |
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

## 3. Деплой: как перезапустить сайт из ОБЫЧНОГО шелла

### Тупик, который создал правильный фикс

S4U + `RunLevel=Highest` = задача живёт в **сессии 0**, и порождённый ею
uvicorn тоже повышенный. Неповышенный шелл владельца (обычный терминал,
git-хук, агент) такой процесс **убить не может**: `taskkill /F /T` печатает
свой «SUCCESS», возвращает `0` — и не делает ничего. Порт остаётся занят,
сайт продолжает отдавать СТАРЫЙ код. Владелец `Yaroslav` в
`BUILTIN\Administrators`, но токен отфильтрован UAC (`deny only`), поэтому
членства в группе тут не хватает — нужен именно **Run as administrator**.

Хуже отказа то, что он **тихий**: скрипт деплоя рапортует «перезапущено»,
а версия не меняется. Это ровно та ловушка из раздела «Осиротевший uvicorn»,
только теперь постоянная.

### Механизм: попросить, а не убивать

Неповышенный процесс кладёт **маркер-запрос**, а уже повышенный watchdog на
очередном тике его валидирует, потребляет и перезапускает сервер сам.
Никакой новой службы, задачи, хранимого пароля или открытого порта — только
файл в каталоге данных.

```
<PERSONA_DATA_DIR>/restart.request        запрос (JSON)     кладёт деплой
<PERSONA_DATA_DIR>/restart.request.seen   журнал nonce'ов   пишет watchdog
<PERSONA_DATA_DIR>/restart.result         вердикт            пишет watchdog
```

Код: `ops/restart_request.py` (контракт + валидация),
`ops/persona_watchdog.py` (`_handle_restart_request` / `_restart_now`),
`ops/deploy_restart.py` (хелпер), тесты — `tests/test_restart_request.py`
и `tests/test_watchdog_restart_request.py` (**повышение не нужно**).

### Команда

```powershell
.venv\Scripts\python.exe ops\deploy_restart.py
```

| Ключ | Зачем |
|---|---|
| `--status` | только проверить, что отдаётся; ничего не просить |
| `--force` | попросить рестарт, даже если версия уже совпала |
| `--timeout 300` | сколько ждать (деф. 240 с; маркер протухает за 300 с) |
| `--no-preflight` | не собирать `create_app()` перед запросом (быстрее, опаснее) |
| `--no-nudge` | не дёргать `schtasks /Run`, ждать естественный тик |

Что делает по шагам:

1. читает `__version__` из `app/__init__.py` — это цель деплоя;
2. если версия **уже** отдаётся — ничего не делает (нужен `--force`);
3. **preflight**: собирает `create_app()` отдельным процессом (~15 с). Не
   собралось — маркер НЕ кладётся, живой сервер не трогаем, выход `2`.
   Иначе рестарт убил бы рабочий сайт ради кода, который не стартует;
4. кладёт маркер атомарно (`tmp` + `os.replace`) и дёргает
   `schtasks /Run /TN PersonaWatchdog` (запуск своей задачи разрешён и без
   повышения) — чтобы не ждать до минуты;
5. ждёт **вердикт watchdog'а по своему `nonce`** в `restart.result`:
   `running` → `ok` / `failed` / `ignored`. Именно вердикт, а не «версия
   совпала»: при рестарте на ту же версию (`--force`) её отдаёт ещё СТАРЫЙ
   процесс, и «успех» через 4 секунды не означал бы ничего;
6. получив `ok`, **всё равно проверяет сам по HTTP** — слово watchdog'а не
   доказательство. Успех печатается только когда сошлось и то, и другое.

В строке вердикта видно смену процессов, напр.
`pids [210980, 227516] (were [131836, 234552]), serving version 2.34.0` —
это и есть доказательство, что рестарт реально произошёл.

Выход `0` — сайт отдаёт нужную версию. `1` — нет, и тогда печатается
точная команда для повышенного шелла. `2` — не собралось, деплой отменён,
сайт не тронут.

### Честная проверка (почему «тихий успех» теперь невозможен)

Две независимые HTTP-проверки, обе обязаны сойтись с `app/__init__.py`:

* `/healthz` → `version` — версия **процесса, который реально слушает порт**;
* `?v=` в HTML `/landing` — то, что уедет в браузер (cache-busting).

Плюс сам watchdog не считает рестарт удавшимся, пока `/healthz` не отдаст
версию из рабочей копии, а `_restart_now` перед стартом проверяет, что старые
PID'ы действительно мертвы. Провал уходит в `~/.persona/watchdog.log` строкой
**`RESTART-REQUEST FAILED`** и в `restart.result` со `status=failed` —
не проглатывается.

### Почему маркер нельзя подделать

| Проверка | Что даёт |
|---|---|
| единственный путь `<PERSONA_DATA_DIR>/restart.request` | каталог внутри профиля владельца — писать может только он (+ SYSTEM/админы); файл с другим именем или в другом каталоге для watchdog'а не существует |
| SID владельца файла == SID каталога данных | чужой аккаунт, каким-то образом получивший запись, всё равно не сработает (best-effort: SID не читается — проверка пропускается) |
| `kind` = `persona-restart-request/1` | случайный файл — не запрос |
| `repo` == репо watchdog'а | маркер от другого проекта игнорируется |
| `version` по белому списку символов, `nonce` — hex | ничего постороннего в лог и в сравнения |
| возраст: и `requested_at`, и **mtime файла** ≤ 300 с | подсунуть старый файл со свежим телом не выйдет; «из будущего» тоже отвергается |
| размер ≤ 4 КБ | огромный файл даже не парсится |

### Почему не бывает петли рестартов

* маркер **удаляется до** начала рестарта и **при любом исходе** — принят он
  или отвергнут (иначе битый файл жевался бы каждую минуту вечно);
* принятый `nonce` пишется в журнал `restart.request.seen` **до** рестарта:
  если удаление не прошло (файл залочен) или маркер вернули — это replay,
  отказ;
* маркер от рухнувшего на середине деплоя просто **протухает** через 300 с;
* `MIN_INTERVAL_SECONDS = 15` — два принятых запроса подряд быстрее этого
  интервала невозможны.

### Escape hatch: сделать руками (ТРЕБУЕТ АДМИНА)

Если хелпер вышел с `1` — маркер не подхватили, или рестарт не удался.
Открыть PowerShell **от имени администратора**:

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like '*app.web.main*' } |
    ForEach-Object { taskkill /F /PID $_.ProcessId /T }
Start-ScheduledTask -TaskName PersonaWatchdog
```

Затем — обязательно — проверить версию:

```powershell
.venv\Scripts\python.exe ops\deploy_restart.py --status
```

### Что по-прежнему требует повышения

* установка/починка самих задач (раздел 4) — S4U, `RunLevel=Highest`,
  `BootTrigger`;
* ручное убийство uvicorn'а сессии 0 (escape hatch выше);
* чтение `CommandLine` чужих процессов — из неповышенного шелла оно **пустое**
  (см. раздел 5, проверка сессии идёт через `Get-NetTCPConnection`).

Обычный деплой ничего из этого не требует.

---

## 4. Установка / починка (ТРЕБУЕТ АДМИНА)

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
Сделать один чистый цикл — **в том же повышенном окне**, пока оно открыто:

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like '*app.web.main*' } |
    ForEach-Object { taskkill /F /PID $_.ProcessId /T }
Start-ScheduledTask -TaskName PersonaWatchdog
```

Потом проверить версию (это уже можно из обычного шелла):

```powershell
.venv\Scripts\python.exe ops\deploy_restart.py --status
```

---

## 5. Проверка

```powershell
# принципал и триггеры
$t = Get-ScheduledTask -TaskName PersonaWatchdog
"$($t.Principal.LogonType) / $($t.Principal.RunLevel)"          # ждём: S4U / Highest
$t.Triggers | ForEach-Object { $_.CimClass.CimClassName }        # ждём: BootTrigger + TimeTrigger
$t.Settings | Format-List StartWhenAvailable, ExecutionTimeLimit, MultipleInstances

# принудительный прогон
Start-ScheduledTask -TaskName PersonaWatchdog
Get-ScheduledTaskInfo -TaskName PersonaWatchdog | Format-List LastRunTime, LastTaskResult   # ждём 0

# ГЛАВНОЕ: живой ли сайт и СВЕЖИЙ ли код (порт сам по себе ничего не доказывает).
# Одной командой, обе проверки сразу, ненулевой код возврата при расхождении:
.venv\Scripts\python.exe ops\deploy_restart.py --status

# то же руками
$r = Invoke-WebRequest http://127.0.0.1:8000/landing -UseBasicParsing
$r.StatusCode                                                    # 200
($r.Content | Select-String '\?v=([0-9.]+)' -AllMatches).Matches |
    ForEach-Object { $_.Groups[1].Value } | Select-Object -Unique # должно совпасть с app/__init__.py __version__
(Invoke-WebRequest http://127.0.0.1:8000/healthz -UseBasicParsing).Content

# доказательство «переживает логаут»: слушатель :8000 сидит в сессии 0.
# Через CommandLine это НЕ проверить из обычного шелла — у чужих процессов
# он приходит ПУСТЫМ, и фильтр '*app.web.main*' молча даёт ноль строк
# (легко решить, что сервер не запущен, и наплодить дублей).
Get-NetTCPConnection -LocalPort 8000 -State Listen | ForEach-Object {
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$($_.OwningProcess)"
    "PID $($p.ProcessId)  SessionId=$($p.SessionId)  $($p.Name)"  # SessionId ждём 0
}

# последний запрос на рестарт и его вердикт
Get-Content C:\Users\Yaroslav\.persona\restart.result
Select-String RESTART-REQUEST C:\Users\Yaroslav\.persona\watchdog.log | Select-Object -Last 5
```

### После перезагрузки

Зайти по RDP **не входя** под владельцем (или просто подождать) и с любой
машины дёрнуть сайт по HTTP. Если отвечает 200 — BootTrigger + S4U работают.
Дополнительно: `Get-ScheduledTaskInfo -TaskName PersonaWatchdog` покажет
`LastRunTime` вскоре после времени загрузки.

---

## 6. Откат

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

## 7. Известные грабли

**Осиротевший uvicorn на унаследованном сокете.** Самая частая беда. Старый
процесс держит `:8000` и отдаёт код прошлой версии; порт «живой», сайт
«работает», но версия не та. Порт НИКОГДА не доказывает свежесть кода —
проверять `?v=` и `/healthz` против `app/__init__.py __version__`. Лечится
деплоем из раздела 3 (`ops/deploy_restart.py`), который на это и заточен: без
HTTP-подтверждения версии он не рапортует успех.

**`taskkill` из обычного шелла врёт.** По процессу сессии 0 он печатает
`SUCCESS`, возвращает `0` и ничего не убивает. Не «чинить» это ретраями —
это отсутствие прав. См. раздел 3.

**Процессы сессии 0 не видны в Диспетчере задач, а их `CommandLine` пуст.**
Вкладка «Процессы» показывает только текущую сессию — нужна вкладка
«Подробности» с колонкой *ИД сеанса*. И отдельно: из неповышенного шелла
`Win32_Process.CommandLine` у чужих процессов приходит **пустым**, поэтому
привычный фильтр `Where-Object { $_.CommandLine -like '*app.web.main*' }`
даёт **ноль строк даже когда сервер работает**. Легко решить, что он не
запущен, и наплодить дублей. Проверять через `Get-NetTCPConnection -LocalPort
8000` → `OwningProcess` → `SessionId` (раздел 5).

**Маркер рестарта лежит, а ничего не происходит.** Смотреть
`~/.persona/restart.result` и строки `RESTART-REQUEST` в
`~/.persona/watchdog.log`. `status=ignored` — маркер не прошёл валидацию
(причина там же: протух, чужой репо, replay, cooldown); `status=failed` —
watchdog не смог перезапустить (в `detail` обычно PID'ы, которые он не убил).
Молчание — задача не тикает: `Get-ScheduledTaskInfo -TaskName PersonaWatchdog`.

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
