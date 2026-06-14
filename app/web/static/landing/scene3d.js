/* ============================================================
   Persona — сквозная 3D-сцена на весь сайт, управляемая скроллом.
   Один фиксированный WebGL-canvas за всем контентом:
     • морфящийся иридесцентный объект (GLSL: шум-дисплейсмент + френель-
       переливы), расширяется / вращается / перетекает по экрану и меняет
       палитру по мере скролла;
     • поле частиц с параллаксом.
   Перформанс: DPR≤1.5, пауза на скрытой вкладке, прогресс скролла сглажен.
   reduced-motion → статичный кадр без rAF. Нет THREE → тихо выходим.
   ============================================================ */
(function () {
  'use strict';
  var canvas = document.getElementById('scene3d');
  if (!canvas || typeof window.THREE === 'undefined') return;
  var THREE = window.THREE;
  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var small = window.innerWidth < 760;
  var DPR = Math.min(window.devicePixelRatio || 1, small ? 1.25 : 1.5);

  var renderer;
  try {
    renderer = new THREE.WebGLRenderer({ canvas: canvas, antialias: !small, alpha: true, powerPreference: 'high-performance' });
  } catch (e) { return; }
  renderer.setPixelRatio(DPR);
  renderer.setSize(window.innerWidth, window.innerHeight, false);

  var scene = new THREE.Scene();
  var camera = new THREE.PerspectiveCamera(48, window.innerWidth / window.innerHeight, 0.1, 100);
  camera.position.set(0, 0, 5.2);

  // мягкий круглый спрайт для точек (иначе PointsMaterial рисует квадраты)
  function discTex() {
    var c = document.createElement('canvas'); c.width = c.height = 64;
    var x = c.getContext('2d');
    var g = x.createRadialGradient(32, 32, 0, 32, 32, 32);
    g.addColorStop(0, 'rgba(255,255,255,1)');
    g.addColorStop(0.45, 'rgba(255,255,255,.55)');
    g.addColorStop(1, 'rgba(255,255,255,0)');
    x.fillStyle = g; x.beginPath(); x.arc(32, 32, 32, 0, 7); x.fill();
    return new THREE.CanvasTexture(c);
  }
  var DISC = discTex();

  // ---------- иридесцентный морф-объект ----------
  var NOISE = [
    'vec3 mod289(vec3 x){return x-floor(x*(1.0/289.0))*289.0;}',
    'vec4 mod289(vec4 x){return x-floor(x*(1.0/289.0))*289.0;}',
    'vec4 permute(vec4 x){return mod289(((x*34.0)+1.0)*x);}',
    'vec4 taylorInvSqrt(vec4 r){return 1.79284291400159-0.85373472095314*r;}',
    'float snoise(vec3 v){',
    ' const vec2 C=vec2(1.0/6.0,1.0/3.0);const vec4 D=vec4(0.0,0.5,1.0,2.0);',
    ' vec3 i=floor(v+dot(v,C.yyy));vec3 x0=v-i+dot(i,C.xxx);',
    ' vec3 g=step(x0.yzx,x0.xyz);vec3 l=1.0-g;vec3 i1=min(g.xyz,l.zxy);vec3 i2=max(g.xyz,l.zxy);',
    ' vec3 x1=x0-i1+C.xxx;vec3 x2=x0-i2+C.yyy;vec3 x3=x0-D.yyy;',
    ' i=mod289(i);',
    ' vec4 p=permute(permute(permute(i.z+vec4(0.0,i1.z,i2.z,1.0))+i.y+vec4(0.0,i1.y,i2.y,1.0))+i.x+vec4(0.0,i1.x,i2.x,1.0));',
    ' float n_=0.142857142857;vec3 ns=n_*D.wyz-D.xzx;',
    ' vec4 j=p-49.0*floor(p*ns.z*ns.z);',
    ' vec4 x_=floor(j*ns.z);vec4 y_=floor(j-7.0*x_);',
    ' vec4 x=x_*ns.x+ns.yyyy;vec4 y=y_*ns.x+ns.yyyy;vec4 h=1.0-abs(x)-abs(y);',
    ' vec4 b0=vec4(x.xy,y.xy);vec4 b1=vec4(x.zw,y.zw);',
    ' vec4 s0=floor(b0)*2.0+1.0;vec4 s1=floor(b1)*2.0+1.0;vec4 sh=-step(h,vec4(0.0));',
    ' vec4 a0=b0.xzyw+s0.xzyw*sh.xxyy;vec4 a1=b1.xzyw+s1.xzyw*sh.zzww;',
    ' vec3 p0=vec3(a0.xy,h.x);vec3 p1=vec3(a0.zw,h.y);vec3 p2=vec3(a1.xy,h.z);vec3 p3=vec3(a1.zw,h.w);',
    ' vec4 norm=taylorInvSqrt(vec4(dot(p0,p0),dot(p1,p1),dot(p2,p2),dot(p3,p3)));',
    ' p0*=norm.x;p1*=norm.y;p2*=norm.z;p3*=norm.w;',
    ' vec4 m=max(0.6-vec4(dot(x0,x0),dot(x1,x1),dot(x2,x2),dot(x3,x3)),0.0);m=m*m;',
    ' return 42.0*dot(m*m,vec4(dot(p0,x0),dot(p1,x1),dot(p2,x2),dot(p3,x3)));}',
  ].join('\n');

  var vert = [
    'uniform float uTime;uniform float uProgress;uniform float uAmp;',
    'varying vec3 vNormal;varying vec3 vView;varying float vDisp;',
    NOISE,
    'void main(){',
    ' float t=uTime*0.28;',
    ' float freq=1.15+uProgress*1.6;',
    ' float n=snoise(normal*freq+vec3(0.0,0.0,t));',
    ' float n2=snoise(normal*freq*2.1+vec3(t*0.6,0.0,0.0));',
    ' float disp=(n*0.62+n2*0.38)*uAmp;',
    ' vec3 pos=position+normal*disp;vDisp=disp;',
    ' vec4 mv=modelViewMatrix*vec4(pos,1.0);',
    ' vNormal=normalize(normalMatrix*normal);',
    ' vView=normalize(-mv.xyz);',
    ' gl_Position=projectionMatrix*mv;}',
  ].join('\n');

  var frag = [
    'precision highp float;',
    'uniform float uTime;uniform float uProgress;',
    'varying vec3 vNormal;varying vec3 vView;varying float vDisp;',
    'vec3 hsv2rgb(vec3 c){vec4 K=vec4(1.0,2.0/3.0,1.0/3.0,3.0);vec3 p=abs(fract(c.xxx+K.xyz)*6.0-K.www);return c.z*mix(K.xxx,clamp(p-K.xxx,0.0,1.0),c.y);}',
    'void main(){',
    ' float fres=pow(1.0-max(dot(vView,vNormal),0.0),2.4);',
    ' float hue=fract(0.70+uProgress*0.55+fres*0.33+vDisp*0.45+uTime*0.015);',
    ' vec3 glow=hsv2rgb(vec3(hue,0.70,1.0));',
    ' vec3 base=hsv2rgb(vec3(fract(hue+0.5),0.55,0.22));',
    ' vec3 col=mix(base,glow,clamp(fres*1.25,0.0,1.0))+glow*fres*0.65;',
    ' gl_FragColor=vec4(col,0.9);}',
  ].join('\n');

  var uniforms = {
    uTime: { value: 0 }, uProgress: { value: 0 }, uAmp: { value: 0.2 },
  };
  var seg = small ? 64 : 110;
  var blob = new THREE.Mesh(
    new THREE.SphereGeometry(1.5, seg, seg),
    new THREE.ShaderMaterial({ uniforms: uniforms, vertexShader: vert, fragmentShader: frag, transparent: true })
  );
  scene.add(blob);

  // тонкий каркас-ореол вокруг
  var halo = new THREE.LineSegments(
    new THREE.EdgesGeometry(new THREE.IcosahedronGeometry(2.05, 1)),
    new THREE.LineBasicMaterial({ color: 0x6f7bff, transparent: true, opacity: 0.18 })
  );
  scene.add(halo);

  // ---------- поле частиц ----------
  var COUNT = small ? 360 : 900;
  var pos = new Float32Array(COUNT * 3), col = new Float32Array(COUNT * 3);
  var A = new THREE.Color(0x36e0ff), B = new THREE.Color(0x9b7bff), Cc = new THREE.Color(0xe879f9);
  for (var i = 0; i < COUNT; i++) {
    pos[i * 3] = (Math.random() - 0.5) * 18;
    pos[i * 3 + 1] = (Math.random() - 0.5) * 18;
    pos[i * 3 + 2] = (Math.random() - 0.5) * 12 - 2;
    var c = Math.random() < 0.4 ? A : (Math.random() < 0.6 ? B : Cc);
    col[i * 3] = c.r; col[i * 3 + 1] = c.g; col[i * 3 + 2] = c.b;
  }
  var pg = new THREE.BufferGeometry();
  pg.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  pg.setAttribute('color', new THREE.BufferAttribute(col, 3));
  var particles = new THREE.Points(pg, new THREE.PointsMaterial({
    size: 0.07, map: DISC, vertexColors: true, transparent: true, opacity: 0.85,
    blending: THREE.AdditiveBlending, depthWrite: false,
  }));
  scene.add(particles);

  // ---------- звёздные слои (космос) ----------
  function makeStars(count, spread, size, opacity) {
    var sp = new Float32Array(count * 3), sc = new Float32Array(count * 3);
    var white = new THREE.Color(0xffffff), blue = new THREE.Color(0xa8c4ff), vio = new THREE.Color(0xceb8ff);
    for (var k = 0; k < count; k++) {
      sp[k * 3] = (Math.random() - 0.5) * spread;
      sp[k * 3 + 1] = (Math.random() - 0.5) * spread;
      sp[k * 3 + 2] = (Math.random() - 0.5) * spread * 0.7 - spread * 0.15;
      var r = Math.random();
      var cc = r < 0.7 ? white : (r < 0.86 ? blue : vio);
      var b = 0.6 + Math.random() * 0.4;
      sc[k * 3] = cc.r * b; sc[k * 3 + 1] = cc.g * b; sc[k * 3 + 2] = cc.b * b;
    }
    var g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(sp, 3));
    g.setAttribute('color', new THREE.BufferAttribute(sc, 3));
    return new THREE.Points(g, new THREE.PointsMaterial({
      size: size, map: DISC, vertexColors: true, transparent: true, opacity: opacity,
      blending: THREE.AdditiveBlending, depthWrite: false, sizeAttenuation: true,
    }));
  }
  var starsFar = makeStars(small ? 700 : 1500, 44, 0.07, 0.9);
  var starsNear = makeStars(small ? 220 : 520, 26, 0.12, 0.95);
  scene.add(starsFar); scene.add(starsNear);

  // ---------- кольцо-орбита вокруг ядра (как у планеты) ----------
  var rings = new THREE.Group();
  var ringMat1 = new THREE.MeshBasicMaterial({ color: 0x8ea2ff, transparent: true, opacity: 0.55, blending: THREE.AdditiveBlending });
  var ringMat2 = new THREE.MeshBasicMaterial({ color: 0x46e6ff, transparent: true, opacity: 0.35, blending: THREE.AdditiveBlending });
  var ring1 = new THREE.Mesh(new THREE.TorusGeometry(2.55, 0.012, 8, 180), ringMat1);
  var ring2 = new THREE.Mesh(new THREE.TorusGeometry(2.95, 0.008, 8, 180), ringMat2);
  rings.add(ring1); rings.add(ring2);
  rings.rotation.x = Math.PI * 0.46;
  rings.rotation.y = Math.PI * 0.08;
  scene.add(rings);

  // ---------- скролл + курсор ----------
  var prog = 0, progTarget = 0, mx = 0, my = 0, cmx = 0, cmy = 0;
  function onScroll() {
    var max = document.documentElement.scrollHeight - window.innerHeight;
    progTarget = max > 0 ? Math.min(Math.max(window.scrollY / max, 0), 1) : 0;
  }
  window.addEventListener('scroll', onScroll, { passive: true });
  onScroll();
  if (!reduce) {
    window.addEventListener('pointermove', function (e) {
      mx = e.clientX / window.innerWidth - 0.5;
      my = e.clientY / window.innerHeight - 0.5;
    }, { passive: true });
  }

  function resize() {
    camera.aspect = window.innerWidth / window.innerHeight;
    camera.updateProjectionMatrix();
    renderer.setSize(window.innerWidth, window.innerHeight, false);
    onScroll();
  }
  window.addEventListener('resize', resize, { passive: true });

  // объект «путешествует» по экрану: waypoints x по прогрессу
  function waypointX(p) {
    // hero справа → центр → слева → центр …
    return Math.sin(p * Math.PI * 3.0) * 2.1;
  }
  function waypointY(p) {
    return Math.cos(p * Math.PI * 2.0) * 0.9;
  }

  function frame(t) {
    var time = t * 0.001;
    prog += (progTarget - prog) * 0.06;
    cmx += (mx - cmx) * 0.05; cmy += (my - cmy) * 0.05;

    uniforms.uTime.value = time;
    uniforms.uProgress.value = prog;
    uniforms.uAmp.value = 0.16 + prog * 0.55 + Math.sin(time * 1.3) * 0.04;

    var sc = 0.85 + prog * 1.15 + Math.sin(time * 0.9) * 0.05;
    blob.scale.setScalar(sc);
    blob.position.set(waypointX(prog) + cmx * 0.8, waypointY(prog) - cmy * 0.6, 0);
    blob.rotation.y = time * 0.12 + prog * Math.PI * 2.0;
    blob.rotation.x = Math.sin(time * 0.2) * 0.3;
    halo.position.copy(blob.position);
    halo.scale.setScalar(sc * 1.05);
    halo.rotation.y = -time * 0.08;
    halo.rotation.z = time * 0.05;

    particles.rotation.y = time * 0.02 + prog * 0.4;
    particles.position.y = prog * 2.0;
    particles.position.x = -cmx * 1.2;

    // кольца-орбита вокруг ядра
    rings.position.copy(blob.position);
    rings.scale.setScalar(sc * 1.35);
    rings.rotation.z = time * 0.12 + prog * 1.2;
    ringMat1.opacity = 0.4 + Math.sin(time * 0.8) * 0.15;
    ringMat2.opacity = 0.28 + Math.cos(time * 0.6) * 0.12;

    // звёзды: лёгкий дрейф + параллакс по глубине + мерцание
    starsFar.rotation.y = time * 0.006;
    starsFar.position.x = -cmx * 0.6; starsFar.position.y = -cmy * 0.4 + prog * 1.0;
    starsFar.material.opacity = 0.78 + Math.sin(time * 0.7) * 0.12;
    starsNear.rotation.y = -time * 0.01;
    starsNear.position.x = -cmx * 1.6; starsNear.position.y = -cmy * 1.1 + prog * 2.4;
    starsNear.material.opacity = 0.85 + Math.cos(time * 1.1) * 0.12;

    camera.position.x = cmx * 0.6;
    camera.position.y = -cmy * 0.5;
    camera.position.z = 5.2 - prog * 0.6;
    camera.lookAt(0, 0, 0);
    renderer.render(scene, camera);
  }

  var raf = 0;
  function loop(t) { raf = requestAnimationFrame(loop); if (document.hidden) return; frame(t); }
  if (reduce) { frame(1500); }
  else { raf = requestAnimationFrame(loop); }
  window.addEventListener('load', resize);
})();
