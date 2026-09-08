let mode = 'i2v';
let imageFile = null;
let lastImageFile = null;   // 尾帧（可选，仅 H3 支持首尾帧过渡）
let nsfwImageFile = null;   // 无审查区首帧图（可选，无则文生）
let shots = [];            // 故事模式分镜列表
let nsfwShots = [];        // 无审查区拆分镜列表
let pendingQueue = [];     // 待生成清单（方式 B）
let activeTasks = new Map(); // taskId -> {mode, shotIdx, state, progress, queueState, queuePos, msg}
let creativeProfiles = { scenes: [], characters: [] };
let selectedCharacterIds = new Set();

const $ = (id) => document.getElementById(id);

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

const profileId = () => (crypto.randomUUID ? crypto.randomUUID() : `profile_${Date.now()}_${Math.random().toString(16).slice(2)}`);
const fieldValues = (prefix, fields) => Object.fromEntries(fields.map(k => [k, $(prefix + k).value.trim()]));

function selectedCreative() {
  return { sceneId: $('scene-select').value, characterIds: [...selectedCharacterIds] };
}

function fillProfile(prefix, item, fields) {
  fields.forEach(k => { $(prefix + k).value = item?.[k] || ''; });
}

function upsertProfile(kind, prefix, fields) {
  const select = $(kind === 'scenes' ? 'scene-select' : 'character-select');
  const values = fieldValues(prefix, fields);
  if (!values.name) return null;
  let item = creativeProfiles[kind].find(x => x.id === select.value);
  if (!item) {
    item = { id: profileId() };
    creativeProfiles[kind].push(item);
  }
  Object.assign(item, values);
  select.value = item.id;
  return item;
}

function renderCreativeProfiles() {
  const sceneValue = $('scene-select').value;
  const characterValue = $('character-select').value;
  $('scene-select').innerHTML = '<option value="">不使用场景预设</option>' + creativeProfiles.scenes.map(x => `<option value="${esc(x.id)}">${esc(x.name)}</option>`).join('');
  $('character-select').innerHTML = '<option value="">选择人物加入本次制作</option>' + creativeProfiles.characters.map(x => `<option value="${esc(x.id)}">${esc(x.name)}</option>`).join('');
  $('scene-select').value = creativeProfiles.scenes.some(x => x.id === sceneValue) ? sceneValue : '';
  $('character-select').value = creativeProfiles.characters.some(x => x.id === characterValue) ? characterValue : '';
  selectedCharacterIds = new Set([...selectedCharacterIds].filter(id => creativeProfiles.characters.some(x => x.id === id)));
  $('selected-characters').innerHTML = [...selectedCharacterIds].map(id => {
    const person = creativeProfiles.characters.find(x => x.id === id);
    return person ? `<button class="character-chip" data-id="${esc(id)}">${esc(person.name)} <span>×</span></button>` : '';
  }).join('') || '<span class="creative-empty">本次还没有选人物</span>';
  $('selected-characters').querySelectorAll('.character-chip').forEach(btn => btn.addEventListener('click', () => {
    selectedCharacterIds.delete(btn.dataset.id); renderCreativeProfiles();
  }));
}

async function saveCreativeProfiles() {
  const scene = upsertProfile('scenes', 'scene-', ['name', 'place', 'era', 'atmosphere', 'lighting', 'palette', 'camera']);
  const character = upsertProfile('characters', 'character-', ['name', 'identity', 'appearance', 'wardrobe', 'behavior']);
  if (!scene && !character && !creativeProfiles.scenes.length && !creativeProfiles.characters.length) return;
  try {
    const r = await fetch('/api/creative-profiles', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(creativeProfiles) });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || '保存失败');
    creativeProfiles = data;
    if (character) selectedCharacterIds.add(character.id);
    renderCreativeProfiles();
  } catch (e) { alert('保存制片资料失败：' + e.message); }
}

async function loadCreativeProfiles() {
  try {
    const r = await fetch('/api/creative-profiles');
    if (!r.ok) return;
    creativeProfiles = await r.json();
    renderCreativeProfiles();
  } catch (e) { /* 后端未就绪时保留空资料库 */ }
}

function addCreativeToForm(fd, shotNote = '') {
  const creative = selectedCreative();
  fd.append('scene_id', creative.sceneId);
  fd.append('character_ids', creative.characterIds.join(','));
  if (shotNote.trim()) fd.append('shot_note', shotNote.trim());
}

$('scene-select').addEventListener('change', () => fillProfile('scene-', creativeProfiles.scenes.find(x => x.id === $('scene-select').value), ['name', 'place', 'era', 'atmosphere', 'lighting', 'palette', 'camera']));
$('character-select').addEventListener('change', () => {
  const person = creativeProfiles.characters.find(x => x.id === $('character-select').value);
  fillProfile('character-', person, ['name', 'identity', 'appearance', 'wardrobe', 'behavior']);
  if (person) { selectedCharacterIds.add(person.id); renderCreativeProfiles(); }
});
$('save-profiles-btn').addEventListener('click', saveCreativeProfiles);
$('new-scene-btn').addEventListener('click', () => { $('scene-select').value = ''; fillProfile('scene-', null, ['name', 'place', 'era', 'atmosphere', 'lighting', 'palette', 'camera']); });
$('new-character-btn').addEventListener('click', () => { $('character-select').value = ''; fillProfile('character-', null, ['name', 'identity', 'appearance', 'wardrobe', 'behavior']); });
$('delete-scene-btn').addEventListener('click', () => { const id = $('scene-select').value; if (!id) return; creativeProfiles.scenes = creativeProfiles.scenes.filter(x => x.id !== id); $('scene-select').value = ''; renderCreativeProfiles(); saveCreativeProfiles(); });
$('delete-character-btn').addEventListener('click', () => { const id = $('character-select').value; if (!id) return; creativeProfiles.characters = creativeProfiles.characters.filter(x => x.id !== id); selectedCharacterIds.delete(id); $('character-select').value = ''; renderCreativeProfiles(); saveCreativeProfiles(); });

// ===== Tab 切换 =====
document.querySelectorAll('.tab').forEach(btn => {
  btn.addEventListener('click', () => {
    mode = btn.dataset.mode;
    document.querySelectorAll('.tab').forEach(b => b.classList.toggle('active', b === btn));
    $('panel-i2v').hidden = mode !== 'i2v';
    $('panel-t2v').hidden = mode !== 't2v';
    $('panel-story').hidden = mode !== 'story';
    $('panel-nsfw').hidden = mode !== 'nsfw';
    $('generate-btn').style.display = mode === 'story' ? 'none' : '';
    $('add-queue-btn').style.display = mode === 'story' ? 'none' : '';
    // 无审查 tab：显示本地模型状态灯；模型下拉强制 Wan（无审查只有 Wan 有 LoRA）
    $('uncensored-status').hidden = mode !== 'nsfw';
    if (mode === 'nsfw') $('model').value = 'wan';
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

// ===== 无审查区首帧图上传（复用一个 handleFile，写入独立变量） =====
const nsfwZone = $('nsfw-dropzone');
const nsfwInput = $('nsfw-image-input');
nsfwZone.addEventListener('click', () => nsfwInput.click());
nsfwZone.addEventListener('dragover', e => { e.preventDefault(); nsfwZone.classList.add('over'); });
nsfwZone.addEventListener('dragleave', () => nsfwZone.classList.remove('over'));
nsfwZone.addEventListener('drop', e => {
  e.preventDefault();
  nsfwZone.classList.remove('over');
  if (e.dataTransfer.files.length) handleNsfwFile(e.dataTransfer.files[0]);
});
nsfwInput.addEventListener('change', () => { if (nsfwInput.files.length) handleNsfwFile(nsfwInput.files[0]); });

function handleNsfwFile(f) {
  nsfwImageFile = f;
  $('nsfw-preview').src = URL.createObjectURL(f);
  $('nsfw-preview').hidden = false;
  $('nsfw-drop-hint').hidden = true;
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
  // 无审查 tab：有首帧图则按图生（i2v），否则文生（t2v）；模型强制 Wan，并带上 nsfw 标志。
  const isNsfw = mode === 'nsfw';
  const effMode = isNsfw ? (nsfwImageFile ? 'i2v' : 't2v') : (mode === 'story' ? 't2v' : mode);
  return {
    mode: effMode,
    model: isNsfw ? 'wan' : $('model').value,
    prompt: isNsfw ? $('nsfw-prompt').value : (mode === 'i2v' ? $('i2v-prompt').value : $('t2v-prompt').value),
    resolution: $('resolution').value,
    duration: $('duration').value,
    steps: $('steps').value,
    segments: $('segments').value,
    image: isNsfw ? nsfwImageFile : imageFile,
    lastImage: isNsfw ? null : lastImageFile,
    nsfw: isNsfw,
    nsfwLocal: isNsfw ? $('nsfw-local').checked : false,
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
  if (cfg.nsfw) fd.append('nsfw', '1');
  addCreativeToForm(fd);
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
  if (cfg.mode === 't2v' && !cfg.prompt.trim() && !cfg.nsfw) { alert('请填写画面描述'); return; }
  if (cfg.nsfw && !cfg.prompt.trim()) { alert('请填写无审查的画面描述'); return; }
  submitTask(buildForm(cfg), { mode: cfg.mode });
}

// 方式 B：先把配置攒进清单，再一起开始
function addToQueue() {
  const cfg = currentConfig();
  if (cfg.mode === 'i2v' && !cfg.image) { alert('请先上传一张图片'); return; }
  if (cfg.mode === 't2v' && !cfg.prompt.trim() && !cfg.nsfw) { alert('请填写画面描述'); return; }
  if (cfg.nsfw && !cfg.prompt.trim()) { alert('请填写无审查的画面描述'); return; }
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
  if (info.state === 'awaiting_review') return info.review_stage === 'storyboard' ? '⏸ 等待分镜审查' : '⏸ 等待成片审查';
  if (info.state === 'awaiting_shot') return `🎬 第 ${Number(info.cur_shot ?? 0) + 1} 镜待审查`;
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
    : info.mode === 'oneclick' ? '🚀 一键成片'
    : info.mode === 'concat' ? '🎬 拼接' : '🎞️ 历史';
  const modelName = info.model === 'h3' ? 'MiniMax H3' : info.model === 'wan' ? 'Wan 2.2' : '';
  const done = info.state === 'done';
  const err = info.state === 'error';
  const cancelled = info.state === 'cancelled';
  const active = info.state === 'queued' || info.state === 'running';
  const review = info.state === 'awaiting_review';
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
  if (review && info.review_stage === 'storyboard') {
    // 分镜审查面板：列出全部镜头 + 每镜意见输入框
    const shots = Array.isArray(info.shots) ? info.shots : [];
    body = `<div class="review-box">
      <div class="review-tip">📋 分镜审查：每镜显示「中文剧情 + 英文生成提示词」。不满意哪一镜，在意见框里写（如「改成她坐下」），点「提交意见并改写」；没问题直接点「通过，开始生成」。</div>
      <div class="review-shots">${shots.map((s, i) => `
        <div class="review-shot">
          <div class="review-shot-head">第 ${i + 1} 镜</div>
          <div class="review-shot-cn">🎬 ${esc(String(s.scene || ''))}</div>
          <div class="review-shot-cn">🎙 ${esc(String(s.narration || ''))}</div>
          ${s.preview ? `<img class="review-preview" src="/api/shot-preview/${taskId}/${i}" alt="预览">` : `<img class="review-preview" src="/api/shot-preview/${taskId}/${i}" alt="预览生成中" onerror="this.style.opacity='0.15'">`}
          <details><summary>英文提示词</summary><div class="review-shot-prompt">${esc(String(s.prompt || ''))}</div></details>
          <input class="review-note" id="review-note-${taskId}-${i}" placeholder="修改意见（可空）">
        </div>`).join('')}</div>
      <div class="review-actions">
        <button class="generate" onclick="submitStoryboardReview('${taskId}', false)">📝 提交意见并改写</button>
        <button class="generate" onclick="submitStoryboardReview('${taskId}', true)">✅ 通过，开始生成</button>
      </div>
    </div>`;
  } else if (review && info.review_stage === 'final') {
    // 成片审查面板：成片预览 + 每镜重抽/换锚 + 检查修复 + 通过
    const segMeta = info.seg_meta || {};
    const segBtns = Object.keys(segMeta).map(i => `
      <span class="review-seg">
        <a class="review-seg-link" href="/api/segment-video/${taskId}/${i}" target="_blank">第 ${Number(i) + 1} 镜</a>
        <button class="review-mini" onclick="resampleShot('${taskId}', ${i})">🎲 重抽</button>
      </span>`).join(' ');
    body = `<div class="review-box">
      <video class="task-video" src="/api/video/${taskId}" controls></video>
      <div class="review-tip">🎬 成片审查：看一遍成片，单镜不满意点「🎲 重抽」（可多镜同时标记）；或点「🔍 自动检查修复」让 AI 找断裂镜头。</div>
      <div class="review-segs">${segBtns}</div>
      <div class="review-actions">
        <button class="generate" onclick="fixCheck('${taskId}')">🔍 自动检查修复</button>
        <button class="generate" onclick="recardAnchor('${taskId}')">🔄 重跑抽卡</button>
        <button class="generate" onclick="approveFinal('${taskId}')">✅ 通过，完成</button>
      </div>
      <div class="review-resample">
        <input id="resample-prompt-${taskId}" placeholder="可选：重抽时替换第几镜的提示词格式：镜号: 新提示词（留空则用原提示词）">
      </div>
    </div>`;
  } else if (info.state === 'awaiting_shot') {
    // 逐镜审查：当前镜视频 + 通过/重抽/直接拼片
    const cur = Number(info.cur_shot ?? 0);
    const total = (info.shots || []).length;
    body = `<div class="review-box">
      <video class="task-video" src="/api/segment-video/${taskId}/${cur}" controls autoplay loop></video>
      <div class="review-tip">🎬 第 ${cur + 1}/${total} 镜已生成。看过没问题点「✅ 通过，下一镜」（尾帧自动接续下一镜）；不满意点「🎲 重抽」（可在框里输入替换提示词）。</div>
      <input class="review-note" id="shot-prompt-${taskId}" placeholder="可选：重抽时的替换提示词（英文，留空用原提示词）">
      <div class="review-actions" style="margin-top:10px">
        <button class="generate" onclick="shotNext('${taskId}')">✅ 通过，下一镜</button>
        <button class="generate" onclick="shotResample('${taskId}')">🎲 重抽本镜</button>
        <button class="generate" onclick="shotFinish('${taskId}')">⏭ 跳过剩余直接拼片</button>
      </div>
    </div>`;
  } else if (done) {
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

function reviewInputBusy() {
  // 审查输入框正在输入（聚焦或非空）时跳过任务面板重渲染，避免页面自动滚顶打断审查
  for (const el of document.querySelectorAll('.review-note, .review-resample input')) {
    if (document.activeElement === el || el.value.trim()) return true;
  }
  return false;
}

function renderTasks() {
  if (reviewInputBusy()) return;  // 用户正在填审查意见，别动 DOM
  const list = $('task-list');
  if (!activeTasks.size) {
    list.innerHTML = '<div class="task-empty">还没有任务。点上面的「✨ 生成视频」直接排队，或「➕ 加入清单」攒几条一起开始。</div>';
    return;
  }
  const groups = { active: [], done: [], ended: [] };
  for (const [taskId, info] of activeTasks) {
    if (info.state === 'done') groups.done.push([taskId, info]);
    else if (info.state === 'error' || info.state === 'cancelled') groups.ended.push([taskId, info]);
    else groups.active.push([taskId, info]);  // awaiting_review 也在进行中组（等待用户操作）
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

// ===== 人工审查操作 =====
async function submitStoryboardReview(taskId, approve) {
  // 收集所有填了意见的镜号，拼成 "镜号: 意见" 多行文本
  const info = activeTasks.get(taskId);
  const lines = [];
  (info?.shots || []).forEach((_, i) => {
    const el = document.getElementById(`review-note-${taskId}-${i}`);
    const v = el ? el.value.trim() : '';
    if (v) lines.push(`${i + 1}: ${v}`);
  });
  const fd = new FormData();
  fd.append('task_id', taskId);
  fd.append('feedback', lines.join('\n'));
  fd.append('approve', approve ? '1' : '0');
  try {
    const r = await fetch('/api/review/storyboard', { method: 'POST', body: fd });
    const data = await r.json();
    if (!r.ok) { alert(data.error || '提交失败'); return; }
    if (data.status === 'revised') {
      // 改写成功：更新本地分镜，用户继续审
      if (info) info.shots = data.shots;
      renderTasks();
      alert('已按意见改写（改动见分镜列表），可继续审查或通过');
    } else if (data.status === 'approved') {
      if (info) { info.state = 'queued'; info.review_stage = null; info.msg = '审查通过，开始生成…'; }
      renderTasks();
    }
  } catch (e) { alert('提交失败：' + (e.message || e)); }
}

async function resampleShot(taskId, shotIndex) {
  const promptEl = document.getElementById(`resample-prompt-${taskId}`);
  let newPrompt = '';
  if (promptEl && promptEl.value.trim()) {
    const m = promptEl.value.trim().match(/^(\d+)\s*[:：]\s*(.+)$/);
    if (m && Number(m[1]) - 1 === shotIndex) newPrompt = m[2];
  }
  const fd = new FormData();
  fd.append('task_id', taskId);
  fd.append('shot_index', String(shotIndex));
  fd.append('new_prompt', newPrompt);
  try {
    const r = await fetch('/api/review/resample', { method: 'POST', body: fd });
    const data = await r.json();
    if (!r.ok) { alert(data.error || '重抽失败'); return; }
    const info = activeTasks.get(taskId);
    if (info) { info.state = 'running'; info.review_stage = null; info.msg = `第 ${shotIndex + 1} 镜重抽中…`; }
    renderTasks();
  } catch (e) { alert('重抽失败：' + (e.message || e)); }
}

async function fixCheck(taskId) {
  const fd = new FormData();
  fd.append('task_id', taskId);
  try {
    const r = await fetch('/api/review/fixcheck', { method: 'POST', body: fd });
    const data = await r.json();
    if (!r.ok) { alert(data.error || '检查失败'); return; }
    const info = activeTasks.get(taskId);
    if (data.status === 'ok') {
      alert('检查完毕：所有镜头衔接连贯，无需修复');
    } else {
      if (info) { info.state = 'running'; info.review_stage = null; info.msg = `发现 ${data.broken.length} 处断裂，修复中…`; }
      renderTasks();
    }
  } catch (e) { alert('检查失败：' + (e.message || e)); }
}

async function approveFinal(taskId) {
  const fd = new FormData();
  fd.append('task_id', taskId);
  try {
    const r = await fetch('/api/review/approve', { method: 'POST', body: fd });
    const data = await r.json();
    if (!r.ok) { alert(data.error || '操作失败'); return; }
    const info = activeTasks.get(taskId);
    if (info) { info.state = 'done'; info.review_stage = null; info.msg = '审查通过，成片完成'; }
    renderTasks();
  } catch (e) { alert('操作失败：' + (e.message || e)); }
}

async function shotNext(taskId) {
  const fd = new FormData(); fd.append('task_id', taskId);
  try {
    const r = await fetch('/api/review/shot_next', { method: 'POST', body: fd });
    const data = await r.json();
    if (!r.ok) { alert(data.error || '操作失败'); return; }
    const info = activeTasks.get(taskId);
    if (info) { info.state = 'running'; info.msg = data.status === 'concatenating' ? '拼接成片…' : `第 ${data.next + 1} 镜生成中…`; }
    renderTasks();
  } catch (e) { alert('操作失败：' + (e.message || e)); }
}

async function shotResample(taskId) {
  const el = document.getElementById(`shot-prompt-${taskId}`);
  const fd = new FormData(); fd.append('task_id', taskId); fd.append('new_prompt', el ? el.value.trim() : '');
  try {
    const r = await fetch('/api/review/shot_resample', { method: 'POST', body: fd });
    const data = await r.json();
    if (!r.ok) { alert(data.error || '重抽失败'); return; }
    const info = activeTasks.get(taskId);
    if (info) { info.state = 'running'; info.msg = `第 ${data.shot + 1} 镜重抽中…`; }
    renderTasks();
  } catch (e) { alert('重抽失败：' + (e.message || e)); }
}

async function shotFinish(taskId) {
  const fd = new FormData(); fd.append('task_id', taskId);
  try {
    const r = await fetch('/api/review/shot_finish', { method: 'POST', body: fd });
    const data = await r.json();
    if (!r.ok) { alert(data.error || '操作失败'); return; }
    const info = activeTasks.get(taskId);
    if (info) { info.state = 'running'; info.msg = '拼接成片…'; }
    renderTasks();
  } catch (e) { alert('操作失败：' + (e.message || e)); }
}

async function recardAnchor(taskId) {
  if (!confirm('重跑人物抽卡：丢弃当前人物锚，重新抽 4 候选并重锚第 1 镜（约 20 分钟）。继续？')) return;
  const fd = new FormData();
  fd.append('task_id', taskId);
  try {
    const r = await fetch('/api/review/recard', { method: 'POST', body: fd });
    const data = await r.json();
    if (!r.ok) { alert(data.error || '重跑抽卡失败'); return; }
    const info = activeTasks.get(taskId);
    if (info) { info.state = 'running'; info.review_stage = null; info.msg = '重跑人物抽卡…'; }
    renderTasks();
  } catch (e) { alert('重跑抽卡失败：' + (e.message || e)); }
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
      if (t.review_stage) info.review_stage = t.review_stage;
      if (t.cur_shot !== undefined) info.cur_shot = t.cur_shot;
      if (t.shots) info.shots = t.shots;
      if (t.seg_meta) info.seg_meta = t.seg_meta;
      if (t.video) info.video = t.video;
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
    addCreativeToForm(fd);
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
      <label>镜头专属补充（可覆盖全局场景/人物设定）</label>
      <textarea class="shot-note" rows="2" placeholder="例如：本镜头改为晨光；林夏换成雨衣">${esc(shot.note || '')}</textarea>
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
    card.querySelector('.shot-note').addEventListener('input', e => { shot.note = e.target.value; });
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
  addCreativeToForm(fd, shot.note || '');
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
  const prompts = shots.map(s => [s.note || '', s.prompt || ''].filter(Boolean).join('\n')).filter(Boolean);
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
    addCreativeToForm(fd);
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

// ===== 一键成片 =====
$('oneclick-btn').addEventListener('click', async () => {
  const idea = $('oneclick-idea').value.trim();
  const scriptFile = $('oneclick-script').value;
  if (!idea && !scriptFile) { alert('写一句想法，或选一个剧本文件'); return; }
  $('oneclick-btn').disabled = true;
  $('oneclick-btn').textContent = '生成中（约2小时）…';
  try {
    const fd = new FormData();
    fd.append('idea', idea);
    fd.append('script_file', scriptFile);
    fd.append('style', $('oneclick-style').value);
    const r = await fetch('/api/oneclick', { method: 'POST', body: fd });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || '提交失败');
    activeTasks.set(data.task_id, {
      mode: 'oneclick', model: 'wan', prompt: idea.slice(0, 40) + '…',
      resolution: '', duration: '约1.5分钟', steps: '',
      state: 'queued', msg: '排队中…', progress: 0, queueState: '', queuePos: 0,
      created: Math.floor(Date.now() / 1000),
    });
    renderTasks();
    $('oneclick-result').textContent = `已提交（任务 ${data.task_id}），进度看下方任务队列`;
  } catch (e) {
    alert('一键成片失败：' + (e.message || e));
  } finally {
    $('oneclick-btn').disabled = false;
    $('oneclick-btn').textContent = '🚀 一键成片';
  }
});

// juben/ 剧本文件下拉（不加载内容，只列文件名）
(async () => {
  try {
    const r = await fetch('/api/juben');
    const d = await r.json();
    const sel = $('oneclick-script');
    (d.files || []).forEach(f => {
      const opt = document.createElement('option');
      opt.value = f; opt.textContent = '📄 ' + f;
      sel.appendChild(opt);
    });
  } catch (e) { /* 后端未就绪时忽略 */ }
})();

// ===== 无审查区：本地拆剧本 + 连续成片（全程不碰云端） =====
$('nsfw-split-btn').addEventListener('click', nsfwSplitStory);
$('nsfw-chain-btn').addEventListener('click', nsfwChainStory);

async function nsfwSplitStory() {
  const story = $('nsfw-story-input').value.trim();
  if (!story) { alert('请先粘贴一段无审查故事'); return; }
  $('nsfw-split-btn').disabled = true;
  $('nsfw-split-btn').textContent = '本地拆解中…';
  try {
    const fd = new FormData();
    fd.append('story', story);
    fd.append('n_shots', $('nsfw-shots').value);
    fd.append('nsfw', '1');   // 拆剧本走本地 uncensored 模型
    addCreativeToForm(fd);
    const r = await fetch('/api/storyboard', { method: 'POST', body: fd });
    const data = await r.json();
    if (!r.ok) { alert(data.error || '本地拆解失败'); return; }
    nsfwShots = data.shots.map(s => ({ ...s, prompt: s.prompt }));
    renderNsfwShots();
  } catch (e) {
    alert('本地拆解失败：' + e);
  } finally {
    $('nsfw-split-btn').disabled = false;
    $('nsfw-split-btn').textContent = '📋 本地拆成剧本';
  }
}

function renderNsfwShots() {
  const list = $('nsfw-shot-list');
  list.innerHTML = '';
  nsfwShots.forEach((shot, idx) => {
    const card = document.createElement('div');
    card.className = 'shot-card';
    card.innerHTML = `
      <div class="shot-head"><span class="shot-no">镜头 ${shot.id}</span><span class="shot-scene">${esc(shot.scene)}</span></div>
      <div class="shot-narr">🎙 ${esc(shot.narration)}</div>
      <label>画面提示词（可改，本地模型不拦）</label>
      <textarea class="nsfw-shot-prompt" rows="2">${esc(shot.prompt)}</textarea>`;
    card.querySelector('.nsfw-shot-prompt').addEventListener('input', e => { shot.prompt = e.target.value; });
    list.appendChild(card);
  });
  $('nsfw-chain-area').hidden = nsfwShots.length < 2;
}

async function nsfwChainStory() {
  // 无审查连续成片：首段 prompt 就是用户填的无审查提示词，走 t2v；后续段 i2v 接续。
  const prompts = nsfwShots.map(s => s.prompt.trim()).filter(Boolean);
  if (prompts.length < 2) { alert('至少需要 2 个镜头才能连续成片'); return; }
  $('nsfw-chain-btn').disabled = true;
  $('nsfw-chain-btn').textContent = '已提交，生成中…';
  try {
    const fd = new FormData();
    fd.append('model', 'wan');
    fd.append('prompts', prompts.join('\n'));
    fd.append('resolution', $('resolution').value);
    fd.append('duration', $('duration').value);
    fd.append('steps', $('steps').value);
    fd.append('nsfw', '1');   // 连续成片走无审查 LoRA
    fd.append('nsfw_local', $('nsfw-local').checked ? '1' : '0');   // 开关决定剧情接续是否由本地模型代写
    addCreativeToForm(fd);
    const r = await fetch('/api/story-long', { method: 'POST', body: fd });
    const data = await r.json();
    if (!r.ok) { alert(data.error || '连续成片失败'); return; }
    activeTasks.set(data.task_id, {
      mode: 'story', model: 'wan', nsfw: true,
      prompt: prompts.slice(0, 3).join('、') + (prompts.length > 3 ? '…' : ''),
      resolution: $('resolution').value, duration: $('duration').value, steps: $('steps').value,
      state: 'queued', msg: '排队中…', progress: 0, queueState: '', queuePos: 0,
      created: Math.floor(Date.now() / 1000),
    });
    renderTasks();
    $('nsfw-chain-result').innerHTML = `<div class="task-msg" style="color:var(--muted)">已提交「无审查连续成片」，进度见下方任务队列（ID：${data.task_id}）</div>`;
  } catch (e) {
    alert('连续成片失败：' + e);
  } finally {
    $('nsfw-chain-btn').disabled = false;
    $('nsfw-chain-btn').textContent = '🎬 本地连续成片（一条长视频）';
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
    setDot($('uncensored-status'), h.uncensored_ready ? 'ok' : 'bad',
      h.uncensored_ready ? '本地无审查模型在线' : '本地无审查模型离线（拆剧本不可用）');
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
loadCreativeProfiles();

checkHealth();
loadTasks();
setInterval(checkHealth, 15000);
setInterval(pollTasks, 2000);
