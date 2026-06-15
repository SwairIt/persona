/* ============================================================
   Persona · Landing v2 — СКВОЗНОЙ КОСМОС + 3D ЧЁРНАЯ ДЫРА
   ------------------------------------------------------------
   Один фиксированный полноэкранный WebGL-canvas за ВСЕМ сайтом:
   единая сцена-галактика, через которую «летит» камера по мере
   скролла, поэтому все блоки — одно целое.
     • детальная туманность (domain-warped fbm, фирменные цвета),
       пыль-полосы, яркое ядро-вихрь;
     • многослойное звёздное небо с параллаксом и мерцанием;
     • чёрная дыра: гравитационное линзирование фона + аккреционный
       диск; в hero — крупная по центру, при скролле отъезжает/уменьшается
       и снова расцветает к финальному CTA.
   Перформанс: DPR/шаги по устройству, пауза на скрытой вкладке,
   обработка потери контекста, reduced-motion → 1 кадр + CSS-постер.
   Параметры — в window.PERSONA_BH (см. defaults).
   ============================================================ */
(function () {
  'use strict';

  var canvas = document.getElementById('blackhole');
  if (!canvas || typeof window.THREE === 'undefined') return;
  var THREE = window.THREE;

  function v3(c) { var x = new THREE.Color(c); return new THREE.Vector3(x.r, x.g, x.b); }
  var defaults = {
    colInner: '#e9d6ff', // внутренняя кромка диска
    colMid:   '#a07cf2', // тело диска
    colOuter: '#5f78dd', // внешний край
    ring:     '#cdb9ff', // фотонное кольцо
    neb1:     '#0a0626', // тёмная база/пыль
    neb2:     '#4a2a8f', // фиолетовые облака
    neb3:     '#9a4ad0', // розово-пурпурные подсветки
    neb4:     '#3257b5', // синие зоны
    core:     '#dcc4ff', // яркие вихри ядра
    diskA: 0.9,          // яркость диска
    beam: 0.5,           // доплер
    nebA: 0.5,           // яркость туманности (с запасом на читабельность текста)
    camDist: 12.0,       // базовая дистанция камеры
    tilt: 0.30,          // базовый наклон
    fov: 0.95,
    spin: 0.16,
  };
  var CFG = Object.assign({}, defaults, window.PERSONA_BH || {});

  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var small = window.innerWidth < 760;
  var DPR = Math.min(window.devicePixelRatio || 1, small ? 1.2 : 1.5);
  var RENDER_SCALE = small ? 0.8 : 1.0;
  var STEPS = small ? 110 : 190;           // шаги интегрирования геодезики

  var renderer;
  try {
    renderer = new THREE.WebGLRenderer({
      canvas: canvas, antialias: false, alpha: true, powerPreference: 'high-performance',
    });
  } catch (e) { return; }

  var scene = new THREE.Scene();
  var cam = new THREE.OrthographicCamera(-1, 1, 1, -1, 0, 1);

  var uniforms = {
    uTime: { value: 0 }, uRes: { value: new THREE.Vector2(1, 1) },
    uMouse: { value: new THREE.Vector2(0, 0) }, uScroll: { value: 0 }, uFade: { value: 0 },
    uColInner: { value: v3(CFG.colInner) }, uColMid: { value: v3(CFG.colMid) }, uColOuter: { value: v3(CFG.colOuter) },
    uRing: { value: v3(CFG.ring) },
    uNeb1: { value: v3(CFG.neb1) }, uNeb2: { value: v3(CFG.neb2) }, uNeb3: { value: v3(CFG.neb3) },
    uNeb4: { value: v3(CFG.neb4) }, uCore: { value: v3(CFG.core) },
    uDiskA: { value: CFG.diskA }, uBeam: { value: CFG.beam }, uNebA: { value: CFG.nebA },
    uCamDist: { value: CFG.camDist }, uTilt: { value: CFG.tilt }, uFov: { value: CFG.fov }, uSpin: { value: CFG.spin },
  };

  var frag = [
    'precision highp float;',
    'varying vec2 vUv;',
    'uniform float uTime,uScroll,uFade,uDiskA,uBeam,uNebA,uCamDist,uTilt,uFov,uSpin;',
    'uniform vec2 uRes,uMouse;',
    'uniform vec3 uColInner,uColMid,uColOuter,uRing,uNeb1,uNeb2,uNeb3,uNeb4,uCore;',
    '',
    '#define STEPS ' + STEPS,
    'const float Rs=1.0;',
    'const float DISK_IN=2.4;',
    'const float DISK_OUT=11.0;',     // диск шире → край уходит за кадр
    'const float ESCAPE=30.0;',
    '',
    'float hash31(vec3 p){p=fract(p*0.3183099+0.1);p*=17.0;return fract(p.x*p.y*p.z*(p.x+p.y+p.z));}',
    'float vnoise(vec3 p){vec3 i=floor(p),f=fract(p);f=f*f*(3.0-2.0*f);',
    ' float n=mix(mix(mix(hash31(i+vec3(0,0,0)),hash31(i+vec3(1,0,0)),f.x),',
    '                 mix(hash31(i+vec3(0,1,0)),hash31(i+vec3(1,1,0)),f.x),f.y),',
    '             mix(mix(hash31(i+vec3(0,0,1)),hash31(i+vec3(1,0,1)),f.x),',
    '                 mix(hash31(i+vec3(0,1,1)),hash31(i+vec3(1,1,1)),f.x),f.y),f.z);return n;}',
    'float fbm(vec3 p){float a=0.5,s=0.0;for(int i=0;i<5;i++){s+=a*vnoise(p);p=p*2.02+vec3(11.3,7.1,5.7);a*=0.5;}return s;}',
    'float fbm3(vec3 p){float a=0.5,s=0.0;for(int i=0;i<3;i++){s+=a*vnoise(p);p=p*2.05+vec3(3.0,7.0,1.0);a*=0.5;}return s;}',
    '',
    // --- многослойное звёздное небо в направлении луча ---
    'vec3 starField(vec3 dir){',
    '  vec3 col=vec3(0.0);',
    '  for(int L=0;L<4;L++){',
    '    float sc=20.0+float(L)*40.0;',
    '    vec3 g=dir*sc; vec3 id=floor(g); vec2 r=vec2(hash31(id),hash31(id+9.13));',
    '    float d=length(fract(g)-0.5);',
    '    float thr=0.978-float(L)*0.004;',
    '    float star=smoothstep(0.18,0.0,d)*step(thr,r.x);',
    '    float tw=0.65+0.35*sin(uTime*1.4+r.y*42.0);',
    '    vec3 tint=mix(vec3(0.85,0.9,1.0),mix(vec3(1.0,0.85,0.95),vec3(0.8,0.86,1.0),r.x),r.y);',
    '    col+=star*tw*tint*(1.0-float(L)*0.16);',
    '  }',
    '  return col;',
    '}',
    '',
    // --- детальная туманность (domain-warp + пыль + ядро) ---
    'vec3 nebula(vec3 dir){',
    '  vec3 p=dir*1.7 + vec3(3.0, -1.0, uScroll*2.6 + uTime*0.012);', // дрейф со скроллом/временем
    '  float w=fbm3(p*1.2 + vec3(uTime*0.01,0.0,0.0));',
    '  p+=vec3(w,w*0.7,w*0.4)*0.9;',                    // domain warp → завихрения
    '  float base=fbm(p);',
    '  float det=vnoise(p*2.6+5.0);',          // дешёвая высокочастотная деталь
    '  float dust=fbm3(p*0.6-2.0);',
    '  float dens=clamp(base*0.85+det*0.3-0.32,0.0,1.0);',
    '  dens*=smoothstep(0.18,0.62,dust);',              // вырезаем тёмные пылевые полосы
    '  dens=pow(dens,1.4);',
    '  vec3 c=mix(uNeb1,uNeb2,smoothstep(0.0,0.55,base));',
    '  c=mix(c,uNeb3,smoothstep(0.45,0.95,det)*0.8);',
    '  c=mix(c,uNeb4,smoothstep(0.3,0.8,dust)*0.4);',
    '  c+=uCore*pow(clamp(base*det,0.0,1.0),3.5)*1.3;', // яркие вихри
    '  return c*dens*uNebA;',
    '}',
    '',
    'vec3 diskColor(vec3 hit, vec3 camPos){',
    '  float rd=length(hit.xz);',
    '  float t=clamp((rd-DISK_IN)/(DISK_OUT-DISK_IN),0.0,1.0);',
    '  vec3 grad = t<0.5 ? mix(uColInner,uColMid,t*2.0) : mix(uColMid,uColOuter,(t-0.5)*2.0);',
    '  float ang=atan(hit.z,hit.x);',
    '  float spin=uTime*uSpin*(1.6/(0.5+rd*0.18));',
    '  float bands=fbm(vec3(cos(ang+spin)*rd*0.5, sin(ang+spin)*rd*0.5, rd*0.35-spin*0.4));',
    '  float turb=0.5+0.9*bands;',
    '  vec3 tang=normalize(vec3(-hit.z,0.0,hit.x));',
    '  vec3 toCam=normalize(camPos-hit);',
    '  float dopp=pow(max(1.0+uBeam*dot(tang,toCam),0.0),2.2);',
    '  float inten=pow(1.0-t,1.7)*1.05 + smoothstep(0.10,0.0,t)*0.6;',
    // очень мягкие края: и внутренний, и внешний растворяются (нет «конца колец»)
    '  float edgefade=smoothstep(0.0,0.07,t)*smoothstep(1.0,0.62,t);',
    // диск ярок в hero, приглушается при скролле → не засвечивает текст ниже
    '  float dim=mix(1.0,0.4,smoothstep(0.05,0.34,uScroll));',
    '  return grad*inten*turb*dopp*edgefade*uDiskA*dim;',
    '}',
    '',
    'vec3 aces(vec3 x){return clamp((x*(2.51*x+0.03))/(x*(2.43*x+0.59)+0.14),0.0,1.0);}',
    '',
    'void main(){',
    '  vec2 uv=(gl_FragCoord.xy-0.5*uRes)/uRes.y;',
    '  float sp=clamp(uScroll,0.0,1.0);',
    // дыра всегда РЕБРОМ к нам (драматичный силуэт Гаргантюа), не вид сверху;
    // дистанция постоянна, чтобы она не «схлопывалась» в плоский круг.
    '  float dist=uCamDist;',
    '  float yaw=uMouse.x*0.3 + sin(uTime*0.025)*0.06;',
    '  float pitch=uTilt + uMouse.y*0.10 + sin(uTime*0.018)*0.03;',
    '  vec3 camPos=vec3(sin(yaw)*cos(pitch), sin(pitch), cos(yaw)*cos(pitch))*dist;',
    '  vec3 fwd=normalize(-camPos);',
    '  vec3 right=normalize(cross(vec3(0.0,1.0,0.0),fwd));',
    '  vec3 up=cross(fwd,right);',
    // по скроллу дыра всплывает вверх по экрану (panning) — «в разных местах», но всегда ребром
    '  float pan=mix(-0.05, 1.2, sp);',
    '  vec2 luv=vec2(uv.x, uv.y - pan);',
    '  vec3 dir=normalize(fwd + (luv.x*right + luv.y*up)*uFov);',
    '',
    '  vec3 pos=camPos; vec3 col=vec3(0.0); float transm=1.0; float minR=1e9; bool captured=false;',
    '  for(int i=0;i<STEPS;i++){',
    '    float r2=dot(pos,pos); float r=sqrt(r2); minR=min(minR,r);',
    '    vec3 h=cross(pos,dir);',
    '    vec3 acc=-1.5*dot(h,h)*pos/pow(r2,2.5);',
    '    float dt=clamp(r*0.14,0.02,0.5);',
    '    vec3 prev=pos; dir+=acc*dt; pos+=dir*dt;',
    '    if(dot(pos,pos)<Rs*Rs){captured=true;break;}',
    '    if(prev.y*pos.y<0.0){',
    '      float k=prev.y/(prev.y-pos.y); vec3 hit=mix(prev,pos,k); float rd=length(hit.xz);',
    '      if(rd>DISK_IN && rd<DISK_OUT){ col+=diskColor(hit,camPos)*transm; transm*=0.6; }',
    '    }',
    '    if(dot(pos,pos)>ESCAPE*ESCAPE)break;',
    '  }',
    '',
    '  if(!captured){',
    '    vec3 nd=normalize(dir);',
    '    vec3 bg=vec3(0.011,0.004,0.05);',     // глубокий void
    '    bg+=nebula(nd);',
    '    bg+=starField(nd);',
    '    col+=bg*transm;',
    '  }',
    '  float ringG=smoothstep(1.60,1.5,minR)*smoothstep(1.30,1.49,minR);',
    '  col+=uRing*ringG*mix(1.0,0.5,smoothstep(0.05,0.34,sp));',
    // мягкая виньетка по краям кадра — фокус к центру + читабельность
    '  float vig=1.0-0.32*dot(uv,uv);',
    '  col*=vig;',
    '  col=aces(col*0.9);',
    '  col=pow(col,vec3(0.4545));',
    '  gl_FragColor=vec4(col*uFade, 1.0);',
    '}',
  ].join('\n');

  var vert = ['varying vec2 vUv;', 'void main(){vUv=uv; gl_Position=vec4(position.xy,0.0,1.0);}'].join('\n');

  var quad = new THREE.Mesh(
    new THREE.PlaneGeometry(2, 2),
    new THREE.ShaderMaterial({ uniforms: uniforms, vertexShader: vert, fragmentShader: frag })
  );
  quad.frustumCulled = false;
  scene.add(quad);

  // --- размеры (DPR/scale пересчёт на ресайз) ------------------------------
  function size() {
    small = window.innerWidth < 760;
    DPR = Math.min(window.devicePixelRatio || 1, small ? 1.2 : 1.5);
    RENDER_SCALE = small ? 0.8 : 1.0;
    renderer.setPixelRatio(DPR * RENDER_SCALE);
    var w = window.innerWidth, h = window.innerHeight;
    renderer.setSize(w, h, false);
    uniforms.uRes.value.set(w * DPR * RENDER_SCALE, h * DPR * RENDER_SCALE);
  }
  window.addEventListener('resize', size, { passive: true });

  // --- деградация: потеря WebGL-контекста → CSS-постер ---------------------
  var dead = false;
  canvas.addEventListener('webglcontextlost', function (e) {
    e.preventDefault(); dead = true; cancelAnimationFrame(raf); raf = 0; canvas.style.display = 'none';
  }, false);
  canvas.addEventListener('webglcontextrestored', function () {
    dead = false; canvas.style.display = ''; uniforms.uFade.value = 0; size(); if (!reduce) raf = requestAnimationFrame(loop);
  }, false);

  // --- ввод: мышь + скролл всей страницы -----------------------------------
  var mx = 0, my = 0, cmx = 0, cmy = 0, scrollT = 0, scrollC = 0;
  if (!reduce) {
    window.addEventListener('pointermove', function (e) {
      mx = e.clientX / window.innerWidth - 0.5;
      my = e.clientY / window.innerHeight - 0.5;
    }, { passive: true });
  }
  function onScroll() {
    var max = document.documentElement.scrollHeight - window.innerHeight;
    scrollT = max > 0 ? Math.min(Math.max(window.scrollY / max, 0), 1) : 0;
  }
  window.addEventListener('scroll', onScroll, { passive: true });

  // --- цикл ----------------------------------------------------------------
  function frame(t) {
    var time = t * 0.001;
    cmx += (mx - cmx) * 0.04; cmy += (my - cmy) * 0.04;
    scrollC += (scrollT - scrollC) * 0.07;
    uniforms.uTime.value = time;
    uniforms.uMouse.value.set(cmx, cmy);
    uniforms.uScroll.value = scrollC;
    if (uniforms.uFade.value < 1) uniforms.uFade.value = Math.min(1, uniforms.uFade.value + 0.02);
    try { renderer.render(scene, cam); }
    catch (err) { dead = true; cancelAnimationFrame(raf); raf = 0; canvas.style.display = 'none'; }
  }

  var raf = 0;
  function loop(t) { if (dead) return; raf = requestAnimationFrame(loop); if (document.hidden) return; frame(t); }

  function start() {
    size(); onScroll();
    if (reduce) { uniforms.uFade.value = 1; frame(1200); }
    else { raf = requestAnimationFrame(loop); }
  }
  if (document.readyState === 'complete') start();
  else window.addEventListener('load', start);
  size();
})();
