/* ============================================================
   Persona · Landing v2 — 3D ЧЁРНАЯ ДЫРА (gravitational lensing)
   ------------------------------------------------------------
   Один полноэкранный фрагментный шейдер на WebGL (Three.js):
     • реальное искривление лучей вокруг горизонта событий
       (приближение фотонной геодезики Шварцшильда: a = -1.5·h²·r/|r|⁵);
     • аккреционный диск в фирменных цветах (розовый→фиолет→синий),
       турбулентность + доплеровское усиление одной стороны;
     • фотонное кольцо (яркий лавандовый ободок тени);
     • линзованное звёздное небо + лёгкая туманность за дырой;
     • ACES tone-mapping, мягкое свечение диска как «bloom».
   Перформанс: DPR≤1.6 (моб. ≤1.25), render-scale на слабых, пауза на
   скрытой вкладке и когда канвас вне экрана. reduced-motion → 1 статичный
   кадр. Нет THREE / нет WebGL → тихо выходим (CSS-фолбэк под канвасом).

   Параметры можно переопределить через window.PERSONA_BH (см. defaults).
   ============================================================ */
(function () {
  'use strict';

  var canvas = document.getElementById('blackhole');
  if (!canvas || typeof window.THREE === 'undefined') return;
  var THREE = window.THREE;

  // --- настройки (фирменная палитра по умолчанию) ---------------------------
  function hex(c) { var v = new THREE.Color(c); return new THREE.Vector3(v.r, v.g, v.b); }
  var defaults = {
    colInner: '#fbe3ff', // раскалённая внутренняя кромка
    colMid:   '#ba9cff', // фиолетовое тело диска (cosmic gradient core)
    colOuter: '#7c93ff', // холодный синий внешний край
    ring:     '#c9b8ff', // фотонное кольцо
    nebula:   '#2a1a5e', // подсветка туманности
    diskA: 1.5,          // яркость диска
    beam: 0.55,          // сила доплера (асимметрия яркости)
    camDist: 9.0,        // дистанция камеры
    tilt: 0.30,          // наклон обзора к плоскости диска (рад)
    fov: 1.0,            // «зум» (меньше = ближе)
    spin: 0.16,          // скорость вращения диска
  };
  var CFG = Object.assign({}, defaults, window.PERSONA_BH || {});

  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var small = window.innerWidth < 760;
  var DPR = Math.min(window.devicePixelRatio || 1, small ? 1.25 : 1.6);
  var RENDER_SCALE = small ? 0.85 : 1.0;   // внутренний рендер чуть меньше → апскейл CSS
  var STEPS = small ? 140 : 240;           // шаги интегрирования геодезики

  var renderer;
  try {
    renderer = new THREE.WebGLRenderer({
      canvas: canvas, antialias: false, alpha: true, powerPreference: 'high-performance',
    });
  } catch (e) { return; }
  renderer.setPixelRatio(DPR * RENDER_SCALE);

  var scene = new THREE.Scene();
  var cam = new THREE.OrthographicCamera(-1, 1, 1, -1, 0, 1);

  var uniforms = {
    uTime:    { value: 0 },
    uRes:     { value: new THREE.Vector2(1, 1) },
    uMouse:   { value: new THREE.Vector2(0, 0) },
    uScroll:  { value: 0 },
    uFade:    { value: 0 },
    uColInner:{ value: hex(CFG.colInner) },
    uColMid:  { value: hex(CFG.colMid) },
    uColOuter:{ value: hex(CFG.colOuter) },
    uRing:    { value: hex(CFG.ring) },
    uNebula:  { value: hex(CFG.nebula) },
    uDiskA:   { value: CFG.diskA },
    uBeam:    { value: CFG.beam },
    uCamDist: { value: CFG.camDist },
    uTilt:    { value: CFG.tilt },
    uFov:     { value: CFG.fov },
    uSpin:    { value: CFG.spin },
  };

  var frag = [
    'precision highp float;',
    'varying vec2 vUv;',
    'uniform float uTime, uScroll, uFade, uDiskA, uBeam, uCamDist, uTilt, uFov, uSpin;',
    'uniform vec2 uRes, uMouse;',
    'uniform vec3 uColInner, uColMid, uColOuter, uRing, uNebula;',
    '',
    '#define STEPS ' + STEPS,
    'const float Rs = 1.0;',          // радиус горизонта (масштаб сцены)
    'const float DISK_IN = 2.3;',     // внутр. радиус диска
    'const float DISK_OUT = 8.2;',    // внешн. радиус диска
    'const float ESCAPE = 26.0;',     // дальше — луч ушёл в фон',
    '',
    // --- hash / noise ---
    'float hash21(vec2 p){p=fract(p*vec2(123.34,345.45));p+=dot(p,p+34.345);return fract(p.x*p.y);}',
    'float hash31(vec3 p){p=fract(p*0.3183099+0.1);p*=17.0;return fract(p.x*p.y*p.z*(p.x+p.y+p.z));}',
    'float vnoise(vec3 p){',
    '  vec3 i=floor(p),f=fract(p);f=f*f*(3.0-2.0*f);',
    '  float n=mix(mix(mix(hash31(i+vec3(0,0,0)),hash31(i+vec3(1,0,0)),f.x),',
    '                  mix(hash31(i+vec3(0,1,0)),hash31(i+vec3(1,1,0)),f.x),f.y),',
    '              mix(mix(hash31(i+vec3(0,0,1)),hash31(i+vec3(1,0,1)),f.x),',
    '                  mix(hash31(i+vec3(0,1,1)),hash31(i+vec3(1,1,1)),f.x),f.y),f.z);',
    '  return n;',
    '}',
    'float fbm(vec3 p){float a=0.5,s=0.0;for(int i=0;i<4;i++){s+=a*vnoise(p);p*=2.03;a*=0.5;}return s;}',
    '',
    // --- звёздное небо в направлении луча ---
    'vec3 starField(vec3 dir){',
    '  vec3 col=vec3(0.0);',
    '  for(int L=0;L<3;L++){',
    '    float sc=24.0+float(L)*42.0;',
    '    vec3 g=dir*sc; vec3 id=floor(g); vec2 uv=vec2(hash31(id),hash31(id+7.3));',
    '    float d=length(fract(g)-0.5);',
    '    float star=smoothstep(0.16,0.0,d)*step(0.972+float(L)*0.006,uv.x);',
    '    float tw=0.7+0.3*sin(uTime*1.5+uv.y*40.0);',
    '    vec3 tint=mix(vec3(0.8,0.86,1.0),vec3(0.95,0.85,1.0),uv.y);',
    '    col+=star*tw*tint*(1.0-float(L)*0.22);',
    '  }',
    '  return col;',
    '}',
    '',
    // --- цвет аккреционного диска в точке пересечения ---
    'vec3 diskColor(vec3 hit, vec3 camPos){',
    '  float rd=length(hit.xz);',
    '  float t=clamp((rd-DISK_IN)/(DISK_OUT-DISK_IN),0.0,1.0);',
    '  vec3 grad = t<0.5 ? mix(uColInner,uColMid,t*2.0) : mix(uColMid,uColOuter,(t-0.5)*2.0);',
    '  float ang=atan(hit.z,hit.x);',
    // турбулентность: спиральные полосы, вращаются во времени
    '  float spin=uTime*uSpin*(1.6/ (0.5+rd*0.18));',     // внутри быстрее (кеплеровски)
    '  float bands=fbm(vec3(cos(ang+spin)*rd*0.55, sin(ang+spin)*rd*0.55, rd*0.4 - spin*0.4));',
    '  float turb=0.55+0.85*bands;',
    // доплер: сторона, идущая на нас, ярче
    '  vec3 tang=normalize(vec3(-hit.z,0.0,hit.x));',
    '  vec3 toCam=normalize(camPos-hit);',
    '  float dopp=1.0+uBeam*dot(tang,toCam);',
    '  dopp=pow(max(dopp,0.0),2.2);',
    // профиль яркости: пик у внутренней кромки, спад наружу
    '  float inten=pow(1.0-t,1.7)*1.6 + smoothstep(0.12,0.0,t)*1.4;',
    '  float edgefade=smoothstep(0.0,0.06,t)*smoothstep(1.0,0.86,t);', // мягкие края
    '  return grad*inten*turb*dopp*edgefade*uDiskA;',
    '}',
    '',
    'vec3 aces(vec3 x){return clamp((x*(2.51*x+0.03))/(x*(2.43*x+0.59)+0.14),0.0,1.0);}',
    '',
    'void main(){',
    '  vec2 uv=(gl_FragCoord.xy-0.5*uRes)/uRes.y;',
    // камера орбитой вокруг дыры; мышь/скролл слегка двигают обзор
    '  float yaw=uMouse.x*0.5;',
    '  float pitch=uTilt + uMouse.y*0.28 + uScroll*0.22;',
    '  float D=uCamDist - uScroll*2.6;',          // на скролле приближаемся
    '  vec3 camPos=vec3(sin(yaw)*cos(pitch), sin(pitch), cos(yaw)*cos(pitch))*D;',
    '  vec3 fwd=normalize(-camPos);',
    '  vec3 right=normalize(cross(vec3(0.0,1.0,0.0),fwd));',
    '  vec3 up=cross(fwd,right);',
    '  vec3 dir=normalize(fwd + (uv.x*right + uv.y*up)*uFov);',
    '',
    '  vec3 pos=camPos;',
    '  vec3 col=vec3(0.0);',
    '  float transm=1.0;',
    '  float minR=1e9;',
    '  bool captured=false;',
    '  for(int i=0;i<STEPS;i++){',
    '    float r2=dot(pos,pos); float r=sqrt(r2);',
    '    minR=min(minR,r);',
    '    vec3 h=cross(pos,dir);',
    '    vec3 acc=-1.5*dot(h,h)*pos/pow(r2,2.5);',  // искривление света
    '    float dt=clamp(r*0.14,0.015,0.42);',       // адаптивный шаг
    '    vec3 prev=pos;',
    '    dir+=acc*dt; pos+=dir*dt;',
    '    if(dot(pos,pos)<Rs*Rs){captured=true;break;}',
    // пересечение плоскости диска (y=0) между prev и pos
    '    if(prev.y*pos.y<0.0){',
    '      float k=prev.y/(prev.y-pos.y);',
    '      vec3 hit=mix(prev,pos,k);',
    '      float rd=length(hit.xz);',
    '      if(rd>DISK_IN && rd<DISK_OUT){',
    '        vec3 dc=diskColor(hit,camPos);',
    '        col+=dc*transm;',
    '        transm*=0.62;',          // диск частично непрозрачен (второй виток слабее)
    '      }',
    '    }',
    '    if(dot(pos,pos)>ESCAPE*ESCAPE)break;',
    '  }',
    '',
    // фон (звёзды + туманность) — только если луч ушёл
    '  if(!captured){',
    '    vec3 bg=vec3(0.012,0.0,0.078);',         // void canvas #030014 — бесшовно с фоном страницы
    '    bg+=starField(normalize(dir));',
    '    float neb=fbm(normalize(dir)*2.2+vec3(0.0,0.0,uTime*0.01));',
    '    bg+=uNebula*pow(neb,2.5)*0.55;',
    '    col+=bg*transm;',
    '  }',
    // горизонт событий: captured-луч без диска даёт col≈0 → чёрная дыра темнее фона
    // фотонное кольцо: чем ближе луч подошёл к фотонной сфере (~1.5Rs), тем ярче
    '  float ringG=smoothstep(1.62,1.5,minR)*smoothstep(1.2,1.49,minR);',
    '  col+=uRing*ringG*2.0;',
    '',
    '  col=aces(col*1.12);',                    // tone-map (диск ярче, но без неона)
    '  col=pow(col,vec3(0.4545));',             // gamma
    '  gl_FragColor=vec4(col*uFade, 1.0);',     // непрозрачный канвас (void+дыра+диск)
    '}',
  ].join('\n');

  var vert = [
    'varying vec2 vUv;',
    'void main(){ vUv=uv; gl_Position=vec4(position.xy,0.0,1.0); }',
  ].join('\n');

  var quad = new THREE.Mesh(
    new THREE.PlaneGeometry(2, 2),
    new THREE.ShaderMaterial({ uniforms: uniforms, vertexShader: vert, fragmentShader: frag, transparent: true })
  );
  quad.frustumCulled = false; // вершинный шейдер игнорирует камеру → иначе меш отсекается
  scene.add(quad);

  // --- размеры -------------------------------------------------------------
  // DPR/render-scale пересчитываются на ресайз (поворот экрана, переход через
  // breakpoint). STEPS «запечён» в шейдер через #define и остаётся load-time.
  function size() {
    small = window.innerWidth < 760;
    DPR = Math.min(window.devicePixelRatio || 1, small ? 1.25 : 1.6);
    RENDER_SCALE = small ? 0.85 : 1.0;
    renderer.setPixelRatio(DPR * RENDER_SCALE);
    var w = canvas.clientWidth || canvas.offsetWidth || window.innerWidth;
    var h = canvas.clientHeight || canvas.offsetHeight || window.innerHeight;
    renderer.setSize(w, h, false);
    uniforms.uRes.value.set(w * DPR * RENDER_SCALE, h * DPR * RENDER_SCALE);
  }
  window.addEventListener('resize', size, { passive: true });

  // --- деградация: потеря WebGL-контекста → прячем канвас, виден CSS-постер ---
  var dead = false;
  canvas.addEventListener('webglcontextlost', function (e) {
    e.preventDefault();           // обязательно, иначе восстановление невозможно
    dead = true; cancelAnimationFrame(raf); raf = 0;
    canvas.style.display = 'none'; // .bh-fallback просвечивает
  }, false);
  canvas.addEventListener('webglcontextrestored', function () {
    dead = false; canvas.style.display = ''; uniforms.uFade.value = 0;
    size(); if (!reduce) raf = requestAnimationFrame(loop);
  }, false);

  // --- ввод (мышь / скролл) ------------------------------------------------
  var mx = 0, my = 0, cmx = 0, cmy = 0, scrollT = 0, scrollC = 0;
  if (!reduce) {
    window.addEventListener('pointermove', function (e) {
      mx = (e.clientX / window.innerWidth - 0.5);
      my = (e.clientY / window.innerHeight - 0.5);
    }, { passive: true });
  }
  function onScroll() {
    // прогресс в пределах hero-секции (канвас живёт в hero)
    var rect = canvas.getBoundingClientRect();
    var vh = window.innerHeight;
    var p = 1.0 - (rect.bottom) / (rect.height + vh);
    scrollT = Math.min(Math.max(p, 0), 1);
  }
  window.addEventListener('scroll', onScroll, { passive: true });

  // --- пауза, когда канвас вне экрана --------------------------------------
  var onScreen = true;
  if ('IntersectionObserver' in window) {
    new IntersectionObserver(function (es) { onScreen = es[0].isIntersecting; }, { threshold: 0.01 })
      .observe(canvas);
  }

  // --- цикл ----------------------------------------------------------------
  function frame(t) {
    var time = t * 0.001;
    cmx += (mx - cmx) * 0.045; cmy += (my - cmy) * 0.045;
    scrollC += (scrollT - scrollC) * 0.06;
    uniforms.uTime.value = time;
    uniforms.uMouse.value.set(cmx, cmy);
    uniforms.uScroll.value = scrollC;
    if (uniforms.uFade.value < 1) uniforms.uFade.value = Math.min(1, uniforms.uFade.value + 0.02);
    // если шейдер/драйвер падает в render() — мягко выходим на CSS-постер
    try { renderer.render(scene, cam); }
    catch (err) { dead = true; cancelAnimationFrame(raf); raf = 0; canvas.style.display = 'none'; }
  }

  var raf = 0;
  function loop(t) { if (dead) return; raf = requestAnimationFrame(loop); if (document.hidden || !onScreen) return; frame(t); }

  function start() {
    size(); onScroll();
    if (reduce) { uniforms.uFade.value = 1; frame(1200); }
    else { raf = requestAnimationFrame(loop); }
  }
  // ждём, пока канвас получит размеры из CSS
  if (document.readyState === 'complete') start();
  else window.addEventListener('load', start);
  // первый размер сразу (на случай, если CSS уже применён)
  size();
})();
