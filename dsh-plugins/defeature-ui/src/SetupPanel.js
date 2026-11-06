import React, { useState } from 'react'
import { styles } from './styles.js'

export function SetupPanel({ backend, onUpdate }) {
  const [nxBase, setNxBase] = useState(backend.nxBase || '')
  const [pythonExecutable, setPython] = useState('')
  const [error, setError] = useState('')
  async function submit(values) {
    setError('')
    try {
      const response = await fetch('/defeature/setup', {
        method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(values),
      })
      const value = await response.json()
      if (!response.ok) throw new Error(value.detail)
      onUpdate(value)
      if (value.restartRequired) setError('配置已保存。请在当前 CAD 操作结束后重启 DSH，使新配置生效。')
    } catch (error) { setError(error.message) }
  }
  return React.createElement('section', { style: styles.panel },
    React.createElement('h2', null, '本地 NX 与 Python/OCC 环境'),
    React.createElement('p', null, 'NX 和许可证由用户提供。首次安装将从 conda-forge 下载独立环境，不修改系统 Python。'),
    React.createElement('label', null, 'NX 安装目录 ', React.createElement('input', {
      value: nxBase, placeholder: 'C:\\Program Files\\Siemens\\NX2406', onChange: e => setNxBase(e.target.value),
    })),
    React.createElement('label', null, ' 可选：已有 Python ', React.createElement('input', {
      value: pythonExecutable, placeholder: 'python.exe', onChange: e => setPython(e.target.value),
    })),
    React.createElement('button', { style: styles.button, disabled: ['installing', 'starting'].includes(backend.state),
      onClick: () => submit({ action: 'configure', nxBase, ...(pythonExecutable ? { pythonExecutable } : {}) }),
    }, '保存配置并检测'),
    React.createElement('button', { style: styles.button, disabled: ['installing', 'starting', 'ready'].includes(backend.state), onClick: () => {
      if (window.confirm('确认下载并安装独立 Python/OCC 环境？需要联网和磁盘空间；依赖来自 conda-forge。')) {
        void submit({ action: 'install-environment', confirmed: true })
      }
    } }, backend.state === 'installing' ? '正在安装，请等待…' : '确认安装 Python/OCC'),
    error && React.createElement('p', { style: styles.error }, error),
    React.createElement('pre', { style: { whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' } }, (backend.logTail || []).slice(-8).join('\n')),
  )
}
