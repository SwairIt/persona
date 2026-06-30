/*
 * vad.js — лёгкий браузерный VAD (Voice Activity Detection) для авто-стопа
 * микрофона по тишине.
 *
 * Зачем: на путях server-STT / MediaRecorder (Safari, отсутствие Web Speech)
 * у записи нет авто-остановки — пользователь обязан тапнуть «стоп» вручную.
 * Web Speech путь НЕ затрагиваем — там авто-стоп уже есть (continuous=false).
 *
 * Как: WebAudio AnalyserNode считает энергию (RMS) входного потока в цикле на
 * requestAnimationFrame. Когда уровень держится ниже порога дольше silenceMs —
 * дёргаем onSilence(). Без тяжёлых wasm-зависимостей; кросс-браузерно.
 *
 * При отсутствии WebAudio (старый браузер / нет AudioContext) — тихий no-op:
 * запись продолжает работать как раньше, просто без авто-стопа.
 *
 * Контракт:
 *   const detach = attachSilenceVad(mediaStream, onSilence, opts);
 *   // detach() — снять VAD (остановить цикл, закрыть AudioContext).
 *
 * opts:
 *   silenceMs  — мс тишины до срабатывания (деф. 1500)
 *   threshold  — порог RMS [0..1], ниже которого считаем тишиной (деф. 0.015)
 *   minSpeechMs— минимум мс речи до того, как тишина начнёт «считаться»
 *                (деф. 300) — чтобы не стопнуть до того, как человек заговорил
 *   onSilence  — колбэк при обнаружении тишины (вызывается один раз)
 */
(function (global) {
  'use strict';

  // Безопасный no-op: вызвать колбэк-отписку нечем — возвращаем пустышку.
  function noop() {}

  function attachSilenceVad(mediaStream, onSilence, opts) {
    opts = opts || {};
    var silenceMs = typeof opts.silenceMs === 'number' ? opts.silenceMs : 1500;
    var threshold = typeof opts.threshold === 'number' ? opts.threshold : 0.015;
    var minSpeechMs = typeof opts.minSpeechMs === 'number' ? opts.minSpeechMs : 300;

    // Нет потока или колбэка — нечего делать.
    if (!mediaStream || typeof onSilence !== 'function') return noop;

    var AudioCtx = global.AudioContext || global.webkitAudioContext;
    // Нет WebAudio — тихий no-op: запись просто без авто-стопа (как было).
    if (!AudioCtx) return noop;

    var ctx, source, analyser, data, raf, timer;
    var fired = false;            // onSilence срабатывает только один раз
    var spokeAt = 0;              // момент, когда впервые услышали речь
    var silenceStart = 0;        // момент начала текущей паузы (0 = тишины нет)
    var stopped = false;

    try {
      ctx = new AudioCtx();
      source = ctx.createMediaStreamSource(mediaStream);
      analyser = ctx.createAnalyser();
      analyser.fftSize = 2048;          // компромисс точность/нагрузка
      analyser.smoothingTimeConstant = 0.4;
      source.connect(analyser);
      data = new Uint8Array(analyser.fftSize);
    } catch (e) {
      // Любой сбой инициализации WebAudio → тихий no-op.
      try { if (ctx && ctx.close) ctx.close(); } catch (_e) {}
      return noop;
    }

    // Некоторые браузеры создают AudioContext в состоянии 'suspended' —
    // пробуем возобновить (best-effort, без await).
    try { if (ctx.state === 'suspended' && ctx.resume) ctx.resume(); } catch (_e) {}

    function rms() {
      // Текущий уровень громкости как RMS по time-domain выборке.
      analyser.getByteTimeDomainData(data);
      var sum = 0;
      for (var i = 0; i < data.length; i++) {
        var v = (data[i] - 128) / 128; // [-1..1]
        sum += v * v;
      }
      return Math.sqrt(sum / data.length);
    }

    function tick() {
      if (stopped || fired) return;
      var now = (global.performance && performance.now) ? performance.now() : Date.now();
      var level = rms();

      if (level >= threshold) {
        // Речь/шум выше порога — фиксируем, что человек заговорил, сбрасываем паузу.
        if (!spokeAt) spokeAt = now;
        silenceStart = 0;
      } else {
        // Тишина учитывается только ПОСЛЕ того, как была речь (minSpeechMs).
        if (spokeAt && (now - spokeAt) >= minSpeechMs) {
          if (!silenceStart) silenceStart = now;
          else if ((now - silenceStart) >= silenceMs) {
            fired = true;
            try { onSilence(); } catch (_e) {}
            return; // больше не планируем кадры — ждём detach()
          }
        }
      }

      // Предпочитаем rAF (синхронизация с кадром, пауза в фоне), но держим
      // запасной setTimeout — на случай, если rAF недоступен.
      if (global.requestAnimationFrame) raf = global.requestAnimationFrame(tick);
      else timer = global.setTimeout(tick, 100);
    }

    // Старт цикла.
    if (global.requestAnimationFrame) raf = global.requestAnimationFrame(tick);
    else timer = global.setTimeout(tick, 100);

    // Отписка: остановить цикл и освободить аудио-ресурсы (идемпотентно).
    return function detach() {
      if (stopped) return;
      stopped = true;
      try { if (raf && global.cancelAnimationFrame) global.cancelAnimationFrame(raf); } catch (_e) {}
      try { if (timer) global.clearTimeout(timer); } catch (_e) {}
      try { if (source && source.disconnect) source.disconnect(); } catch (_e) {}
      try { if (analyser && analyser.disconnect) analyser.disconnect(); } catch (_e) {}
      try { if (ctx && ctx.close) ctx.close(); } catch (_e) {}
    };
  }

  // Глобальный экспорт (модулей в проекте нет — кладём в window).
  global.attachSilenceVad = attachSilenceVad;
})(typeof window !== 'undefined' ? window : this);
