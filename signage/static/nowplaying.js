// Now playing panel: live snapshot of the TV, scrubber, pause and +/-10s.
(() => {
  const $ = (id) => document.getElementById(id);
  const token = document.querySelector('meta[name=csrf-token]').content;
  const STATUS_MS = 1000;
  // Each snapshot costs the Pi ~1s of work and a few dropped frames on the TV
  // (measured: ~3-4 per snapshot on 4K HEVC), so keep them occasional.
  const SNAPSHOT_MS = 15000;

  const range = $('np-range');
  let state = null;          // last now_playing from the server
  let dragging = false;
  let lastSnapshot = 0;
  let seekHoldUntil = 0;     // ignore stale positions right after a seek

  async function post(path, body) {
    const r = await fetch(path, {method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': token},
      body: JSON.stringify(body || {})});
    if (r.status === 401) location.href = '/login';
    if (!r.ok) {
      let msg = r.statusText;
      try { msg = (await r.json()).error || msg; } catch (_) { /* empty */ }
      throw new Error(msg);
    }
  }
  const command = (cmd, extra) => post('/api/command', {cmd, ...extra});

  function fmt(t) {
    if (t == null || !isFinite(t)) return '–:––';
    t = Math.max(0, Math.round(t));
    const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
    const ss = String(s).padStart(2, '0');
    return h ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`;
  }

  function render() {
    const np = state;
    const isVideo = np && np.kind === 'video' && np.duration;
    $('np-scrub').hidden = !isVideo;
    for (const id of ['np-back', 'np-pause', 'np-fwd']) $(id).hidden = !isVideo;
    if (!isVideo) return;
    $('np-pause').textContent = np.paused ? 'Play' : 'Pause';
    $('np-dur').textContent = fmt(np.duration);
    range.max = np.duration;
    if (!dragging && Date.now() > seekHoldUntil && np.pos != null) {
      range.value = np.pos;
      $('np-pos').textContent = fmt(np.pos);
    }
  }

  async function refresh() {
    try {
      const r = await fetch('/api/status', {headers: {Accept: 'application/json'}});
      if (r.status === 401) { location.href = '/login'; return; }
      const s = await r.json();
      state = s.player_online ? s.now_playing : null;
      render();
      if (s.snapshot_at && s.snapshot_at !== lastSnapshot) {
        lastSnapshot = s.snapshot_at;
        loadSnapshot();
      }
      showAge(s.snapshot_at);
    } catch (_) { /* network blip: try again next tick */ }
  }

  function loadSnapshot() {
    const img = $('np-img');
    const probe = new Image();
    probe.onload = () => { img.src = probe.src; img.hidden = false; $('np-empty').hidden = true; };
    probe.onerror = () => { img.hidden = true; $('np-empty').hidden = false; $('np-empty').textContent = 'Nothing on screen'; };
    probe.src = `/api/screen?t=${Date.now()}`;
  }

  function showAge(at) {
    const el = $('np-age');
    if (!at || $('np-img').hidden) { el.hidden = true; return; }
    const secs = Math.max(0, Math.round(Date.now() / 1000 - at));
    el.hidden = false;
    el.textContent = secs < 3 ? 'Just now' : `${secs}s ago · tap to refresh`;
  }

  function requestSnapshot(force) {
    if ((!$('np-live').checked && !force) || document.hidden) return;
    post('/api/screen/refresh').catch(() => {});
  }
  $('np-screen').addEventListener('click', () => requestSnapshot(true));

  async function seekTo(pos) {
    pos = Math.max(0, Math.min(Number(range.max) || 0, pos));
    range.value = pos;
    $('np-pos').textContent = fmt(pos);
    seekHoldUntil = Date.now() + 2500;
    if (state) state.pos = pos;
    try {
      await command('seek', {pos});
      setTimeout(() => requestSnapshot(true), 1200);   // show the new spot
    } catch (e) { alert(e.message); }
  }

  range.addEventListener('input', () => { dragging = true; $('np-pos').textContent = fmt(Number(range.value)); });
  range.addEventListener('change', () => { dragging = false; seekTo(Number(range.value)); });
  $('np-back').onclick = () => state && seekTo((state.pos || 0) - 10);
  $('np-fwd').onclick = () => state && seekTo((state.pos || 0) + 10);
  $('np-pause').onclick = async () => {
    if (!state) return;
    const paused = !state.paused;
    state.paused = paused;
    render();
    try { await command('pause', {paused}); setTimeout(() => requestSnapshot(true), 600); }
    catch (e) { alert(e.message); }
  };
  $('np-live').addEventListener('change', () => {
    try { localStorage.setItem('np-live', $('np-live').checked ? '1' : '0'); } catch (_) { /* private mode */ }
    if ($('np-live').checked) requestSnapshot(true);
  });
  try { if (localStorage.getItem('np-live') === '0') $('np-live').checked = false; } catch (_) { /* ignore */ }

  document.addEventListener('visibilitychange', () => { if (!document.hidden) { refresh(); requestSnapshot(false); } });
  refresh();
  requestSnapshot(false);
  setInterval(refresh, STATUS_MS);
  setInterval(() => requestSnapshot(false), SNAPSHOT_MS);
  $('skip-btn').addEventListener('click', () => setTimeout(() => requestSnapshot(true), 2500));
})();
