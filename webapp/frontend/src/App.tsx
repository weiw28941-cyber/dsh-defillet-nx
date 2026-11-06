import { useMemo, useRef, useState } from 'react'
import ModelViewer from './components/ModelViewer'
import { execute, getEvents, getMesh, uploadStep } from './lib/api'
import type { Job, JobEvent, MeshPayload, PickedFace } from './types'

const KIND_LABELS: Record<string,string> = {
  status: '状态', stage: '阶段', request: '请求', strategy: '策略',
  attempt_failed: '重试', infrastructure_failed: '系统错误',
  nx_succeeded: 'NX', completed: '完成',
}

function formatEvent(event: JobEvent) {
  const time = new Date(event.created_at).toLocaleTimeString('zh-CN', { hour12: false })
  return `[${time}] [${KIND_LABELS[event.kind] ?? event.kind}] ${event.message}`
}

export default function App() {
  const [jobId,setJobId]=useState('')
  const [job,setJob]=useState<Job|null>(null)
  const [original,setOriginal]=useState<MeshPayload|null>(null)
  const [result,setResult]=useState<MeshPayload|null>(null)
  const [selected,setSelected]=useState<Map<string,PickedFace>>(new Map())
  const [prompt,setPrompt]=useState('删除所选圆角，并保持实体封闭。')
  const [logs,setLogs]=useState<string[]>([])
  const [error,setError]=useState('')
  const [busy,setBusy]=useState(false)
  const lastEventSeq=useRef(0)
  const selectedIds=useMemo(()=>new Set(selected.keys()),[selected])
  const emptySelection=useMemo(()=>new Set<string>(),[])

  async function appendEvents(id:string) {
    const events=await getEvents(id,lastEventSeq.current)
    if(events.length===0)return
    lastEventSeq.current=events[events.length-1].seq
    setLogs(old=>[...old,...events.map(formatEvent)])
  }

  async function upload(file?:File) {
    if(!file)return
    setError('');setLogs(['正在上传并解析 STEP…']);setBusy(true)
    setOriginal(null);setResult(null);setSelected(new Map());lastEventSeq.current=0
    try {
      const created=await uploadStep(file)
      setJobId(created.jobId)
      await appendEvents(created.jobId)
      const mesh=await getMesh(created.jobId,created.revisionId)
      setOriginal(mesh)
      setJob({id:created.jobId,status:'ready',current_revision:created.revisionId})
      setLogs(old=>[...old,`解析完成：${mesh.faceCount} 个 BREP 面`])
    } catch(e) {
      setError(String(e));setLogs(old=>[...old,'处理失败'])
    } finally { setBusy(false) }
  }

  function pick(face:PickedFace) {
    setSelected(old=>{const next=new Map(old);next.has(face.faceId)?next.delete(face.faceId):next.set(face.faceId,face);return next})
  }

  async function run() {
    if(!jobId||!job||selected.size===0)return
    setBusy(true);setError('')
    setLogs(old=>[...old,`提交任务：选择 ${selected.size} 个面，等待大模型规划…`])
    const timer=window.setInterval(()=>{void appendEvents(jobId).catch(()=>{})},500)
    try {
      const next=await execute(jobId,job.current_revision,prompt,[...selected.values()])
      await appendEvents(jobId)
      setJob(next)
      const mesh=await getMesh(jobId,next.current_revision)
      setResult(mesh)
      setLogs(old=>[...old,`结果加载完成：${mesh.faceCount} 个 BREP 面`])
    } catch(e) {
      await appendEvents(jobId).catch(()=>{})
      setError(String(e));setLogs(old=>[...old,'任务执行失败，详细原因见上方事件。'])
    } finally {
      window.clearInterval(timer);setBusy(false)
    }
  }

  return <main>
    <header><div><p className="eyebrow">NX 2406 · OCC · LLM</p><h1>STEP 智能编辑器</h1></div><label className="upload">上传 STEP<input type="file" accept=".stp,.step" onChange={e=>upload(e.target.files?.[0])}/></label></header>
    <div className="status"><span className={`dot ${job?.status??''}`}/><b>{job?.status??'等待上传'}</b>{jobId&&<code>{jobId.slice(0,10)}</code>}{error&&<strong>{error}</strong>}</div>
    <div className="view-grid"><ModelViewer mesh={original} selected={selectedIds} onPick={pick} title="原始模型 · 点击选择 BREP 面"/><ModelViewer mesh={result} selected={emptySelection} title="修改结果"/></div>
    <section className="control">
      <div className="selection"><div className="section-title"><b>已选面</b><button onClick={()=>setSelected(new Map())}>清空</button></div>{selected.size===0?<p className="muted">在左侧模型中点击一个或多个面</p>:[...selected.values()].map(face=><div className="face-row" key={face.faceId}><span>{face.freecadFace}</span><code>{face.stepEntity??'unmapped'}</code><small>{face.surfaceType}</small></div>)}</div>
      <div className="request"><label>编辑要求</label><textarea value={prompt} onChange={e=>setPrompt(e.target.value)} rows={5}/><button className="execute" disabled={busy||selected.size===0||!job} onClick={run}>{busy?'处理中…':'交给大模型规划并执行'}</button>{jobId&&job?.current_revision&&<a href={`/api/jobs/${jobId}/revisions/${job.current_revision}/step`}>下载当前 STEP</a>}</div>
      <div className="logs"><b>任务日志</b><pre>{logs.join('\n')||'等待任务事件…'}</pre></div>
    </section>
  </main>
}
