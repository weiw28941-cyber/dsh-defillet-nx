import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  API,
  STATUS_API,
  acceptCandidate,
  detectFillets,
  getEvents,
  getJob,
  getMesh,
  getNxRuntimeStatus,
  getRevisions,
  recoverJob,
  rejectCandidate,
  setSelection,
  uploadJob,
} from './api.js'
import { ModelViewer } from './ModelViewer.js'
import { SetupPanel } from './SetupPanel.js'
import { extractMappedStepFaceIds, parseMappingJson } from './mapping.js'
import { colors, styles } from './styles.js'

const ALLOWED_PRESETS = new Set(['defeature-agent', 'defeature-agent-dev'])
const WORKBENCH_CACHE = new Map()

function messageText(node) {
  if (node.kind === 'user' || node.kind === 'steering') return (node.content || []).filter(x => x.type === 'text').map(x => x.text).join('\n');
  if (node.kind === 'assistant') {
    return (node.blocks || []).filter(block => {
      const kind = block.kind || block.type;
      return kind === 'text' || kind === 'reasoning';
    }).map(block => {
      const kind = block.kind || block.type;
      return kind === 'reasoning' ? `[分析摘要]\n${block.text}` : block.text;
    }).join('\n');
  }
  return '';
}

function partialMessageText(node) {
  return (node?.blocks || []).filter(block => {
    const kind = block.kind || block.type;
    return kind === 'text' || kind === 'reasoning';
  }).map(block => {
    const kind = block.kind || block.type;
    return kind === 'reasoning' ? `[分析摘要]\n${block.text}` : block.text;
  }).join('\n');
}

export function CadWorkbench({
  sessionId,
  useSessions,
  useSession,
  useInput,
  inputActions
}) {
  const cached = WORKBENCH_CACHE.get(sessionId) || {},
    preset = useSessions(x => x.byId[sessionId]?.agentPreset),
    conversationNodes = useSession(x => x.chat?.legacy?.nodes || x.nodes || []),
    partial = useSession(x => x.partial),
    running = useSession(x => x.running),
    draft = useInput(x => x.draft),
    storageKey = `defeature.job.${sessionId}`;
  const [backend, setBackend] = useState({
      state: 'starting'
    }),
    [job, setJob] = useState(cached.job || null),
    [accepted, setAccepted] = useState(cached.accepted || null),
    [candidate, setCandidate] = useState(cached.candidate || null),
    [revisions, setRevisions] = useState(cached.revisions || []),
    [events, setEvents] = useState(cached.events || []),
    [selected, setSelected] = useState(() => new Map(cached.selected || [])),
    [mappedIds, setMappedIds] = useState(() => new Set(cached.mappedIds || [])),
    [mappingInfo, setMappingInfo] = useState(cached.mappingInfo || null),
    [filletInfo, setFilletInfo] = useState(cached.filletInfo || null),
    [nxRuntime, setNxRuntime] = useState(cached.nxRuntime || null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(''),
    [showEdges, setShowEdges] = useState(true),
    [opacity, setOpacity] = useState(1),
    [viewToken, setViewToken] = useState({
      mode: 'iso',
      n: 0
    });
  const lastSeq = useRef(cached.lastSeq || 0),
    requestEpoch = useRef(0),
    selectionEpoch = useRef(0),
    selectionAbort = useRef(null),
    chatRef = useRef(null),
    selectedIds = useMemo(() => new Set(selected.keys()), [selected]),
    emptyIds = useMemo(() => new Set(), []),
    chatMessages = useMemo(() => {
      const rows = conversationNodes.filter(x => x.kind === 'user' || x.kind === 'steering' || x.kind === 'assistant').map(x => ({
        key: `${x.kind}:${x.seq}`,
        role: x.kind === 'assistant' ? 'Agent' : '你',
        text: messageText(x)
      })).filter(x => x.text);
      if (partial) {
        const text = partialMessageText(partial);
        if (text) rows.push({
          key: 'partial',
          role: 'Agent',
          text
        });
      }
      return rows.slice(-12);
    }, [conversationNodes, partial]);
  useEffect(() => {
    if (!ALLOWED_PRESETS.has(preset)) return;
    let active = true;
    const check = () => fetch(STATUS_API).then(async r => {
      const data = await r.json();
      if (active) setBackend(data);
    }).catch(e => {
      if (active) setBackend({
        state: 'failed',
        error: String(e)
      });
    });
    check();
    const timer = setInterval(check, 3000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [preset]);
  const refresh = useCallback(async (id, known, epoch = requestEpoch.current) => {
    const next = known || (await getJob(id));
    const acceptedId = next.accepted_revision || next.current_revision,
      candidateId = next.candidate_revision,
      [a, c, rs, es] = await Promise.all([
        acceptedId ? getMesh(id, acceptedId) : null,
        candidateId ? getMesh(id, candidateId) : null,
        getRevisions(id),
        getEvents(id, lastSeq.current)
      ]);
    if (requestEpoch.current !== epoch) return false;
    setJob(next);
    setAccepted(old => {
      if (old?.revisionId && old.revisionId !== a?.revisionId) {
        setSelected(new Map());
        setMappedIds(new Set());
      }
      return a;
    });
    setCandidate(c);
    setRevisions(Array.isArray(rs) ? rs : rs.revisions || []);
    if (es.length) {
      lastSeq.current = es.at(-1).seq;
      setEvents(old => [...old, ...es]);
    }
    return true;
  }, []);
  useEffect(() => {
    const epoch = ++requestEpoch.current;
    const saved = WORKBENCH_CACHE.get(sessionId);
    if (!saved) {
      setJob(null);
      setAccepted(null);
      setCandidate(null);
      setRevisions([]);
      setEvents([]);
      setSelected(new Map());
      setMappedIds(new Set());
      setMappingInfo(null);
      setFilletInfo(null);
      lastSeq.current = 0;
    }
    setError('');
    if (!ALLOWED_PRESETS.has(preset)) {
      setBusy(false);
      return;
    }
    let cancelled = false;
    (async () => {
      setBusy(true);
      try {
        const linked = await recoverJob(sessionId);
        if (linked && !cancelled && requestEpoch.current === epoch) {
          localStorage.setItem(storageKey, linked.id);
          const unchanged = saved &&
            saved.job?.id === linked.id &&
            saved.job?.accepted_revision === linked.accepted_revision &&
            saved.job?.candidate_revision === linked.candidate_revision &&
            saved.job?.status === linked.status;
          if (!unchanged) await refresh(linked.id, linked);
        }
      } catch (e) {
        if (!String(e).includes('404') && !cancelled) setError(String(e));
      } finally {
        if (!cancelled && requestEpoch.current === epoch) setBusy(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [preset, sessionId, storageKey, refresh]);
  useEffect(() => {
    if (!job?.id || !busy) return;
    const id = job.id,
      epoch = requestEpoch.current,
      timer = setInterval(() => void refresh(id, undefined, epoch).catch(() => {}), 700);
    return () => clearInterval(timer);
  }, [job?.id, busy, refresh]);
  useEffect(() => {
    if (preset !== 'defeature-agent-dev' || !job?.id) {
      setNxRuntime(null);
      return;
    }
    let active = true;
    const check = () => getNxRuntimeStatus(sessionId).then(value => {
      if (active) setNxRuntime(value);
    }).catch(() => {
      if (active) setNxRuntime({ connected: false, status: 'offline', runtime_state: 0 });
    });
    void check();
    const timer = setInterval(check, 1500);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [preset, sessionId, job?.id]);
  useEffect(() => {
    WORKBENCH_CACHE.set(sessionId, {
      job,
      accepted,
      candidate,
      revisions,
      events,
      selected: [...selected],
      mappedIds: [...mappedIds],
      mappingInfo,
      filletInfo,
      nxRuntime,
      lastSeq: lastSeq.current
    });
  }, [sessionId, job, accepted, candidate, revisions, events, selected, mappedIds, mappingInfo, filletInfo, nxRuntime]);
  useEffect(() => {
    if (!job?.id || !accepted?.revisionId) return;
    const epoch = ++selectionEpoch.current,
      controller = new AbortController();
    selectionAbort.current?.abort();
    selectionAbort.current = controller;
    void setSelection(sessionId, accepted.revisionId, [...selected.values()], controller.signal).catch(e => {
      if (epoch === selectionEpoch.current && e?.name !== 'AbortError') {
        setError(`同步已选面失败：${e instanceof Error ? e.message : String(e)}`);
      }
    });
    return () => controller.abort();
  }, [sessionId, job?.id, accepted?.revisionId, selected]);
  useEffect(() => {
    const node = chatRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [chatMessages]);
  const upload = async file => {
      if (!file) return;
      const epoch = ++requestEpoch.current;
      setBusy(true);
      setError('');
      setJob(null);
      setAccepted(null);
      setSelected(new Map());
      setMappedIds(new Set());
      setMappingInfo(null);
      setFilletInfo(null);
      setCandidate(null);
      setEvents([]);
      lastSeq.current = 0;
      try {
        const created = await uploadJob(sessionId, file),
          id = created.jobId || created.id;
        if (requestEpoch.current !== epoch) return;
        localStorage.setItem(storageKey, id);
        await refresh(id);
      } catch (e) {
        if (requestEpoch.current === epoch) setError(String(e));
      } finally {
        if (requestEpoch.current === epoch) setBusy(false);
      }
    },
    detectAll = async () => {
      if (!job || !accepted) return;
      if (filletInfo) {
        setSelected(old => {
          const next = new Map(old);
          for (const face of accepted.faces || []) if (mappedIds.has(face.stepEntityId)) next.delete(face.faceId);
          return next;
        });
        setMappedIds(new Set());
        setFilletInfo(null);
        return;
      }
      setBusy(true);
      setError('');
      try {
        const result = await detectFillets(job.id, job.accepted_revision || job.current_revision),
          ids = new Set(result.step_entity_ids || []),
          faces = (accepted.faces || []).filter(face => ids.has(face.stepEntityId));
        setMappedIds(ids);
        setSelected(old => {
          const next = new Map(old);
          for (const face of faces) next.set(face.faceId, face);
          return next;
        });
        setMappingInfo(null);
        setFilletInfo({
          count: faces.length,
          engine: result.engine
        });
      } catch (e) {
        setError(`NX 圆角识别：${e instanceof Error ? e.message : String(e)}`);
      } finally {
        setBusy(false);
      }
    },
    uploadMapping = async file => {
      if (!file) return;
      setError('');
      try {
        const payload = parseMappingJson(await file.text()),
          ids = extractMappedStepFaceIds(payload),
          available = new Set((accepted?.faces || []).map(face => face.stepEntityId)),
          matched = ids.filter(id => available.has(id));
        setMappedIds(new Set(matched));
        setSelected(new Map((accepted?.faces || []).filter(face => matched.includes(face.stepEntityId)).map(face => [face.faceId, face])));
        setMappingInfo({
          name: file.name,
          total: ids.length,
          matched: matched.length,
          missing: ids.filter(id => !available.has(id))
        });
      } catch (e) {
        setMappedIds(new Set());
        setMappingInfo(null);
        setError(`Mapping JSON：${e instanceof Error ? e.message : String(e)}`);
      }
    },
    toggleAllMapped = () => setSelected(old => {
      const faces = (accepted?.faces || []).filter(face => mappedIds.has(face.stepEntityId)),
        allSelected = faces.length > 0 && faces.every(face => old.has(face.faceId)),
        next = new Map(old);
      for (const face of faces) allSelected ? next.delete(face.faceId) : next.set(face.faceId, face);
      return next;
    }),
    deselectMapped = face => setSelected(old => {
      const next = new Map(old);
      next.delete(face.faceId);
      return next;
    }),
    toggle = face => setSelected(old => {
      const next = new Map(old);
      next.has(face.faceId) ? next.delete(face.faceId) : next.set(face.faceId, face);
      return next;
    }),
    sendChat = () => {
      if (!draft.trim() || running) return;
      inputActions.submit();
    },
    decide = async action => {
      if (!job) return;
      setBusy(true);
      setError('');
      try {
        const next = action === 'accept' ? await acceptCandidate(job.id) : await rejectCandidate(job.id);
        if (action === 'accept') setSelected(new Map());
        await refresh(job.id, next);
      } catch (e) {
        setError(String(e));
      } finally {
        setBusy(false);
      }
    };
  const allMappedSelected = (accepted?.faces || [])
    .filter(face => mappedIds.has(face.stepEntityId))
    .every(face => selected.has(face.faceId));
  const mappingSummary = mappingInfo &&
    `${mappingInfo.name}: ${mappingInfo.matched}/${mappingInfo.total} 面已标记${
      mappingInfo.missing.length ? `，${mappingInfo.missing.length} 未匹配` : ''
    }`;
  if (!ALLOWED_PRESETS.has(preset)) return React.createElement('main', {
    style: styles.root
  }, React.createElement('div', {
    style: styles.unavailable
  }, React.createElement('div', null, React.createElement('span', {
    style: styles.badge
  }, 'DEFEATURE AGENT ONLY'), React.createElement('h2', null, 'CAD 工作台未在此会话启用'), React.createElement('p', {
    style: styles.lead
  }, '请新建使用 Defeature Agent 或 Defeature Agent Dev preset 的会话。'))));
  return React.createElement('main', {
    style: styles.root
  }, React.createElement('div', {
    style: styles.frame
  }, React.createElement('header', {
    style: styles.header
  }, React.createElement('div', null, React.createElement('h1', {
    style: styles.heading
  }, 'Defeature CAD 工作台'), React.createElement('p', {
    style: styles.lead
  }, `会话 ${sessionId} · ${preset}`)), React.createElement('div', {
    style: styles.toolbar
  }, React.createElement('span', {
    style: styles.badge
  }, 'PHASE 3–5'), React.createElement('label', {
    style: {
      ...styles.button,
      ...styles.primary,
      ...(busy ? styles.disabled : {})
    }
  }, busy ? '处理中…' : '导入 STEP', React.createElement('input', {
    style: styles.file,
    type: 'file',
    accept: '.stp,.step',
    disabled: busy || backend.state !== 'ready',
    onChange: e => {
      const file = e.target.files?.[0];
      e.target.value = '';
      upload(file);
    }
  })), accepted && React.createElement('button', {
    style: {
      ...styles.button,
      ...(busy ? styles.disabled : {})
    },
    disabled: busy,
    onClick: detectAll
  }, busy ? 'NX 识别中…' : filletInfo ? '取消全部圆角标注' : 'NX 标注所有圆角'), accepted && React.createElement('label', {
    style: styles.button
  }, '导入面映射 JSON', React.createElement('input', {
    style: styles.file,
    type: 'file',
    accept: '.json,application/json',
    onChange: e => {
      uploadMapping(e.target.files?.[0]);
      e.target.value = '';
    }
  })), mappingInfo && React.createElement('button', {
    style: styles.button,
    onClick: toggleAllMapped
  }, `${allMappedSelected ? '取消' : '标注'}全部 JSON 面`), mappingInfo && React.createElement('button', {
    style: styles.button,
    onClick: () => {
      setMappedIds(new Set());
      setMappingInfo(null);
    }
  }, `清除映射 (${mappingInfo.matched})`), job && React.createElement('a', {
    style: {
      ...styles.button,
      textDecoration: 'none'
    },
    href: `${API}/jobs/${job.id}/revisions/${job.accepted_revision || job.current_revision}/step`
  }, '下载 Accepted'))), React.createElement('div', {
    style: styles.status
  }, React.createElement('b', {
    style: {
      color: backend.state === 'ready' ? '#61d095' : backend.state === 'failed' ? colors.danger : colors.hover
    }
  }, `后端 ${backend.state || 'unknown'}`), backend.error && React.createElement('span', {
    style: styles.error
  }, backend.error),
  React.createElement('b', null, job?.status || '等待上传'),
  job && React.createElement('code', null, job.id.slice(0, 10)),
  job?.accepted_revision && React.createElement('span', null, `Accepted ${job.accepted_revision}`),
  job?.candidate_revision && React.createElement('span', {
    style: {
      color: colors.hover
    }
  }, `Candidate ${job.candidate_revision}`), preset === 'defeature-agent-dev' && React.createElement('span', {
    style: {
      color: nxRuntime?.connected ? '#61d095' : colors.muted
    }
  }, `NX Runtime ${nxRuntime?.status || 'offline'} · state ${nxRuntime?.runtime_state ?? 0}${nxRuntime?.worker_pid ? ` · PID ${nxRuntime.worker_pid}` : ''}`), filletInfo && React.createElement('span', {
    style: {
      color: colors.mapped
    }
  }, `NX 已标注 ${filletInfo.count} 个圆角面`), mappingInfo && React.createElement('span', {
    style: {
      color: colors.mapped
    }
  }, mappingSummary), React.createElement('span', {
    style: {
      color: colors.selected
    }
  }, '■ 手动选择'), React.createElement('span', {
    style: {
      color: colors.mapped
    }
  }, '■ JSON 面选中（右键取消）'), error && React.createElement('strong', {
    style: styles.error
    }, error)), React.createElement(SetupPanel, { backend, onUpdate: setBackend }), React.createElement('div', {
    style: styles.toolbar
  }, ['iso', 'front', 'right', 'top'].map(mode => React.createElement('button', {
    key: mode,
    style: styles.button,
    onClick: () => setViewToken(x => ({
      mode,
      n: x.n + 1
    }))
  }, mode)), React.createElement('button', {
    style: styles.button,
    onClick: () => setShowEdges(x => !x)
  }, showEdges ? '隐藏边线' : '显示边线'), React.createElement('label', {
    style: {
      ...styles.muted,
      fontSize: 12
    }
  }, '透明度 ', React.createElement('input', {
    type: 'range',
    min: .25,
    max: 1,
    step: .05,
    value: opacity,
    onChange: e => setOpacity(Number(e.target.value))
  }))), React.createElement('div', {
    style: styles.grid
  }, React.createElement(ModelViewer, {
    mesh: accepted,
    selected: selectedIds,
    mapped: mappedIds,
    onToggle: toggle,
    onMappedDeselect: deselectMapped,
    title: 'Accepted · 点击选择目标面',
    showEdges,
    opacity,
    viewToken,
    cacheKey: `${sessionId}:accepted:${accepted?.revisionId || ''}`
  }), React.createElement(ModelViewer, {
    mesh: candidate,
    selected: emptyIds,
    title: 'Candidate · 待审核结果',
    showEdges,
    opacity: 1,
    viewToken,
    cacheKey: `${sessionId}:candidate:${candidate?.revisionId || ''}`
  })), React.createElement('div', {
    style: styles.lower
  }, React.createElement('section', {
    style: styles.panel
  }, React.createElement('h2', {
    style: styles.title
  }, `已选面 (${selected.size})`), selected.size === 0 ? React.createElement('div', {
    style: styles.empty
  }, '在 Accepted 模型中点击一个或多个 BREP 面。') : [...selected.values()].map(face => React.createElement('div', {
    key: face.faceId,
    style: styles.row
  }, React.createElement('span', null, `${face.stepEntity || 'unmapped'} · ${face.surfaceType}`, React.createElement('div', {
    style: styles.muted
  }, `${face.freecadFace} · area ${Number(face.area).toFixed(2)}`)), React.createElement('button', {
    style: styles.button,
    onClick: () => toggle(face)
  }, '移除'))), selected.size > 0 && React.createElement('button', {
    style: {
      ...styles.button,
      width: '100%'
    },
    onClick: () => setSelected(new Map())
  }, '清空选择')), React.createElement('section', {
    style: styles.panel
  }, React.createElement('h2', {
    style: styles.title
  }, '操作与 Revision'), React.createElement('div', {
    style: {
      ...styles.muted,
      marginBottom: 9
    }
  }, '这里只展示 CAD 执行记录和 Revision 状态。请在右侧 CAD Agent 对话中输入操作要求。'), React.createElement('div', {
    style: {
      ...styles.toolbar,
      marginTop: 9
    }
  }, React.createElement('button', {
    style: {
      ...styles.button,
      ...(!candidate ? styles.disabled : {})
    },
    disabled: busy || !candidate,
    onClick: () => decide('accept')
  }, '接受 Candidate'), React.createElement('button', {
    style: {
      ...styles.button,
      ...styles.danger,
      ...(!candidate ? styles.disabled : {})
    },
    disabled: busy || !candidate,
    onClick: () => decide('reject')
  }, '拒绝 Candidate'), candidate && React.createElement('a', {
    style: {
      ...styles.button,
      textDecoration: 'none'
    },
    href: `${API}/jobs/${job.id}/revisions/${job.candidate_revision}/step`
  }, '下载 Candidate')), React.createElement('div', {
    style: {
      marginTop: 10
    }
  }, revisions.length ? revisions.map(rev => React.createElement('div', {
    key: rev.revision_id || rev.id,
    style: styles.rev
  },
  React.createElement('b', null, rev.revision_id || rev.id),
  ' · ',
  rev.status || 'unknown',
  rev.parent_revision && ` · from ${rev.parent_revision}`
  )) : React.createElement('div', {
    style: styles.empty
  }, '暂无 Revision'))), React.createElement('section', {
    style: styles.panel
  }, React.createElement('h2', {
    style: styles.title
  }, 'CAD Agent 对话'), React.createElement('div', {
    ref: chatRef,
    style: {
      height: 180,
      overflow: 'auto',
      display: 'flex',
      flexDirection: 'column',
      gap: 8,
      marginBottom: 9
    }
  }, chatMessages.length ? chatMessages.map(message => React.createElement('div', {
    key: message.key,
    style: {
      padding: '7px 9px',
      borderRadius: 8,
      background: message.role === 'Agent' ? '#111923' : '#20303d',
      fontSize: 12,
      whiteSpace: 'pre-wrap'
    }
  }, React.createElement('b', {
    style: {
      color: message.role === 'Agent' ? '#74b9ff' : '#61d095'
    }
  }, message.role), React.createElement('div', {
    style: {
      marginTop: 4
    }
  }, message.text))) : React.createElement('div', {
    style: styles.empty
  }, '暂无对话内容')), React.createElement('textarea', {
    style: styles.input,
    rows: 3,
    value: draft,
    placeholder: '在这里向 CAD Agent 输入操作要求…',
    onChange: e => inputActions.setDraft(e.target.value),
    onKeyDown: e => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendChat();
      }
    }
  }), React.createElement('button', {
    style: {
      ...styles.button,
      ...styles.primary,
      ...(!draft.trim() || running ? styles.disabled : {}),
      marginTop: 8
    },
    disabled: !draft.trim() || running,
    onClick: sendChat
  }, running ? 'Agent 正在处理…' : '发送')), React.createElement('section', {
    style: styles.panel
  }, React.createElement('h2', {
    style: styles.title
  }, '任务时间线'), React.createElement('pre', {
    style: styles.log
  }, events.length ? events.map(e => `[${new Date(e.created_at).toLocaleTimeString('zh-CN', {
    hour12: false
  })}] [${e.kind}] ${e.message}`).join('\n') : '等待任务事件…')))));
}
