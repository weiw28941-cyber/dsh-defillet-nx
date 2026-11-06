import { spawn, spawnSync } from 'node:child_process'
import { createWriteStream, existsSync, mkdirSync } from 'node:fs'
import { createServer } from 'node:net'
import { homedir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { dataRoot, installEnvironment, installPresets, packageRoot, readInstallation, saveInstallation } from './installation.js'

export const name = '@defeature/dsh-ui'
const ROUTE_PREFIX = '/defeature/api'
const START_TIMEOUT_MS = 45_000

function sendJson(response, status, value) { response.statusCode=status;response.setHeader('content-type','application/json; charset=utf-8');response.end(JSON.stringify(value)) }
function commandExists(command) { return spawnSync(command,['--version'],{stdio:'ignore',shell:false}).status===0 }
function resolveRepoRoot(config={}) { const configured=config.repoRoot||process.env.DEFEATURE_REPO_ROOT;return configured?resolve(configured):existsSync(join(packageRoot,'runtime'))?join(packageRoot,'runtime'):resolve(dirname(fileURLToPath(import.meta.url)),'../../..') }
function resolvePython(config={}) { const explicit=config.pythonExecutable||process.env.DEFEATURE_PYTHON||readInstallation(config).pythonExecutable;if(explicit)return explicit;const candidates=[join(dataRoot(config),'python-env','python.exe'),join(homedir(),'AppData','Local','miniforge3','envs','defeature','python.exe'),'python'];for(const candidate of candidates)if(candidate==='python'?commandExists(candidate):existsSync(candidate))return candidate;return 'python' }
function reservePort(){return new Promise((done,reject)=>{const server=createServer();server.unref();server.once('error',reject);server.listen(0,'127.0.0.1',()=>{const address=server.address(),port=typeof address==='object'&&address?address.port:0;server.close(error=>error?reject(error):done(port))})})}

class BackendService {
  constructor(config={}){this.config=config;this.repoRoot=resolveRepoRoot(config);this.python=resolvePython(config);this.port=0;this.child=undefined;this.state='stopped';this.error=undefined;this.logTail=[];this.startPromise=undefined}
  snapshot(){return{state:this.state,port:this.port||null,python:this.python,repoRoot:this.repoRoot,dataRoot:dataRoot(this.config),nxBase:readInstallation(this.config).nxBase||process.env.UGII_BASE_DIR||'',error:this.error||null,logTail:this.logTail.slice(-30)}}
  record(chunk){for(const line of String(chunk).split(/\r?\n/))if(line.trim())this.logTail.push(line.trim());if(this.logTail.length>200)this.logTail.splice(0,this.logTail.length-200)}
  async ensureStarted(){if(this.state==='ready')return;if(this.state==='installing')throw new Error('Python/OCC installation is in progress');if(this.startPromise)return this.startPromise;this.startPromise=this.start().finally(()=>{this.startPromise=undefined});return this.startPromise}
  async start(){
    if(!existsSync(join(this.repoRoot,'webapp','backend','app','main.py'))){this.state='failed';this.error=`Defeature repo root is invalid: ${this.repoRoot}`;throw new Error(this.error)}
    this.python=resolvePython(this.config)
    const probe=spawnSync(this.python,['-c','import OCC.Core, fastapi, uvicorn, multipart, pydantic, httpx, PIL'],{stdio:'ignore',windowsHide:true})
    if(probe.status!==0){this.state='setup_required';this.error='Python/OCC environment is missing. Confirm installation in the CAD workbench or configure a compatible Python executable.';return}
    const controller=new AbortController();this.startController=controller
    this.state='starting';this.error=undefined;this.port=await reservePort()
    if(controller.signal.aborted)return
    const logDir=join(process.env.DSH_HOME||join(homedir(),'.dsh'),'logs');mkdirSync(logDir,{recursive:true});const log=createWriteStream(join(logDir,'defeature-backend.log'),{flags:'a'}),env={...process.env,PYTHONUNBUFFERED:'1'}
    const installation=readInstallation(this.config)
    env.STEP_WEB_JOBS_ROOT=env.STEP_WEB_JOBS_ROOT||join(dataRoot(this.config),'jobs')
    if(installation.nxBase)env.UGII_BASE_DIR=installation.nxBase
    if(installation.runJournal)env.NX_RUN_JOURNAL=installation.runJournal
    if(installation.stepSettings)env.NX_STEP214_EXPORT_DEF=installation.stepSettings
    const child=spawn(this.python,['-m','uvicorn','webapp.backend.app.main:app','--host','127.0.0.1','--port',String(this.port)],{cwd:this.repoRoot,env,windowsHide:true,stdio:['ignore','pipe','pipe']});this.child=child
    child.stdout.on('data',chunk=>{log.write(chunk);this.record(chunk)});child.stderr.on('data',chunk=>{log.write(chunk);this.record(chunk)})
    child.once('exit',(code,signal)=>{log.end();if(this.child!==child)return;if(this.state!=='stopped'){this.state='failed';this.error=`FastAPI exited (code=${code}, signal=${signal||'none'})`}this.child=undefined});child.once('error',error=>{log.end();if(controller.signal.aborted)return;this.state='failed';this.error=error.message;this.record(error.stack||error.message)})
    const timeout=this.config.startupTimeoutMs||START_TIMEOUT_MS,deadline=Date.now()+timeout
    while(Date.now()<deadline){if(controller.signal.aborted)return;if(this.state==='failed')throw new Error(this.error||'FastAPI failed to start');try{const response=await fetch(`http://127.0.0.1:${this.port}/api/health`,{signal:AbortSignal.any([controller.signal,AbortSignal.timeout(2000)])});if(controller.signal.aborted)return;if(response.ok){this.state='ready';return}}catch{}await new Promise(done=>setTimeout(done,250))}
    if(controller.signal.aborted)return
    this.error=`FastAPI startup timed out after ${timeout} ms`;this.state='failed';await this.stop();throw new Error(this.error)
  }
  async stop(){this.startController?.abort();const child=this.child;this.state='stopped';this.child=undefined;if(child&&child.exitCode===null&&child.pid){if(process.platform==='win32')spawnSync('taskkill',['/pid',String(child.pid),'/t','/f'],{stdio:'ignore',windowsHide:true});else child.kill('SIGTERM')}}
}
function copyHeaders(headers){const result={};for(const[name,value]of Object.entries(headers))if(value!==undefined&&!['host','connection','content-length','transfer-encoding'].includes(name.toLowerCase()))result[name]=value;return result}
async function proxyRequest(service,request,response){try{await service.ensureStarted();const incoming=new URL(request.url||'/','http://localhost'),tail=incoming.pathname.slice(ROUTE_PREFIX.length),target=`http://127.0.0.1:${service.port}/api${tail}${incoming.search}`,method=request.method||'GET',init={method,headers:copyHeaders(request.headers),redirect:'manual'};if(!['GET','HEAD'].includes(method)){init.body=request;init.duplex='half'}const upstream=await fetch(target,init);response.statusCode=upstream.status;upstream.headers.forEach((value,name)=>{if(!['connection','content-encoding','transfer-encoding','content-length'].includes(name.toLowerCase()))response.setHeader(name,value)});if(!upstream.body)return response.end();const reader=upstream.body.getReader();while(true){const{value,done}=await reader.read();if(done)break;if(!response.write(Buffer.from(value)))await new Promise(done=>response.once('drain',done))}response.end()}catch(error){if(!response.headersSent)sendJson(response,503,{detail:`Defeature backend unavailable: ${error instanceof Error?error.message:String(error)}`,backend:service.snapshot()});else response.destroy(error)}}

async function setupRequest(service, request, response) {
  try {
    if (request.method !== 'POST') return sendJson(response, 405, { detail: 'POST required' })
    // Browser-only setup mutations require a same-origin JSON request.
    const origin = request.headers.origin
    if (!origin || new URL(origin).host !== request.headers.host ||
        !String(request.headers['content-type'] || '').startsWith('application/json')) {
      return sendJson(response, 403, { detail: 'Same-origin JSON request required' })
    }
    let text = ''
    for await (const chunk of request) {
      text += chunk
      if (text.length > 8192) return sendJson(response, 413, { detail: 'Request too large' })
    }
    const value = JSON.parse(text)
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Invalid setup request')
    if (value.action !== 'configure' && !(value.action === 'install-environment' && value.confirmed === true)) {
      return sendJson(response, 400, { detail: 'Explicit environment installation confirmation or configuration required' })
    }
    if (service.state === 'installing' || service.state === 'starting' ||
        (service.state === 'ready' && value.action === 'install-environment')) {
      return sendJson(response, 409, { detail: 'Stop the backend before changing its installation' })
    }
    if (value.action === 'install-environment' && value.confirmed === true) {
      service.state = 'installing'
      service.error = undefined
      void installEnvironment(service.config, chunk => service.record(chunk))
        .then(() => { service.state = 'stopped'; return service.ensureStarted() })
        .catch(error => { service.state = 'failed'; service.error = error.message })
      return sendJson(response, 202, service.snapshot())
    }
    if (value.action === 'configure') {
      const values = {}
      for (const key of ['pythonExecutable', 'nxBase', 'runJournal', 'stepSettings']) {
        if (value[key] !== undefined) {
          if (typeof value[key] !== 'string') throw new Error('Invalid ' + key)
          values[key] = value[key].trim()
        }
      }
      saveInstallation(service.config, values)
      const restartRequired = service.state === 'ready'
      if (!restartRequired) await service.ensureStarted()
      return sendJson(response, 200, { ...service.snapshot(), restartRequired })
    }
    return sendJson(response, 400, { detail: 'Explicit environment installation confirmation or configuration required' })
  } catch (error) { sendJson(response, 400, { detail: error.message }) }
}

export function apply(ctx,config={}) {
  installPresets()
  ctx.inject(['webServer'],host=>{
    const service=new BackendService(config)
    host.effect(()=>{
      void service.ensureStarted().catch(error=>console.error('[defeature] backend startup failed:',error))
      const disposers=[
        host.webServer.register({kind:'exact',path:'/defeature/status',handler:(_request,response)=>sendJson(response,service.state==='failed'?503:200,service.snapshot())}),
        host.webServer.register({kind:'exact',path:'/defeature/setup',handler:(request,response)=>setupRequest(service,request,response)}),
        host.webServer.register({kind:'prefix',path:ROUTE_PREFIX,handler:(request,response)=>proxyRequest(service,request,response)})
      ]
      return()=>{for(const dispose of disposers.reverse())dispose();return service.stop()}
    },'defeature: backend and proxy')
  })
}
