/* ============================================================
   Persona landing — 3D «ядро памяти» (Three.js, WebGL).
   Светящийся икосаэдр + облако частиц + связи, реагирует на курсор.
   Перформанс: DPR ≤ 1.5, пауза вне экрана и на скрытой вкладке,
   при prefers-reduced-motion рисуем один статичный кадр (без rAF).
   Грейсфул-фолбэк: если THREE не загрузился — тихо выходим, лендинг живёт.
   ============================================================ */
(function () {
  'use strict';
  var canvas = document.getElementById('hero3d');
  if (!canvas || typeof window.THREE === 'undefined') return;

  var THREE = window.THREE;
  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var DPR = Math.min(window.devicePixelRatio || 1, 1.5);

  var host = canvas.parentElement || canvas;
  var W = host.clientWidth || 600;
  var H = host.clientHeight || 600;

  var renderer;
  try {
    renderer = new THREE.WebGLRenderer({ canvas: canvas, antialias: true, alpha: true, powerPreference: 'high-performance' });
  } catch (e) { return; }
  renderer.setPixelRatio(DPR);
  renderer.setSize(W, H, false);

  var scene = new THREE.Scene();
  var camera = new THREE.PerspectiveCamera(50, W / H, 0.1, 100);
  camera.position.set(0, 0, 6.2);

  var group = new THREE.Group();
  scene.add(group);

  var COL_VIOLET = new THREE.Color(0x9b7bff);
  var COL_CYAN = new THREE.Color(0x36e0ff);
  var COL_MAGENTA = new THREE.Color(0xe879f9);

  // --- ядро: икосаэдр (каркас + полупрозрачная оболочка) ---
  var icoGeo = new THREE.IcosahedronGeometry(1.55, 1);
  var wire = new THREE.LineSegments(
    new THREE.EdgesGeometry(icoGeo),
    new THREE.LineBasicMaterial({ color: COL_VIOLET, transparent: true, opacity: 0.85 })
  );
  group.add(wire);

  var shell = new THREE.Mesh(
    new THREE.IcosahedronGeometry(1.5, 1),
    new THREE.MeshStandardMaterial({
      color: 0x140b2e, emissive: COL_VIOLET, emissiveIntensity: 0.35,
      metalness: 0.6, roughness: 0.25, transparent: true, opacity: 0.55, flatShading: true,
    })
  );
  group.add(shell);

  // ядро-сфера в центре (мягкое свечение)
  var coreDot = new THREE.Mesh(
    new THREE.SphereGeometry(0.42, 24, 24),
    new THREE.MeshBasicMaterial({ color: COL_CYAN, transparent: true, opacity: 0.9 })
  );
  group.add(coreDot);

  // --- облако частиц (память) вокруг ядра ---
  var COUNT = window.innerWidth < 720 ? 420 : 820;
  var positions = new Float32Array(COUNT * 3);
  var colors = new Float32Array(COUNT * 3);
  var seeds = [];
  for (var i = 0; i < COUNT; i++) {
    // распределение в сферической оболочке радиусом 2.2–4.2
    var r = 2.2 + Math.random() * 2.0;
    var th = Math.random() * Math.PI * 2;
    var ph = Math.acos(2 * Math.random() - 1);
    var x = r * Math.sin(ph) * Math.cos(th);
    var y = r * Math.sin(ph) * Math.sin(th);
    var z = r * Math.cos(ph);
    positions[i * 3] = x; positions[i * 3 + 1] = y; positions[i * 3 + 2] = z;
    var c = Math.random() < 0.5 ? COL_CYAN : (Math.random() < 0.5 ? COL_VIOLET : COL_MAGENTA);
    colors[i * 3] = c.r; colors[i * 3 + 1] = c.g; colors[i * 3 + 2] = c.b;
    seeds.push({ r: r, th: th, ph: ph, sp: 0.1 + Math.random() * 0.4 });
  }
  var pGeo = new THREE.BufferGeometry();
  pGeo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
  pGeo.setAttribute('color', new THREE.BufferAttribute(colors, 3));
  var pMat = new THREE.PointsMaterial({
    size: 0.055, vertexColors: true, transparent: true, opacity: 0.9,
    blending: THREE.AdditiveBlending, depthWrite: false,
  });
  var points = new THREE.Points(pGeo, pMat);
  group.add(points);

  // --- свет ---
  scene.add(new THREE.AmbientLight(0x404060, 1.2));
  var l1 = new THREE.PointLight(0x8b5cf6, 2.2, 40); l1.position.set(5, 4, 6); scene.add(l1);
  var l2 = new THREE.PointLight(0x22d3ee, 2.0, 40); l2.position.set(-6, -3, 4); scene.add(l2);

  // --- интерактив: параллакс на курсор ---
  var tx = 0, ty = 0, cx = 0, cy = 0;
  if (!reduce) {
    window.addEventListener('pointermove', function (e) {
      tx = (e.clientX / window.innerWidth - 0.5) * 2;
      ty = (e.clientY / window.innerHeight - 0.5) * 2;
    }, { passive: true });
  }

  function resize() {
    W = host.clientWidth || 600; H = host.clientHeight || 600;
    camera.aspect = W / H; camera.updateProjectionMatrix();
    renderer.setSize(W, H, false);
  }
  window.addEventListener('resize', resize, { passive: true });

  function render() {
    renderer.render(scene, camera);
  }

  function frame(t) {
    var time = t * 0.001;
    group.rotation.y = time * 0.18;
    group.rotation.x = Math.sin(time * 0.25) * 0.18;
    points.rotation.y = -time * 0.05;
    wire.rotation.z = time * 0.06;
    var s = 1 + Math.sin(time * 1.6) * 0.04;  // «дыхание» ядра
    coreDot.scale.setScalar(s);
    coreDot.material.opacity = 0.7 + Math.sin(time * 1.6) * 0.2;
    // плавный параллакс
    cx += (tx - cx) * 0.05; cy += (ty - cy) * 0.05;
    group.position.x = cx * 0.5;
    group.position.y = -cy * 0.4;
    camera.lookAt(0, 0, 0);
    render();
  }

  // --- цикл с паузой вне экрана / на скрытой вкладке ---
  var raf = 0, visible = true;
  function loop(t) { raf = requestAnimationFrame(loop); if (!visible || document.hidden) return; frame(t); }

  if (reduce) {
    frame(1200);  // один статичный кадр
  } else {
    if ('IntersectionObserver' in window) {
      new IntersectionObserver(function (ents) {
        visible = ents[0].isIntersecting;
      }, { threshold: 0.01 }).observe(host);
    }
    raf = requestAnimationFrame(loop);
  }

  // на всякий случай дорисуем после полной загрузки шрифтов/лейаута
  window.addEventListener('load', resize);
})();
