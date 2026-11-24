"use strict";
/* simcfg editör prototipi.
 * Tüm formlar /api/catalog'daki tanımlayıcılardan üretilir; yeni tipler için editör kodu değişmez.
 * Düzenleme modeli: kullanıcı sadece SEÇİLİ dokümanın ham (raw) halini değiştirir. Miras alınan değerler
 * gri gösterilir; bir değeri değiştirmek, bu dokümana o alan için bir override yazar. "↺" override'ı kaldırır.
 */

// ============================================================================ yardımcılar
const $ = (s, el = document) => el.querySelector(s);
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k === "value") el.value = v;
    else if (k === "checked" || k === "selected" || k === "disabled") el[k] = !!v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}
const clone = (x) => (x === undefined ? undefined : JSON.parse(JSON.stringify(x)));
const txt = (t) => (t == null ? "" : typeof t === "string" ? t : t.tr || t.en || Object.values(t)[0] || "");
const short = (id) => String(id || "").split(".").pop();
const isObj = (x) => x && typeof x === "object" && !Array.isArray(x);
const isDel = (x) => isObj(x) && x.$delete === true;
const fmt = (v) => (v === undefined ? "—" : typeof v === "string" ? v : JSON.stringify(v));
function getIn(o, path) { for (const p of path) { if (!isObj(o) && !Array.isArray(o)) return undefined; o = o[p]; } return o; }
function setIn(o, path, v) {
  for (let i = 0; i < path.length - 1; i++) { if (!isObj(o[path[i]])) o[path[i]] = {}; o = o[path[i]]; }
  o[path[path.length - 1]] = v;
}
function deleteIn(root, path) {
  const parents = [];
  let o = root;
  for (let i = 0; i < path.length - 1; i++) { if (!isObj(o[path[i]])) return; parents.push([o, path[i]]); o = o[path[i]]; }
  delete o[path[path.length - 1]];
  for (let i = parents.length - 1; i >= 1; i--) {  // boşalan ara nesneleri temizle (spec hariç)
    const [p, k] = parents[i];
    if (isObj(p[k]) && Object.keys(p[k]).length === 0) delete p[k]; else break;
  }
}
async function call(url, body) {
  const r = await fetch(url, body === undefined ? {} : { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
  const d = await r.json();
  if (!r.ok) throw new Error(d.error || r.statusText);
  return d;
}
function parseLoose(s) {
  const t = s.trim();
  if (t === "") return undefined;
  try { return JSON.parse(t); } catch { return t; }
}
function looseText(v) { return v === undefined ? "" : typeof v === "string" ? v : JSON.stringify(v); }

const KIND_ORDER = ["Scenario", "UnitTemplate", "PlatformTemplate", "SystemTemplate", "Munition", "Behavior", "ActionType", "ConditionType",
  "EventType", "Maneuver", "AreaType", "SystemType", "PlatformType", "MunitionType", "Enumeration", "UnitCatalog"];
const KIND_LABEL = {
  Scenario: "Senaryolar", UnitTemplate: "Birlikler", PlatformTemplate: "Platformlar", SystemTemplate: "Sistemler", Munition: "Mühimmatlar",
  Behavior: "Davranışlar", ActionType: "Aksiyon tipleri", ConditionType: "Koşul tipleri", EventType: "Olay tipleri",
  Maneuver: "Manevralar", AreaType: "Alan tipleri", SystemType: "Sistem tipleri", PlatformType: "Platform tipleri",
  MunitionType: "Mühimmat tipleri", Enumeration: "Listeler", UnitCatalog: "Birimler",
};

// ============================================================================ durum
const S = {
  cat: null, docs: {}, diags: [], sel: null, view: null, draft: null, dirty: false,
  tab: "form", filter: "", collapsed: { ActionType: true, ConditionType: true, EventType: true, Enumeration: true, UnitCatalog: true },
  open: {}, diagScope: "doc", status: "", compiled: null,
};
const LV = () => S.view;  // canlı görünüm (son kontrol/yüklemeden)

async function loadCatalog() {
  setStatus("yükleniyor…");
  const r = await call("/api/catalog");
  S.cat = r.catalog; S.diags = r.diagnostics; S.docs = {};
  for (const list of Object.values(S.cat.documents)) for (const e of list) S.docs[e.id] = e;
  setStatus("");
  renderSidebar(); renderCounts(); renderDiags();
}

async function select(id, { force = false } = {}) {
  if (S.dirty && !force && id !== S.sel && !confirm("Kaydedilmemiş değişiklikler var. Vazgeçilsin mi?")) return;
  S.sel = id; S.compiled = null;
  if (location.hash !== "#" + encodeURIComponent(id)) history.replaceState(null, "", "#" + encodeURIComponent(id));
  const v = await call("/api/doc?id=" + encodeURIComponent(id));
  S.view = v; S.draft = clone(v.raw); S.dirty = false;
  if (S.tab === "compile" && v.kind !== "Scenario") S.tab = "form";
  render();
}

let checkTimer = null;
function markDirty() {
  S.dirty = true;
  setStatus("doğrulanıyor…");
  clearTimeout(checkTimer);
  checkTimer = setTimeout(runCheck, 300);
  render();
}
async function runCheck() {
  try {
    const r = await call("/api/check", { id: S.sel, raw: S.draft });
    S.view = r.view; S.diags = r.diagnostics;
    setStatus("");
  } catch (e) { setStatus("kontrol hatası: " + e.message); }
  render(); renderCounts();
}
function setOwn(path, value) {
  if (value === undefined) deleteIn(S.draft, path); else setIn(S.draft, path, clone(value));
  markDirty();
}
async function save() {
  try {
    const r = await call("/api/save", { id: S.sel, raw: S.draft });
    const newId = r.view.id;
    await loadCatalog();
    S.dirty = false;
    await select(newId, { force: true });
    setStatus("kaydedildi: " + r.saved);
  } catch (e) { alert("Kaydedilemedi: " + e.message); }
}
function revert() { S.draft = clone(S.view.raw); S.dirty = false; select(S.sel, { force: true }); }
function setStatus(s) { S.status = s; const el = $("#status"); if (el) { el.textContent = statusText(); el.className = "status" + (S.dirty ? " dirty" : ""); } }
const statusText = () => S.status || (S.dirty ? "kaydedilmemiş değişiklikler" : "");

// ============================================================================ tip yardımcıları
function isA(typeId, base) { const e = S.docs[typeId]; return !!e && e.lineage.includes(base); }
function enumValues(d) {
  const vals = (d.values || []).map((v) => (isObj(v) ? v.value : v));
  if (d.enumRef && S.docs[d.enumRef]) vals.push(...(S.docs[d.enumRef].spec.values || []).map((v) => (isObj(v) ? v.value : v)));
  return vals;
}
function docsOf(kind, filter) {
  return Object.values(S.docs).filter((e) => e.kind === kind && (!filter || filter(e))).sort((a, b) => a.id.localeCompare(b.id));
}
function refOptions(d) {
  return docsOf(d.refKind, (e) => (!e.abstract || d.allowAbstract) &&
    Object.entries(d.constraint || {}).every(([k, base]) => isA(e.spec[k], base)));
}
function provOf(path) {
  const prov = (LV() && LV().provenance) || {};
  let best = "";
  for (const p of Object.keys(prov)) if ((path === p || path.startsWith(p + ".")) && p.length > best.length) best = p;
  if (best) return prov[best];
  const lin = LV().lineage || [];
  const subs = Object.entries(prov).filter(([p]) => p.startsWith(path + ".")).map(([, o]) => o);
  return subs.length ? subs.sort((a, b) => lin.indexOf(b) - lin.indexOf(a))[0] : null;
}
function origins(prefix) {
  const prov = (LV() && LV().provenance) || {};
  return [...new Set(Object.entries(prov).filter(([p]) => p === prefix || p.startsWith(prefix + ".")).map(([, o]) => o))];
}
function docDiags(id = S.sel) { return S.diags.filter((d) => d.doc === id); }
function diagsAt(prefix) {
  return docDiags().filter((d) => d.path && (d.path === prefix || d.path.startsWith(prefix + ".") || d.path.startsWith(prefix + "[")));
}
function srcChip(origin) {
  if (!origin) return h("span", { class: "hint" }, "—");
  if (origin === S.sel) return h("span", { class: "chip own" }, "bu doküman");
  return h("span", { class: "chip link", title: origin, onclick: () => select(origin) }, short(origin));
}

// ============================================================================ kenar çubuğu
function renderSidebar() {
  const tree = $("#tree"); tree.replaceChildren();
  const q = S.filter.toLowerCase();
  const errs = {}, warns = {};
  for (const d of S.diags) { if (!d.doc) continue; if (d.severity === "error") errs[d.doc] = (errs[d.doc] || 0) + 1; if (d.severity === "warning") warns[d.doc] = (warns[d.doc] || 0) + 1; }
  for (const kind of KIND_ORDER) {
    const list = S.cat.documents[kind] || [];
    if (!list.length) continue;
    const match = (e) => !q || e.id.toLowerCase().includes(q) || txt(e.meta.name).toLowerCase().includes(q);
    const ids = new Set(list.map((e) => e.id));
    const subtreeMatch = (e) => match(e) || e.children.some((c) => ids.has(c) && subtreeMatch(S.docs[c]));
    const visible = list.filter(subtreeMatch);
    if (q && !visible.length) continue;
    const collapsed = !q && S.collapsed[kind];
    tree.append(h("div", { class: "kind-head", onclick: () => { S.collapsed[kind] = !S.collapsed[kind]; renderSidebar(); } },
      h("span", {}, (collapsed ? "▸ " : "▾ ") + (KIND_LABEL[kind] || kind)), h("span", {}, list.length)));
    if (collapsed) continue;
    const roots = visible.filter((e) => !e.extends.some((p) => ids.has(p)));
    const seen = new Set();
    const node = (e, depth) => {
      const key = e.id + "@" + depth;
      if (seen.has(key) || depth > 12) return;
      seen.add(key);
      tree.append(h("div", {
        class: "node" + (e.id === S.sel ? " sel" : "") + (e.abstract ? " abstract" : ""),
        style: `padding-left:${6 + depth * 14}px`, title: e.id, onclick: () => select(e.id),
      }, h("span", { class: "nname" }, txt(e.meta.name) || short(e.id)), h("span", { class: "nid" }, short(e.id)),
      errs[e.id] ? h("span", { class: "badge e" }, errs[e.id]) : null, warns[e.id] ? h("span", { class: "badge w" }, warns[e.id]) : null));
      for (const c of e.children) if (ids.has(c) && subtreeMatch(S.docs[c])) node(S.docs[c], depth + 1);
    };
    roots.forEach((e) => node(e, 0));
  }
}
function renderCounts() {
  const e = S.diags.filter((d) => d.severity === "error").length, w = S.diags.filter((d) => d.severity === "warning").length;
  $("#counts").replaceChildren(e || w ? h("span", {}, h("span", { class: "e" }, `${e} hata`), " · ", h("span", { class: "w" }, `${w} uyarı`)) : h("span", { class: "ok" }, "✓ çalışma alanı geçerli"));
}

// ============================================================================ tanı paneli
function renderDiags() {
  const list = S.diagScope === "doc" ? docDiags() : S.diags;
  const box = $("#diag-list"); box.replaceChildren();
  document.querySelectorAll(".diag-tab").forEach((b) => b.classList.toggle("active", b.dataset.scope === S.diagScope));
  $(".diag-tab[data-scope=doc]").textContent = `Bu doküman (${docDiags().length})`;
  $(".diag-tab[data-scope=all]").textContent = `Tümü (${S.diags.length})`;
  if (!list.length) { box.append(h("div", { class: "empty" }, S.sel || S.diagScope === "all" ? "Tanı yok ✓" : "Bir doküman seçin")); return; }
  const icon = { error: "●", warning: "▲", info: "ℹ" };
  for (const d of list) {
    box.append(h("div", { class: "diag", onclick: () => d.doc && d.doc !== S.sel && select(d.doc) },
      h("span", { class: "sev " + d.severity }, icon[d.severity]), h("span", { class: "code" }, d.code),
      h("span", {}, d.message, " ", h("span", { class: "where" }, [S.diagScope === "all" ? d.doc : null, d.path].filter(Boolean).join(" @ ")))));
  }
}

// ============================================================================ ana çizim
function render() {
  const ae = document.activeElement, focusKey = ae && ae.dataset && ae.dataset.path;
  const selStart = ae && "selectionStart" in ae ? ae.selectionStart : null;
  const main = $("#main"), scroll = main.scrollTop;
  main.replaceChildren(S.sel && S.view && !S.view.missing ? renderDoc() : renderWelcome());
  main.scrollTop = scroll;
  if (focusKey) {
    const el = main.querySelector(`[data-path="${CSS.escape(focusKey)}"]`);
    if (el) { el.focus(); try { if (selStart !== null) el.setSelectionRange(selStart, selStart); } catch { /* select */ } }
  }
  renderSidebar(); renderDiags();
}

function renderWelcome() {
  const stats = KIND_ORDER.filter((k) => (S.cat.documents[k] || []).length).map((k) =>
    h("div", { class: "stat", onclick: () => { S.collapsed[k] = false; renderSidebar(); } }, h("b", {}, S.cat.documents[k].length), KIND_LABEL[k]));
  return h("div", { class: "welcome" },
    h("h1", {}, "simcfg editör"),
    h("p", { class: "hint" }, "Soldan bir doküman seçin. Gri değerler miras alınmıştır; bir değeri değiştirmek bu dokümana override yazar, ↺ kaldırır. Değişiklikler kaydetmeden önce sunucuda canlı doğrulanır."),
    h("div", { class: "stats" }, stats),
    h("p", { class: "hint" }, "Paketler: ", Object.entries(S.cat.packages).map(([k, p]) => `${k} ${p.version}`).join(", ")));
}

function renderDoc() {
  const v = LV(), e = S.docs[S.sel] || {}, raw = S.draft;
  const head = h("div", { class: "doc-head" },
    h("h1", {}, txt(raw.meta && raw.meta.name) || short(S.sel)),
    h("div", { class: "meta" }, h("span", { class: "chip kind" }, KIND_LABEL[v.kind] || v.kind), h("code", {}, v.id),
      raw.abstract ? h("span", { class: "chip" }, "abstract") : null, h("span", { class: "hint" }, v.file + (v.index !== null ? `[${v.index}]` : ""))),
    h("div", { class: "lineage" }, "soy: ", v.lineage.map((id, i) => [i ? " → " : "", id === S.sel ? h("b", {}, id) : h("a", { onclick: () => select(id) }, id)])),
    v.children.length ? h("div", { class: "lineage" }, "türevler: ", v.children.map((c, i) => [i ? ", " : "", h("a", { onclick: () => select(c) }, c)])) : null);
  const toolbar = h("div", { class: "toolbar" },
    h("button", { class: "primary", disabled: !S.dirty, onclick: save }, "Kaydet"),
    h("button", { disabled: !S.dirty, onclick: revert }, "Vazgeç"),
    h("button", { onclick: () => deriveDialog(S.sel) }, "Türet…"),
    h("span", { id: "status", class: "status" + (S.dirty ? " dirty" : "") }, statusText()));
  const tabs = [["form", "Form"], ["resolved", "Çözülmüş"], ["json", "JSON"]];
  if (v.kind === "Scenario") tabs.push(["compile", "Derle"]);
  const tabBar = h("div", { class: "tabs" }, tabs.map(([k, l]) => h("button", { class: S.tab === k ? "active" : "", onclick: () => { S.tab = k; render(); } }, l)));
  let body;
  if (S.tab === "json") body = renderJson();
  else if (S.tab === "resolved") body = renderResolved();
  else if (S.tab === "compile") body = renderCompile();
  else body = renderForm();
  void e;
  return h("div", {}, head, toolbar, tabBar, body);
}

function renderForm() {
  switch (LV().kind) {
    case "SystemTemplate": return formTemplate("systemType", [["parameters", "parameters", "Parametreler"], ["initialState", "state", "Başlangıç durumu"]]);
    case "Munition": return formTemplate("munitionType", [["parameters", "parameters", "Parametreler"]]);
    case "PlatformTemplate": return formPlatform();
    case "Behavior": return formBehavior();
    case "Scenario": return formScenario();
    case "Enumeration": return formEnum();
    case "UnitTemplate": return formUnit();
    default: return formTypeDescriptors();
  }
}

// ============================================================================ parametre tablosu (model editörünün çekirdeği)
function paramTable(descs, ownPath, effective, provPrefix) {
  descs = descs || {};
  const own = getIn(S.draft.spec, ownPath) || {};
  const keys = Object.keys(descs).sort((a, b) => ((descs[a].ui || {}).order ?? 999) - ((descs[b].ui || {}).order ?? 999));
  const groups = {};
  for (const k of keys) (groups[(descs[k].ui || {}).group || "Genel"] ||= []).push(k);
  const unknown = Object.keys(own).filter((k) => !(k in descs));
  if (unknown.length) groups["Tanımsız (hatalı) alanlar"] = unknown;
  const out = h("div");
  for (const [g, ks] of Object.entries(groups)) {
    if (Object.keys(groups).length > 1) out.append(h("h3", { class: "group" }, g));
    const rows = ks.map((k) => {
      const d = descs[k] || { type: "any" };
      const ov = own[k];
      const eff = effective ? effective[k] : undefined;
      const specPath = ["spec", ...ownPath, k].join(".");
      const errs = diagsAt(specPath).filter((x) => x.severity === "error");
      return h("tr", { class: (ov !== undefined ? "own " : "") + (errs.length ? "has-err" : "") },
        h("td", { class: "pname", title: txt(d.description) },
          h("div", {}, txt(d.label) || k, d.required ? h("span", { class: "req", title: "zorunlu" }, "*") : null, d.readonly ? h("span", { class: "ro", title: "salt okunur (motor yazar)" }, "🔒") : null),
          h("div", { class: "k" }, k, d.unit ? ` · ${d.unit}` : "", ` · ${d.type}`)),
        h("td", { class: "pin" }, valueInput(d, ov, eff, ["spec", ...ownPath, k]), errs.map((x) => h("div", { class: "diag-inline" }, x.message))),
        h("td", { class: "peff", title: "efektif (normalize edilmiş) değer" }, fmt(eff), d.unit && eff !== undefined && eff !== null ? " " + d.unit : ""),
        h("td", { class: "psrc" }, ov !== undefined ? srcChip(S.sel) : srcChip(provOf(provPrefix + k)),
          ov !== undefined ? h("button", { class: "ghost small", title: "override'ı kaldır (mirasa dön)", onclick: () => setOwn(["spec", ...ownPath, k], undefined) }, "↺") : null));
    });
    out.append(h("table", { class: "grid" }, h("tr", {}, h("th", {}, "Alan"), h("th", {}, "Bu dokümandaki değer"), h("th", {}, "Efektif"), h("th", {}, "Kaynak")), rows));
  }
  if (!keys.length && !unknown.length) out.append(h("div", { class: "hint" }, "Tanımlı parametre yok."));
  return out;
}

function valueInput(d, own, eff, path) {
  const key = path.join(".");
  const set = (v) => setOwn(path, v);
  const inheritLabel = `(miras: ${fmt(eff)})`;
  if (d.type === "boolean") {
    return h("select", { dataset: { path: key }, onchange: (ev) => set(ev.target.value === "" ? undefined : ev.target.value === "true") },
      h("option", { value: "" }, inheritLabel), h("option", { value: "true", selected: own === true }, "true"), h("option", { value: "false", selected: own === false }, "false"));
  }
  if (d.type === "enum" || d.type === "ref") {
    const opts = d.type === "enum" ? enumValues(d).map((x) => [String(x), String(x)]) : refOptions(d).map((e) => [e.id, `${txt(e.meta.name) || short(e.id)} (${e.id})`]);
    if (own !== undefined && own !== null && !opts.some(([v]) => v === String(own))) opts.push([String(own), `${own} (geçersiz)`]);
    return h("select", { dataset: { path: key }, onchange: (ev) => set(ev.target.value === "" ? undefined : ev.target.value === "\u0000null" ? null : ev.target.value) },
      h("option", { value: "" }, inheritLabel), d.nullable ? h("option", { value: "\u0000null", selected: own === null }, "null") : null,
      opts.map(([v, l]) => h("option", { value: v, selected: own !== undefined && String(own) === v }, l)));
  }
  if (["number", "integer", "string", "entityRef", "areaRef", "routeRef", "sideRef", "systemRole"].includes(d.type)) {
    const numeric = d.type === "number" || d.type === "integer";
    return h("input", {
      type: "text", dataset: { path: key }, value: own === undefined ? "" : isObj(own) ? JSON.stringify(own) : String(own),
      placeholder: fmt(eff) + (numeric && d.unit ? " " + d.unit : ""),
      title: numeric ? `Sayı (${d.unit || "birimsiz"}) veya birimli: "80 nmi", "10 GHz"` : "",
      onchange: (ev) => {
        const t = ev.target.value.trim();
        if (t === "") return set(undefined);
        if (numeric) return set(t !== "" && isFinite(Number(t)) ? Number(t) : t.startsWith("{") ? parseLoose(t) : t);
        return set(t);
      },
    });
  }
  return jsonArea(own, eff, key, set);
}

function jsonArea(own, eff, key, set, rows) {
  const ta = h("textarea", {
    dataset: { path: key }, rows: rows || Math.min(8, Math.max(2, JSON.stringify(own ?? eff ?? "", null, 1).split("\n").length)),
    placeholder: eff === undefined ? "" : JSON.stringify(eff), value: own === undefined ? "" : JSON.stringify(own, null, 1),
    onchange: (ev) => {
      const t = ev.target.value.trim();
      if (t === "") return set(undefined);
      try { set(JSON.parse(t)); } catch { ev.target.classList.add("invalid"); }
    },
    oninput: (ev) => ev.target.classList.remove("invalid"),
  });
  return ta;
}

function card(title, ...kids) { return h("section", { class: "card" }, h("h2", {}, title), ...kids); }

// ============================================================================ SystemTemplate / Munition
function formTemplate(typeField, sections) {
  const v = LV(), spec = v.resolved.spec, typeId = spec[typeField], type = S.docs[typeId];
  const typeKind = typeField === "systemType" ? "SystemType" : "MunitionType";
  const eff = v.effective || {};
  const typeSel = h("select", { dataset: { path: "spec." + typeField }, onchange: (ev) => setOwn(["spec", typeField], ev.target.value || undefined) },
    S.draft.spec && S.draft.spec[typeField] === undefined ? h("option", { value: "" }, `(miras: ${typeId})`) : null,
    docsOf(typeKind, (e) => !e.abstract).map((e) => h("option", { value: e.id, selected: S.draft.spec && S.draft.spec[typeField] === e.id }, e.id)));
  const out = [card("Tip", h("div", { class: "inline" }, typeSel, type ? h("button", { class: "small", onclick: () => select(typeId) }, "tipi aç") : null,
    type && type.spec.implementation ? h("span", { class: "hint" }, "motor eklentisi: ", h("code", {}, type.spec.implementation)) : null))];
  if (!type) return h("div", {}, out, h("p", { class: "hint" }, "Tip çözülemedi."));
  for (const [specKey, typeKey, title] of sections) {
    out.push(card(title, paramTable(type.spec[typeKey], [specKey], eff[specKey === "initialState" ? "initialState" : "parameters"], specKey + ".")));
  }
  return h("div", {}, out);
}

// ============================================================================ PlatformTemplate
function formPlatform() {
  const v = LV(), spec = v.resolved.spec, eff = v.effective || {};
  const ptype = S.docs[spec.platformType];
  if (!ptype) return h("p", { class: "hint" }, "Platform tipi çözülemedi: " + spec.platformType);
  const allowed = ptype.spec.allowedSystemTypes || [];
  const sysTemplates = docsOf("SystemTemplate", (e) => !e.abstract && (!allowed.length || allowed.some((a) => isA(e.spec.systemType, a))));
  const ownSys = (S.draft.spec && S.draft.spec.systems) || {};
  const mounts = [...new Set([...Object.keys(spec.systems || {}), ...Object.keys(ownSys)])];

  const sysRows = mounts.map((mk) => {
    const m = (spec.systems || {})[mk], own = ownSys[mk];
    const deleted = isDel(own);
    const inherited = origins("systems." + mk).some((o) => o !== S.sel);
    if (deleted) {
      return h("tr", { class: "deleted" }, h("td", {}, mk), h("td", { colspan: 3 }, "kaldırıldı"),
        h("td", {}, h("button", { class: "small", onclick: () => setOwn(["spec", "systems", mk], undefined) }, "geri al")));
    }
    const fitted = !m || m.fitted !== false;
    const tmplId = m && m.template, tmpl = S.docs[tmplId];
    const st = tmpl && S.docs[tmpl.spec.systemType];
    const openKey = "mount:" + mk;
    const effSys = (eff.systems || {})[mk];
    const errs = diagsAt("spec.systems." + mk);
    return [
      h("tr", { class: (own ? "own " : "") + (errs.some((x) => x.severity === "error") ? "has-err" : "") },
        h("td", {}, h("b", {}, mk), h("div", { class: "hint" }, st ? short(st.id) : "?")),
        h("td", {}, h("select", { dataset: { path: `sys.${mk}.template` }, onchange: (ev) => setOwn(["spec", "systems", mk, "template"], ev.target.value || undefined) },
          h("option", { value: "" }, own && own.template ? "(mirasa dön)" : `(miras: ${tmplId || "-"})`),
          sysTemplates.map((e) => h("option", { value: e.id, selected: own && own.template === e.id }, e.id)))),
        h("td", {}, h("label", { class: "inline", title: "Sistem bu platformda bulunuyor mu?" },
          h("input", { type: "checkbox", checked: fitted, onchange: (ev) => setOwn(["spec", "systems", mk, "fitted"], ev.target.checked) }), "takılı")),
        h("td", {}, srcChip(provOf(`systems.${mk}.template`))),
        h("td", { class: "psrc" },
          h("button", { class: "small", onclick: () => { S.open[openKey] = !S.open[openKey]; render(); } }, S.open[openKey] ? "▾ parametreler" : "▸ parametreler"),
          own ? h("button", { class: "ghost small", title: "bu dokümandaki mount override'larını kaldır", onclick: () => setOwn(["spec", "systems", mk], undefined) }, "↺") : null,
          h("button", { class: "ghost small danger", title: "mount'u kaldır", onclick: () => setOwn(["spec", "systems", mk], inherited ? { $delete: true } : undefined) }, "✕"))),
      S.open[openKey] && st ? h("tr", {}, h("td", { colspan: 5 },
        errs.map((x) => h("div", { class: "diag-inline" }, x.path + ": " + x.message)),
        !fitted ? h("div", { class: "hint" }, "Takılı değil: derlenmiş çıktıya girmez.") : null,
        paramTable(st.spec.parameters, ["systems", mk, "parameters"], effSys && effSys.parameters, `systems.${mk}.parameters.`),
        h("details", {}, h("summary", {}, "başlangıç durumu"), paramTable(st.spec.state, ["systems", mk, "initialState"], effSys && effSys.initialState, `systems.${mk}.initialState.`)))) : null,
    ];
  });
  const newName = h("input", { type: "text", placeholder: "mount adı" });
  const newTmpl = h("select", {}, sysTemplates.map((e) => h("option", { value: e.id }, e.id)));
  const addSys = h("div", { class: "inline", style: "margin-top:8px" }, newName, newTmpl,
    h("button", { onclick: () => { const n = newName.value.trim(); if (/^[A-Za-z_][A-Za-z0-9_-]*$/.test(n)) setOwn(["spec", "systems", n], { template: newTmpl.value }); else alert("Geçersiz mount adı"); } }, "+ sistem ekle"));

  const ownB = (S.draft.spec && S.draft.spec.behaviors) || {};
  const atts = [...new Set([...Object.keys(spec.behaviors || {}), ...Object.keys(ownB)])];
  const behaviorDocs = docsOf("Behavior", (e) => !e.abstract);
  const bCards = atts.map((bk) => {
    const a = (spec.behaviors || {})[bk], own = ownB[bk];
    if (isDel(own)) return h("div", { class: "rule deleted" }, h("div", { class: "rule-head" }, h("span", { class: "rk" }, bk), "kaldırıldı", h("span", { class: "spacer" }),
      h("button", { class: "small", onclick: () => setOwn(["spec", "behaviors", bk], undefined) }, "geri al")));
    const bid = a && a.behavior, bdoc = S.docs[bid], effB = (eff.behaviors || {})[bk] || {};
    const enabled = !a || a.enabled !== false;
    const openKey = "beh:" + bk;
    const inherited = origins("behaviors." + bk).some((o) => o !== S.sel);
    const rules = effB.rules || {};
    return h("div", { class: "rule" + (enabled ? "" : " disabled") },
      h("div", { class: "rule-head" },
        h("input", { type: "checkbox", checked: enabled, title: "etkin", onchange: (ev) => setOwn(["spec", "behaviors", bk, "enabled"], ev.target.checked) }),
        h("span", { class: "rk" }, bk),
        h("select", { dataset: { path: `beh.${bk}.behavior` }, onchange: (ev) => setOwn(["spec", "behaviors", bk, "behavior"], ev.target.value || undefined) },
          h("option", { value: "" }, own && own.behavior ? "(mirasa dön)" : `(miras: ${bid || "-"})`),
          behaviorDocs.map((e) => h("option", { value: e.id, selected: own && own.behavior === e.id }, e.id))),
        srcChip(provOf(`behaviors.${bk}.behavior`)),
        h("span", { class: "spacer" }),
        h("button", { class: "small", onclick: () => { S.open[openKey] = !S.open[openKey]; render(); } }, S.open[openKey] ? "▾" : "▸"),
        own ? h("button", { class: "ghost small", title: "override'ları kaldır", onclick: () => setOwn(["spec", "behaviors", bk], undefined) }, "↺") : null,
        h("button", { class: "ghost small danger", onclick: () => setOwn(["spec", "behaviors", bk], inherited ? { $delete: true } : undefined) }, "✕")),
      h("div", { class: "rule-sum" },
        effB.bindings ? ["roller: ", Object.entries(effB.bindings).map(([r, m]) => `${r}→${m ?? "∅"}`).join(", "), " · "] : null,
        Object.keys(rules).length ? `${Object.values(rules).filter((r) => r.enabled).length}/${Object.keys(rules).length} kural etkin` : enabled ? "" : "devre dışı"),
      S.open[openKey] && bdoc ? h("div", { class: "rule-body" },
        h("div", {}, h("h3", { class: "group" }, "Parametre değerleri (bu platform için)"),
          paramTable(bdoc.spec.parameters, ["behaviors", bk, "parameters"], effB.parameters, `behaviors.${bk}.parameters.`)),
        h("div", {}, h("h3", { class: "group" }, "Kurallar (bu platformda aç/kapat)"),
          h("table", { class: "grid" }, Object.entries(rules).sort((x, y) => y[1].priority - x[1].priority).map(([rk, r]) => {
            const ownEn = getIn(S.draft.spec, ["behaviors", bk, "rules", rk, "enabled"]);
            return h("tr", { class: ownEn !== undefined ? "own" : "" },
              h("td", {}, h("input", { type: "checkbox", checked: r.enabled, onchange: (ev) => setOwn(["spec", "behaviors", bk, "rules", rk, "enabled"], ev.target.checked) })),
              h("td", { class: "mono" }, rk), h("td", {}, r.priority), h("td", { class: "hint" }, r.disabledReason || triggerText(r.trigger)),
              h("td", {}, ownEn !== undefined ? h("button", { class: "ghost small", onclick: () => setOwn(["spec", "behaviors", bk, "rules", rk, "enabled"], undefined) }, "↺") : null));
          }))),
        h("details", {}, h("summary", {}, "rol bağlamaları (bindings)"),
          jsonArea(own && own.bindings, a && a.bindings, `beh.${bk}.bindings`, (val) => setOwn(["spec", "behaviors", bk, "bindings"], val), 3))) : null);
  });
  const newB = h("input", { type: "text", placeholder: "ek adı" });
  const newBid = h("select", {}, behaviorDocs.map((e) => h("option", { value: e.id }, e.id)));
  return h("div", {},
    card("Platform tipi", h("div", { class: "inline" }, h("code", {}, ptype.id), h("button", { class: "small", onclick: () => select(ptype.id) }, "tipi aç"),
      h("span", { class: "hint" }, "dinamik model: ", h("code", {}, ptype.spec.implementation || "-")))),
    card("Platform parametreleri", paramTable(ptype.spec.parameters, ["parameters"], eff.parameters, "parameters.")),
    card("Sistemler", h("table", { class: "grid" }, h("tr", {}, h("th", {}, "Mount"), h("th", {}, "Şablon"), h("th", {}, "Bulundurma"), h("th", {}, "Kaynak"), h("th", {})), sysRows), addSys),
    card("Davranışlar", bCards, h("div", { class: "inline", style: "margin-top:8px" }, newB, newBid,
      h("button", { onclick: () => { const n = newB.value.trim(); if (/^[A-Za-z_][A-Za-z0-9_-]*$/.test(n)) setOwn(["spec", "behaviors", n], { behavior: newBid.value }); else alert("Geçersiz ad"); } }, "+ davranış ekle"))));
}

// ============================================================================ ifade / tetikleyici metinleri
const INFIX = { eq: "=", ne: "≠", lt: "<", le: "≤", gt: ">", ge: "≥", add: "+", sub: "−", mul: "×", div: "÷", mod: "mod" };
function exprText(n) {
  if (n === undefined) return "—";
  if (n === null) return "null";
  if (typeof n !== "object") return JSON.stringify(n);
  if (Array.isArray(n)) return "[" + n.map(exprText).join(", ") + "]";
  if ("var" in n) return n.var;
  if ("param" in n) return "$" + n.param;
  if ("all" in n) return n.all.length ? "(" + n.all.map(exprText).join(" VE ") + ")" : "doğru";
  if ("any" in n) return n.any.length ? "(" + n.any.map(exprText).join(" VEYA ") + ")" : "yanlış";
  if ("not" in n) return "DEĞİL " + exprText(n.not);
  if ("cond" in n) return short(n.cond) + "(" + Object.entries(n.params || {}).map(([k, v]) => k + "=" + exprText(v)).join(", ") + ")";
  if ("op" in n) {
    const a = n.args || [];
    if (INFIX[n.op] && a.length === 2) return `${exprText(a[0])} ${INFIX[n.op]} ${exprText(a[1])}`;
    if (n.op === "in") return `${exprText(a[0])} ∈ ${exprText(a[1])}`;
    return n.op + "(" + a.map(exprText).join(", ") + ")";
  }
  return JSON.stringify(n);
}
function triggerText(t) {
  if (!t) return "—";
  if (t.event) return "⚡ " + t.event.replace("core.event.", "") + (t.filter ? " | " + exprText(t.filter) : "");
  if (t.every !== undefined) return "⟳ her " + fmt(t.every);
  if (t.at !== undefined) return "⏱ t=" + fmt(t.at);
  if (t.edge) return "⇅ koşul kenarı: " + t.edge;
  return JSON.stringify(t);
}

// ============================================================================ kural editörü
function ruleEditor(rules, ownPath, provBase, opts = {}) {
  rules = rules || {};
  const ownRules = getIn(S.draft.spec, ownPath) || {};
  const keys = [...new Set([...Object.keys(rules), ...Object.keys(ownRules)])]
    .sort((a, b) => ((rules[b] || {}).priority || 0) - ((rules[a] || {}).priority || 0));
  const setRule = (rk, field, val) => {
    let r = clone(getIn(S.draft.spec, [...ownPath, rk]));
    if (!isObj(r) || isDel(r)) r = {};
    if (val === undefined) delete r[field]; else r[field] = val;
    setOwn(["spec", ...ownPath, rk], Object.keys(r).length ? r : undefined);
  };
  const cards = keys.map((rk) => {
    const own = ownRules[rk];
    const inherited = origins(provBase + rk).some((o) => o !== S.sel);
    if (isDel(own)) {
      return h("div", { class: "rule disabled" }, h("div", { class: "rule-head" }, h("span", { class: "rk" }, rk), h("span", { class: "hint" }, "bu dokümanda silindi"),
        h("span", { class: "spacer" }), h("button", { class: "small", onclick: () => setOwn(["spec", ...ownPath, rk], undefined) }, "geri al")));
    }
    const r = rules[rk] || own || {};
    const enabled = r.enabled !== false;
    const openKey = "rule:" + provBase + rk;
    const errs = diagsAt(["spec", ...ownPath, rk].join("."));
    const srcs = origins(provBase + rk);
    return h("div", { class: "rule" + (enabled ? "" : " disabled") + (errs.some((x) => x.severity === "error") ? " has-err" : "") },
      h("div", { class: "rule-head", onclick: (ev) => { if (ev.target.closest("input,select,button")) return; S.open[openKey] = !S.open[openKey]; render(); } },
        h("input", { type: "checkbox", checked: enabled, title: "etkin", onchange: (ev) => setRule(rk, "enabled", ev.target.checked) }),
        h("span", {}, S.open[openKey] ? "▾" : "▸"),
        h("span", { class: "rk" }, rk), h("span", { class: "hint" }, txt(r.label)),
        h("span", { class: "spacer" }),
        h("span", { class: "hint" }, "öncelik"),
        h("input", { type: "text", class: "prio", dataset: { path: `${openKey}.priority` }, value: r.priority ?? 0, onchange: (ev) => setRule(rk, "priority", Number(ev.target.value) || 0) }),
        srcs.map(srcChip),
        own && inherited ? h("button", { class: "ghost small", title: "bu dokümandaki değişiklikleri kaldır", onclick: () => setOwn(["spec", ...ownPath, rk], undefined) }, "↺") : null,
        h("button", { class: "ghost small danger", title: "kuralı sil", onclick: () => setOwn(["spec", ...ownPath, rk], inherited ? { $delete: true } : undefined) }, "✕")),
      h("div", { class: "rule-sum" }, triggerText(r.trigger), r.condition !== undefined ? " ∧ " + exprText(r.condition) : "", " → ",
        (r.actions || []).map((a) => short(a.action)).join(", ") || "—", r.cooldown !== undefined ? ` · cooldown ${fmt(r.cooldown)}` : ""),
      S.open[openKey] ? h("div", { class: "rule-body" },
        errs.map((x) => h("div", { class: "diag-inline" }, x.path.replace(["spec", ...ownPath, rk].join("."), "") + ": " + x.message)),
        h("div", { class: "field-row" }, h("label", {}, "Tetikleyici"), triggerEditor(r.trigger, (t) => setRule(rk, "trigger", t), openKey)),
        h("div", { class: "field-row" }, h("label", {}, "Koşul"), h("div", {}, h("div", { class: "expr" }, exprText(r.condition)),
          jsonArea(r.condition, undefined, `${openKey}.condition`, (val) => setRule(rk, "condition", val), 3))),
        h("div", { class: "field-row" }, h("label", {}, "Aksiyonlar"), actionList(r.actions || [], (acts) => setRule(rk, "actions", acts), openKey + ".actions", opts)),
        h("div", { class: "field-row" }, h("label", {}, "Cooldown / limit"), h("div", { class: "inline" },
          h("input", { type: "text", dataset: { path: `${openKey}.cooldown` }, placeholder: "ör. 4 s", value: looseText(r.cooldown), onchange: (ev) => setRule(rk, "cooldown", parseLoose(ev.target.value)) }),
          h("span", { class: "hint" }, "maxFires"),
          h("input", { type: "text", dataset: { path: `${openKey}.maxFires` }, value: looseText(r.maxFires), onchange: (ev) => setRule(rk, "maxFires", parseLoose(ev.target.value)) })))) : null);
  });
  const nk = h("input", { type: "text", placeholder: "yeni_kural_adı" });
  const events = docsOf("EventType", (e) => !e.abstract);
  return h("div", {}, cards, h("div", { class: "inline" }, nk, h("button", {
    onclick: () => {
      const k = nk.value.trim();
      if (!/^[A-Za-z_][A-Za-z0-9_-]*$/.test(k) || keys.includes(k)) return alert("Geçersiz veya mevcut kural adı");
      S.open["rule:" + provBase + k] = true;
      setOwn(["spec", ...ownPath, k], { priority: 50, trigger: { event: (events[0] || {}).id }, actions: [{ action: "core.action.log", params: { message: k } }] });
    },
  }, "+ yeni kural")));
}

function triggerEditor(t, onChange, key) {
  t = t || {};
  const type = t.event ? "event" : t.every !== undefined ? "every" : t.at !== undefined ? "at" : t.edge ? "edge" : "event";
  const typeSel = h("select", { dataset: { path: key + ".trigtype" }, onchange: (ev) => {
    const v = ev.target.value;
    onChange(v === "event" ? { event: "core.event.scenario.started" } : v === "every" ? { every: "10 s" } : v === "at" ? { at: "0 s" } : { edge: "rising" });
  } }, [["event", "olay"], ["every", "periyodik"], ["at", "zamanda"], ["edge", "koşul kenarı"]].map(([v, l]) => h("option", { value: v, selected: v === type }, l)));
  let detail;
  if (type === "event") {
    const bySrc = {};
    for (const e of docsOf("EventType", (x) => !x.abstract)) (bySrc[e.spec.source || "diğer"] ||= []).push(e);
    detail = [h("select", { dataset: { path: key + ".event" }, onchange: (ev) => onChange({ ...t, event: ev.target.value }) },
      Object.entries(bySrc).map(([src, list]) => h("optgroup", { label: src }, list.map((e) => h("option", { value: e.id, selected: e.id === t.event }, e.id))))),
    h("div", { class: "hint" }, "filtre: ", exprText(t.filter)),
    jsonArea(t.filter, undefined, key + ".filter", (f) => { const n = { ...t }; if (f === undefined) delete n.filter; else n.filter = f; onChange(n); }, 2)];
    const ev = S.docs[t.event];
    if (ev) detail.push(h("div", { class: "hint" }, "payload: ", Object.keys({ entity: 1, time: 1, ...(ev.spec.payload || {}) }).map((k) => "event." + k).join(", ")));
  } else if (type === "edge") {
    detail = h("select", { dataset: { path: key + ".edge" }, onchange: (ev) => onChange({ edge: ev.target.value }) },
      ["rising", "falling", "both"].map((v) => h("option", { value: v, selected: v === t.edge }, v)));
  } else {
    detail = h("input", { type: "text", dataset: { path: key + "." + type }, value: looseText(t[type]), onchange: (ev) => onChange({ [type]: parseLoose(ev.target.value) }) });
  }
  return h("div", {}, h("div", { class: "inline" }, typeSel), detail);
}

function actionList(actions, onChange, key, opts) {
  const pal = S.cat.palettes.actions || {};
  const upd = (i, inv) => { const a = clone(actions); if (inv === undefined) a.splice(i, 1); else a[i] = inv; onChange(a); };
  const move = (i, d) => { const a = clone(actions); const j = i + d; if (j < 0 || j >= a.length) return; [a[i], a[j]] = [a[j], a[i]]; onChange(a); };
  const rows = actions.map((inv, i) => {
    const at = S.docs[inv.action];
    const descs = (at && at.spec.parameters) || {};
    const params = inv.params || {};
    const setP = (k, v) => { const n = clone(inv); n.params = n.params || {}; if (v === undefined) delete n.params[k]; else n.params[k] = v; upd(i, n); };
    return h("div", { class: "action" },
      h("div", { class: "action-head" },
        h("select", { dataset: { path: `${key}.${i}.action` }, onchange: (ev) => upd(i, { action: ev.target.value, params: {} }) },
          Object.entries(pal).map(([cat, ids]) => h("optgroup", { label: cat }, ids.map((id) => h("option", { value: id, selected: id === inv.action }, id))))),
        opts.scenario ? h("select", { title: "hedef varlık (on)", onchange: (ev) => { const n = clone(inv); if (ev.target.value) n.on = ev.target.value; else delete n.on; upd(i, n); } },
          h("option", { value: "" }, "(senaryo)"), Object.keys(LV().resolved.spec.entities || {}).map((k) => h("option", { value: k, selected: inv.on === k }, "on: " + k))) : null,
        h("button", { class: "ghost small", onclick: () => move(i, -1) }, "↑"), h("button", { class: "ghost small", onclick: () => move(i, 1) }, "↓"),
        h("button", { class: "ghost small danger", onclick: () => upd(i, undefined) }, "✕")),
      h("div", { class: "aparams" }, Object.entries(descs).map(([k, d]) => {
        const v = params[k];
        const complex = ["action", "actionList", "object", "map", "list", "args", "geoPoint", "table", "expression"].includes(d.type) || (isObj(v) && !("param" in v));
        const ph = d.default !== undefined ? `varsayılan: ${fmt(d.default)}` : d.required ? "zorunlu" : "";
        return [h("label", { title: txt(d.description) }, k, d.required ? h("span", { class: "req" }, "*") : null, d.unit ? ` (${d.unit})` : ""),
          complex ? h("div", {}, isObj(v) || Array.isArray(v) ? h("div", { class: "expr" }, d.type === "actionList" && Array.isArray(v) ? v.map((x) => short(x.action)).join(" ; ") : exprText(v)) : null,
            jsonArea(v, undefined, `${key}.${i}.p.${k}`, (val) => setP(k, val), 2))
            : h("input", { type: "text", dataset: { path: `${key}.${i}.p.${k}` }, placeholder: ph, value: looseText(v),
              title: 'Literal veya ifade: 5, "chaff", {"param":"chaffBurst"}, {"var":"event.bearing"}',
              onchange: (ev) => setP(k, parseLoose(ev.target.value)) })];
      })));
  });
  const addSel = h("select", {}, Object.entries(pal).map(([cat, ids]) => h("optgroup", { label: cat }, ids.map((id) => h("option", { value: id }, id)))));
  return h("div", {}, rows, h("div", { class: "inline" }, addSel, h("button", { onclick: () => onChange([...clone(actions), { action: addSel.value, params: {} }]) }, "+ aksiyon")));
}

// ============================================================================ Behavior
function formBehavior() {
  const spec = LV().resolved.spec;
  const req = spec.requires || {};
  const roles = Object.entries(req.systems || {});
  const descs = spec.parameters || {};
  const ownP = (S.draft.spec && S.draft.spec.parameters) || {};
  return h("div", {},
    card("Gereken sistem rolleri", roles.length ? h("table", { class: "grid" }, roles.map(([r, x]) => h("tr", {},
      h("td", { class: "mono" }, r), h("td", {}, h("span", { class: "chip link", onclick: () => select(x.systemType) }, x.systemType)),
      h("td", {}, x.optional ? h("span", { class: "chip" }, "opsiyonel") : h("span", { class: "chip" }, "zorunlu")),
      h("td", {}, srcChip(provOf(`requires.systems.${r}`)))))) : h("div", { class: "hint" }, "Rol gerektirmiyor."),
    req.platformType ? h("div", { class: "hint", style: "margin-top:6px" }, "platform tipi: ", h("code", {}, req.platformType)) : null),
    card("Parametreler (davranışın ayar düğmeleri)", h("table", { class: "grid" },
      h("tr", {}, h("th", {}, "Ad"), h("th", {}, "Tip"), h("th", {}, "Varsayılan (bu dokümanda)"), h("th", {}, "Kaynak")),
      Object.entries(descs).map(([k, d]) => {
        const ownDef = ownP[k] && ownP[k].default;
        return h("tr", { class: ownDef !== undefined ? "own" : "" },
          h("td", { class: "pname", title: txt(d.description) }, txt(d.label) || k, h("div", { class: "k" }, k, d.overridableAt ? ` · ${d.overridableAt}` : "")),
          h("td", { class: "mono" }, d.type, d.unit ? ` (${d.unit})` : "", d.enumRef ? ` ${short(d.enumRef)}` : "", d.min !== undefined || d.max !== undefined ? ` [${d.min ?? ""}…${d.max ?? ""}]` : ""),
          h("td", {}, h("input", { type: "text", dataset: { path: `bparam.${k}` }, placeholder: fmt(d.default), value: looseText(ownDef),
            onchange: (ev) => setOwn(["spec", "parameters", k, "default"], parseLoose(ev.target.value)) })),
          h("td", { class: "psrc" }, srcChip(provOf(`parameters.${k}`))));
      })), h("div", { class: "hint" }, "Yeni parametre tanımı eklemek için JSON sekmesini kullanın.")),
    card("Kurallar", ruleEditor(spec.rules, ["rules"], "rules.")));
}

// ============================================================================ Scenario
function formScenario() {
  const spec = LV().resolved.spec;
  const ownE = (S.draft.spec && S.draft.spec.entities) || {};
  const ents = [...new Set([...Object.keys(spec.entities || {}), ...Object.keys(ownE)])];
  const platforms = docsOf("PlatformTemplate", (e) => !e.abstract);
  const sides = Object.keys(spec.sides || {});
  const rows = ents.map((ek) => {
    const e = (spec.entities || {})[ek], own = ownE[ek];
    const inherited = origins("entities." + ek).some((o) => o !== S.sel);
    if (isDel(own)) return h("tr", { class: "deleted" }, h("td", {}, ek), h("td", { colspan: 4 }, "bu varyantta silindi"),
      h("td", {}, h("button", { class: "small", onclick: () => setOwn(["spec", "entities", ek], undefined) }, "geri al")));
    const openKey = "ent:" + ek;
    const errs = diagsAt("spec.entities." + ek);
    return [h("tr", { class: (own ? "own " : "") + (errs.some((x) => x.severity === "error") ? "has-err" : "") },
      h("td", {}, h("b", {}, ek), h("div", { class: "hint" }, e && e.callsign)),
      h("td", {}, h("select", { onchange: (ev) => setOwn(["spec", "entities", ek, "platform"], ev.target.value || undefined) },
        h("option", { value: "" }, own && own.platform ? "(mirasa dön)" : `(miras: ${e && e.platform})`),
        platforms.map((p) => h("option", { value: p.id, selected: own && own.platform === p.id }, p.id))),
        e && S.docs[e.platform] ? h("button", { class: "ghost small", onclick: () => select(e.platform) }, "aç") : null),
      h("td", {}, h("select", { onchange: (ev) => setOwn(["spec", "entities", ek, "side"], ev.target.value || undefined) },
        h("option", { value: "" }, `(miras: ${e && e.side})`), sides.map((s) => h("option", { value: s, selected: own && own.side === s }, s)))),
      h("td", {}, srcChip(provOf(`entities.${ek}.platform`))),
      h("td", { class: "hint" }, e && e.overrides ? Object.keys(e.overrides).join(", ") : ""),
      h("td", { class: "psrc" }, h("button", { class: "small", onclick: () => { S.open[openKey] = !S.open[openKey]; render(); } }, S.open[openKey] ? "▾" : "▸"),
        h("button", { class: "ghost small danger", onclick: () => setOwn(["spec", "entities", ek], inherited ? { $delete: true } : undefined) }, "✕"))),
    S.open[openKey] ? h("tr", {}, h("td", { colspan: 6 }, errs.map((x) => h("div", { class: "diag-inline" }, x.path + ": " + x.message)),
      h("div", { class: "hint" }, "Bu dokümandaki varlık yaması (initial, overrides, plan...). Efektif: ", h("code", {}, JSON.stringify(e).slice(0, 160) + "…")),
      jsonArea(own, undefined, `ent.${ek}.raw`, (v) => setOwn(["spec", "entities", ek], v), 10))) : null];
  });
  const nk = h("input", { type: "text", placeholder: "varlık anahtarı" });
  const np = h("select", {}, platforms.map((p) => h("option", { value: p.id }, p.id)));
  const ns = h("select", {}, sides.map((s) => h("option", { value: s }, s)));
  const areas = Object.entries(spec.areas || {});
  const uinst = Object.entries(spec.units || {});
  const unitCard = card("Birlikler (şablondan formasyonla açılır)",
    uinst.length ? h("table", { class: "grid" }, h("tr", {}, h("th", {}, "Anahtar"), h("th", {}, "Şablon"), h("th", {}, "Taraf"), h("th", {}, "Çağrı adı"), h("th", {}, "Kaynak")),
      uinst.map(([k, u]) => h("tr", {}, h("td", { class: "mono" }, k), h("td", {}, h("span", { class: "chip link", onclick: () => select(u.template) }, short(u.template))),
        h("td", {}, u.side), h("td", {}, u.callsign || ""), h("td", {}, srcChip(provOf("units." + k)))))) : h("div", { class: "hint" }, "Birlik örneği yok."),
    h("details", {}, h("summary", {}, "birlik örneklerini düzenle (JSON)"),
      jsonArea(S.draft.spec && S.draft.spec.units, undefined, "scn.units", (v) => setOwn(["spec", "units"], v), 12)));
  return h("div", {}, unitCard,
    card("Varlıklar", h("table", { class: "grid" }, h("tr", {}, h("th", {}, "Anahtar"), h("th", {}, "Platform"), h("th", {}, "Taraf"), h("th", {}, "Kaynak"), h("th", {}, "Override"), h("th", {})), rows),
      h("div", { class: "inline", style: "margin-top:8px" }, nk, np, ns, h("button", { onclick: () => {
        const k = nk.value.trim(); if (!/^[A-Za-z_][A-Za-z0-9_-]*$/.test(k)) return alert("Geçersiz anahtar");
        setOwn(["spec", "entities", k], { platform: np.value, side: ns.value, initial: { position: { lat: 40, lon: 27 } } });
      } }, "+ varlık"))),
    card("Alanlar", h("table", { class: "grid" }, areas.map(([k, a]) => h("tr", {}, h("td", { class: "mono" }, k), h("td", {}, short(a.type)),
      h("td", {}, a.geometry && a.geometry.shape), h("td", { class: "hint" }, txt(a.label)), h("td", {}, srcChip(provOf("areas." + k)))))),
      h("details", {}, h("summary", {}, "alanları bu dokümanda düzenle (JSON)"),
        jsonArea(S.draft.spec && S.draft.spec.areas, undefined, "scn.areas", (v) => setOwn(["spec", "areas"], v), 12))),
    card("Davranış override'ları (bu senaryodaki tüm kullanımlar)",
      h("div", { class: "hint" }, "Behavior id → {enabled, parameters, rules}. Sıra: platform eki < bu alan < varlık overrides."),
      jsonArea(S.draft.spec && S.draft.spec.behaviorOverrides, spec.behaviorOverrides, "scn.bo", (v) => setOwn(["spec", "behaviorOverrides"], v), 8)),
    card("Senaryo kuralları", ruleEditor(spec.rules, ["rules"], "rules.", { scenario: true })));
}

function renderCompile() {
  const btn = h("button", { class: "primary", onclick: async () => {
    if (S.dirty) return alert("Önce kaydedin (derleme diskteki halini kullanır).");
    S.compiled = { loading: true }; render();
    try { S.compiled = await call("/api/compile", { scenario: S.sel }); } catch (e) { S.compiled = { error: e.message }; }
    render();
  } }, "▶ Derlemeyi çalıştır");
  const c = S.compiled;
  let body = h("p", { class: "hint" }, "Senaryoyu motor formatına (simcfg/compiled/v1) derler ve özetini gösterir.");
  if (c && c.loading) body = h("p", {}, "derleniyor…");
  else if (c && c.error) body = h("p", { class: "diag-inline" }, c.error);
  else if (c && c.compiled) {
    const ents = Object.entries(c.compiled.entities);
    body = h("div", {}, h("p", { class: "hint" }, `${ents.length} varlık · ${Object.keys(c.compiled.areas).length} alan · ${Object.keys(c.compiled.rules).length} senaryo kuralı · ${c.diagnostics.filter((d) => d.severity === "error").length} hata`),
      ents.map(([k, e]) => card(`${k} — ${e.callsign || ""} [${e.side}]`,
        h("div", { class: "ent-sum" }, h("div", {}, h("code", {}, e.template), " ← ", e.lineage.slice(0, -1).map(short).join(" ← ")),
          h("div", {}, "sistemler: ", Object.entries(e.systems).map(([m, s]) => `${m}=${short(s.template)}`).join(", "),
            e.unfittedSystems.length ? h("span", { class: "off" }, " " + e.unfittedSystems.join(", ")) : null),
          Object.entries(e.behaviors).map(([bk, b]) => h("div", {}, h("b", {}, bk), " ", short(b.behavior), b.enabled ? "" : h("span", { class: "tag-off" }, " (devre dışı)"),
            b.rules ? h("div", { class: "hint" }, Object.entries(b.rules).map(([rk, r]) => r.enabled ? rk : h("span", { class: "off", title: r.disabledReason || "" }, rk)).flatMap((x, i) => (i ? [", ", x] : [x]))) : null))))),
      h("details", {}, h("summary", {}, "tam çıktı (JSON)"), h("pre", { class: "json" }, JSON.stringify(c.compiled, null, 2))));
  }
  return h("div", {}, h("div", { class: "toolbar" }, btn), body);
}

// ============================================================================ UnitTemplate
function formUnit() {
  const spec = LV().resolved.spec;
  const platforms = docsOf("PlatformTemplate", (e) => !e.abstract);
  const units = docsOf("UnitTemplate", (e) => !e.abstract && e.id !== S.sel);
  const ownM = (S.draft.spec && S.draft.spec.members) || {};
  const total = (u, depth = 0) => {
    if (!u || depth > 8) return 0;
    let n = Object.values(u.spec.members || {}).reduce((a, m) => a + (m.count || 1), 0);
    for (const sub of Object.values(u.spec.subunits || {})) n += (sub.count || 1) * total(S.docs[sub.unit], depth + 1);
    return n;
  };
  const memberRows = Object.entries(spec.members || {}).map(([mk, m]) => h("tr", { class: ownM[mk] ? "own" : "" },
    h("td", {}, h("b", {}, mk), m.leader ? h("span", { class: "chip", style: "margin-left:6px" }, "lider") : null, h("div", { class: "hint" }, m.role || "")),
    h("td", {}, h("select", { onchange: (ev) => setOwn(["spec", "members", mk, "platform"], ev.target.value || undefined) },
      h("option", { value: "" }, `(miras: ${m.platform})`), platforms.map((p) => h("option", { value: p.id, selected: ownM[mk] && ownM[mk].platform === p.id }, p.id))),
      S.docs[m.platform] ? h("button", { class: "ghost small", onclick: () => select(m.platform) }, "aç") : null),
    h("td", {}, h("input", { type: "text", dataset: { path: `unit.${mk}.count` }, value: m.count || 1, style: "width:60px",
      onchange: (ev) => setOwn(["spec", "members", mk, "count"], Math.max(1, parseInt(ev.target.value, 10) || 1)) })),
    h("td", {}, srcChip(provOf(`members.${mk}`)))));
  const subRows = Object.entries(spec.subunits || {}).map(([sk, sub]) => h("tr", {},
    h("td", {}, h("b", {}, sk)), h("td", {}, h("span", { class: "chip link", onclick: () => select(sub.unit) }, sub.unit)),
    h("td", {}, h("input", { type: "text", dataset: { path: `unit.sub.${sk}.count` }, value: sub.count || 1, style: "width:60px",
      onchange: (ev) => setOwn(["spec", "subunits", sk, "count"], Math.max(1, parseInt(ev.target.value, 10) || 1)) })),
    h("td", { class: "hint" }, `${total(S.docs[sub.unit])} kişi/araç × ${sub.count || 1}`)));
  const f = spec.formation || {};
  return h("div", {},
    card("Birlik", h("div", { class: "inline" }, h("span", { class: "chip kind" }, spec.echelon || "-"), h("span", { class: "hint" }, `toplam ${total(S.docs[S.sel] && { spec })} varlık`),
      h("span", { class: "hint" }, "formasyon:"),
      h("select", { onchange: (ev) => setOwn(["spec", "formation", "type"], ev.target.value || undefined) },
        enumValues({ enumRef: "core.enum.formation" }).map((v) => h("option", { value: v, selected: v === f.type }, v))),
      h("input", { type: "text", dataset: { path: "unit.spacing" }, value: looseText(f.spacing), placeholder: "aralık", style: "width:90px",
        onchange: (ev) => setOwn(["spec", "formation", "spacing"], parseLoose(ev.target.value)) }))),
    card("Üyeler", h("table", { class: "grid" }, h("tr", {}, h("th", {}, "Üye"), h("th", {}, "Platform"), h("th", {}, "Adet"), h("th", {}, "Kaynak")), memberRows),
      h("details", {}, h("summary", {}, "üye override'ları (JSON)"), jsonArea(S.draft.spec && S.draft.spec.members, spec.members, "unit.members", (v) => setOwn(["spec", "members"], v), 8))),
    card("Alt birlikler", subRows.length ? h("table", { class: "grid" }, subRows) : h("div", { class: "hint" }, "Alt birlik yok."),
      h("details", {}, h("summary", {}, "alt birlikleri düzenle (JSON)"), h("div", { class: "hint" }, "Mevcut birlikler: " + units.map((u) => u.id).join(", ")),
        jsonArea(S.draft.spec && S.draft.spec.subunits, spec.subunits, "unit.subs", (v) => setOwn(["spec", "subunits"], v), 5))));
}

// ============================================================================ tip dokümanları (salt görünüm)
function descriptorTable(descs, provPrefix) {
  const rows = Object.entries(descs || {}).map(([k, d]) => h("tr", {},
    h("td", { class: "pname", title: txt(d.description) }, txt(d.label) || k, h("div", { class: "k" }, k)),
    h("td", { class: "mono" }, d.type, d.unit ? ` (${d.unit})` : ""),
    h("td", { class: "mono" }, d.default !== undefined ? fmt(d.default) : d.defaultFrom ? `← ${d.defaultFrom}` : d.required ? "zorunlu" : "—"),
    h("td", { class: "hint" }, [d.min !== undefined || d.max !== undefined ? `[${d.min ?? ""}…${d.max ?? ""}]` : "", d.enumRef ? short(d.enumRef) : "",
      d.values ? enumValues(d).join("|") : "", d.readonly ? "salt okunur" : "", d.overridableAt ? `override: ${d.overridableAt}` : "",
      d.constraint ? Object.values(d.constraint).map(short).join(",") : ""].filter(Boolean).join(" · ")),
    h("td", { class: "psrc" }, srcChip(provOf(provPrefix + k)))));
  return rows.length ? h("table", { class: "grid" }, h("tr", {}, h("th", {}, "Ad"), h("th", {}, "Tip"), h("th", {}, "Varsayılan"), h("th", {}, "Kısıt"), h("th", {}, "Kaynak")), rows)
    : h("div", { class: "hint" }, "—");
}
function formTypeDescriptors() {
  const v = LV(), spec = v.resolved.spec;
  const parts = [];
  const sections = { parameters: "Parametreler", state: "Durum (gözlemlenebilir)", payload: "Olay alanları (payload)", properties: "Alan özellikleri" };
  if (spec.implementation) parts.push(card("Uygulama", h("pre", { class: "json" }, JSON.stringify(spec.implementation, null, 2)),
    spec.implementation.body ? h("div", { class: "expr" }, spec.implementation.type === "expression" ? exprText(spec.implementation.body) : "") : null));
  for (const [k, title] of Object.entries(sections)) if (spec[k]) parts.push(card(title, descriptorTable(spec[k], k + ".")));
  if (spec.commands) parts.push(card("Komutlar", Object.entries(spec.commands).map(([c, x]) => h("div", {}, h("h3", { class: "group" }, c), descriptorTable(x.parameters, `commands.${c}.parameters.`)))));
  if (spec.emits) parts.push(card("Yaydığı olaylar", spec.emits.map((e) => h("span", { class: "chip link", onclick: () => select(e), style: "margin:2px" }, e))));
  if (spec.allowedSystemTypes) parts.push(card("Monte edilebilir sistem tipleri", spec.allowedSystemTypes.map((e) => h("span", { class: "chip link", onclick: () => select(e) }, e))));
  if (spec.channels) parts.push(card("Kanallar", spec.channels.length ? spec.channels.join(", ") : "—", spec.durative ? " · süreli" : " · anlık"));
  if (spec.segments) parts.push(card("Segmentler", h("ol", {}, spec.segments.map((s) => h("li", { class: "mono" }, s.type + " " + Object.entries(s).filter(([k]) => k !== "type").map(([k, x]) => `${k}=${exprText(x)}`).join(" "))))));
  parts.push(h("p", { class: "hint" }, "Tip tanımları (meta-model) JSON sekmesinden düzenlenir; bu tipi kullanan tüm form ve paletler otomatik güncellenir."));
  return h("div", {}, parts);
}
function formEnum() {
  const vals = LV().resolved.spec.values || [];
  return card("Değerler", h("table", { class: "grid" }, vals.map((x) => {
    const o = isObj(x) ? x : { value: x };
    return h("tr", {}, h("td", { class: "mono" }, o.value), h("td", {}, txt(o.label)), h("td", { class: "hint" }, txt(o.description)),
      h("td", {}, o.color ? h("span", { class: "chip", style: `background:${o.color};color:#fff` }, o.color) : null));
  })), h("p", { class: "hint" }, "Türetilmiş listelerde değer eklemek için {\"$append\": [...]} kullanın (JSON sekmesi)."));
}

// ============================================================================ JSON / çözülmüş
function renderJson() {
  const ta = h("textarea", { rows: 32, value: JSON.stringify(S.draft, null, 2), dataset: { path: "rawjson" }, oninput: () => ta.classList.remove("invalid") });
  return h("div", {}, h("p", { class: "hint" }, "Ham doküman (diskte saklanan hali). Direktifler: $delete, $replace, $append, $prepend."), ta,
    h("div", { class: "toolbar" }, h("button", { onclick: () => {
      try { S.draft = JSON.parse(ta.value); markDirty(); } catch (e) { ta.classList.add("invalid"); alert("JSON hatası: " + e.message); }
    } }, "Uygula ve doğrula")));
}
function renderResolved() {
  const v = LV();
  const prov = Object.entries(v.provenance || {}).sort(([a], [b]) => a.localeCompare(b));
  return h("div", {},
    v.effective ? card("Efektif görünüm (varsayılanlar + birim normalizasyonu)", h("pre", { class: "json" }, JSON.stringify(v.effective, null, 2))) : null,
    card("Çözülmüş doküman (kalıtım uygulanmış)", h("pre", { class: "json" }, JSON.stringify(v.resolved, null, 2))),
    card("Kaynak haritası (provenance)", h("table", { class: "grid" }, prov.map(([p, o]) => h("tr", {}, h("td", { class: "mono" }, p), h("td", {}, srcChip(o)))))));
}

// ============================================================================ türetme diyaloğu
function deriveDialog(baseId) {
  const pkgs = Object.keys(S.cat.packages).filter((p) => p !== "core");
  const pkg = pkgs[0] || baseId.split(".")[0];
  const parts = baseId.split(".");
  const idIn = h("input", { type: "text", value: [pkg, ...parts.slice(1, -1), parts[parts.length - 1] + "_variant"].join(".") });
  const nameIn = h("input", { type: "text", placeholder: "görünen ad" });
  const absIn = h("input", { type: "checkbox" });
  const err = h("div", { class: "diag-inline" });
  const close = () => { $("#modal").hidden = true; $("#modal").replaceChildren(); };
  $("#modal").replaceChildren(h("div", { class: "dialog" },
    h("h3", {}, "Türet: ", h("code", {}, baseId)),
    h("label", {}, "Yeni kimlik", idIn), h("label", {}, "Ad", nameIn),
    h("label", { class: "inline" }, absIn, "soyut (abstract): sadece türetmek için"),
    err,
    h("div", { class: "row" }, h("button", { onclick: close }, "İptal"), h("button", { class: "primary", onclick: async () => {
      const r = await call("/api/derive", { base: baseId, newId: idIn.value.trim(), name: nameIn.value.trim() || undefined, abstract: absIn.checked });
      if (!r.ok) { err.textContent = r.diagnostics.map((d) => d.message).join("; "); return; }
      close(); await loadCatalog(); S.dirty = false; await select(r.id, { force: true });
    } }, "Oluştur"))));
  $("#modal").hidden = false;
  idIn.focus();
}

// ============================================================================ başlat
async function init() {
  $("#filter").addEventListener("input", (ev) => { S.filter = ev.target.value; renderSidebar(); });
  $("#btn-reload").addEventListener("click", async () => { await loadCatalog(); if (S.sel) await select(S.sel, { force: true }); else render(); });
  document.querySelectorAll(".diag-tab").forEach((b) => b.addEventListener("click", () => { S.diagScope = b.dataset.scope; renderDiags(); }));
  $("#diag-toggle").addEventListener("click", () => { const p = $("#diag-panel"); p.classList.toggle("collapsed");
    document.documentElement.style.setProperty("--diag-h", p.classList.contains("collapsed") ? "32px" : "180px"); });
  window.addEventListener("beforeunload", (ev) => { if (S.dirty) { ev.preventDefault(); ev.returnValue = ""; } });
  document.addEventListener("keydown", (ev) => { if ((ev.metaKey || ev.ctrlKey) && ev.key === "s") { ev.preventDefault(); if (S.dirty) save(); } });
  try { await loadCatalog(); } catch (e) { $("#main").replaceChildren(h("p", { class: "diag-inline" }, "Sunucuya bağlanılamadı: " + e.message)); return; }
  const id = decodeURIComponent(location.hash.slice(1));
  if (id && S.docs[id]) await select(id); else render();
}
init();
