# Persona — лендинг

Эпичный 3D-лендинг для Persona: scroll-driven анимации, parallax, glassmorphism,
Three.js-герой. Чистый HTML/CSS/JS + CDN — без билд-шага.

## Запуск локально

Из-за ES-модулей (Three.js) нужен http-сервер, не `file://`:

```bash
# из папки landing/
python -m http.server 8088
# открыть http://localhost:8088
```

## Структура

```
landing/
├── index.html        # разметка, секции, SEO/OG, importmap Three.js
├── css/style.css     # glassmorphism, градиенты, адаптив, reduced-motion
└── js/
    ├── scene.js      # Three.js «нейро-ядро» героя (ESM) + WebGL-fallback
    └── main.js       # Lenis + GSAP ScrollTrigger оркестрация
```

## Что внутри (по скиллам skills.sh)

- **scroll-experience** — story beats Hook→Context→Features→How→Privacy→CTA,
  pinned-герой, parallax-слои разной скорости, hue-сдвиг фона по секциям, прогресс-бар.
- **gsap-framer-scroll-animation** — vanilla GSAP ScrollTrigger; `scrub` только с
  `ease:none`; анимируются только transform/opacity.
- **3d-web-experience** — Three.js vanilla; DPR=1 на мобиле / cap 2 на десктопе;
  WebGL-детект + CSS-fallback орб; пауза рендера вне вьюпорта и на скрытой вкладке.
- **web-performance-optimization** — критический CSS инлайном, preconnect к CDN,
  defer-скрипты, `prefers-reduced-motion` полностью гасит движение.

## Оптимизация под слабые устройства

- На мобиле: меньше частиц (350 vs 900), меньше детализации икосаэдра, DPR=1,
  слабее blur у blob'ов, нативный тач-скролл (Lenis smoothTouch=off).
- Рендер 3D ставится на паузу, когда герой ушёл из вьюпорта или вкладка неактивна.
- `prefers-reduced-motion` → 3D не поднимается вообще, показывается статичный орб,
  весь контент сразу виден.

## Адаптив

Mobile-first, `clamp()`-типографика, grid с auto-fit. Контент читается без JS
(progressive enhancement) — текст в DOM, не в canvas (SEO).
