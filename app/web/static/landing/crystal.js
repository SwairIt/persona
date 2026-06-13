/* Интерактивный 3D-кристалл (Three.js, ESM).
   Ключ к «не лагает»: ОГРАНИЧЕННЫЙ canvas (не fullscreen), ленивый старт
   (грузим Three.js только когда секцию долистали), кап 30 FPS, пауза вне
   вьюпорта/скрытой вкладки, drag-rotate без сторонних зависимостей. */

const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const canvas = document.getElementById('crystal-canvas');

if (canvas) {
  let started = false;
  const lazy = new IntersectionObserver((entries) => {
    if (entries[0].isIntersecting && !started) {
      started = true;
      lazy.disconnect();
      start().catch((e) => { console.warn('[crystal]', e); canvas.style.display = 'none'; });
    }
  }, { threshold: 0.15 });
  lazy.observe(canvas);
}

async function start() {
  const THREE = await import('three');

  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true, powerPreference: 'high-performance' });
  } catch (e) {
    canvas.style.display = 'none';
    return;
  }
  const sizeOf = () => Math.min(canvas.parentElement.clientWidth, 460);
  let S = sizeOf();
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setSize(S, S);
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.1;

  const scene = new THREE.Scene();
  const cam = new THREE.PerspectiveCamera(45, 1, 0.1, 100);
  cam.position.z = 4.3;

  const group = new THREE.Group();
  scene.add(group);

  const geo = new THREE.IcosahedronGeometry(1.5, 1);
  group.add(new THREE.LineSegments(
    new THREE.WireframeGeometry(geo),
    new THREE.LineBasicMaterial({ color: 0xb79cff, transparent: true, opacity: 0.9, blending: THREE.AdditiveBlending })
  ));
  group.add(new THREE.Points(
    geo,
    new THREE.PointsMaterial({ color: 0x5eead4, size: 0.1, transparent: true, blending: THREE.AdditiveBlending })
  ));
  group.add(new THREE.Mesh(
    new THREE.IcosahedronGeometry(1.12, 1),
    new THREE.MeshBasicMaterial({ color: 0x7c3aed, transparent: true, opacity: 0.28, blending: THREE.AdditiveBlending })
  ));
  scene.add(new THREE.AmbientLight(0xffffff, 0.85));
  const l1 = new THREE.PointLight(0x22d3ee, 40, 40); l1.position.set(4, 3, 4); scene.add(l1);
  const l2 = new THREE.PointLight(0xf472b6, 25, 40); l2.position.set(-4, -2, 2); scene.add(l2);

  // --- drag-rotate (мышь/тач), с инерцией ---
  let dragging = false, px = 0, py = 0, vx = 0, vy = 0, rx = 0.3, ry = 0.2;
  const hint = document.getElementById('core3d-hint');
  const at = (e) => { const t = e.touches ? e.touches[0] : e; return { x: t.clientX, y: t.clientY }; };
  canvas.style.touchAction = 'none';
  canvas.addEventListener('pointerdown', (e) => { dragging = true; const p = at(e); px = p.x; py = p.y; canvas.classList.add('grabbing'); if (hint) hint.style.opacity = '0'; });
  window.addEventListener('pointermove', (e) => {
    if (!dragging) return;
    const p = at(e);
    vy = (p.x - px) * 0.01; vx = (p.y - py) * 0.01;
    ry += vy; rx += vx; px = p.x; py = p.y;
  });
  window.addEventListener('pointerup', () => { dragging = false; canvas.classList.remove('grabbing'); });

  let visible = true;
  new IntersectionObserver(([e]) => { visible = e.isIntersecting; }, { threshold: 0 }).observe(canvas);
  window.addEventListener('resize', () => { S = sizeOf(); renderer.setSize(S, S); }, { passive: true });

  const FRAME = 1000 / 30;
  let last = 0;
  function tick(now) {
    requestAnimationFrame(tick);
    if (!visible || document.hidden) return;
    if (now - last < FRAME) return;
    last = now;
    if (!dragging) {
      if (!reduce) ry += 0.004;   // мягкое авто-вращение
      ry += vy; rx += vx; vy *= 0.93; vx *= 0.93;  // инерция
    }
    group.rotation.y = ry;
    group.rotation.x = rx;
    renderer.render(scene, cam);
  }
  requestAnimationFrame(tick);
}
