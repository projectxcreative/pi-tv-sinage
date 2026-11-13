// Wi-Fi section of the Settings page. Network names come from anything nearby,
// so they are only ever inserted with textContent, never as HTML.
(() => {
  const $ = (id) => document.getElementById(id);
  const token = document.querySelector('meta[name=csrf-token]').content;
  const el = (tag, text, cls) => {
    const n = document.createElement(tag);
    if (text != null) n.textContent = text;
    if (cls) n.className = cls;
    return n;
  };
  const bars = (s) => (s >= 75 ? 'Excellent' : s >= 50 ? 'Good' : s >= 25 ? 'Fair' : 'Weak');

  async function api(path, opts = {}) {
    const r = await fetch(path, {
      ...opts,
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': token, ...(opts.headers || {})},
    });
    let body = {};
    try { body = await r.json(); } catch (_) { /* empty */ }
    if (!r.ok) throw new Error(body.error || `Request failed (${r.status})`);
    return body;
  }

  let target = null;     // {ssid, secure, hidden}
  let polling = null;
  let lastJobKey = null;

  function showJob(job) {
    const box = $('wifi-job');
    if (!job || job.state === 'idle') { box.hidden = true; return; }
    box.hidden = false;
    box.className = 'alert ' + (job.state === 'ok' ? 'ok' : job.state === 'failed' ? 'error' : 'info');
    box.textContent =
      job.state === 'connecting' ? `Joining "${job.ssid}"… this can take up to a minute.` :
      job.state === 'ok' ? `Connected to "${job.ssid}".` :
      `Couldn't join "${job.ssid}": ${job.message || 'unknown error'} The Pi has gone back to its previous network.`;
  }

  function renderStatus(data) {
    const p = $('wifi-status');
    p.textContent = '';
    if (!data.available) { p.textContent = 'This Pi has no Wi-Fi adapter.'; return; }
    const st = data.status || {};
    if (st.ssid) {
      p.append('Connected to ', el('strong', st.ssid), ` · signal ${bars(st.signal || 0).toLowerCase()}`);
      if (st.ip) p.append(` · ${st.ip}`);
    } else {
      p.append(st.wired ? 'Not on Wi-Fi (using a network cable).' : 'Not connected to Wi-Fi.');
    }
  }

  function renderSaved(saved) {
    const ul = $('wifi-saved');
    ul.textContent = '';
    if (!saved.length) { ul.append(el('li', 'No saved networks.', 'muted')); return; }
    for (const p of saved) {
      const li = el('li');
      li.append(el('span', p.ssid, 'net'));
      const right = el('span', null, 'right');
      if (p.active) {
        right.append(el('span', 'In use', 'badge now'));
      } else {
        const b = el('button', 'Forget', 'danger');
        b.type = 'button';
        b.onclick = async () => {
          if (!confirm(`Forget "${p.ssid}"? The Pi won't join it by itself any more.`)) return;
          try { await api('/api/wifi/forget', {method: 'POST', body: JSON.stringify({uuid: p.uuid})}); refresh(); }
          catch (e) { alert(e.message); }
        };
        right.append(b);
      }
      li.append(right);
      ul.append(li);
    }
  }

  async function refresh() {
    try {
      const data = await api('/api/wifi');
      renderStatus(data);
      renderSaved(data.saved || []);
      showJob(data.job);
      const key = data.job && `${data.job.state}:${data.job.ssid}:${data.job.finished || ''}`;
      if (data.job && data.job.state === 'connecting') startPolling();
      else if (polling) {
        stopPolling();
        if (key !== lastJobKey) scan();
      }
      lastJobKey = key;
      return data;
    } catch (e) {
      // While switching networks the Pi may be unreachable for a while: keep trying.
      if (polling) $('wifi-status').textContent = 'Waiting for the Pi to come back on the network…';
      else $('wifi-status').textContent = e.message;
    }
  }

  function startPolling() { if (!polling) polling = setInterval(refresh, 3000); }
  function stopPolling() { clearInterval(polling); polling = null; }

  async function scan() {
    const btn = $('wifi-scan');
    const ul = $('wifi-networks');
    btn.disabled = true;
    btn.textContent = 'Scanning…';
    try {
      const {networks} = await api('/api/wifi/scan');
      ul.textContent = '';
      for (const n of networks) {
        const li = el('li');
        li.append(el('span', n.ssid, 'net'));
        const right = el('span', null, 'right');
        right.append(el('span', `${n.secure ? 'Locked' : 'Open'} · ${bars(n.signal)}`, 'meta'));
        if (n.in_use) right.append(el('span', 'Connected', 'badge now'));
        else {
          const b = el('button', 'Join', 'secondary');
          b.type = 'button';
          b.onclick = () => openForm({ssid: n.ssid, secure: n.secure, hidden: false});
          right.append(b);
        }
        li.append(right);
        ul.append(li);
      }
      const other = el('li');
      other.append(el('span', 'Other network (hidden)…', 'net muted'));
      const ob = el('button', 'Join', 'secondary');
      ob.type = 'button';
      ob.onclick = () => openForm({ssid: '', secure: true, hidden: true});
      other.append(ob);
      ul.append(other);
      if (!networks.length) ul.prepend(el('li', 'No networks found. Try again, or move the Pi closer.', 'muted'));
    } catch (e) {
      ul.textContent = '';
      ul.append(el('li', e.message, 'muted'));
    } finally {
      btn.disabled = false;
      btn.textContent = 'Scan';
    }
  }

  function openForm(t) {
    target = t;
    $('wifi-form').hidden = false;
    $('wifi-form-title').textContent = t.hidden ? 'Join a hidden network' : `Join "${t.ssid}"`;
    $('wifi-ssid-row').hidden = !t.hidden;
    $('wifi-ssid').value = t.ssid;
    $('wifi-pass-row').hidden = !t.secure;
    $('wifi-pass').value = '';
    $('wifi-pass').type = 'password';
    $('wifi-show').textContent = 'Show';
    (t.hidden ? $('wifi-ssid') : $('wifi-pass')).focus();
    $('wifi-form').scrollIntoView({behavior: 'smooth', block: 'nearest'});
  }

  $('wifi-show').onclick = () => {
    const i = $('wifi-pass');
    i.type = i.type === 'password' ? 'text' : 'password';
    $('wifi-show').textContent = i.type === 'password' ? 'Show' : 'Hide';
  };
  $('wifi-cancel').onclick = () => { $('wifi-form').hidden = true; target = null; };
  $('wifi-scan').onclick = scan;

  $('wifi-form').onsubmit = async (ev) => {
    ev.preventDefault();
    if (!target) return;
    const ssid = target.hidden ? $('wifi-ssid').value.trim() : target.ssid;
    const password = target.secure ? $('wifi-pass').value : '';
    if (!ssid) { alert('Enter the network name.'); return; }
    if (target.secure && !target.hidden && password.length < 8) {
      alert('Wi-Fi passwords are at least 8 characters.');
      return;
    }
    $('wifi-join').disabled = true;
    try {
      await api('/api/wifi/connect', {method: 'POST', body: JSON.stringify({ssid, password, hidden: target.hidden})});
      $('wifi-form').hidden = true;
      $('wifi-pass').value = '';
      showJob({state: 'connecting', ssid});
      startPolling();
    } catch (e) {
      alert(e.message);
    } finally {
      $('wifi-join').disabled = false;
    }
  };

  refresh().then((d) => { if (d && d.available) scan(); });
})();
