"""FastAPI-приложение монитора нагрузки (порт 8770 по умолчанию).

Фоновый сэмплер пишет нагрузку в SQLite каждые LOADMON_INTERVAL сек. API +
дашборд показывают live-нагрузку и историю; Persona шлёт сюда события
генерации и читает /api/load/now для индикатора в чате.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from loadmon import store
from loadmon.sampler import sample, sample_for_db

_INTERVAL = float(os.environ.get("LOADMON_INTERVAL", "3"))


async def _sampler_loop(stop: asyncio.Event) -> None:
    # первый вызов cpu_percent возвращает 0 — «прогреваем»
    try:
        sample_for_db()
    except Exception:
        pass
    while not stop.is_set():
        try:
            store.insert_sample(sample_for_db())
        except Exception:
            pass
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=_INTERVAL)


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):
    store.init_db()
    stop = asyncio.Event()
    task = asyncio.create_task(_sampler_loop(stop))
    app.state.stop = stop
    try:
        yield
    finally:
        stop.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def create_app() -> FastAPI:
    app = FastAPI(title="loadmon — монитор нагрузки", lifespan=_lifespan)
    # CORS: Persona (8000) и локальные страницы должны иметь доступ
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    async def health() -> JSONResponse:
        return JSONResponse({"ok": True, "service": "loadmon"})

    @app.get("/api/load/now")
    async def load_now() -> JSONResponse:
        return JSONResponse(sample())

    @app.get("/api/load/history")
    async def load_history(minutes: int = 10) -> JSONResponse:
        return JSONResponse({"samples": store.recent_samples(minutes=minutes)})

    @app.get("/api/models")
    async def models() -> JSONResponse:
        return JSONResponse({"models": store.model_stats()})

    @app.post("/api/load/event")
    async def load_event(request: Request) -> JSONResponse:
        try:
            body: dict[str, Any] = await request.json()
        except Exception:
            body = {}
        eid = store.insert_event(body)
        return JSONResponse({"ok": True, "id": eid})

    @app.get("/", response_class=HTMLResponse)
    async def dashboard() -> HTMLResponse:
        return HTMLResponse(_DASHBOARD_HTML)

    return app


_DASHBOARD_HTML = """<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>loadmon — нагрузка ПК и моделей</title>
<style>
 body{margin:0;background:#0b0e14;color:#e4e4e7;font:14px/1.5 system-ui,sans-serif}
 .wrap{max-width:900px;margin:0 auto;padding:24px}
 h1{font-size:20px;margin:0 0 4px} .sub{color:#71717a;font-size:12px;margin-bottom:20px}
 .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}
 .card{background:#151922;border:1px solid #232a36;border-radius:12px;padding:14px}
 .lbl{color:#71717a;font-size:12px} .val{font-size:24px;font-weight:700;margin-top:2px}
 .bar{height:8px;background:#232a36;border-radius:99px;overflow:hidden;margin-top:8px}
 .fill{height:100%;background:linear-gradient(90deg,#22c55e,#eab308,#ef4444);transition:width .4s}
 table{width:100%;border-collapse:collapse;margin-top:8px;font-size:13px}
 th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #1c2330} th{color:#71717a;font-weight:500}
 .mut{color:#71717a;font-size:12px} .ok{color:#22c55e} .warn{color:#eab308}
 .dot{display:inline-block;width:8px;height:8px;border-radius:99px;margin-right:6px}
</style></head><body><div class=wrap>
 <h1>🖥️ Нагрузка ПК и моделей</h1>
 <div class=sub>Обновление каждые 2с · отдельный сервис loadmon · <span id=gpuname>—</span></div>
 <div class=grid id=gauges></div>
 <div class=card style="margin-top:16px">
   <div class=lbl>Загруженные модели Ollama (сейчас)</div>
   <table id=loaded><thead><tr><th>модель</th><th>VRAM</th><th>на GPU</th></tr></thead><tbody></tbody></table>
 </div>
 <div class=card style="margin-top:12px">
   <div class=lbl>Статистика моделей (по событиям генерации Persona)</div>
   <table id=stats><thead><tr><th>модель</th><th>ток/с (сред/макс)</th><th>событий</th><th>заметка</th></tr></thead><tbody></tbody></table>
 </div>
</div>
<script>
const g=(id)=>document.getElementById(id);
function gauge(lbl,val,sub,pct){return `<div class=card><div class=lbl>${lbl}</div><div class=val>${val}</div><div class=mut>${sub||''}</div>${pct!=null?`<div class=bar><div class=fill style="width:${Math.min(100,pct)}%"></div></div>`:''}</div>`}
async function tick(){
 try{
  const s=await (await fetch('/api/load/now')).json();
  g('gpuname').textContent = s.gpu_name || (s.nvidia_smi?'NVIDIA':'GPU не найден (нет nvidia-smi)');
  let h='';
  h+=gauge('CPU', (s.cpu_pct??'—')+'%', (s.cpu_cores||'')+' ядер', s.cpu_pct);
  h+=gauge('RAM', (s.ram_pct??'—')+'%', `${s.ram_used_mb||0} / ${s.ram_total_mb||0} МБ`, s.ram_pct);
  if(s.gpu_pct!=null||s.vram_used_mb!=null){
    h+=gauge('GPU', (s.gpu_pct??'—')+'%', s.gpu_temp_c!=null?(s.gpu_temp_c+'°C'):'', s.gpu_pct);
    const vp = (s.vram_used_mb&&s.vram_total_mb)?Math.round(100*s.vram_used_mb/s.vram_total_mb):null;
    h+=gauge('VRAM', `${s.vram_used_mb||0} МБ`, `из ${s.vram_total_mb||0} МБ`, vp);
  } else {
    h+=gauge('GPU','—','nvidia-smi недоступен',null);
  }
  g('gauges').innerHTML=h;
  const lb=g('loaded').querySelector('tbody');
  lb.innerHTML=(s.models||[]).map(m=>`<tr><td>${m.name}</td><td>${m.vram_mb||'—'} МБ</td><td>${m.on_gpu_pct!=null?m.on_gpu_pct+'%':'—'}</td></tr>`).join('')||'<tr><td colspan=3 class=mut>нет загруженных моделей (Ollama не запущен или idle)</td></tr>';
 }catch(e){}
 try{
  const ms=(await (await fetch('/api/models')).json()).models||[];
  g('stats').querySelector('tbody').innerHTML=ms.map(m=>`<tr><td>${m.model}</td><td>${m.avg_tps?m.avg_tps.toFixed(1):'—'} / ${m.max_tps?m.max_tps.toFixed(1):'—'}</td><td>${m.n}</td><td class="${(m.avg_tps>=8)?'ok':'warn'}">${m.hint}</td></tr>`).join('')||'<tr><td colspan=4 class=mut>пока нет событий — Persona ещё не присылала метрики генерации</td></tr>';
 }catch(e){}
}
tick(); setInterval(tick,2000);
</script></body></html>"""


__all__ = ["create_app"]
