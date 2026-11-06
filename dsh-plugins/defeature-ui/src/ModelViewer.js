import React, { useEffect, useRef } from 'react'
import * as THREE from 'three'
import { OrbitControls } from 'three/addons/controls/OrbitControls.js'
import { colors, styles } from './styles.js'

const EMPTY_SET = new Set()
const VIEWER_CAMERA_CACHE = new Map()

function boundaryGeometry(geometry) {
  const index = geometry.index?.array,
    position = geometry.getAttribute('position'),
    counts = new Map();
  if (!index || !position) return new THREE.BufferGeometry();
  const add = (a, b) => {
    const lo = Math.min(a, b),
      hi = Math.max(a, b),
      key = `${lo}:${hi}`,
      item = counts.get(key);
    item ? item.count++ : counts.set(key, {
      a,
      b,
      count: 1
    });
  };
  for (let i = 0; i < index.length; i += 3) {
    add(index[i], index[i + 1]);
    add(index[i + 1], index[i + 2]);
    add(index[i + 2], index[i]);
  }
  const points = [];
  for (const edge of counts.values()) {
    if (edge.count === 1) {
      for (const node of [edge.a, edge.b]) {
        points.push(position.getX(node), position.getY(node), position.getZ(node));
      }
    }
  }
  const result = new THREE.BufferGeometry();
  result.setAttribute('position', new THREE.Float32BufferAttribute(points, 3));
  return result;
}

export function ModelViewer({
  mesh,
  selected,
  mapped = EMPTY_SET,
  onToggle,
  onMappedDeselect,
  title,
  showEdges,
  opacity,
  viewToken,
  cacheKey
}) {
  const hostRef = useRef(null),
    selectedRef = useRef(selected),
    mappedRef = useRef(mapped),
    toggleRef = useRef(onToggle),
    deselectRef = useRef(onMappedDeselect),
    hoverRef = useRef(null);
  selectedRef.current = selected;
  mappedRef.current = mapped;
  toggleRef.current = onToggle;
  deselectRef.current = onMappedDeselect;
  useEffect(() => {
    const host = hostRef.current;
    if (!host || !mesh) return;
    const scene = new THREE.Scene();
    scene.background = new THREE.Color('#09111e');
    const camera = new THREE.PerspectiveCamera(42, 1, .01, 100000),
      renderer = new THREE.WebGLRenderer({
        antialias: true
      });
    renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.25;
    renderer.domElement.style.cssText = 'width:100%;height:100%;display:block';
    host.appendChild(renderer.domElement);
    scene.add(new THREE.AmbientLight('#ffffff', 1.35));
    scene.add(new THREE.HemisphereLight('#ffffff', '#52647c', 3.2));
    const key = new THREE.DirectionalLight('#fff', 2.5);
    key.position.set(4, 6, 3.5);
    scene.add(key);
    const fill = new THREE.DirectionalLight('#8bbcff', 2.0);
    fill.position.set(-45, -30, -50);
    scene.add(fill);
    const bottom = new THREE.DirectionalLight('#ffffff', 1.25);
    bottom.position.set(0, -55, 25);
    scene.add(bottom);
    const group = new THREE.Group(),
      objects = [];
    for (const face of mesh.faces) {
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute('position', new THREE.Float32BufferAttribute(face.positions, 3));
      geometry.setIndex(face.indices);
      geometry.computeVertexNormals();
      const material = new THREE.MeshStandardMaterial({
          color: '#b8c4d8',
          roughness: .65,
          metalness: .08,
          side: THREE.DoubleSide,
          transparent: opacity < 1,
          opacity,
          polygonOffset: true,
          polygonOffsetFactor: 1,
          polygonOffsetUnits: 1
        }),
        object = new THREE.Mesh(geometry, material);
      object.userData.face = face;
      group.add(object);
      objects.push(object);
      if (showEdges) {
        const edge = new THREE.LineSegments(boundaryGeometry(geometry), new THREE.LineBasicMaterial({
          color: '#05070a',
          transparent: true,
          opacity: .85
        }));
        edge.raycast = () => undefined;
        group.add(edge);
      }
    }
    scene.add(group);
    const box = new THREE.Box3().setFromObject(group),
      size = box.getSize(new THREE.Vector3()).length() || 1,
      center = box.getCenter(new THREE.Vector3()),
      controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    const defaultMiddle = controls.mouseButtons.MIDDLE,
      shiftMiddleDown = e => {
        if (e.button === 1 && e.shiftKey) controls.mouseButtons.MIDDLE = THREE.MOUSE.PAN;
      },
      restoreMiddle = () => {
        controls.mouseButtons.MIDDLE = defaultMiddle;
      };
    renderer.domElement.addEventListener('pointerdown', shiftMiddleDown, true);
    window.addEventListener('pointerup', restoreMiddle, true);
    camera.near = Math.max(size / 1000, .01);
    camera.far = size * 100;
    const directions = {
        iso: [1, .8, 1],
        front: [0, 0, 1],
        back: [0, 0, -1],
        left: [-1, 0, 0],
        right: [1, 0, 0],
        top: [0, 1, 0],
        bottom: [0, -1, 0]
      },
      dir = directions[viewToken?.mode] || directions.iso;
    camera.position.copy(center).add(new THREE.Vector3(...dir).normalize().multiplyScalar(size * 1.15));
    camera.up.set(0, 1, 0);
    if (viewToken?.mode === 'top' || viewToken?.mode === 'bottom') camera.up.set(0, 0, viewToken.mode === 'top' ? -1 : 1);
    controls.target.copy(center);
    const saved = cacheKey && VIEWER_CAMERA_CACHE.get(cacheKey);
    if (saved && (!viewToken || viewToken.n === 0)) {
      camera.position.fromArray(saved.position);
      camera.up.fromArray(saved.up);
      controls.target.fromArray(saved.target);
    }
    controls.update();
    const raycaster = new THREE.Raycaster(),
      pointer = new THREE.Vector2();
    let down;
    const hit = e => {
        const r = renderer.domElement.getBoundingClientRect();
        pointer.set((e.clientX - r.left) / r.width * 2 - 1, -(e.clientY - r.top) / r.height * 2 + 1);
        raycaster.setFromCamera(pointer, camera);
        return raycaster.intersectObjects(objects, false)[0];
      },
      pointerDown = e => {
        down = [e.clientX, e.clientY];
      },
      click = e => {
        if (!down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 4) return;
        const found = hit(e);
        if (found && toggleRef.current) toggleRef.current({
          ...found.object.userData.face,
          pickPoint: [found.point.x, found.point.y, found.point.z]
        });
      },
      move = e => {
        const found = hit(e);
        hoverRef.current = found?.object?.userData?.face?.faceId || null;
        renderer.domElement.style.cursor = found && toggleRef.current ? 'pointer' : 'grab';
      },
      leave = () => {
        hoverRef.current = null;
        renderer.domElement.style.cursor = 'default';
      };
    renderer.domElement.addEventListener('pointerdown', pointerDown);
    renderer.domElement.addEventListener('click', click);
    renderer.domElement.addEventListener('pointermove', move);
    renderer.domElement.addEventListener('pointerleave', leave);
    const contextmenu = e => {
      const found = hit(e),
        face = found?.object?.userData?.face;
      if (face && mappedRef.current.has(face.stepEntityId) && selectedRef.current.has(face.faceId) && deselectRef.current) {
        e.preventDefault();
        deselectRef.current(face);
      }
    };
    renderer.domElement.addEventListener('contextmenu', contextmenu);
    let frame = 0;
    const draw = () => {
        for (const object of objects) {
          const id = object.userData.face.faceId;
          const selected = selectedRef.current.has(id),
            mapped = mappedRef.current.has(object.userData.face.stepEntityId);
          const color = selected && mapped
            ? colors.overlap
            : selected
              ? colors.selected
              : hoverRef.current === id
                ? colors.hover
                : '#b8c4d8';
          object.material.color.set(color);
        }
        controls.update();
        renderer.render(scene, camera);
        frame = requestAnimationFrame(draw);
      },
      resize = () => {
        const w = host.clientWidth || 1,
          h = host.clientHeight || 1;
        renderer.setSize(w, h, false);
        camera.aspect = w / h;
        camera.updateProjectionMatrix();
      };
    const observer = new ResizeObserver(resize);
    observer.observe(host);
    resize();
    draw();
    return () => {
      if (cacheKey) VIEWER_CAMERA_CACHE.set(cacheKey, {
        position: camera.position.toArray(),
        up: camera.up.toArray(),
        target: controls.target.toArray()
      });
      cancelAnimationFrame(frame);
      observer.disconnect();
      renderer.domElement.removeEventListener('pointerdown', pointerDown);
      renderer.domElement.removeEventListener('click', click);
      renderer.domElement.removeEventListener('pointermove', move);
      renderer.domElement.removeEventListener('pointerleave', leave);
      renderer.domElement.removeEventListener('contextmenu', contextmenu);
      renderer.domElement.removeEventListener('pointerdown', shiftMiddleDown, true);
      window.removeEventListener('pointerup', restoreMiddle, true);
      controls.dispose();
      scene.traverse(v => {
        v.geometry?.dispose?.();
        Array.isArray(v.material) ? v.material.forEach(x => x.dispose()) : v.material?.dispose?.();
      });
      renderer.dispose();
      renderer.domElement.remove();
    };
  }, [mesh, showEdges, opacity, viewToken, cacheKey]);
  return React.createElement('section', {
    style: styles.viewerCard
  }, React.createElement('div', {
    style: styles.viewerHeader
  }, React.createElement('b', null, title), React.createElement('span', {
    style: styles.muted
  }, mesh ? `${mesh.faceCount} faces` : '无模型')), React.createElement('div', {
    style: styles.viewer
  }, mesh ? React.createElement('div', {
    ref: hostRef,
    style: styles.canvas
  }) : React.createElement('div', {
    style: {
      ...styles.empty,
      margin: 30
    }
  }, '等待模型…'), mesh && React.createElement('div', {
    style: styles.overlay
  }, `${mesh.revisionId} · 点击面选择`)));
}
