(() => {
  const token = document.querySelector('meta[name=csrf-token]').content;
  const $ = (id) => document.getElementById(id);
  const list = $('playlist');
  let items = [];
  let nowPlayingId = null;

  async function api(method, url, body) {
    const res = await fetch(url, {
      method,
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': token },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (res.status === 401) { location.href = '/login'; throw new Error('logged out'); }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || res.statusText);
    return data;
  }

  const fmtTime = (s) => {
    if (s == null) return '';
    s = Math.round(s);
    const m = Math.floor(s / 60), r = s % 60;
    return m ? `${m}m ${String(r).padStart(2, '0')}s` : `${r}s`;
  };
  const fmtSize = (b) => b == null ? '' : b > 1e9 ? (b / 1e9).toFixed(1) + ' GB' : (b / 1e6).toFixed(1) + ' MB';
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  // ---- rendering ---------------------------------------------------------------
  async function load() {
    const data = await api('GET', '/api/media');
    items = data.items;
    render(data.image_duration);
  }

  function render(defaultDuration) {
    list.innerHTML = '';
    $('empty').hidden = items.length > 0;
    let total = 0, active = 0;
    items.forEach((m, i) => {
      if (m.enabled) { active++; total += m.effective_duration || 0; }
      const li = document.createElement('li');
      li.className = 'item' + (m.enabled ? '' : ' disabled') + (m.id === nowPlayingId ? ' playing' : '');
      li.dataset.id = m.id;
      const res = m.width ? `${m.width}×${m.height}` : '';
      const codec = m.codec ? m.codec.toUpperCase() : '';
      li.innerHTML = `
        <div class="handle" title="Drag to reorder">⋮⋮</div>
        <img class="thumb" src="${m.thumb}" alt="" loading="lazy">
        <div class="meta">
          <div class="name" title="${esc(m.name)}">${esc(m.name)}</div>
          <div class="details">
            ${m.id === nowPlayingId ? '<span class="badge now">On screen</span>' : ''}
            <span class="badge">${m.kind === 'video' ? 'Video' : 'Photo'}</span>
            ${[res, codec, m.kind === 'video' ? fmtTime(m.effective_duration) : '', fmtSize(m.size_bytes)].filter(Boolean).join(' · ')}
          </div>
          ${m.kind === 'image' ? `<label class="dur">Show for <input type="number" min="1" max="3600"
              placeholder="${defaultDuration}" value="${m.duration ?? ''}" data-act="duration"> s</label>` : ''}
          ${m.warning ? `<div class="warning">⚠ ${esc(m.warning)}</div>` : ''}
        </div>
        <div class="actions">
          <button class="icon secondary" data-act="up" title="Move up" ${i === 0 ? 'disabled' : ''}>↑</button>
          <button class="icon secondary" data-act="down" title="Move down" ${i === items.length - 1 ? 'disabled' : ''}>↓</button>
          <label class="toggle"><input type="checkbox" data-act="enabled" ${m.enabled ? 'checked' : ''}> On</label>
          <button class="secondary" data-act="play" ${m.enabled ? '' : 'disabled'}>Play now</button>
          <button class="secondary danger" data-act="delete">Delete</button>
        </div>`;
      list.appendChild(li);
    });
    $('summary').textContent = items.length
      ? `${active} of ${items.length} playing · loop ≈ ${fmtTime(total)}` : '';
  }

  // ---- item actions --------------------------------------------------------------
  list.addEventListener('click', async (e) => {
    const li = e.target.closest('.item');
    if (!li) return;
    const id = Number(li.dataset.id);
    const m = items.find((x) => x.id === id);
    if (e.target.classList.contains('thumb')) return showPreview(m);
    const act = e.target.dataset.act;
    try {
      if (act === 'delete') {
        if (!confirm(`Delete "${m.name}"? This can't be undone.`)) return;
        await api('DELETE', `/api/media/${id}`);
        await load();
      } else if (act === 'up' || act === 'down') {
        const i = items.indexOf(m), j = act === 'up' ? i - 1 : i + 1;
        [items[i], items[j]] = [items[j], items[i]];
        await saveOrder(items.map((x) => x.id));
      } else if (act === 'play') {
        await api('POST', '/api/command', { cmd: 'play', id });
        setTimeout(refreshStatus, 1500);
      }
    } catch (err) { alert(err.message); }
  });

  list.addEventListener('change', async (e) => {
    const li = e.target.closest('.item');
    if (!li) return;
    const id = Number(li.dataset.id);
    try {
      if (e.target.dataset.act === 'enabled') {
        await api('PATCH', `/api/media/${id}`, { enabled: e.target.checked });
      } else if (e.target.dataset.act === 'duration') {
        await api('PATCH', `/api/media/${id}`, { duration: e.target.value === '' ? null : Number(e.target.value) });
      }
      await load();
    } catch (err) { alert(err.message); load(); }
  });

  async function saveOrder(ids) {
    await api('POST', '/api/reorder', { ids });
    await load();
  }

  function showPreview(m) {
    const body = $('preview-body');
    body.innerHTML = m.kind === 'video'
      ? `<video src="${m.preview}" controls autoplay muted></video>`
      : `<img src="${m.preview}" alt="">`;
    const dlg = $('preview-dialog');
    dlg.addEventListener('close', () => { body.innerHTML = ''; }, { once: true });
    dlg.showModal();
  }

  // ---- drag to reorder (mouse and touch) -------------------------------------------
  let drag = null;
  list.addEventListener('pointerdown', (e) => {
    const handle = e.target.closest('.handle');
    if (!handle) return;
    e.preventDefault();
    const li = handle.closest('.item');
    drag = { li, pointerId: e.pointerId };
    li.classList.add('dragging');
    handle.setPointerCapture(e.pointerId);
  });
  list.addEventListener('pointermove', (e) => {
    if (!drag || e.pointerId !== drag.pointerId) return;
    const siblings = [...list.children].filter((x) => x !== drag.li);
    const after = siblings.find((x) => {
      const r = x.getBoundingClientRect();
      return e.clientY < r.top + r.height / 2;
    });
    if (after) list.insertBefore(drag.li, after); else list.appendChild(drag.li);
  });
  const endDrag = async (e) => {
    if (!drag || e.pointerId !== drag.pointerId) return;
    drag.li.classList.remove('dragging');
    drag = null;
    const ids = [...list.children].map((x) => Number(x.dataset.id));
    if (ids.join() !== items.map((x) => x.id).join()) {
      try { await saveOrder(ids); } catch (err) { alert(err.message); load(); }
    }
  };
  list.addEventListener('pointerup', endDrag);
  list.addEventListener('pointercancel', endDrag);

  // ---- uploads -----------------------------------------------------------------------
  const dz = $('dropzone');
  const input = $('file-input');
  ['dragenter', 'dragover'].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add('over'); }));
  ['dragleave', 'drop'].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove('over'); }));
  dz.addEventListener('drop', (e) => uploadAll([...e.dataTransfer.files]));
  input.addEventListener('change', () => { uploadAll([...input.files]); input.value = ''; });

  async function uploadAll(files) {
    for (const f of files) await uploadOne(f);
    await load();
  }

  function uploadOne(file) {
    return new Promise((resolve) => {
      const row = document.createElement('li');
      row.innerHTML = `<div>${esc(file.name)} <span class="muted pct">0%</span></div><progress max="100" value="0"></progress>`;
      $('uploads').appendChild(row);
      const bar = row.querySelector('progress'), pct = row.querySelector('.pct');
      const form = new FormData();
      form.append('file', file);
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/upload');
      xhr.setRequestHeader('X-CSRF-Token', token);
      xhr.upload.onprogress = (e) => {
        if (!e.lengthComputable) return;
        const p = Math.round((e.loaded / e.total) * 100);
        bar.value = p;
        pct.textContent = p < 100 ? `${p}%` : 'Processing…';
      };
      xhr.onload = () => {
        let data = {};
        try { data = JSON.parse(xhr.responseText); } catch (_) {}
        if (xhr.status === 200) {
          row.remove();
        } else {
          const msg = (data.errors && data.errors.join(' ')) || data.error || `Upload failed (${xhr.status})`;
          row.innerHTML = `<span class="err">${esc(file.name)}: ${esc(msg)}</span>`;
        }
        resolve();
      };
      xhr.onerror = () => { row.innerHTML = `<span class="err">${esc(file.name)}: network error</span>`; resolve(); };
      xhr.send(form);
    });
  }

  // ---- player status -------------------------------------------------------------------
  async function refreshStatus() {
    try {
      const s = await api('GET', '/api/status');
      $('status-dot').className = 'dot ' + (s.player_online ? 'on' : 'off');
      $('status-text').textContent = !s.player_online ? 'Player is offline'
        : s.now_playing ? `On screen: ${s.now_playing.name}` : 'Player is running (nothing to play)';
      const id = s.now_playing ? s.now_playing.id : null;
      if (id !== nowPlayingId) {
        nowPlayingId = id;
        list.querySelectorAll('.item').forEach((li) => {
          const on = Number(li.dataset.id) === id;
          li.classList.toggle('playing', on);
          const badge = li.querySelector('.badge.now');
          if (on && !badge) li.querySelector('.details').insertAdjacentHTML('afterbegin', '<span class="badge now">On screen</span>');
          if (!on && badge) badge.remove();
        });
      }
    } catch (_) { /* ignore */ }
  }
  $('skip-btn').addEventListener('click', async () => {
    try { await api('POST', '/api/command', { cmd: 'next' }); setTimeout(refreshStatus, 1500); }
    catch (err) { alert(err.message); }
  });

  load();
  refreshStatus();
  setInterval(refreshStatus, 5000);
})();
