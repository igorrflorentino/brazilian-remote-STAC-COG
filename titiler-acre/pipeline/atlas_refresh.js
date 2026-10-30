// Swaps an Atlas map's WMTS layers for new links: adds the new layers below the
// vector ones, checks them pixel by pixel against the service, deletes the old
// layers and connections. S.isNew and S.isOld tell the two apart by bucket
// prefix (last used for the move to linear/); adjust them for another swap.
//
// Paste into the browser console of the Atlas project page. Long loops run
// unawaited and keep progress on S (S.added, S.verified, S.deletedConns):
//   S.runAdds([{scene, name, url}, ...]);   // from wmts_links.csv, oldest first
//   S.runVerify();                          // gis-api tile against service tile
//   await S.openOldMenu();                  // then choose "Delete N datasets"
//   S.runDeleteConns();                     // leaves "Also delete datasets" unchecked
//
// It reaches Atlas' layer store through Turbopack, which is not a public API:
// the module id in S.ads may change with Atlas' builds.
window.__sw = window.__sw || {};
var S = window.__sw;
S.sleep = ms => new Promise(r => setTimeout(r, ms));
S.btn = t => [...document.querySelectorAll('button')].find(b => b.textContent.trim() === t);
S.until = async (fn, ms, what) => { const t0 = Date.now(); while (Date.now() - t0 < ms) { const v = fn(); if (v) return v; await S.sleep(250); } throw new Error('timeout: ' + what); };
S.setInput = (el, v) => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, v); el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('change', {bubbles: true})); };

// Layer store, through Turbopack: register a module, then run it as a runtime module.
S.ads = async () => {
  if (S._ads) return S._ads;
  const id = 990000000 + Math.floor(Math.random() * 1e6);
  TURBOPACK.push([`/_next/static/chunks/__probe${id}.js`, id, e => { S._tp = e; }]);
  TURBOPACK.push([`/_next/static/chunks/__probe_rt${id}.js`, {otherChunks: [], runtimeModuleIds: [id]}]);
  await S.until(() => S._tp, 5000, 'turbopack probe');
  S._ads = S._tp.i(277257).ActiveDataState;
  return S._ads;
};
S.root = () => S._ads.state.projectFolder;
S.items = () => { const s = S._ads.state; return s.activeDataTree[s.projectFolder].childIds.map(id => s.activeData[id]).filter(Boolean); };
S.isNew = i => !!(i.server_url && i.server_url.includes('-wpm%2Flinear%2Fv001%2F'));
S.isOld = i => !!(i.server_url && i.server_url.includes('-wpm%2Fv001%2F'));
S.moveTo = (id, pos) => S._ads.moveItem({itemId: id, depth: 0, oldParentId: S.root(), targetParentId: S.root(), positionInParent: pos});

S.openConnections = async () => {
  if (S.btn('New connection')) return;
  S.btn('Add data').click();
  (await S.until(() => S.btn('Connections'), 15000, 'connections tab')).click();
  await S.until(() => S.btn('New connection'), 15000, 'new conn');
};
S.addWmts = async (url, rowText) => {
  if (!S.btn('Connect')) {
    await S.openConnections();
    S.btn('New connection').click();
    (await S.until(() => [...document.querySelectorAll('h3')].find(e => e.textContent.trim() === 'OGC services'), 8000, 'ogc')).closest('button').click();
  }
  const inp = await S.until(() => document.querySelector('input[type=url]'), 8000, 'url input');
  S.setInput(inp, url); await S.sleep(300);
  S.btn('Connect').click();
  const cb = await S.until(() => S.btn('Add WMTS Datasets') && [...document.querySelectorAll('input[type=checkbox]')].find(c => c.closest('tr') && c.closest('tr').textContent.includes(rowText)), 40000, 'layer row');
  const row = cb.closest('tr');
  cb.click();
  await S.until(() => cb.checked && !S.btn('Add WMTS Datasets').disabled && row.lastElementChild.querySelector('svg'), 30000, 'validated');
  await S.sleep(1000);
  S.btn('Add WMTS Datasets').click();
  await S.until(() => !S.btn('Add WMTS Datasets'), 20000, 'dialog closed');
};

// list: [{scene, name, url}], oldest first. Each new layer lands on top; it is
// renamed and moved right below the vector layers, so the newest ends on top
// of the scenes.
S.addNew = async list => {
  await S.ads();
  S.added = S.added || JSON.parse(localStorage.getItem('__sw_added') || '[]');
  for (const L of list) {
    if (S.added.some(a => a.scene === L.scene)) continue;
    S.current = L.scene;
    const before = new Set(S.items().map(i => i.id));
    await S.addWmts(L.url, 'CBERS4A_WPM_PCA_RGB321_' + L.scene);
    const it = await S.until(() => S.items().find(i => !before.has(i.id) && S.isNew(i) && i.server_url.includes(L.scene)), 30000, 'new layer ' + L.scene);
    if (it.server_url !== L.url) throw new Error('server_url differs for ' + L.scene);
    await S.ads().then(a => a.renameItem(it.id, L.name));
    await S.moveTo(it.id, S.items().filter(i => i.type === 'vector').length);
    S.added.push({scene: L.scene, id: it.id});
    localStorage.setItem('__sw_added', JSON.stringify(S.added));
    await S.sleep(800);
  }
  S.current = null;
};
S.runAdds = list => { S.addErr = null; S.adding = true; S.addNew(list).catch(e => { S.addErr = String(e); }).finally(() => { S.adding = false; }); };

// Pixel checks: the gis-api tile of each new layer against the service tile of
// its template, at the centre of its bbox.
S.findMap = () => { const isMap = v => v && typeof v === 'object' && typeof v.getStyle === 'function' && typeof v.getCanvas === 'function'; let el = document.querySelector('.maplibregl-map'); for (let k = 0; k < 25 && el; k++, el = el.parentElement) { const fk = Object.keys(el).find(x => x.startsWith('__reactFiber')); let f = fk && el[fk]; for (let j = 0; j < 30 && f; j++, f = f.return) { let h = f.memoizedState; for (let q = 0; q < 40 && h; q++, h = h.next) { const s = h.memoizedState; if (isMap(s)) return s; if (s && isMap(s.current)) return s.current; if (s && typeof s === 'object') for (const v of Object.values(s)) if (isMap(v)) return v; } if (f.memoizedProps) for (const v of Object.values(f.memoizedProps)) { if (isMap(v)) return v; if (v && isMap(v.current)) return v.current; } } } return null; };
S.tile = (lon, lat, z) => { const n = 2 ** z, x = Math.floor((lon + 180) / 360 * n), r = lat * Math.PI / 180; return [x, Math.floor((1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2 * n)]; };
S.pixels = async url => { let init = {cache: 'no-store'}; if (url.includes('atlas.co')) { const t = await S.map._requestManager.transformRequest(url, 'Tile'); url = t.url || url; init = {headers: t.headers, credentials: t.credentials || 'include', cache: 'no-store'}; } const r = await fetch(url, init); if (r.status !== 200) return {status: r.status}; const bmp = await createImageBitmap(await r.blob()); const c = new OffscreenCanvas(bmp.width, bmp.height), g = c.getContext('2d'); g.drawImage(bmp, 0, 0); return {status: 200, d: g.getImageData(0, 0, bmp.width, bmp.height).data}; };
S.diff = (a, b) => { if (a.status !== 200 || b.status !== 200 || a.d.length !== b.d.length) return null; let s = 0, n = 0; for (let i = 0; i < a.d.length; i += 4) { if (a.d[i + 3] === 0 && b.d[i + 3] === 0) continue; n++; s += Math.abs(a.d[i] - b.d[i]) + Math.abs(a.d[i + 1] - b.d[i + 1]) + Math.abs(a.d[i + 2] - b.d[i + 2]) + Math.abs(a.d[i + 3] - b.d[i + 3]); } return n ? +(s / n / 4).toFixed(2) : 0; };
S.opaque = p => { if (!p.d) return null; let o = 0; for (let i = 3; i < p.d.length; i += 4) o += p.d[i] > 0; return Math.round(100 * o / (p.d.length / 4)); };
S.verifyNew = async (zooms = [12, 15]) => {
  await S.ads(); S.map = S.map || S.findMap();
  const rows = [];
  for (const it of S.items().filter(S.isNew)) {
    const [w, s, e, n] = it.bbox, lon = (w + e) / 2, lat = (s + n) / 2;
    for (const z of zooms) {
      const [x, y] = S.tile(lon, lat, z);
      const svc = it.template_url.replace('{TileMatrix}', z).replace('{TileCol}', x).replace('{TileRow}', y);
      const [a, b] = await Promise.all([S.pixels(`https://gis-api.atlas.co/datasets/raster/wmts/${it.id}/${z}/${x}/${y}`), S.pixels(svc)]);
      rows.push({name: it.name, z, atlas: a.status, svc: b.status, diff: S.diff(a, b), opaque: S.opaque(a), webp: svc.includes('.webp?')});
    }
  }
  S.verified = rows;
};
S.runVerify = () => { S.verified = null; S.verifyNew().catch(e => { S.verified = 'ERR ' + e; }); };

// Old layers: select the contiguous block of old rows, then the context menu's
// "Delete N datasets".
S.layerId = inp => { let el = inp; for (let k = 0; k < 8 && el; k++, el = el.parentElement) { const fk = Object.keys(el).find(x => x.startsWith('__reactFiber')); let f = fk && el[fk]; for (let j = 0; j < 8 && f; j++, f = f.return) { const p = f.memoizedProps; if (p && typeof p.layerId === 'string') return p.layerId; } } return null; };
S.layerInputs = () => [...document.querySelectorAll('input')].filter(i => S.layerId(i));
S.up = (el, n) => { for (let k = 0; k < n; k++) el = el.parentElement; return el; };
S.mouse = extra => Object.assign({bubbles: true, cancelable: true, view: window, button: 0}, extra);
S.openOldMenu = async () => {
  await S.ads();
  const oldIds = new Set(S.items().filter(S.isOld).map(i => i.id));
  const inputs = S.layerInputs(), idx = inputs.map((inp, k) => oldIds.has(S.layerId(inp)) ? k : -1).filter(k => k >= 0);
  if (idx.length !== oldIds.size || idx.some((v, i) => i && v !== idx[i - 1] + 1)) throw new Error('old rows not contiguous: ' + idx);
  S.up(inputs[idx[0]], 5).dispatchEvent(new MouseEvent('click', S.mouse({})));
  await S.sleep(500);
  S.up(inputs[idx[idx.length - 1]], 5).dispatchEvent(new MouseEvent('click', S.mouse({shiftKey: true})));
  await S.sleep(500);
  const bg = S.up(inputs[idx[0]], 6), r = bg.getBoundingClientRect();
  bg.dispatchEvent(new MouseEvent('contextmenu', S.mouse({button: 2, clientX: r.left + 40, clientY: r.top + r.height / 2})));
  await S.sleep(800);
  return {old: idx.length, selected: S._ads.state.selectedLayerIds.length, menu: [...document.querySelectorAll('[role=menuitem]')].map(m => m.textContent.trim())};
};

// Old connections: Connection actions -> Delete, with "Also delete datasets"
// left unchecked.
S.connections = () => {
  for (const start of [...document.querySelectorAll('input, button')]) {
    let e = start;
    for (let k = 0; k < 20 && e; k++, e = e.parentElement) {
      const fk = Object.keys(e).find(x => x.startsWith('__reactFiber')); let f = fk && e[fk];
      for (let j = 0; j < 10 && f; j++, f = f.return) { const p = f.memoizedProps; if (p && p.value && p.value.foreignDatasets) return p.value.foreignDatasets; }
    }
  }
  return null;
};
S.connRow = url => {
  const dlg = [...document.querySelectorAll('[role=dialog]')].find(d => d.textContent.includes('New connection'));
  const leaf = dlg && [...dlg.querySelectorAll('span')].find(e => e.textContent.trim() === url);
  if (!leaf) return null;
  let row = leaf; for (let k = 0; k < 8 && row; k++, row = row.parentElement) if (row.querySelector('button[aria-label="Connection actions"]')) return row;
  return null;
};
S.confirmDelete = async url => {
  for (let attempt = 0; attempt < 6; attempt++) {
    const conf = [...document.querySelectorAll('[role=dialog]')].find(d => /Are you sure/i.test(d.textContent));
    if (conf) {
      if ([...conf.querySelectorAll('input[type=checkbox]')].some(c => c.checked)) throw new Error('checkbox checked');
      [...conf.querySelectorAll('button')].find(b => b.textContent.trim() === 'Delete').click();
    }
    const t0 = Date.now();
    while (Date.now() - t0 < 5000) { if (!S.connRow(url)) return; await S.sleep(250); }
  }
  throw new Error('row gone (after retries)');
};
S.deleteConn = async url => {
  const row = S.connRow(url); if (!row) throw new Error('row not found');
  if (row.querySelectorAll('button[aria-label="Connection actions"]').length !== 1) throw new Error('ambiguous row');
  row.querySelector('button[aria-label="Connection actions"]').click();
  const del = await S.until(() => [...document.querySelectorAll('[role=menuitem]')].find(m => m.textContent.trim() === 'Delete'), 5000, 'delete item');
  del.click();
  await S.until(() => [...document.querySelectorAll('[role=dialog]')].find(d => /Are you sure/i.test(d.textContent)), 5000, 'confirm');
  await S.sleep(400);
  await S.confirmDelete(url);
};
S.runDeleteConns = () => {
  S.oldConns = (S.connections() || []).filter(c => c.url && c.url.includes('-wpm%2Fv001%2F'));
  S.deletedConns = S.deletedConns || [];
  S.cerr = null; S.crunning = true;
  (async () => {
    for (const c of S.oldConns) {
      if (S.deletedConns.includes(c.id)) continue;
      await S.deleteConn(c.url);
      S.deletedConns.push(c.id);
      await S.sleep(600);
    }
  })().catch(e => { S.cerr = String(e); }).finally(() => { S.crunning = false; });
};
'atlas_refresh loaded';
