import { Canvas, ThreeEvent } from '@react-three/fiber'
import { Bounds, OrbitControls } from '@react-three/drei'
import * as THREE from 'three'
import { Component, type ErrorInfo, type ReactNode, useMemo } from 'react'
import type { FaceData, MeshPayload, PickedFace } from '../types'

class ViewerErrorBoundary extends Component<{children:ReactNode},{failed:boolean}> {
  state = { failed: false }
  static getDerivedStateFromError() { return { failed: true } }
  componentDidCatch(error: Error, info: ErrorInfo) { console.error('3D viewer failed', error, info) }
  render() { return this.state.failed ? <div className="viewer-error">3D 视图加载失败，请刷新页面重试。</div> : this.props.children }
}

function boundaryGeometry(geometry: THREE.BufferGeometry) {
  const index = geometry.index?.array
  const position = geometry.getAttribute('position')
  const counts = new Map<string, {a:number;b:number;count:number}>()
  if (!index || !position) return new THREE.BufferGeometry()
  const add = (a:number,b:number) => { const lo=Math.min(a,b), hi=Math.max(a,b), key=`${lo}:${hi}`, item=counts.get(key); if(item)item.count++;else counts.set(key,{a,b,count:1}) }
  for(let i=0;i<index.length;i+=3){add(index[i],index[i+1]);add(index[i+1],index[i+2]);add(index[i+2],index[i])}
  const points:number[]=[]
  for(const edge of counts.values())if(edge.count===1)for(const node of [edge.a,edge.b])points.push(position.getX(node),position.getY(node),position.getZ(node))
  const result=new THREE.BufferGeometry();result.setAttribute('position',new THREE.Float32BufferAttribute(points,3));return result
}

function FaceMesh({face, selected, onPick}:{face:FaceData;selected:boolean;onPick?:(face:PickedFace)=>void}) {
  const geometry = useMemo(() => {
    const value = new THREE.BufferGeometry()
    value.setAttribute('position', new THREE.Float32BufferAttribute(face.positions, 3))
    value.setIndex(face.indices); value.computeVertexNormals(); return value
  }, [face])
  const edges = useMemo(() => boundaryGeometry(geometry), [geometry])
  const selectable = Boolean(onPick)
  const pick = onPick ? (event:ThreeEvent<MouseEvent>) => { event.stopPropagation(); onPick({...face,pickPoint:[event.point.x,event.point.y,event.point.z]}) } : undefined
  return <group>
    <mesh geometry={geometry} onClick={pick} onPointerOver={(e)=>{if(selectable){e.stopPropagation();document.body.style.cursor='pointer'}}} onPointerOut={()=>document.body.style.cursor='default'}>
      <meshStandardMaterial color={selected?'#ff5a3c':'#b8c4d8'} roughness={0.65} metalness={0.08} side={THREE.DoubleSide} polygonOffset polygonOffsetFactor={1} polygonOffsetUnits={1}/>
    </mesh>
    <lineSegments geometry={edges} raycast={()=>null}>
      <lineBasicMaterial color="#05070a" transparent opacity={0.9}/>
    </lineSegments>
  </group>
}

export default function ModelViewer({mesh,selected,onPick,title}:{mesh:MeshPayload|null;selected:Set<string>;onPick?:(face:PickedFace)=>void;title:string}) {
  return <section className="viewer"><div className="viewer-title"><span>{title}</span><span>{mesh?`${mesh.faceCount} faces`:'No model'}</span></div>
    <ViewerErrorBoundary>
      <Canvas camera={{position:[80,70,80],fov:42}} gl={{antialias:true,toneMapping:THREE.ACESFilmicToneMapping,toneMappingExposure:1.25}}>
        <color attach="background" args={['#09111e']}/>
        <ambientLight intensity={1.35}/>
        <hemisphereLight args={['#ffffff','#52647c',3.2]}/>
        <directionalLight position={[40,60,35]} intensity={2.5}/>
        <directionalLight position={[-45,-30,-50]} intensity={2.0}/>
        <directionalLight position={[0,-55,25]} intensity={1.25}/>
        {mesh && <Bounds key={mesh.revisionId} fit clip margin={1.15}>{mesh.faces.map(face=><FaceMesh key={face.faceId} face={face} selected={selected.has(face.faceId)} onPick={onPick}/>)}</Bounds>}
        <OrbitControls makeDefault enableDamping={false}/>
      </Canvas>
    </ViewerErrorBoundary>
  </section>
}
