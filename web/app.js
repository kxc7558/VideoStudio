let mode = 'i2v';
let imageFile = null;
let lastImageFile = null;   // 尾帧（可选，仅 H3 支持首尾帧过渡）
let shots = [];            // 故事模式分镜列表
let pendingQueue = [];     // 待生成清单（方式 B）
let activeTasks = new Map(); // taskId -> {mode, shotIdx, state, progress, queueState, queuePos, msg}

const $ = (id) => document.getElementById(id);

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// ===== Tab 切换 =====
document.querySelectorAll('.tab').forEach(btn => {
  btn.addEventListener('click', () => {
    mode = btn.dataset.mode;
    document.querySelectorAll('.tab').forEach(b => b.classList.toggle('active', b === btn));
    $('panel-i2v').hidden = mode !== 'i2v';
    $('panel-t2v').hidden = mode !== 't2v';
    $('panel-story').hidden = mode !== 'story';
    $('generate-btn').style.display = mode === 'story' ? 'none' : '';
    $('add-queue-btn').style.display = mode === 'story' ? 'none' : '';
  });
});

// ===== 上传图片（图生视频）=====
const dropzone = $('dropzone');
const fileInput = $('image-input');
dropzone.addEventListener('click', () => fileInput.click());
dropzone.addEventListener('dragover', e => { e.preventDefault(); dropzone.classList.add('over'); });
dropzone.addEventListener('dragleave', () => dropzone.classList.remove('over'));
dropzone.addEventListener('drop', e => {
  e.preventDefault();
  dropzone.classList.remove('over');
  if (e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0]);
});
fileInput.addEventListener('change', () => { if (fileInput.files.length) handleFile(fileInput.files[0]); });

function handleFile(f) {
  imageFile = f;
  $('preview').src = URL.createObjectURL(f);
  $('preview').hidden = false;
  $('drop-hint').hidden = true;
}

// ===== 尾帧（可选，H3 首尾帧过渡） =====
const lastZone = $('lastframe-zone');
const lastInput = $('last-image-input');
lastZone.addEventListener('click', () => lastInput.click());
lastZone.addEventListener('dragover', e => { e.preventDefault(); lastZone.classList.add('over'); });
lastZone.addEventListener('dragleave', () => lastZone.classList.remove('over'));
lastZone.addEventListener('drop', e => {
  e.preventDefault();
  lastZone.classList.remove('over');
  if (e.dataTransfer.files.length) handleLastFile(e.dataTransfer.files[0]);
});
lastInput.addEventListener('change', () => { if (lastInput.files.length) handleLastFile(lastInput.files[0]); });

function handleLastFile(f) {
  lastImageFile = f;
  $('last-preview').src = URL.createObjectURL(f);
  $('last-preview').hidden = false;
  $('last-drop-hint').hidden = true;
}

// ===== 提交工具 =====
async function submitGenerate(formData) {
  const r = await fetch('/api/generate', { method: 'POST', body: formData });
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || '生成请求失败');
  return data;
}

function currentConfig() {
  return {
    mode,
    model: $('model').value,
    prompt: mode === 'i2v' ? $('i2v-prompt').value : $('t2v-prompt').value,
    resolution: $('resolution').value,
    duration: $('duration').value,
    steps: $('steps').value,
    segments: $('segments').value,
    image: imageFile,
    lastImage: lastImageFile,
  };
}

function buildForm(cfg) {
  const fd = new FormData();
  fd.append('mode', cfg.mode);
  fd.append('model', cfg.model || 'wan');
  fd.append('prompt', cfg.prompt);
  fd.append('resolution', cfg.resolution);
  fd.append('duration', cfg.duration);
  fd.append('steps', cfg.steps || '20');
  fd.append('segments', cfg.segments || '1');
  if (cfg.mode === 'i2v' && cfg.image) fd.append('image', cfg.image);
  if (cfg.mode === 'i2v' && cfg.lastImage) fd.append('last_image', cfg.lastImage);
  return fd;
}

async function submitTask(formData, meta = {}) {
  try {
    const { task_id } = await submitGenerate(formData);
    activeTasks.set(task_id, {
      mode: meta.mode || formData.get('mode'),
      model: formData.get('model') || 'wan',
      prompt: formData.get('prompt') || '',
      resolution: formData.get('resolution') || '',
      duration: formData.get('duration') || '',
      steps: formData.get('steps') || '',
      shotIdx: meta.shotIdx ?? null,
      state: 'queued', msg: '提交中…', progress: 0, queueState: '', queuePos: 0,
      created: Math.floor(Date.now() / 1000),
    });
    renderTasks();
    return task_id;
  } catch (e) {
    alert('提交失败：' + (e.message || e));
    return null;
  }
}

// 方式 A：点「生成视频」立即提交排队（按钮不锁，可连续提交）
function generate() {
  const cfg = currentConfig();
  if (cfg.mode === 'i2v' && !cfg.image) { alert('请先上传一张图片'); return; }
  if (cfg.mode === 't2v' && !cfg.prompt.trim()) { alert('请填写画面描述'); return; }
  submitTask(buildForm(cfg), { mode: cfg.mode });
}

// 方式 B：先把配置攒进清单，再一起开始
function addToQueue() {
  const cfg = currentConfig();
  if (cfg.mode === 'i2v' && !cfg.image) { alert('请先上传一张图片'); return; }
  if (cfg.mode === 't2v' && !cfg.prompt.trim()) { alert('请填写画面描述'); return; }
  pendingQueue.push(cfg);
  renderQueue();
}

function startAll() {
  if (!pendingQueue.length) { alert('清单是空的'); return; }
  const items = pendingQueue.splice(0, pendingQueue.length);
  items.forEach(cfg => submitTask(buildForm(cfg), { mode: cfg.mode }));
  renderQueue();
}

function clearQueue() { pendingQueue = []; renderQueue(); }

function renderQueue() {
  $('queue-count').textContent = pendingQueue.length;
  $('queue-list').hidden = pendingQueue.length === 0;
  const box = $('queue-items');
  box.innerHTML = '';
  pendingQueue.forEach((cfg, i) => {
    const div = document.createElement('div');
    div.className = 'queue-item';
    const label = (cfg.mode === 'i2v' ? '🖼️图生' : '✍️文生') + ' · ' +
      (cfg.prompt.trim() ? esc(cfg.prompt.trim().slice(0, 24)) : '（默认动效）') + ' · ' +
      cfg.resolution + ' · ' + cfg.duration;
    div.innerHTML = `<span>${i + 1}. ${label}</span><button class="queue-del" data-i="${i}">✕</button>`;
    box.appendChild(div);
  });
  box.querySelectorAll('.queue-del').forEach(b => b.addEventListener('click', () => {
    pendingQueue.splice(Number(b.dataset.i), 1);
    renderQueue();
  }));
}

// ===== 任务队列面板 =====
function statusText(info) {
  if (info.state === 'done') return '✅ 完成';
  if (info.state === 'error') return '❌ 失败';
  if (info.state === 'cancelled') return '🚫 已取消';
  if (info.queueState === 'pending') return `⏳ 排队中（第 ${info.queuePos} 位）`;
  if (info.queueState === 'running') {
    return info.progress > 0 ? `🎨 生成中 ${Math.round(info.progress)}%` : '🎨 生成中（加载模型…）';
  }
  return '📤 提交中…';
}

function fmtTime(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

const groupExpanded = { active: true, done: false, ended: false };

function toggleGroup(key) {
  groupExpanded[key] = !groupExpanded[key];
  renderTasks();
}

function taskCard(taskId, info) {
  const modeLabel = info.mode === 'i2v' ? '🖼️ 图生'
    : info.mode === 't2v' ? '✍️ 文生'
    : info.mode === 'story' ? '📖 故事'
    : info.mode === 'concat' ? '🎬 拼接' : '🎞️ 历史';
  const modelName = info.model === 'h3' ? 'MiniMax H3' : info.model === 'wan' ? 'Wan 2.2' : '';
  const done = info.state === 'done';
  const err = info.state === 'error';
  const cancelled = info.state === 'cancelled';
  const active = info.state === 'queued' || info.state === 'running';
  const prog = Math.max(0, Math.min(100, Number(info.progress) || 0));

  // 信息条：分辨率 / 时长 / 步数 / 时间
  const chips = [];
  if (info.resolution) chips.push(esc(info.resolution));
  if (info.duration) chips.push(esc(info.duration));
  if (active && info.steps) chips.push(esc(String(info.steps)) + ' 步');
  const ts = info.created || info.updated;
  if (ts) chips.push(fmtTime(ts));
  const metaHtml = chips.length ? `<div class="task-meta">${chips.map(c => `<span>${c}</span>`).join('')}</div>` : '';

  const prompt = String(info.prompt || '').trim();
  const promptHtml = prompt ? `<div class="task-prompt">${esc(prompt.slice(0, 40))}${prompt.length > 40 ? '…' : ''}</div>` : '';

  const aiPrompts = Array.isArray(info.ai_prompts) && info.ai_prompts.length
    ? `<div class="task-ai">${info.ai_prompts.map(p =>
        `<div class="task-ai-item">🤖 第 ${esc(String(p.segment))} 段提示词已被 AI 重写：<br>` +
        `<span class="task-ai-orig">原：${esc(p.original || '')}</span><br>` +
        `<span class="task-ai-rewritten">新：${esc(p.rewritten || '')}</span></div>`
      ).join('')}</div>` : '';

  let body;
  if (done) {
    if (info.expanded) {
      body = `<video class="task-video" src="/api/video/${taskId}" controls></video>
              <a class="task-dl" href="/api/video/${taskId}" download="${taskId}.mp4">⬇️ 下载</a>`;
    } else {
      body = `<div class="task-row">
                <button class="task-preview" onclick="togglePreview('${taskId}')">▶ 预览</button>
                <a class="task-dl" href="/api/video/${taskId}" download="${taskId}.mp4">⬇️ 下载</a>
              </div>`;
    }
  } else if (err || cancelled) {
    body = `<div class="task-msg">${esc(info.msg || (cancelled ? '已取消' : '未知错误'))}</div>`;
  } else {
    const runningMsg = String(info.msg || '');
    const msgHtml = runningMsg ? `<div class="task-msg" style="color:var(--muted)">${esc(runningMsg)}</div>` : '';
    body = `${msgHtml}<div class="bar"><div class="bar-fill" style="width:${prog}%"></div></div>`;
  }

  const cancelBtn = active ? `<button class="task-cancel" onclick="cancelTask('${taskId}')">✕ 取消</button>` : '';
  return `<div class="task-card">
    <div class="task-head">
      <span class="task-mode">${modeLabel}${modelName ? ' · ' + modelName : ''}</span>
      <span class="task-id">${taskId}</span>
      <span class="task-status ${done ? 'ok' : (err ? 'bad' : '')}">${statusText(info)}</span>
      ${cancelBtn}
    </div>
    ${metaHtml}
    ${promptHtml}
    ${aiPrompts}
    ${body}
  </div>`;
}

function renderTasks() {
  const list = $('task-list');
  if (!activeTasks.size) {
    list.innerHTML = '<div class="task-empty">还没有任务。点上面的「✨ 生成视频」直接排队，或「➕ 加入清单」攒几条一起开始。</div>';
    return;
  }
  const groups = { active: [], done: [], ended: [] };
  for (const [taskId, info] of activeTasks) {
    if (info.state === 'done') groups.done.push([taskId, info]);
    else if (info.state === 'error' || info.state === 'cancelled') groups.ended.push([taskId, info]);
    else groups.active.push([taskId, info]);
  }
  groups.done.reverse();   // 已完成：新的在前
  groups.ended.reverse();  // 已取消/失败：新的在前

  const meta = {
    active: { title: '⚡ 进行中' },
    done: { title: '✅ 已完成' },
    ended: { title: '🚫 已取消 / 失败' },
  };
  list.innerHTML = '';
  for (const key of ['active', 'done', 'ended']) {
    const items = groups[key];
    if (!items.length) continue;
    const open = groupExpanded[key];
    const g = document.createElement('div');
    g.className = 'task-group';
    g.innerHTML = `
      <div class="group-head" onclick="toggleGroup('${key}')">
        <span class="group-caret">${open ? '▾' : '▸'}</span>
        <span class="group-title">${meta[key].title}</span>
        <span class="group-count">${items.length}</span>
      </div>
      <div class="group-body" ${open ? '' : 'hidden'}>
        ${items.map(([id, info]) => taskCard(id, info)).join('')}
      </div>`;
    list.appendChild(g);
  }
}

function togglePreview(taskId) {
  const info = activeTasks.get(taskId);
  if (!info) return;
  info.expanded = !info.expanded;
  renderTasks();
}

async function cancelTask(taskId) {
  try {
    const r = await fetch('/api/cancel/' + taskId, { method: 'POST' });
    const data = await r.json();
    if (!r.ok) { alert(data.error || '取消失败'); return; }
    const info = activeTasks.get(taskId);
    if (info) { info.state = 'cancelled'; info.msg = '已取消'; }
    renderTasks();
  } catch (e) {
    alert('取消失败：' + (e.message || e));
  }
}

async function pollTasks() {
  let changed = false;
  for (const [taskId, info] of activeTasks) {
    if (info.state === 'done' || info.state === 'error' || info.state === 'cancelled') continue;
    try {
      const r = await fetch('/api/status/' + taskId);
      if (!r.ok) continue;
      const t = await r.json();
      info.state = t.state;
      info.msg = t.msg;
      info.progress = t.progress || 0;
      info.queueState = t.queue_state || '';
      info.queuePos = t.queue_pos || 0;
      if (t.ai_prompts) info.ai_prompts = t.ai_prompts;
      if (t.state === 'done') {
        if (info.shotIdx != null) onShotDone(info.shotIdx, taskId);
      } else if (t.state === 'error') {
        if (info.shotIdx != null) onShotError(info.shotIdx);
      }
      changed = true;
    } catch (e) { /* 网络抖动忽略 */ }
  }
  if (changed) renderTasks();
}

// ===== 故事模式 =====
$('split-btn').addEventListener('click', splitStory);

async function splitStory() {
  const story = $('story-input').value.trim();
  if (!story) { alert('请先粘贴一段故事'); return; }
  $('split-btn').disabled = true;
  $('split-btn').textContent = '拆解中…';
  try {
    const fd = new FormData();
    fd.append('story', story);
    fd.append('n_shots', $('n-shots').value);
    const r = await fetch('/api/storyboard', { method: 'POST', body: fd });
    const data = await r.json();
    if (!r.ok) { alert(data.error || '拆解失败'); return; }
    shots = data.shots.map(s => ({ ...s, currentTask: null, selectedTask: null, videoUrl: null, generating: false }));
    renderShots();
    $('concat-area').hidden = true;
  } catch (e) {
    alert('拆解失败：' + e);
  } finally {
    $('split-btn').disabled = false;
    $('split-btn').textContent = '📋 拆成剧本';
  }
}

function renderShots() {
  const list = $('shot-list');
  list.innerHTML = '';
  shots.forEach((shot, idx) => {
    const card = document.createElement('div');
    card.className = 'shot-card';
    card.innerHTML = `
      <div class="shot-head">
        <span class="shot-no">镜头 ${shot.id}</span>
        <span class="shot-scene">${esc(shot.scene)}</span>
      </div>
      <div class="shot-narr">🎙 ${esc(shot.narration)}</div>
      <label>画面提示词（可改）</label>
      <textarea class="shot-prompt" rows="2">${esc(shot.prompt)}</textarea>
      <div class="shot-video" ${shot.videoUrl ? '' : 'hidden'}>
        <video src="${shot.videoUrl}" controls></video>
      </div>
      <div class="shot-actions">
        <button class="draw-btn" data-idx="${idx}" ${shot.generating ? 'disabled' : ''}>${shot.generating ? '生成中…' : (shot.videoUrl ? '🔄 再抽一张' : '🎲 抽一张')}</button>
        <button class="lock-btn" data-idx="${idx}" ${shot.videoUrl ? '' : 'disabled'}>${shot.selectedTask ? '✅ 已锁定' : '🔒 锁定此片'}</button>
      </div>
    `;
    card.querySelector('.shot-prompt').addEventListener('input', e => { shot.prompt = e.target.value; });
    card.querySelector('.draw-btn').addEventListener('click', () => drawShot(idx));
    card.querySelector('.lock-btn').addEventListener('click', () => lockShot(idx));
    list.appendChild(card);
  });
  updateConcatArea();
}

async function drawShot(idx) {
  const shot = shots[idx];
  if (shot.generating) return;
  shot.generating = true;
  renderShots();
  const fd = new FormData();
  fd.append('mode', 't2v');
  fd.append('model', $('model').value);
  fd.append('prompt', shot.prompt);
  fd.append('resolution', $('resolution').value);
  fd.append('duration', $('duration').value);
  fd.append('steps', $('steps').value);
  const taskId = await submitTask(fd, { mode: 't2v', shotIdx: idx });
  if (!taskId) { shot.generating = false; renderShots(); }
}

function onShotDone(idx, taskId) {
  const shot = shots[idx];
  if (!shot) return;
  shot.videoUrl = '/api/video/' + taskId;
  shot.currentTask = taskId;
  shot.generating = false;
  renderShots();
}

function onShotError(idx) {
  const shot = shots[idx];
  if (!shot) return;
  shot.generating = false;
  renderShots();
  alert('镜头 ' + shot.id + ' 生成失败，请重试');
}

function lockShot(idx) {
  const shot = shots[idx];
  if (!shot.currentTask) return;
  shot.selectedTask = shot.currentTask;
  renderShots();
}

function updateConcatArea() {
  const locked = shots.filter(s => s.selectedTask).length;
  const allLocked = shots.length > 0 && locked === shots.length;
  $('concat-area').hidden = !allLocked;
  // 连续成片：只要有 2 个以上镜头就能用（直接用各镜头提示词接续，无需先逐个生成）
  $('chain-area').hidden = shots.length < 2;
}

$('concat-btn').addEventListener('click', concatAll);
$('chain-btn').addEventListener('click', chainStory);

async function concatAll() {
  const ids = shots.map(s => s.selectedTask).filter(Boolean);
  if (ids.length < 2) { alert('请先锁定至少 2 个镜头'); return; }
  $('concat-btn').disabled = true;
  $('concat-btn').textContent = '拼接中…';
  try {
    const fd = new FormData();
    fd.append('task_ids', ids.join(','));
    const r = await fetch('/api/concat', { method: 'POST', body: fd });
    const data = await r.json();
    if (!r.ok) { alert(data.error || '拼接失败'); return; }
    $('story-result').innerHTML = `
      <video src="/api/video/${data.task_id}" controls></video>
      <a class="download" href="/api/video/${data.task_id}" download="${data.task_id}.mp4">⬇️ 下载成片</a>`;
  } catch (e) {
    alert('拼接失败：' + e);
  } finally {
    $('concat-btn').disabled = false;
    $('concat-btn').textContent = '🎬 拼接成片';
  }
}

async function chainStory() {
  const prompts = shots.map(s => (s.prompt || '').trim()).filter(Boolean);
  if (prompts.length < 2) { alert('至少需要 2 个镜头才能连续成片'); return; }
  $('chain-btn').disabled = true;
  $('chain-btn').textContent = '已提交，生成中…';
  try {
    const fd = new FormData();
    fd.append('model', $('model').value);
    fd.append('prompts', prompts.join('\n'));
    fd.append('resolution', $('resolution').value);
    fd.append('duration', $('duration').value);
    fd.append('steps', $('steps').value);
    const r = await fetch('/api/story-long', { method: 'POST', body: fd });
    const data = await r.json();
    if (!r.ok) { alert(data.error || '连续成片失败'); return; }
    activeTasks.set(data.task_id, {
      mode: 'story', model: $('model').value,
      prompt: prompts.slice(0, 3).join('、') + (prompts.length > 3 ? '…' : ''),
      resolution: $('resolution').value, duration: $('duration').value, steps: $('steps').value,
      state: 'queued', msg: '排队中…', progress: 0, queueState: '', queuePos: 0,
      created: Math.floor(Date.now() / 1000),
    });
    renderTasks();
    $('chain-result').innerHTML = `<div class="task-msg" style="color:var(--muted)">已提交「连续成片」，进度见下方任务队列（ID：${data.task_id}）</div>`;
  } catch (e) {
    alert('连续成片失败：' + e);
  } finally {
    $('chain-btn').disabled = false;
    $('chain-btn').textContent = '🎬 连续成片（首尾帧接续，一条长视频）';
  }
}

// ===== 健康检查 + 历史恢复 =====
async function checkHealth() {
  try {
    const r = await fetch('/api/health');
    const h = await r.json();
    setDot($('engine-status'), h.comfy ? 'ok' : 'bad', h.comfy ? '引擎就绪' : '引擎未启动');
    setDot($('t2v-status'), h.t2v_ready ? 'ok' : 'warn', h.t2v_ready ? '文生视频可用' : '文生视频模型下载中');
    setDot($('h3-status'), h.h3_ready ? 'ok' : 'warn', h.h3_ready ? 'MiniMax H3 可用' : 'MiniMax H3 未就绪');
  } catch (e) {
    setDot($('engine-status'), 'bad', '无法连接服务');
  }
}
function setDot(el, cls, text) {
  el.className = 'dot ' + cls;
  el.textContent = text;
}

// 启动时恢复磁盘上已完成的视频（重启不丢），展示在任务队列面板
async function loadTasks() {
  try {
    const r = await fetch('/api/tasks');
    const data = await r.json();
    (data.tasks || []).forEach(t => {
      if (!activeTasks.has(t.task_id)) {
        activeTasks.set(t.task_id, {
          mode: t.mode, model: t.model, prompt: t.prompt,
          ai_prompts: t.ai_prompts,
          resolution: t.resolution, duration: t.duration, steps: t.steps, seed: t.seed,
          state: t.state, msg: t.msg, video: t.video,
          progress: t.state === 'done' ? 100 : 0,
          queueState: t.state, queuePos: 0,
          created: t.created, updated: t.updated,
        });
      }
    });
    renderTasks();
  } catch (e) { /* 忽略 */ }
}

// ===== 绑定 + 初始化 =====
$('generate-btn').addEventListener('click', generate);
$('add-queue-btn').addEventListener('click', addToQueue);
$('start-all-btn').addEventListener('click', startAll);
$('clear-queue-btn').addEventListener('click', clearQueue);

// 尾帧入口只有 MiniMax H3 支持，切到 Wan 时隐藏
function syncLastFrameZone() {
  $('lastframe-zone').hidden = $('model').value !== 'h3';
}
$('model').addEventListener('change', syncLastFrameZone);
syncLastFrameZone();

checkHealth();
loadTasks();
setInterval(checkHealth, 15000);
setInterval(pollTasks, 2000);
