/**
 * Сборка Tailwind вместо Play-CDN.
 *
 * Раньше каждая страница тянула `static/vendor/tailwind-play.js` — 407 КБ
 * JIT-компилятора, БЕЗ `defer`, то есть блокирующего разбор документа. Он
 * скачивался, парсился, обходил DOM и генерировал CSS заново на КАЖДОЙ
 * загрузке. Замер на этой машине (тёплый кэш, без сетевой задержки):
 * FCP 1320 → 884 мс, DOMContentLoaded 2092 → 1496 мс, когда файл заблокирован.
 * На телефоне разница кратно больше.
 *
 * Конфиг повторяет тот, что был зашит инлайном в шаблонах (base.html,
 * auth_login, auth_signup, mobile, tour) — палитра ink/accent и шрифты.
 * Пересборка: `npm run css` в ops/tailwind (см. ops/tailwind/README.md).
 */
module.exports = {
  darkMode: 'class',
  content: [
    './app/web/templates/**/*.html',
    './app/web/static/**/*.js',
    './landing/**/*.html',
  ],
  theme: {
    extend: {
      colors: {
        ink: { 950: '#0a0a0c', 900: '#111114', 800: '#1a1a1f', 700: '#26262e' },
        accent: { 400: '#a78bfa', 500: '#8b5cf6', 600: '#7c3aed' },
      },
      fontFamily: {
        sans: ['system-ui', '-apple-system', 'Segoe UI', 'Roboto', 'sans-serif'],
        mono: ['JetBrains Mono', 'Cascadia Code', 'Consolas', 'monospace'],
      },
    },
  },
};
