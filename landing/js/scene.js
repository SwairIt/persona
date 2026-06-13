/* ============================================================
   Persona — 3D hero scene (Three.js, ESM)
   Скилл 3d-web-experience:
   - DPR=1 на мобиле, cap 2 на десктопе
   - WebGL-fallback на CSS-орб (body.no-webgl)
   - пауза рендера, когда герой вне вьюпорта / вкладка скрыта
   - reduced-motion → сцену не поднимаем вообще
   "Нейро-ядро" Persona: икосаэдр-каркас + облако частиц + ядро-свечение.
   Реагирует на скролл (setHeroProgress) и курсор (десктоп).
   ============================================================ */

const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

// Заглушка API на случай раннего вызова из main.js
window.PersonaScene = {
  setHeroProgress() {},
  ready: false,
};

function markNoWebGL() {
  document.body.classList.add('no-webgl');
}

// reduced-motion: статичный fallback-орб, 3D не грузим
if (reduceMotion) {
  markNoWebGL();
} else {
  boot();
}

async function boot() {
  let THREE;
  try {
    THREE = await import('three');
  } catch (e) {
    console.warn('[scene] three.js не загрузился:', e);
    markNoWebGL();
    return;
  }

  const canvas = document.getElementById('hero-canvas');
  if (!canvas) { markNoWebGL(); return; }

  // --- детект WebGL ---
  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({
      canvas,
      antialias: window.innerWidth > 768,
      alpha: true,
      powerPreference: 'high-performance',
    });
  } catch (e) {
    console.warn('[scene] нет WebGL:', e);
    markNoWebGL();
    return;
  }

  const isMobile = window.matchMedia('(max-width: 768px)').matches;
  const DPR_CAP = isMobile ? 1 : 2;                          // скилл: DPR=1 на мобиле
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, DPR_CAP));
  renderer.setSize(window.innerWidth, window.innerHeight);
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.15;

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(
    50, window.innerWidth / window.innerHeight, 0.1, 100
  );
  camera.position.z = 6;

  // --- группа-ядро ---
  const core = new THREE.Group();
  scene.add(core);

  const detail = isMobile ? 1 : 2;                           // меньше геометрии на мобиле
  const geo = new THREE.IcosahedronGeometry(1.7, detail);

  // каркас (additive — светится на тёмном)
  const wire = new THREE.LineSegments(
    new THREE.WireframeGeometry(geo),
    new THREE.LineBasicMaterial({
      color: 0xb79cff, transparent: true, opacity: 0.9, blending: THREE.AdditiveBlending,
    })
  );
  core.add(wire);

  // вершины как точки
  const pts = new THREE.Points(
    geo,
    new THREE.PointsMaterial({
      color: 0x5eead4, size: 0.1, transparent: true, opacity: 1, blending: THREE.AdditiveBlending,
    })
  );
  core.add(pts);

  // внутреннее свечение (несколько слоёв для bloom-эффекта без постпроцесса)
  const glow = new THREE.Mesh(
    new THREE.IcosahedronGeometry(1.32, 1),
    new THREE.MeshBasicMaterial({
      color: 0xf472b6, transparent: true, opacity: 0.22, blending: THREE.AdditiveBlending,
    })
  );
  core.add(glow);
  const glow2 = new THREE.Mesh(
    new THREE.SphereGeometry(1.05, 24, 24),
    new THREE.MeshBasicMaterial({
      color: 0x7c3aed, transparent: true, opacity: 0.30, blending: THREE.AdditiveBlending,
    })
  );
  core.add(glow2);

  // --- облако частиц вокруг ---
  const STAR_COUNT = isMobile ? 350 : 900;
  const starPos = new Float32Array(STAR_COUNT * 3);
  for (let i = 0; i < STAR_COUNT; i++) {
    const r = 4 + Math.random() * 9;
    const t = Math.random() * Math.PI * 2;
    const p = Math.acos(2 * Math.random() - 1);
    starPos[i * 3]     = r * Math.sin(p) * Math.cos(t);
    starPos[i * 3 + 1] = r * Math.sin(p) * Math.sin(t);
    starPos[i * 3 + 2] = r * Math.cos(p);
  }
  const starGeo = new THREE.BufferGeometry();
  starGeo.setAttribute('position', new THREE.BufferAttribute(starPos, 3));
  const stars = new THREE.Points(
    starGeo,
    new THREE.PointsMaterial({ color: 0x9aa0d8, size: 0.035, transparent: true, opacity: 0.6 })
  );
  scene.add(stars);

  // --- свет ---
  scene.add(new THREE.AmbientLight(0xffffff, 0.7));
  const l1 = new THREE.PointLight(0x8b5cf6, 60, 50); l1.position.set(5, 5, 5); scene.add(l1);
  const l2 = new THREE.PointLight(0x22d3ee, 45, 50); l2.position.set(-5, -3, 4); scene.add(l2);
  const l3 = new THREE.PointLight(0xf472b6, 35, 50); l3.position.set(0, -5, 2); scene.add(l3);

  // --- состояние скролла/курсора ---
  let heroProgress = 0;        // 0..1 прогресс внутри pinned-героя (ведёт ScrollTrigger)
  let targetMouseX = 0, targetMouseY = 0;
  let mouseX = 0, mouseY = 0;

  if (!isMobile) {
    window.addEventListener('pointermove', (e) => {
      targetMouseX = (e.clientX / window.innerWidth - 0.5);
      targetMouseY = (e.clientY / window.innerHeight - 0.5);
    }, { passive: true });
  }

  // публичное API для main.js
  window.PersonaScene = {
    ready: true,
    setHeroProgress(p) { heroProgress = Math.max(0, Math.min(1, p || 0)); },
  };

  // --- пауза рендера, когда ГЕРОЙ ушёл из вьюпорта / вкладка скрыта ---
  // (canvas — fixed на весь экран, поэтому наблюдаем за секцией героя, а не за canvas:
  //  иначе 3D крутился бы всю страницу и грузил слабые устройства)
  let visible = true, tabActive = true;
  const heroEl = document.getElementById('hero') || canvas;
  const io = new IntersectionObserver(
    ([entry]) => {
      visible = entry.isIntersecting;
      canvas.classList.toggle('ready', visible);   // плавно гасим/возвращаем 3D
    },
    { threshold: 0, rootMargin: '0px 0px -10% 0px' }
  );
  io.observe(heroEl);
  document.addEventListener('visibilitychange', () => { tabActive = !document.hidden; });

  // --- resize (throttled через rAF) ---
  let resizePending = false;
  window.addEventListener('resize', () => {
    if (resizePending) return;
    resizePending = true;
    requestAnimationFrame(() => {
      camera.aspect = window.innerWidth / window.innerHeight;
      camera.updateProjectionMatrix();
      renderer.setSize(window.innerWidth, window.innerHeight);
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, DPR_CAP));
      resizePending = false;
    });
  }, { passive: true });

  // показать canvas после первого кадра
  canvas.classList.add('ready');

  let t = 0;
  function tick() {
    requestAnimationFrame(tick);
    if (!visible || !tabActive) return;     // экономим на слабых: не рисуем зря
    t += 0.016;

    // плавный курсор-параллакс
    mouseX += (targetMouseX - mouseX) * 0.05;
    mouseY += (targetMouseY - mouseY) * 0.05;

    // базовое вращение + вклад скролла (объект «оживает» по мере скролла)
    core.rotation.y = t * 0.25 + heroProgress * Math.PI * 1.5 + mouseX * 0.6;
    core.rotation.x = t * 0.12 + mouseY * 0.5;

    // по скроллу ядро приближается и слегка раздувается, затем уходит вглубь
    const s = 1 + heroProgress * 0.55;
    core.scale.setScalar(s);
    camera.position.z = 6 - heroProgress * 1.4;
    glow.material.opacity = 0.10 + heroProgress * 0.18;

    stars.rotation.y = t * 0.02;
    renderer.render(scene, camera);
  }
  tick();
}
