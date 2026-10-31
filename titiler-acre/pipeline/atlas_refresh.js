// Swaps an Atlas map's WMTS layers for new links: adds the new layers to the
// "Imagens CBERS-4A (2 m)" folder in date order (newest first), checks them
// pixel by pixel against the service, deletes the old layers and connections.
// S.isNew and S.isOld tell the two apart by bucket prefix (last used for the
// move to linear/); adjust them for another swap. The project's layers sit in
// folders (S.FOLDERS, see the README), so every lookup walks the folder tree.
//
// Paste into the browser console of the Atlas project page. Long loops run
// unawaited and keep progress on S (S.added, S.verified, S.deletedConns):
//   S.runAdds([{scene, name, url}, ...]);   // from wmts_links.csv, any order
//   S.runVerify();                          // gis-api tile against service tile (optionally S.runVerify(['20221009_241_124']))
//   await S.openOldMenu();                  // then choose "Delete N datasets"
//   S.runDeleteConns();                     // leaves "Also delete datasets" unchecked
//   await S.organize();                     // files every layer into S.FOLDERS (safe to repeat)
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
// The layers sit in folders: S.rootItems is the top level (folders included) and
// S.items every layer, folders walked in list order.
S.rootItems = () => { const s = S._ads.state; return s.activeDataTree[s.projectFolder].childIds.map(id => s.activeData[id]).filter(Boolean); };
S.isFolder = i => i.item_type === 'folder';
S.items = () => {
  const s = S._ads.state, out = [];
  const walk = id => { for (const c of s.activeDataTree[id].childIds || []) { const it = s.activeData[c]; if (!it) continue; if (S.isFolder(it)) walk(c); else out.push(it); } };
  walk(s.projectFolder);
  return out;
};
S.folder = name => S.rootItems().find(i => S.isFolder(i) && i.name === name);
S.isNew = i => !!(i.server_url && i.server_url.includes('-wpm%2Flinear%2Fv001%2F'));
S.isOld = i => !!(i.server_url && i.server_url.includes('-wpm%2Fv001%2F'));
S.moveTo = (id, pos) => S._ads.moveItem({itemId: id, depth: 0, oldParentId: S.root(), targetParentId: S.root(), positionInParent: pos});
S.moveInto = (id, folderId, pos) => { const t = S._ads.state.activeDataTree; return S._ads.moveItem({itemId: id, depth: t[folderId].depth + 1, oldParentId: t[id].parentId, targetParentId: folderId, positionInParent: pos}); };
// Atlas draws no rows for the layers of a collapsed folder, so the steps that read
// or click the layer list need every folder open.
S.expandAll = async () => { for (const i of Object.values(S._ads.state.activeData)) if (S.isFolder(i) && !i.expanded) await S._ads.setFolderExpanded(i.id, true); await S.sleep(500); };

S.SCENES_FOLDER = 'Imagens CBERS-4A (2 m)';
S.SCENES = /^CBERS-4A (\d{4}-\d\d-\d\d) \d{3}\/\d{3}$/;
// Index in the folder that keeps its scene layers newest first.
S.slotFor = (name, folderId, exceptId) => {
  const s = S._ads.state, date = n => (n.match(S.SCENES) || [])[1], d = date(name);
  const kids = s.activeDataTree[folderId].childIds.filter(c => c !== exceptId).map(c => s.activeData[c]);
  const j = kids.findIndex(i => date(i.name) && date(i.name) < d);
  return j < 0 ? kids.length : j;
};

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

// list: [{scene, name, url}], in any order. Each new layer lands on top of the
// list; it is renamed and moved into the scenes folder at the slot that keeps
// the scenes newest first (without that folder: right below the vector layers).
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
    const folder = S.folder(S.SCENES_FOLDER);
    if (folder) await S.moveInto(it.id, folder.id, S.slotFor(L.name, folder.id, it.id));
    else await S.moveTo(it.id, S.items().filter(i => i.type === 'vector').length);
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
S.verifyNew = async (zooms = [12, 15], only) => {
  await S.ads(); S.map = S.map || S.findMap();
  const rows = [];
  for (const it of S.items().filter(S.isNew).filter(i => !only || only.some(id => i.server_url.includes(id)))) {
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
S.runVerify = only => { S.verified = null; S.verifyNew([12, 15], only).catch(e => { S.verified = 'ERR ' + e; }); };

// Old layers: select the contiguous block of old rows, then the context menu's
// "Delete N datasets".
S.layerId = inp => { let el = inp; for (let k = 0; k < 8 && el; k++, el = el.parentElement) { const fk = Object.keys(el).find(x => x.startsWith('__reactFiber')); let f = fk && el[fk]; for (let j = 0; j < 8 && f; j++, f = f.return) { const p = f.memoizedProps; if (p && typeof p.layerId === 'string') return p.layerId; } } return null; };
S.layerInputs = () => [...document.querySelectorAll('input')].filter(i => S.layerId(i));
S.up = (el, n) => { for (let k = 0; k < n; k++) el = el.parentElement; return el; };
S.mouse = extra => Object.assign({bubbles: true, cancelable: true, view: window, button: 0}, extra);
S.openOldMenu = async () => {
  await S.ads();
  await S.expandAll();
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
// ---- Folders. S.FOLDERS is the structure of the project, top to bottom; a member is
// a layer name or a regexp (the scenes come out newest first). Keep the names in
// sync with HY.NAMES in hybrid_layers.js. S.organize creates the missing folders,
// files every known layer into its own, in this order, and puts the folders at the
// top of the list; layers it does not know stay below them. Safe to run again.
S.FOLDERS = [
  ['Pontos de interesse', ['Aeródromos (ANAC)', 'Escolas e saúde (OSM)']],
  ['Vias e hidrografia', ['Estradas e ramais (OSM)', 'Ruas urbanas (OSM)', 'Rios e igarapés (OSM)']],
  ['Desmatamento (INPE)', ['Alertas DETER, últimos 12 meses (INPE)', 'Desmatamento PRODES 2016-2025 (INPE)']],
  ['Alertas MapBiomas', ['Alertas validados (MapBiomas Alerta, ao vivo)', 'Imóveis com alerta (MapBiomas Alerta, ao vivo)']],
  ['Fundiário e fiscalização', ['Focos de calor, mês atual (INPE, ao vivo)', 'Focos de calor, últimos 12 meses (INPE)', 'Embargos (IBAMA)', 'Assentamentos (INCRA via INPE)', 'Imóveis rurais (CAR/SICAR)']],
  ['Limites e áreas protegidas', ['Terras Indígenas (FUNAI via INPE)', 'Unidades de Conservação (MMA/CNUC)', 'Municípios (IBGE via INPE)', 'Limite do Acre']],
  [S.SCENES_FOLDER, ['Datas das imagens CBERS-4A', S.SCENES]],
  ['Fundo Sentinel-2', [/^Fundo: /]],
];
S.organize = async () => {
  await S.ads();
  for (const [name] of S.FOLDERS) if (!S.folder(name)) { await S._ads.createFolder(name); await S.sleep(800); }
  const date = n => (n.match(S.SCENES) || [])[1] || '';
  for (const [name, members] of S.FOLDERS) {
    const layers = S.items(), ids = [];
    for (const m of members) {
      const hits = layers.filter(i => typeof m === 'string' ? i.name === m : m.test(i.name));
      if (m === S.SCENES) hits.sort((a, b) => date(b.name).localeCompare(date(a.name)));
      for (const h of hits) if (!ids.includes(h.id)) ids.push(h.id);
    }
    if (!ids.length) continue;
    const folder = S.folder(name);
    await S._ads.moveSelectedItemsMulti({itemIds: ids, depth: S._ads.state.activeDataTree[folder.id].depth + 1, parentId: folder.id, basePosition: 0});
    await S.sleep(800);
  }
  for (let k = 0; k < S.FOLDERS.length; k++) { await S.moveTo(S.folder(S.FOLDERS[k][0]).id, k); await S.sleep(500); }
  return S.rootItems().map(i => i.name);
};
'atlas_refresh loaded';
