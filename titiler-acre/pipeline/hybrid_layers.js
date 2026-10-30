// Builds the hybrid-map vector layers of Acre and uploads them to an Atlas map.
//
// Paste into the browser console of the Atlas project page (app.atlas.co), then:
//   await HY.run(['limite', 'cenas', 'municipios', 'ucs', 'tis', 'prodes', 'deter',
//                 'rios', 'ruas', 'estradas', 'servicos', 'aerodromos']);
//   await HY.uploadAll(['cenas', 'municipios', ...]);   // bottom layer first
// Every source below sends CORS headers, so the page fetches them directly.
// Overpass queries take 40-60 s each; HY.log shows progress.
//
// Styles (HY.STYLES) and renames go through Atlas' own React state, which is
// not a public API: if HY.applyStyle stops working, set them in the Layer panel.
{ // block scope, so the script can be pasted again in the same page
window.HY = window.HY || {data: {}, log: []};
const HY = window.HY;
HY.sleep = ms => new Promise(r => setTimeout(r, ms));
HY.say = m => HY.log.push(new Date().toISOString().slice(11, 19) + ' ' + m);
HY.until = async (fn, ms, what) => { const t0 = Date.now(); while (Date.now() - t0 < ms) { const v = fn(); if (v) return v; await HY.sleep(300); } throw new Error('timeout: ' + what); };

// ---- Geometry: Douglas-Peucker in degrees, coordinates rounded to 5 decimals (~1 m)
HY.r5 = c => Math.round(c * 1e5) / 1e5;
HY.dp = (pts, tol) => {
  if (pts.length <= 2) return pts;
  const keep = new Uint8Array(pts.length); keep[0] = keep[pts.length - 1] = 1;
  const stack = [[0, pts.length - 1]], t2 = tol * tol;
  while (stack.length) {
    const [a, b] = stack.pop(); let maxd = -1, idx = -1;
    const [ax, ay] = pts[a], [bx, by] = pts[b], dx = bx - ax, dy = by - ay, L = dx * dx + dy * dy;
    for (let i = a + 1; i < b; i++) {
      const [px, py] = pts[i]; let t = L ? ((px - ax) * dx + (py - ay) * dy) / L : 0; t = Math.max(0, Math.min(1, t));
      const ex = ax + t * dx - px, ey = ay + t * dy - py, d = ex * ex + ey * ey;
      if (d > maxd) { maxd = d; idx = i; }
    }
    if (maxd > t2) { keep[idx] = 1; stack.push([a, idx], [idx, b]); }
  }
  return pts.filter((_, i) => keep[i]);
};
HY.line = (l, tol) => { const s = HY.dp(l, tol).map(([x, y]) => [HY.r5(x), HY.r5(y)]); return s.length >= 2 ? s : null; };
HY.ring = (r, tol) => { const s = HY.dp(r, tol).map(([x, y]) => [HY.r5(x), HY.r5(y)]); return s.length >= 4 ? s : null; };
HY.geom = (g, tol) => {
  if (!g) return null;
  if (g.type === 'Point') return {type: 'Point', coordinates: g.coordinates.map(HY.r5)};
  if (g.type === 'MultiPoint') return {type: 'Point', coordinates: g.coordinates[0].map(HY.r5)};
  if (g.type === 'LineString') { const c = HY.line(g.coordinates, tol); return c && {type: 'LineString', coordinates: c}; }
  if (g.type === 'MultiLineString') { const c = g.coordinates.map(l => HY.line(l, tol)).filter(Boolean); return c.length ? {type: 'MultiLineString', coordinates: c} : null; }
  const poly = p => { const outer = HY.ring(p[0], tol); return outer && [outer, ...p.slice(1).map(r => HY.ring(r, tol)).filter(Boolean)]; };
  if (g.type === 'Polygon') { const c = poly(g.coordinates); return c && {type: 'Polygon', coordinates: c}; }
  if (g.type === 'MultiPolygon') { const c = g.coordinates.map(poly).filter(Boolean); return c.length ? {type: 'MultiPolygon', coordinates: c} : null; }
  return null;
};
HY.fc = (feats, tol, props) => ({type: 'FeatureCollection', features: feats.map(f => ({type: 'Feature', properties: props(f.properties || {}), geometry: HY.geom(f.geometry, tol)})).filter(f => f.geometry)});
const small = new Set(['de', 'do', 'da', 'dos', 'das', 'e']);
HY.title = s => (s || '').toLowerCase().split(/(\s+|-|\/)/).map((w, i) => (i > 0 && small.has(w)) ? w : w.charAt(0).toUpperCase() + w.slice(1)).join('');

// ---- Sources
HY.TB = 'https://terrabrasilis.dpi.inpe.br/geoserver/ows?service=WFS&version=2.0.0&request=GetFeature&outputFormat=application/json&typeNames=';
HY.tb = async (layer, cql) => (await (await fetch(HY.TB + layer + '&CQL_FILTER=' + encodeURIComponent(cql))).json()).features;
HY.INDE = 'https://geoservicos.inde.gov.br/geoserver/ows?service=WFS&version=2.0.0&request=GetFeature&outputFormat=application/json&typeNames=';
HY.inde = async (layer, cql) => (await (await fetch(HY.INDE + encodeURIComponent(layer) + '&CQL_FILTER=' + encodeURIComponent(cql))).json()).features;
HY.overpass = async body => {
  for (let k = 0; k < 4; k++) {
    const r = await fetch('https://overpass-api.de/api/interpreter', {method: 'POST', body: 'data=' + encodeURIComponent(body)});
    if (r.ok) return (await r.json()).elements;
    HY.say('overpass ' + r.status + ', retrying'); await HY.sleep(20000 * (k + 1));
  }
  throw new Error('overpass failed');
};
HY.AREA = '[out:json][timeout:240];area["ISO3166-2"="BR-AC"]->.a;';
HY.osmWays = (els, tol, props) => ({type: 'FeatureCollection', features: els.filter(e => e.type === 'way' && e.geometry)
  .map(e => ({type: 'Feature', properties: props(e.tags || {}), geometry: HY.geom({type: 'LineString', coordinates: e.geometry.map(p => [p.lon, p.lat])}, tol)})).filter(f => f.geometry)});
// TerraBrasilis' WFS 2.0 reads CQL geometries in EPSG:4674 as "lat lon"; a lon-lat polygon matches nothing.
HY.acreWkt = () => {
  const g = HY.data.limite.features[0].geometry; const polys = g.type === 'Polygon' ? [g.coordinates] : g.coordinates;
  const big = polys.slice().sort((a, b) => b[0].length - a[0].length)[0][0];
  return 'POLYGON((' + HY.dp(big, 0.02).map(([x, y]) => y.toFixed(4) + ' ' + x.toFixed(4)).join(',') + '))';
};
HY.surface = s => /asphalt|paved|concrete|sett|paving/.test(s || '') && !/unpaved/.test(s || '') ? 'pavimentada'
  : /unpaved|dirt|ground|gravel|sand|compacted|earth|grass|mud/.test(s || '') ? 'não pavimentada' : '';
HY.ref = r => (r || '').split(/[;,]/).map(s => s.trim().replace(/^AC (\d+)$/, 'AC-$1')).filter(Boolean).join(' / ');
HY.DETER = {DESMATAMENTO_CR: 'Desmatamento (corte raso)', DESMATAMENTO_VEG: 'Desmatamento com vegetação', DEGRADACAO: 'Degradação',
  CICATRIZ_DE_QUEIMADA: 'Cicatriz de queimada', CS_DESORDENADO: 'Corte seletivo', CS_GEOMETRICO: 'Corte seletivo', MINERACAO: 'Mineração'};

// ---- Layers: key -> FeatureCollection with short Portuguese attributes
HY.build = {
  limite: async () => HY.fc(await HY.tb('prodes-legal-amz:states_legal_amazon', "sigla='AC'"), 0.0001, () => ({nome: 'Acre', fonte: 'IBGE via INPE/TerraBrasilis'})),
  municipios: async () => HY.fc(await HY.tb('prodes-legal-amz:municipalities_legal_amazon', "geocodigo LIKE '12%'"), 0.0001, p => ({nome: p.nome, geocodigo: String(p.geocodigo)})),
  tis: async () => {
    const fc = HY.fc(await HY.tb('prodes-legal-amz:indigenous_area_legal_amazon', `INTERSECTS(geom, ${HY.acreWkt()})`), 0.0001, p => ({nome: p.terrai_nom, etnia: p.etnia_nome, fase: p.fase_ti}));
    fc.features.forEach(f => { f.properties.rotulo = 'TI ' + f.properties.nome; });
    return fc;
  },
  // MMA's CNUC is the official registry; TerraBrasilis adds the state Resex do Rio Gregório, which CNUC lacks.
  ucs: async () => {
    const cnuc = HY.fc(await HY.inde('MMA:cnuc_26_07_31', "uf LIKE '%ACRE%'"), 0.0001, p => ({nome: HY.title(p.nome_uc), categoria: p.categoria, grupo: p.grupo, esfera: p.esfera,
      criacao: (p.cria_ano || '').slice(-4), area_ha: Math.round(parseFloat(p.ha_total)), gestor: HY.title(p.org_gestor), fonte: 'MMA/CNUC 07/2026'}));
    const tb = HY.fc(await HY.tb('prodes-legal-amz:conservation_units_legal_amazon', `INTERSECTS(geom, ${HY.acreWkt()})`), 0.0001, p => ({nome: HY.title(p.nome), categoria: 'Reserva Extrativista',
      grupo: 'Uso Sustentável', esfera: 'Estadual', criacao: String(p.ano_cria || ''), area_ha: null, gestor: '', fonte: 'INPE/TerraBrasilis'}));
    const have = new Set(cnuc.features.map(f => f.properties.nome.toLowerCase()));
    return {type: 'FeatureCollection', features: [...cnuc.features, ...tb.features.filter(f => /Rio Gregório/.test(f.properties.nome) && !have.has(f.properties.nome.toLowerCase()))]};
  },
  prodes: async () => {
    const feats = [];
    for (let y = 2016; y <= 2025; y++) { feats.push(...await HY.tb('prodes-legal-amz:yearly_deforestation', `state='AC' AND year=${y}`)); HY.say('prodes ' + y + ' -> ' + feats.length); }
    return HY.fc(feats, 0.0001, p => ({ano: p.year, area_ha: Math.round(p.area_km * 1000) / 10, data_imagem: p.image_date}));
  },
  deter: async () => {
    const since = new Date(Date.now() - 365 * 864e5).toISOString().slice(0, 10);
    return HY.fc(await HY.tb('deter-amz:deter_amz', `uf='AC' AND view_date>='${since}'`), 0.0001,
      p => ({classe: HY.DETER[p.classname] || p.classname, data: p.view_date, area_ha: Math.round(p.areamunkm * 1000) / 10, municipio: p.municipality, satelite: p.satellite}));
  },
  aerodromos: async () => {
    const f = [...await HY.inde('MInfra:Aeródromos civis públicos', "uf='AC'"), ...await HY.inde('MInfra:Aeródromos civis privados', "uf='Acre'")];
    return {type: 'FeatureCollection', features: f.map(x => { const p = x.properties, codigo = p.codigo_oac || p.codigo_aoc || '', nome = HY.title(p.nome);
      return {type: 'Feature', properties: {nome, tipo: p.uf === 'AC' ? 'Público' : 'Privado', codigo, rotulo: codigo ? `${nome} (${codigo})` : nome, municipio: HY.title(p.municipio),
        pista_m: parseInt(p.compriment) || null, superficie: p.superficie}, geometry: {type: 'Point', coordinates: x.geometry.coordinates[0].map(HY.r5)}}; })};
  },
  estradas: async () => HY.osmWays(await HY.overpass(HY.AREA + 'way["highway"~"^(motorway|trunk|primary|secondary|tertiary|unclassified|track)(_link)?$"](area.a);out geom;'), 0.00003, t => {
    const codigo = HY.ref(t.ref), h = (t.highway || '').replace('_link', '');
    const tipo = /BR-/.test(codigo) ? 'Rodovia federal' : /AC-/.test(codigo) ? 'Rodovia estadual' : h === 'unclassified' ? 'Estrada vicinal' : h === 'track' ? 'Ramal' : 'Estrada';
    return {tipo, codigo, nome: t.name || '', rotulo: codigo || t.name || '', pavimento: HY.surface(t.surface)};
  }),
  ruas: async () => HY.osmWays(await HY.overpass(HY.AREA + 'way["highway"~"^(residential|living_street)$"](area.a);out geom;'), 0.00003,
    t => ({categoria: 'Via urbana', nome: t.name || '', pavimento: HY.surface(t.surface)})),
  rios: async () => HY.osmWays(await HY.overpass(HY.AREA + 'way["waterway"~"^(river|stream)$"](area.a);out geom;'), 0.00005,
    t => ({categoria: t.waterway === 'river' ? 'Rio' : 'Igarapé', nome: t.name || ''})),
  servicos: async () => {
    const els = await HY.overpass(HY.AREA + 'nwr["amenity"~"^(school|hospital|clinic|doctors)$"](area.a);out center tags;');
    const cat = {school: 'Escola', hospital: 'Hospital', clinic: 'Unidade de saúde', doctors: 'Unidade de saúde'};
    return {type: 'FeatureCollection', features: els.map(e => { const c = e.type === 'node' ? [e.lon, e.lat] : e.center && [e.center.lon, e.center.lat];
      return c && {type: 'Feature', properties: {categoria: cat[e.tags.amenity], nome: e.tags.name || ''}, geometry: {type: 'Point', coordinates: c.map(HY.r5)}}; }).filter(Boolean)};
  },
  // Footprints of the scenes in the map, from the layer names "CBERS-4A YYYY-MM-DD ppp/rrr".
  cenas: async () => {
    const names = [...new Set([...document.querySelectorAll('input')].map(i => i.value).filter(v => /^CBERS-4A \d{4}-\d\d-\d\d \d{3}\/\d{3}$/.test(v)))];
    const feats = [];
    for (const n of names) {
      const [, y, m, d, path, row] = n.match(/(\d{4})-(\d\d)-(\d\d) (\d{3})\/(\d{3})/);
      const it = await (await fetch(`https://data.inpe.br/bdc/stac/v1/collections/CB4A-WPM-PCA-FUSED-1/items/CBERS4A_WPM_PCA_RGB321_${y}${m}${d}_${path}_${row}`)).json();
      feats.push({type: 'Feature', properties: {data: `${y}-${m}-${d}`, orbita_ponto: `${path}/${row}`, camada: n}, geometry: HY.geom(it.geometry, 0.0001)});
    }
    return {type: 'FeatureCollection', features: feats.filter(f => f.geometry)};
  },
};
HY.run = async keys => {
  for (const k of keys) {
    try { const t0 = Date.now(); HY.data[k] = await HY.build[k]();
      HY.say(`${k}: ${HY.data[k].features.length} features, ${(JSON.stringify(HY.data[k]).length / 1e6).toFixed(1)} MB, ${Math.round((Date.now() - t0) / 1000)} s`); }
    catch (e) { HY.say(`${k}: ERROR ${e}`); }
  }
};

// ---- Upload through Add data > Upload data. Atlas names the layer after the file,
// with spaces turned into underscores; HY.NAMES holds the final names for HY.rename.
HY.NAMES = {
  aerodromos: 'Aeródromos (ANAC)', servicos: 'Escolas e saúde (OSM)', estradas: 'Estradas e ramais (OSM)', ruas: 'Ruas urbanas (OSM)',
  rios: 'Rios e igarapés (OSM)', deter: 'Alertas DETER, últimos 12 meses (INPE)', prodes: 'Desmatamento PRODES 2016-2025 (INPE)',
  tis: 'Terras Indígenas (FUNAI via INPE)', ucs: 'Unidades de Conservação (MMA/CNUC)', municipios: 'Municípios (IBGE via INPE)',
  cenas: 'Datas das imagens CBERS-4A', limite: 'Limite do Acre',
};
HY.btn = t => [...document.querySelectorAll('button')].find(b => b.textContent.trim() === t);
HY.layerNames = () => new Set([...document.querySelectorAll('input:not([type=file])')].map(i => i.value));
HY.upload = async key => {
  const before = HY.layerNames(), file = key + '.geojson';
  if (!HY.btn('Upload data')) { HY.btn('Add data').click(); await HY.until(() => HY.btn('Upload data'), 15000, 'add data'); }
  HY.btn('Upload data').click(); await HY.sleep(1200);
  const input = await HY.until(() => [...document.querySelectorAll('input[type=file]')].find(i => i.closest('[role=dialog]')), 10000, 'file input');
  const dt = new DataTransfer(); dt.items.add(new File([JSON.stringify(HY.data[key])], file, {type: 'application/geo+json'}));
  input.files = dt.files; input.dispatchEvent(new Event('change', {bubbles: true}));
  await HY.until(() => /1 ready/.test(document.querySelector('[role=dialog]').innerText), 60000, 'file ready');
  HY.btn('Upload').click();
  const name = await HY.until(() => [...HY.layerNames()].find(v => !before.has(v) && v === key), 600000, 'layer ' + key);
  await HY.rename(name, HY.NAMES[key]);
};
// New layers land at the top of the list, so upload the bottom one first.
HY.uploadAll = async keys => { for (const k of keys) { await HY.upload(k); HY.say('uploaded ' + k); await HY.sleep(2500); } };

// Rename: double-click the name, type, then focus out (Atlas saves on blur, not on Enter).
HY.rename = async (from, to) => {
  const find = v => [...document.querySelectorAll('input')].find(x => x.value === v);
  let inp = find(from);
  inp.dispatchEvent(new MouseEvent('dblclick', {bubbles: true, detail: 2})); await HY.sleep(700); inp = find(from);
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(inp, to);
  inp.dispatchEvent(new Event('input', {bubbles: true})); await HY.sleep(300);
  inp.dispatchEvent(new FocusEvent('focusout', {bubbles: true})); await HY.sleep(1500);
};

// ---- Styles, in Atlas' style schema. Colour by field = legacy categorical function plus
// "<fill|outline>-field"; other values may be MapLibre expressions without ["zoom"]
// (Atlas wraps them in "coalesce"). "zoom-visibility" applies after a reload.
const cat = (prop, stops) => ({property: prop, type: 'categorical', stops});
const label = (field, o = {}) => ({'text-field': ['format', field], 'text-color': o.color || '#ffffff', 'text-halo-color': o.halo || '#111111',
  'text-halo-width': o.haloW || 1.5, 'text-size': o.size || 12, ...(o.line ? {'symbol-placement': 'Line'} : {})});
const noFill = {'fill-visible': false, 'fill-opacity': 0};
const deter = cat('classe', [['Desmatamento (corte raso)', '#ff1a1a'], ['Desmatamento com vegetação', '#ff7a00'], ['Degradação', '#ffd400'],
  ['Cicatriz de queimada', '#b14dff'], ['Corte seletivo', '#ff5cc8'], ['Mineração', '#00e5ff']]);
const roads = cat('tipo', [['Estrada', '#ffd60a'], ['Estrada vicinal', '#f2e2c4'], ['Ramal', '#d9a066'], ['Rodovia estadual', '#ff9500'], ['Rodovia federal', '#ff3b30']]);
const rivers = cat('categoria', [['Igarapé', 'rgba(143,220,255,0.6)'], ['Rio', '#38b6ff']]);
const ucs = cat('grupo', [['Proteção Integral', '#00ff9c'], ['Uso Sustentável', '#b6ff4d']]);
const c = ['get', 'camada'];
HY.STYLES = {
  aerodromos: {'circle-color': cat('tipo', [['Privado', '#bf5af2'], ['Público', '#ff2d95']]), 'circle-radius': 6.5, 'circle-opacity': 1, 'circle-stroke-color': '#ffffff',
    'circle-stroke-width': 1.5, 'circle-stroke-opacity': 1, ...label(['get', 'rotulo']), 'text-anchor': 'top', 'text-offset': ['literal', [0, 0.9]]},
  // School names are left to the popup; only health units get a permanent label.
  servicos: {'circle-color': cat('categoria', [['Escola', '#64d2ff'], ['Hospital', '#ff453a'], ['Unidade de saúde', '#ff9f0a']]), 'circle-radius': 4.5, 'circle-opacity': 1,
    'circle-stroke-color': '#111111', 'circle-stroke-width': 1, 'circle-stroke-opacity': 1, 'zoom-visibility': [12, 23],
    ...label(['case', ['==', ['get', 'categoria'], 'Escola'], '', ['get', 'nome']], {size: 11}), 'text-anchor': 'top', 'text-offset': ['literal', [0, 0.7]]},
  estradas: {'outline-color': roads, 'line-color': roads, 'circle-stroke-color': roads, 'outline-field': {value: 'tipo', type: 'str'}, 'outline-opacity': 0.95,
    'outline-width': ['match', ['get', 'tipo'], 'Rodovia federal', 3.5, 'Rodovia estadual', 2.5, 'Estrada', 2, 'Estrada vicinal', 1.5, 1.2], ...label(['get', 'rotulo'], {line: true})},
  ruas: {'outline-color': '#ffffff', 'line-color': '#ffffff', 'outline-width': 1, 'outline-opacity': 0.75, 'zoom-visibility': [12, 23], ...label(['get', 'nome'], {size: 10, line: true})},
  rios: {'outline-color': rivers, 'line-color': rivers, 'outline-field': {value: 'categoria', type: 'str'}, 'outline-width': ['match', ['get', 'categoria'], 'Rio', 2.2, 0.8],
    'outline-opacity': 0.95, ...label(['get', 'nome'], {size: 11, color: '#c9f1ff', halo: '#00243a', line: true})},
  deter: {'fill-color': deter, 'fill-field': {value: 'classe', type: 'str'}, 'fill-opacity': 0.3, 'fill-visible': true,
    'outline-color': deter, 'outline-field': {value: 'classe', type: 'str'}, 'outline-width': 1.6, 'outline-opacity': 1},
  prodes: {'fill-color': cat('ano', [[2016, '#fff3b0'], [2017, '#ffe08a'], [2018, '#ffc857'], [2019, '#ffa63d'], [2020, '#ff8534'], [2021, '#ff5e2e'],
    [2022, '#f2352b'], [2023, '#d61f4f'], [2024, '#b5177a'], [2025, '#e600e6']]), 'fill-field': {value: 'ano', type: 'int'}, 'fill-opacity': 0.55, 'fill-visible': true, 'outline-visible': false},
  tis: {'fill-color': '#ff9f0a', 'fill-opacity': 0.1, 'fill-visible': true, 'outline-color': '#ff9f0a', 'outline-width': 2, 'outline-opacity': 1, ...label(['get', 'rotulo'], {color: '#ffd29a'})},
  ucs: {'fill-color': ucs, 'fill-field': {value: 'grupo', type: 'str'}, 'fill-opacity': 0.07, 'fill-visible': true, 'outline-color': ucs, 'outline-field': {value: 'grupo', type: 'str'},
    'outline-width': 2, 'outline-opacity': 1, 'outline-dasharray': ['literal', [3, 2]], ...label(['get', 'nome'], {color: '#c8ffd8'})},
  municipios: {...noFill, 'outline-color': '#ffffff', 'outline-width': 1, 'outline-opacity': 0.55, 'outline-dasharray': ['literal', [4, 3]], ...label(['get', 'nome'], {size: 13, haloW: 2})},
  // dd/mm/yyyy from the "camada" string: Atlas types a date column as epoch ms in labels.
  cenas: {...noFill, 'outline-color': '#e6e6e6', 'outline-width': 0.8, 'outline-opacity': 0.6, 'outline-dasharray': ['literal', [1, 2]], 'zoom-visibility': [0, 9.5],
    ...label(['concat', ['slice', c, 17, 19], '/', ['slice', c, 14, 16], '/', ['slice', c, 9, 13]], {size: 11, color: '#e0f7ff'})},
  limite: {...noFill, 'outline-color': '#ffffff', 'outline-width': 2.5, 'outline-opacity': 0.9},
};
// Select the layer row, then call the style panel's own callbacks: one draws, the other saves.
HY.applyStyle = async key => {
  const name = HY.NAMES[key], inp = [...document.querySelectorAll('input')].find(x => x.value === name);
  let row = inp; for (let i = 0; i < 5; i++) row = row.parentElement;
  (row.closest('.legendItem') || row).click(); await HY.sleep(1500);
  const el = [...document.querySelectorAll('h2, button, div')].find(e => e.textContent.trim() === 'Styling');
  let f = el[Object.keys(el).find(k => k.startsWith('__reactFiber'))];
  while (f && !(f.memoizedProps && f.memoizedProps.datasetId && f.memoizedProps.style && f.memoizedProps.layer)) f = f.return;
  const fns = []; for (let h = f.memoizedState; h; h = h.next) if (Array.isArray(h.memoizedState) && typeof h.memoizedState[0] === 'function') fns.push(h.memoizedState[0]);
  const draw = fns.find(g => g.toString() === '(e,t)=>{f(e,t)}'), save = fns.find(g => /updateDatasetStyle/.test(g.toString()));
  if (!draw || !save) throw new Error('style callbacks not found; set the style in the Layer panel');
  draw(f.memoizedProps.datasetId, {paint: HY.STYLES[key]}); save(HY.STYLES[key]); await HY.sleep(1500);
};
'HY ready';
}
