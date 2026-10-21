/* ---- 微特自研 3D 查看器（three.js + GLB）----
 * 用法：createWrapViewer(container) → { open(modelKey, texDataUrl, onProgress), setTexture(dataUrl), close(), currentKey }
 * 两种管线：
 *  - official（modely）：ModelY_High_ofuv5.glb 官方资源版——贴膜层 UV1 吃工坊画布（flipY=false），
 *    车壳吃官方 Exterior_BC 贴图（UV0），官方灯贴图/刹车盘/Gemini 轮（2026-10-01）
 *  - legacy（其余车型）：slim GLB，Paint* 材质吃画布（flipY=true，Godot UV 约定）
 */
import * as THREE from './lib/three.module.js?v=61b';
import { GLTFLoader } from './lib/loaders/GLTFLoader.js';
import { OrbitControls } from './lib/controls/OrbitControls.js';
import { MeshoptDecoder } from './lib/libs/meshopt_decoder.module.js';

export const MODEL_FILES = {
  'model3': '/wraps3d/models/Model3_High_slim.glb?v=uv2',
  'modely': '/wraps3d/models/ModelY_High_slim.glb?v=uv2',
  'model3-2024-base': '/wraps3d/models/Bayberry_slim.glb?v=uv2',
  'model3-2024-performance': '/wraps3d/models/Bayberry_slim.glb?v=uv2',
  'modely-2025-base': '/wraps3d/models/BayberryE41_slim.glb?v=uv2',
  'modely-2025-premium': '/wraps3d/models/BayberryE41_slim.glb?v=uv2',
  'modely-2025-performance': '/wraps3d/models/BayberryE41_slim.glb?v=uv2'
};

/* official：官方资源整车（几何=官方 GLB 原样，材质按官方 tscn 接线分层）
 * 2026-10-06 起仪表盘直接用真车车身 key（pickBody：model3h/model3p/modely25/modely25b），与工坊 body 对齐 */
const OFFICIAL_FILES = {
  'modely': '/wraps3d/models/ModelY_High_ofuv5_slim_mo.glb?v=mo1',
  'model3': '/wraps3d/models/Model3_High_cars.glb?v=m3c',
  'model3h': '/wraps3d/models/Poppyseed_cars.glb?v=pp4',
  'model3p': '/wraps3d/models/Poppyseed_cars.glb?v=pp4',
  'modely25': '/wraps3d/models/Bayberry2_cars.glb?v=bb1',
  'modely25b': '/wraps3d/models/Bayberry2_cars.glb?v=bb1',
  'model3-2024-base': '/wraps3d/models/Poppyseed_cars.glb?v=pp4',
  'model3-2024-performance': '/wraps3d/models/Poppyseed_cars.glb?v=pp4',
  'modely-2025-base': '/wraps3d/models/Bayberry2_cars.glb?v=bb1',
  'modely-2025-premium': '/wraps3d/models/Bayberry2_cars.glb?v=bb1',
  'modely-2025-performance': '/wraps3d/models/Bayberry2_cars.glb?v=bb1',
  'modelyl': '/wraps3d/models/BayberryE80_cars.glb?v=y8'
};
/* Poppyseed（新款 Model 3 Highland，v2023 系）：轴向同老款3（x=前+, z=左-）。
 * 材质名+UV1 分派：Paint/Paint2 带 UV1=贴膜层；PaintRough=哑光漆；Exterior*=GLB 原壳保留；
 * *_Fade=开门渐变副本隐藏；RHD=右舵副本隐藏；3h 藏 Perf 杠+尾翼，3P 显示尾翼。 */
const PP_HIDE = new Set(['Floor', 'Headlights_Projections', 'Stoplights_Projections',
  'Plate_US', 'Plate_EU', 'Bumper_F_Perf', 'Bumper_R_Perf', 'Bumper_R_Perf_Reflector',
  'Interior_Body_RHD', 'Dashboard_RHD', 'Screen_Front_RHD', 'Steering_Wheel_Standard_RHD',
  'Stalk_RHD', 'Doorcard_LF_RHD', 'Doorcard_RF_RHD']);
/* Bayberry（新款 Model 3）：按官方 bayberry.tscn 接线——Paint prim 带 UV1=贴膜层，
 * 无 UV1/退化（Underhood）=纯漆色；Trim/Light/Metal 等保留 GLB 自带官方材质（含贴图、无自发光）；
 * 灯光点亮层/投影/玻璃渐变/RHD 等官方默认 visible=false 的全隐藏。 */
const B3_HIDE = new Set(['GroundPlane', 'BrakeLightProjection', 'HeadLightProjection',
  'Parking_Light_Projection', 'Headlights_Beam', 'FaceOnGlow', 'Fade', 'RHD',
  'Doorcard_LF_RHD', 'Doorcard_RF_RHD', 'Skullcap_LF_SierraBlack', 'Skullcap_RF_SierraBlack',
  'Seats_Perf', 'Tesla_Badge', 'Plate_US', 'Plate_EU',
  'Headlights_On', 'DRL', 'Left_Turn_Signal_On', 'Right_Turn_Signal_On',
  'Back_Left_Turn_Signal_On', 'Back_Right_Turn_Signal_On', 'Brake_Lights_On',
  'Parking_Lights_On', 'Reverse Light On', 'Fog_Light_On']);
const TEX_BASE = '/wraps3d/models/tex/';
/* 官方 tscn 接线：PaintSkybox=贴膜层(UV1)、Exterior=官方贴图壳(UV0)、Paint_Rough=哑光漆 */
const WRAP_PRIMS = {Body:[1], Hood:[0], Trunk:[2], Door_LF:[1], Door_RF:[1],
  Door_LR:[1], Door_RR:[1], Door_LF_Mirror:[1], Door_RF_Mirror:[1]};
const EXT_PRIMS = {Body:[0], Hood:[1], Trunk:[0], Door_LF:[0], Door_RF:[0],
  Door_LR:[0], Door_RR:[0], Door_LF_Mirror:[0], Door_RF_Mirror:[0]};
const ROUGH_PRIMS = {Body:[4], Trunk:[4], Door_LF:[2], Door_RF:[2], Door_LR:[2], Door_RR:[2]};
const HIDE_MESH = new Set(['Floor', 'Headlights_Projections', 'Taillights_Projection']);
const GLASS_RE = /^(Window_LF|Window_RF|Window_LR|Window_RR|Windows_Rear|Windows_Top|Lights_Glass|Lights_Glass_Trunk)$/;
const LAMPF_RE = /^(Headlights|DRL|Fog_Lights_Front|Foglights_Cover|Headlights_Trunk|Left_Turn_Signal|Right_Turn_Signal|Lights)$/;
const LAMPR_RE = /^(Brake_Lights_Center|Brake_Lights_Left|Brake_Lights_Right|Brake_Lights_GLOBAL|Reverse_Lights|Fog_Lights_Rear_GLOBAL|Lights_Trunk)$/;
const INTERIOR_RE = /^(Interior_|Seat_|Dashboard|Center_Console|Steering_Wheel|Screen_Front|Doorcard_|Trunk_Card)/;

export function createWrapViewer(container) {
  const scene = new THREE.Scene();
  // 背景跟随应用主题：与主题底色相近但不同（浅色 #f4f6f8→#eef1f5，深色 #0b1120→#101a2e）
  function themeBg(dark) { return dark ? 0x101a2e : 0xeef1f5; }
  function isDark() { return !!(document.body && document.body.classList.contains('dark')); }
  scene.background = new THREE.Color(themeBg(isDark()));
  const camera = new THREE.PerspectiveCamera(40, 1, 0.1, 200);
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.domElement.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;';
  container.appendChild(renderer.domElement);

  scene.add(new THREE.AmbientLight(0xffffff, 1.15));
  const key = new THREE.DirectionalLight(0xffffff, 2.2); key.position.set(5, 8, 6); scene.add(key);
  const fill = new THREE.DirectionalLight(0xdde7ff, 0.9); fill.position.set(-6, 4, -5); scene.add(fill);
const side = new THREE.DirectionalLight(0xffffff, 1.05); side.position.set(0, 6, -9); scene.add(side);   // 正侧面补光（默认侧视的近车侧）

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.target.set(0, 0.6, 0);
  camera.position.set(4.6, 2.2, 5.6);

  function resize() {
    const w = container.clientWidth || 640, h = container.clientHeight || 360;
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h, false);
  }
  const ro = new ResizeObserver(() => resize());
  ro.observe(container);
  resize();
  /* 画布被移入新容器后调用：重绑尺寸监听并立即按新容器重算画幅（防拉伸压扁） */
  function setContainer(el) {
    container = el;
    ro.disconnect();
    ro.observe(el);
    resize();
  }

  const loader = new GLTFLoader();
loader.setMeshoptDecoder(MeshoptDecoder);   // 仪表盘也用 _mo 压缩模型（3.3MB，工坊同款同缓存）
  const cache = new Map();   // modelKey -> { scene, materials, mode, wrapMat }
  let currentKey = null;
  let disposed = false;

  /* ---- official 共享材质（与 uvlab2 ofuv5c 同参）---- */
  const OFF = {
    wrap:     new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 0.38, metalness: 0.05, side: THREE.DoubleSide }),
    ext:      new THREE.MeshStandardMaterial({ color: 0x0a0a0c, roughness: 0.45, metalness: 0.15, side: THREE.DoubleSide }),
    rough:    new THREE.MeshStandardMaterial({ color: 0x0a0a0c, roughness: 0.62, metalness: 0.0, side: THREE.DoubleSide }),
    glass:    new THREE.MeshStandardMaterial({ color: 0x0d1218, transparent: true, opacity: 0.55, roughness: 0.06, metalness: 0.2, side: THREE.DoubleSide }),
    lamp:     new THREE.MeshStandardMaterial({ color: 0x50565c, roughness: 0.35, metalness: 0.2 }),
    interior: new THREE.MeshStandardMaterial({ color: 0x232529, roughness: 0.85 }),
    tire:     new THREE.MeshStandardMaterial({ color: 0x0c0d0f, roughness: 0.95 }),
    rim:      new THREE.MeshStandardMaterial({ color: 0x363a41, roughness: 0.42, metalness: 0.6, side: THREE.DoubleSide }),
    rotor:    new THREE.MeshStandardMaterial({ color: 0xb9bcbf, roughness: 0.45, metalness: 0.9 }),
    caliper:  new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 0.4, metalness: 0.3, side: THREE.DoubleSide })
  };
  function loadTex(name) {
    const t = new THREE.TextureLoader().load(TEX_BASE + name);
    t.flipY = false;
    t.colorSpace = THREE.SRGBColorSpace;
    t.channel = 0;
    return t;
  }
  OFF.caliper.map = loadTex('Brakes_Std_BC.png'); OFF.caliper.needsUpdate = true;
  /* Y_High 专用壳/灯贴图按需加载（Bayberry 系用不到，省显存） */
  let yTexReady = false;
  function ensureYTex() {
    if (yTexReady) return;
    yTexReady = true;
    OFF.ext.map = loadTex('Exterior_BC_Y_High.png'); OFF.ext.needsUpdate = true;
    OFF.lamp.map = loadTex('Lights_BC_Y_High.png'); OFF.lamp.needsUpdate = true;
  }
  /* Bayberry 专用：GLB 自带全部官方贴图，无需外部贴图，纯漆色即可 */
  const OFF_B3 = {
    paint: new THREE.MeshStandardMaterial({ color: 0xf8fafc, roughness: 0.34, metalness: 0.06 }),
    rough: new THREE.MeshStandardMaterial({ color: 0xf8fafc, roughness: 0.62, metalness: 0.0 })
  };
  /* Poppyseed 专用：同 Bayberry，纯漆色；灯组无 Y 贴图用纯暗色（OFF.lamp 带 Y 贴图不能共用） */
  const OFF_PP = {
    paint: new THREE.MeshStandardMaterial({ color: 0xf8fafc, roughness: 0.34, metalness: 0.06 }),
    rough: new THREE.MeshStandardMaterial({ color: 0xf8fafc, roughness: 0.62, metalness: 0.0 }),
    lamp: new THREE.MeshStandardMaterial({ color: 0x50565c, roughness: 0.35, metalness: 0.2 })
  };

  function applyTexToEntry(entry, tex) {
    if (entry.mode === 'official') {
      entry.wrapMat.map = tex || null;
      entry.wrapMat.needsUpdate = true;
      return;
    }
    entry.materials.forEach(m => {
      m.map = tex;
      m.color = new THREE.Color(0xffffff);
      m.needsUpdate = true;
    });
  }
  function makeTex(dataUrl, cb, opts) {
    const img = new Image();
    img.onload = function () {
      const tex = new THREE.Texture(img);
      if (opts && opts.official) {
        tex.flipY = false;           // glTF UV 约定（官方 UV1）
        tex.channel = 1;             // 采样 TEXCOORD_1
      } else {
        tex.flipY = true;            // legacy slim GLB（Godot UV 约定）
      }
      tex.colorSpace = THREE.SRGBColorSpace;
      tex.needsUpdate = true;
      cb(tex);
    };
    img.src = dataUrl;
  }

  function loadModel(modelKey, onProgress) {
    currentKey = modelKey;
    for (const [k, e] of cache) {          // 防双模型：先移出场上旧车（缓存保留，切回秒上）
      if (k !== modelKey && e.scene && e.scene.parent) e.scene.parent.remove(e.scene);
    }
    if (cache.has(modelKey)) {
      const e = cache.get(modelKey);
      scene.add(e.scene);
      if (e.home) {   // 再次进入 3D：回到标准机位（否则停留在上次缩放/旋转的位置，看起来像卡死）
        controls.target.copy(e.home.target);
        camera.position.copy(e.home.pos);
        controls.update();
      }
      return Promise.resolve(e);
    }
    const official = !!OFFICIAL_FILES[modelKey];
    const url = official ? OFFICIAL_FILES[modelKey] : MODEL_FILES[modelKey];
    if (!url) return Promise.reject(new Error('unsupported model: ' + modelKey));
    return new Promise((resolve, reject) => {
      loader.load(url, (gltf) => {
        if (disposed) return resolve(null);
        const car = gltf.scene;
        let entry;
        if (official) {
          const assoc = gltf.parser.associations;
          const jnames = gltf.parser.json.meshes.map(m => (m && m.name) || '');
          const globalHide = new Set(jnames.filter(nm => nm.endsWith('_GLOBAL') && jnames.includes(nm.slice(0, -7))));
          const b3 = /Bayberry/.test(url);
          const poppy = /Poppyseed/.test(url);
          const perf = /performance/.test(modelKey) || modelKey === 'model3p';
          if (modelKey === 'modely') { /* 2026-10-05 起壳层不再挂官方贴图：深色壳+白色漆面层，与贴膜工坊一致，接缝不发白 */ }
          else if (!b3 && !poppy) { OFF.ext.map = null; OFF.lamp.map = null; OFF.ext.needsUpdate = true; OFF.lamp.needsUpdate = true; }
          car.traverse(o => {
            if (!o.isMesh) return;
            if (!o.geometry.attributes.normal) o.geometry.computeVertexNormals();
            const a = assoc.get(o) || {};
            const mi = a.meshes;
            if (mi === undefined) return;
            const base = jnames[mi] || ('mesh' + mi);
            const pidx = a.primitives || 0;
            if (poppy) {
              if (PP_HIDE.has(base) && !(perf && base === 'Spoiler')) { o.visible = false; return; }
              if (base === 'WheelCar' || base === 'WheelPinwheel') { o.material = (pidx === 0) ? OFF.rim : OFF.tire; return; }
              if (base === 'Windows_Top') {
                /* 几何实测（世界包围盒+顶视射线）：prim1=玻璃车顶板（GLB 材质名带 Fade 但
                 * 就是车顶，prim0=其内饰层孪生，藏掉防 z-fight）；prim2/3=后风挡；
                 * prim4/5=前风挡（官方 tscn 覆盖 Skybox/Interior_Tinted）。全部走 OFF.glass。 */
                o.visible = (pidx !== 0);
                o.material = OFF.glass;
                return;
              }
              const m0 = (Array.isArray(o.material) ? o.material[0] : o.material) || {};
              const mn = m0.name || '';
              if (/_Fade$/.test(mn)) { o.visible = false; return; }
              if (mn.startsWith('PaintRough') || mn === 'Paint_Rough_Perf') { o.material = OFF_PP.rough; return; }
              if (mn === 'Paint' || mn === 'Paint2') {
                /* UV1 跨度>0.02 = 官方贴膜区；退化单点（极少）=纯漆色 */
                let span = 0;
                const uv1 = o.geometry.attributes.uv1;
                if (uv1) {
                  const arr = uv1.array;
                  let u0 = 1e9, u1 = -1e9, v0 = 1e9, v1 = -1e9;
                  for (let i2 = 0; i2 < arr.length; i2 += 2) {
                    if (arr[i2] < u0) u0 = arr[i2]; if (arr[i2] > u1) u1 = arr[i2];
                    if (arr[i2 + 1] < v0) v0 = arr[i2 + 1]; if (arr[i2 + 1] > v1) v1 = arr[i2 + 1];
                  }
                  span = Math.max(u1 - u0, v1 - v0);
                }
                o.material = span > 0.02 ? OFF.wrap : OFF_PP.paint;
                return;
              }
              if (mn.startsWith('Glass')) { o.material = OFF.glass; return; }
              if (mn === 'Light' || mn.startsWith('Illumination')) { o.material = OFF_PP.lamp; return; }
              return; /* Exterior 与内饰/饰条/屏幕等保留 GLB 自带官方材质 */
            }
            if (b3) {
              /* 新款 Model 3（Highland）：按材质名+UV1 分派，非漆面保留官方材质 */
              if (B3_HIDE.has(base) && !(perf && (base === 'Fascia_Perf' || base === 'Spoiler_Perf'))) { o.visible = false; return; }
              if (perf && base === 'Fascia_Standard') { o.visible = false; return; }
              if (base === 'WheelHelix' || base === 'WheelCar' || base === 'WheelMachina2') { o.material = (pidx === 0) ? OFF.rim : OFF.tire; return; }
              if (base === 'BrakeF' || base === 'BrakeR') { o.material = (pidx === 0) ? OFF.caliper : OFF.rotor; return; }
              const m0 = (Array.isArray(o.material) ? o.material[0] : o.material) || {};
              const mn = m0.name || '';
              if (base === 'Underhood_Piece' || mn === 'PaintRough') { o.material = OFF_B3.rough; return; }
              if (mn === 'Paint') { o.material = o.geometry.attributes.uv1 ? OFF.wrap : OFF_B3.paint; return; }
              if (mn === 'Interior' || mn === 'Seats') { o.material = OFF.interior; return; }
              if (/^Glass/.test(mn)) { o.material = OFF.glass; return; }
              return;
            }
            if (HIDE_MESH.has(base) || globalHide.has(base)) { o.visible = false; return; }
            if (base === 'WheelGemini' || base === 'WheelPinwheel') { o.material = (pidx === 0) ? OFF.rim : OFF.tire; return; }
            if (base === 'BrakeF' || base === 'BrakeR') { o.material = (pidx === 0) ? OFF.caliper : OFF.rotor; return; }
            if (GLASS_RE.test(base)) { o.material = OFF.glass; return; }
            if (LAMPF_RE.test(base) || LAMPR_RE.test(base)) { o.material = OFF.lamp; return; }
            if (INTERIOR_RE.test(base)) { o.material = OFF.interior; return; }
            if ((WRAP_PRIMS[base] || []).includes(pidx)) { o.material = OFF.wrap; return; }
            if ((EXT_PRIMS[base] || []).includes(pidx)) { o.material = OFF.ext; return; }
            if ((ROUGH_PRIMS[base] || []).includes(pidx)) { o.material = OFF.rough; return; }
            o.material = OFF.ext;
          });
          if (/ModelY/.test(url)) {            // 轮子稍大、往车头靠一点（2026-10-05 用户校型；在 updateMatrixWorld 前改）
            car.traverse(o => {
              if (!/^Wheel_(FL|FR|RL|RR)$/.test(o.name || '')) return;
              o.scale.multiplyScalar(1.07);    // 绕节点原点(轮心)放大
              o.position.x += 0.05;            // 父空间平移，四轮统一向车头
              o.position.y += 0.024;           // 补放大后的下沉量
            });
          }
          entry = { scene: car, mode: 'official', wrapMat: OFF.wrap, materials: [] };
        } else {
          const materials = [];
          car.traverse(o => {
            if (o.isMesh) {
              const ms = Array.isArray(o.material) ? o.material : [o.material];
              ms.forEach(m => { if (/^(Paint|Exterior)/i.test(m.name)) materials.push(m); });
            }
          });
          entry = { scene: car, mode: 'legacy', materials };
        }
        // 包围盒只算可见网格（隐藏的灯光投影大面片会把盒撑大几十米）
        car.updateMatrixWorld(true);
        const box = new THREE.Box3();
        car.traverse(o => {
          if (!o.isMesh || !o.visible) return;
          if (!o.geometry.boundingBox) o.geometry.computeBoundingBox();
          box.union(o.geometry.boundingBox.clone().applyMatrix4(o.matrixWorld));
        });
        const c = box.getCenter(new THREE.Vector3());
        if (official) {
          car.position.x -= c.x; car.position.z -= c.z;   // 官方坐标 y=0 即地面，只回中水平
          controls.target.set(0, c.y, 0);                 // 镜头以车中心为原点
        } else {
          car.position.sub(c);
          controls.target.set(0, 0, 0);
        }
        // 自动取景：按可见车身对角线拉相机距离，保证整车入画
        const carSize = box.getSize(new THREE.Vector3()).length();
        const camDir = new THREE.Vector3(4.6, 2.2, 5.6).normalize();
        camera.position.copy(controls.target).add(camDir.multiplyScalar(carSize * 1.9));
        camera.lookAt(controls.target);
        controls.update();
        entry.home = { target: controls.target.clone(), pos: camera.position.clone() };   // 标准机位
        cache.set(modelKey, entry);
        scene.add(car);
        resolve(entry);
      }, (ev) => {
        if (ev.total && onProgress) onProgress(ev.loaded / ev.total);
      }, (e) => {
        reject(new Error((e && e.message) || 'GLB 加载失败'));
      });
    });
  }

  function renderLoop() {
    if (disposed) return;
    requestAnimationFrame(renderLoop);
    if (container.style.display === 'none') return;   // 隐藏时停渲染省电
    controls.update();
    renderer.render(scene, camera);
  }
  renderLoop();

  return {
    open(modelKey, texDataUrl, onProgress) {
      container.style.display = 'block';
      resize();
      return loadModel(modelKey, onProgress).then((entry) => {
        if (disposed || !entry) return entry;
        if (texDataUrl) {
          makeTex(texDataUrl, (tex) => { if (!disposed) applyTexToEntry(entry, tex); }, { official: entry.mode === 'official' });
        } else {
          applyTexToEntry(entry, null);
        }
        return entry;
      });
    },
    setTexture(dataUrl) {
      if (!currentKey || !cache.has(currentKey)) return;
      const entry = cache.get(currentKey);
      makeTex(dataUrl, (tex) => { if (!disposed) applyTexToEntry(entry, tex); }, { official: entry.mode === 'official' });
    },
    clearWrap() {   // 恢复原车漆：清贴膜层回原厂（2026-10-06 用户需求）
      if (!currentKey || !cache.has(currentKey)) return;
      applyTexToEntry(cache.get(currentKey), null);
    },
    paintWrap(colorHex) {   // 直接染贴膜层本体（official 管线的车漆显示层）——恢复原车漆用，绕开 index 侧材质收集的层错位
      if (!currentKey || !cache.has(currentKey)) return;
      const entry = cache.get(currentKey);
      if (entry.mode === 'official' && entry.wrapMat) {
        entry.wrapMat.map = null;
        entry.wrapMat.color.set(colorHex || 0xffffff);
        entry.wrapMat.needsUpdate = true;
      } else if (entry.materials && entry.materials.length) {
        entry.materials.forEach(m => { m.map = null; m.color.set(colorHex || 0xffffff); m.needsUpdate = true; });
      }
    },
    close() {
      container.style.display = 'none';
    },
    resize: resize,
    setContainer: setContainer,
    setBg(dark) { scene.background.set(themeBg(dark)); },
    get current() { return currentKey; },
    get scene() { return scene; },
    get camera() { return camera; },
    get controls() { return controls; },
    debugMats() {
      const out = [];
      scene.traverse(o => {
        if (!o.isMesh) return;
        const ms = Array.isArray(o.material) ? o.material : [o.material];
        ms.forEach(m => {
          if (/^Paint/i.test(m.name)) out.push({ name: m.name, hasMap: !!m.map, imgW: m.map && m.map.image ? m.map.image.width : 0, color: m.color.getHexString() });
        });
      });
      return out;
    },
    get isCached() { return (k) => cache.has(k); },
    dispose() { disposed = true; ro.disconnect(); renderer.dispose(); }
  };
}
