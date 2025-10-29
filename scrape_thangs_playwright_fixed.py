# -*- coding: utf-8 -*-
"""
Thangs scraper (Playwright) – genérico para cualquier listado con enlaces a /3d-model/
- Robusto frente a 'networkidle' (usa domcontentloaded/load + retries)
- Scroll infinito + fallback paginado
- Parser genérico de enlaces
- Extracción de colores SOLO dentro del bloque 'Shop the filament we used on the Polymaker Website'
  (con fallback global si no aparece ese bloque)
- Normalización básica de nombres de modelo y colores
- CSVs: models_colors.csv, color_counts.csv
- Artefactos de depuración en ./debug/
"""

import csv
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

# ---------- Config ----------
DEBUG_DIR = Path("debug"); DEBUG_DIR.mkdir(exist_ok=True)

# Enlaces de modelos (cualquier diseñador/colección)
MODEL_LINK_RE = re.compile(r"/designer/[^/]+/3d-model/[^?\s]+-\d+$", re.IGNORECASE)

# Encabezado de bloque Polymaker
HEADER_RE = re.compile(
    r"(Want your.*?Shop the filament we used on the Polymaker Website|Shop the filament we used on the Polymaker Website)",
    re.IGNORECASE | re.DOTALL
)

# Coincide con cualquier acabado (Matte, Silk, Glossy, Galaxy...) hasta PLA
POLY_ITEM_RE = re.compile(
    r"Polymaker\s+([A-Za-z]+(?:\s+[A-Za-z]+)*?)\s+PLA\b", re.IGNORECASE
)

# También para fallback global
POLY_LINE_RE = POLY_ITEM_RE

POLY_DOMAIN_RE = re.compile(
    r"https?://([a-z0-9\-]+\.)*polymaker\.com\b", re.IGNORECASE
)

# ---------- Utilidades ----------
def dump_debug(page, name: str):
    """Guarda screenshot y HTML para analizar fallos."""
    try:
        page.screenshot(path=str(DEBUG_DIR / f"{name}.png"), full_page=True)
    except Exception:
        pass
    try:
        (DEBUG_DIR / f"{name}.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass

def safe_goto(page, url: str, label: str = "page", attempts: int = 3):
    """
    Navega con estrategias progresivas evitando 'networkidle' (fragil en Thangs).
    1) domcontentloaded
    2) load
    3) sin wait_until + pequeña espera
    """
    last_err = None
    states = ["domcontentloaded", "load", None]
    for i in range(attempts):
        state = states[min(i, len(states)-1)]
        try:
            if state:
                page.goto(url, wait_until=state, timeout=60000)
            else:
                page.goto(url, timeout=60000)
                page.wait_for_timeout(1500)
            return True
        except PwTimeout as e:
            last_err = e
    dump_debug(page, f"{label}_goto_fail")
    raise last_err or RuntimeError(f"Failed to goto {url}")

def collect_links_from_html(html: str):
    """Devuelve set de URLs absolutas a modelos encontradas en el HTML."""
    soup = BeautifulSoup(html, "lxml")
    urls = set()

    # Preferido: patrón completo /designer/<slug>/3d-model/<slug>-<id>
    for a in soup.find_all("a", href=True):
        href = a["href"] or ""
        if MODEL_LINK_RE.search(href):
            urls.add(urljoin("https://thangs.com", href))

    # Fallback: cualquier /3d-model/ (por si cambia el layout)
    if not urls:
        for a in soup.find_all("a", href=True):
            href = a["href"] or ""
            if "/3d-model/" in href:
                urls.add(urljoin("https://thangs.com", href))

    return urls

def discover_model_urls_scroll(page, listing_url: str):
    """Hace scroll infinito e intenta descubrir enlaces a modelos."""
    urls = set()
    print("[*] Intentando scroll infinito…")

    # Evitar networkidle
    safe_goto(page, listing_url, label="designer")

    # Algún anchor de /3d-model/ como señal inicial
    try:
        page.wait_for_selector('a[href*="/3d-model/"]', timeout=15000)
    except PwTimeout:
        print("[!] No hay enlaces visibles inmediatos; seguimos con scroll + dump")
        dump_debug(page, "designer_initial")

    last_height = 0
    stagnant = 0
    for _ in range(40):
        html = page.content()
        urls |= collect_links_from_html(html)

        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass
        page.wait_for_timeout(900)

        try:
            height = page.evaluate("document.body.scrollHeight")
        except Exception:
            height = last_height

        if height == last_height:
            stagnant += 1
        else:
            stagnant = 0
        last_height = height

        if stagnant >= 4:
            break

    print(f"[*] Scroll recogió {len(urls)} enlaces")
    if not urls:
        dump_debug(page, "designer_after_scroll")
    return sorted(urls)

def discover_model_urls_paged(page, listing_url: str, max_pages: int = 20):
    """Plan B: si hay paginación ?page=N, recorre hasta que no encuentre más."""
    print("[*] Intentando paginación ?page=N…")
    urls = set()
    for n in range(1, max_pages + 1):
        url = listing_url if n == 1 else f"{listing_url}?page={n}"
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except PwTimeout:
            page.goto(url, wait_until="load", timeout=60000)
        page.wait_for_timeout(1000)
        found = collect_links_from_html(page.content())
        print(f"    - page {n}: {len(found)} enlaces")
        urls |= found
        if not found:
            break
    print(f"[*] Paginación recogió {len(urls)} enlaces")
    return sorted(urls)

def _extract_from_poly_block_strict(soup: BeautifulSoup):
    """Solo anchors que apunten a polymaker.com dentro del bloque Polymaker."""
    header_node = soup.find(string=HEADER_RE)
    if not header_node:
        return []

    container = header_node.find_parent(["section", "div", "article", "main"]) or header_node.parent
    colors, seen = [], set()

    for tag in container.find_all(["a", "li", "p", "div", "span"], limit=80):
        if tag.name in ("h1", "h2", "h3", "hr"):
            break
        if tag.name == "a":
            href = (tag.get("href") or "").strip()
            if POLY_DOMAIN_RE.search(href):
                txt = tag.get_text(" ", strip=True)
                for m in POLY_ITEM_RE.findall(txt):
                    color = m.strip()
                    key = color.lower()
                    if key not in seen and len(color.split()) >= 2:
                        seen.add(key)
                        colors.append(color)
    return colors


def _extract_from_poly_block_relaxed(soup: BeautifulSoup):
    """Fallback: mismo bloque, acepta texto 'Polymaker ... PLA' sin requerir enlaces."""
    header_node = soup.find(string=HEADER_RE)
    if not header_node:
        return []

    container = header_node.find_parent(["section", "div", "article", "main"]) or header_node.parent
    colors, seen = [], set()

    for tag in container.find_all(["li", "p", "div", "span"], limit=120):
        if tag.name in ("h1", "h2", "h3", "hr"):
            break
        txt = tag.get_text(" ", strip=True)
        for m in POLY_ITEM_RE.findall(txt):
            color = m.strip()
            key = color.lower()
            if key not in seen and len(color.split()) >= 2:
                seen.add(key)
                colors.append(color)
    return colors

def _normalize_color(c: str) -> str:
    """Limpia/normaliza un nombre de color."""
    x = c.strip()
    x = re.sub(r"\s+", " ", x)
    x = re.sub(r"\bGrey\b", "Gray", x, flags=re.I)
    x = re.sub(r"(?i)^Matte\b", "Matte", x)
    x = re.sub(r"(?i)\s*PLA\s*$", "", x)
    x = x.strip(" -–—·.")
    if x.lower() == "matte" or len(x.split()) < 2:
        return ""
    return x

def extract_polymaker_colors(page, model_url: str):
    """Abre la ficha y extrae título + colores (estricto → relajado → global)."""
    safe_goto(page, model_url, label="model")
    page.wait_for_timeout(1200)

    html = page.content()
    soup = BeautifulSoup(html, "lxml")

    title_node = soup.find(["h1", "title"])
    title_text = title_node.get_text(strip=True) if title_node else model_url.rsplit("/", 1)[-1]
    title_text = re.sub(r"\s*\(No Support.*$", "", title_text).strip()

    # 1) Bloque estricto → 2) relajado → 3) global
    colors = _extract_from_poly_block_strict(soup)
    if not colors:
        colors = _extract_from_poly_block_relaxed(soup)
    if not colors:
        text = soup.get_text("\n", strip=True)
        colors = [m.strip() for m in POLY_LINE_RE.findall(text)]

    # Normalización mínima
    clean, seen = [], set()
    for c in colors:
        x = re.sub(r"\bGrey\b", "Gray", c, flags=re.IGNORECASE)
        x = re.sub(r"\s+", " ", x).strip(" -–—·.")
        if len(x.split()) < 2:
            continue
        key = x.lower()
        if key not in seen:
            seen.add(key)
            clean.append(x)

    return title_text, clean

# ---------- Main ----------
def main():
    designer = None
    # Prioriza argumento CLI; si no, variable de entorno; si no, default Kit Kiln
    if len(sys.argv) > 1 and sys.argv[1]:
        designer = sys.argv[1]
    else:
        designer = os.getenv("DESIGNER_URL", "https://thangs.com/designer/The%20Kit%20Kiln")

    rows = []
    color_to_models = {}

    with sync_playwright() as p:
        # Navegador + contexto
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/128.0.0.0 Safari/537.36")
        )
        page = context.new_page()

        # Bloquea recursos pesados (acelera y evita 'idle' eterno)
        page.route("**/*", lambda route: (
            route.abort() if route.request.resource_type in {"image", "media", "font"} else route.continue_()
        ))

        print(f"[+] Cargando diseñador/listado: {designer}")
        urls = discover_model_urls_scroll(page, designer)

        if not urls:
            dump_debug(page, "designer_after_scroll")
            urls = discover_model_urls_paged(page, designer)

        if not urls:
            print("[X] No se encontraron modelos. Subiendo debug/ para inspeccionar.")
            browser.close()
            # Archivos vacíos para no fallar el job
            Path("models_colors.csv").write_text("model_name,model_url,colors\n", encoding="utf-8")
            Path("color_counts.csv").write_text("color,count,models\n", encoding="utf-8")
            # Deja también el loader en el repo (si existe)
            ensure_loader_exists()
            return

        print(f"[+] Modelos detectados: {len(urls)}")
        total = len(urls)

        for i, url in enumerate(urls, 1):
            print(f"[{i}/{total}] {url}")
            try:
                title, colors = extract_polymaker_colors(page, url)
            except Exception as e:
                print(f"[WARN] {url}: {e}")
                time.sleep(0.5)
                continue

            rows.append({
                "model_name": title,
                "model_url": url,
                "colors": "; ".join(colors)
            })
            for c in colors:
                color_to_models.setdefault(c, []).append(title)

            time.sleep(0.3)

        browser.close()

    # CSVs
    with open("models_colors.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["model_name", "model_url", "colors"])
        w.writeheader(); w.writerows(rows)

    with open("color_counts.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["color", "count", "models"])
        for color, models in sorted(color_to_models.items(), key=lambda x: (-len(x[1]), x[0].lower())):
            w.writerow([color, len(models), "; ".join(models)])

    ensure_loader_exists()
    print("[✓] Listo. Archivos: models_colors.csv, color_counts.csv, thangs_color_matrix_loader.html")
    if color_to_models:
        top = sorted(color_to_models.items(), key=lambda x: -len(x[1]))[:10]
        print("Top colores:")
        for color, models in top:
            print(f"  {color}: {len(models)} usos")

def ensure_loader_exists():
    """Escribe el HTML loader reutilizable si no existe (para cargar cualquier CSV)."""
    path = Path("thangs_color_matrix_loader.html")
    if path.exists():
        return
    path.write_text(LOADER_HTML, encoding="utf-8")

# ---------- Loader HTML reusable (sin datos hardcodeados) ----------
LOADER_HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<title>Matriz de colores — Loader (reutilizable)</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
 body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; margin: 16px; }
 table { border-collapse: collapse; width: 100%; overflow: auto; display: block; }
 th, td { border: 1px solid #ddd; padding: 6px 8px; text-align: left; white-space: nowrap; }
 th.sticky { position: sticky; top: 0; background: #fafafa; z-index: 2; }
 tr:nth-child(even) { background: #fafafa; }
 .controls { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 10px; }
 .pill { display:inline-block; padding:4px 8px; border-radius:999px; background:#eee; margin-right:6px; }
 .tick { text-align:center; font-weight: 700; }
 .count-row td { font-weight: 700; background: #f0f9ff; }
 .link a { color: #0366d6; text-decoration: none; }
 .link a:hover { text-decoration: underline; }
 .right { text-align:right; }
 button { cursor: pointer; }
 .hidden { display: none; }
 .section { margin: 12px 0; }
</style>
</head>
<body>
<h2>Matriz de colores — Reutilizable (carga tu CSV)</h2>

<div class="section">
  <p><strong>Cómo usar:</strong> Exporta o prepara un CSV con columnas <code>model_name</code>, <code>model_url</code>, <code>colors</code> (donde <code>colors</code> es una lista separada por <code>;</code> como <em>Matte Ash Gray; Matte Charcoal Black</em>). Luego cárgalo aquí.</p>
  <input type="file" id="fileInput" accept=".csv" />
  <button id="loadDemo">Cargar demo</button>
</div>

<div class="controls hidden" id="controls">
  <button id="selectAll">Seleccionar todo</button>
  <button id="selectNone">Seleccionar nada</button>
  <label>Buscar: <input type="text" id="searchInput" placeholder="Filtrar por nombre de modelo" /></label>
  <label><input type="checkbox" id="onlySelected" /> Mostrar solo seleccionados</label>
  <button id="exportMatrixCsv">Exportar seleccionados (CSV, matriz)</button>
  <button id="exportSummaryCsv">Exportar resumen por color (CSV)</button>
  <span class="pill">Modelos: <span id="totalModels">0</span></span>
  <span class="pill">Seleccionados: <span id="selectedCount">0</span></span>
</div>

<div style="overflow:auto;" id="tableWrap" class="hidden">
<table id="matrixTbl">
  <thead>
    <tr id="headerRow">
      <th class="sticky">Sel.</th>
      <th class="sticky">Modelo</th>
      <th class="sticky">URL</th>
      <!-- columnas de colores dinámicas -->
    </tr>
  </thead>
  <tbody></tbody>
  <tfoot>
    <tr class="count-row" id="footerRow">
      <td colspan="3" class="right">Usados en seleccionados:</td>
      <!-- celdas de totales dinámicas -->
    </tr>
  </tfoot>
</table>
</div>

<script>
function csvParse(text) {
  const rows = [];
  let i = 0, field = '', row = [], inQuotes = false;
  while (i < text.length) {
    const c = text[i];
    if (inQuotes) {
      if (c === '"') {
        if (text[i+1] === '"') { field += '"'; i += 2; continue; }
        inQuotes = false; i++; continue;
      } else { field += c; i++; continue; }
    } else {
      if (c === '"') { inQuotes = true; i++; continue; }
      if (c === ',') { row.push(field); field = ''; i++; continue; }
      if (c === '\n' || c === '\r') {
        if (c === '\r' && text[i+1] === '\n') i++;
        row.push(field); field = '';
        if (row.length > 1 || (row.length === 1 && row[0] !== '')) rows.push(row);
        row = []; i++; continue;
      }
      field += c; i++; continue;
    }
  }
  if (field.length || row.length) { row.push(field); rows.push(row); }
  return rows;
}
function normalizeColor(s) {
  if (!s) return '';
  let x = s.trim().replace(/\s+/g, ' ');
  x = x.replace(/\bGrey\b/gi, 'Gray');
  x = x.replace(/^matte\b/i, 'Matte');
  x = x.replace(/^[\s\-\–\—·.]+|[\s\-\–\—·.]+$/g, '');
  if (x.toLowerCase() === 'matte' || x.split(' ').length < 2) return '';
  return x;
}
function cleanModelName(name) { return (name || '').replace(/\s*\(No Support.*$/,'').trim(); }
function splitColors(s) {
  if (!s) return [];
  const parts = String(s).split(';').map(t => normalizeColor(t)).filter(Boolean);
  const seen = new Set(); const out = [];
  for (const p of parts) { const k = p.toLowerCase(); if (!seen.has(k)) { seen.add(k); out.push(p); } }
  return out;
}
function download(filename, text) {
  const blob = new Blob([text], {type: 'text/csv;charset=utf-8;'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a'); a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); document.body.removeChild(a);
  URL.revokeObjectURL(url);
}
function csvEscape(val) { if (val == null) return ''; const s = String(val); return /[",\n]/.test(s) ? '"' + s.replace(/"/g,'""') + '"' : s; }

let COLORS = [];
let ROWS = [];
let rowsState = [];

function buildFromCSV(text) {
  const rows = csvParse(text);
  if (!rows.length) { alert("CSV vacío"); return; }
  const headers = rows[0].map(h => h.trim());
  const idxName = headers.findIndex(h => h.toLowerCase() === 'model_name');
  const idxUrl  = headers.findIndex(h => h.toLowerCase() === 'model_url');
  const idxCols = headers.findIndex(h => h.toLowerCase() === 'colors');
  if (idxName === -1 || idxUrl === -1 || idxCols === -1) {
    alert("El CSV debe tener cabeceras: model_name, model_url, colors");
    return;
  }
  const items = [];
  for (let r = 1; r < rows.length; r++) {
    const row = rows[r];
    const name = cleanModelName(row[idxName] || '');
    const url  = row[idxUrl] || '';
    const colors = splitColors(row[idxCols] || '');
    items.push({ name, url, colors });
  }
  const colorSet = new Set();
  items.forEach(it => it.colors.forEach(c => colorSet.add(c)));
  COLORS = Array.from(colorSet).sort((a,b)=>a.localeCompare(b,'en',{sensitivity:'base'}));
  ROWS = items.map(it => {
    const cmap = {}; COLORS.forEach(c => cmap[c] = it.colors.includes(c) ? 1 : 0);
    return { Model: it.name, URL: it.url, colors: cmap };
  });

  document.getElementById("controls").classList.remove("hidden");
  document.getElementById("tableWrap").classList.remove("hidden");
  buildTable();
  updateCounts();
  applyFilters();
  hideEmptyColorColumns();
}

const tbody = document.querySelector("#matrixTbl tbody");
const selectedCountEl = document.getElementById("selectedCount");
const totalModelsEl = document.getElementById("totalModels");
const searchInput = document.getElementById("searchInput");
const onlySelected = document.getElementById("onlySelected");
const headerRow = document.getElementById("headerRow");
const footerRow = document.getElementById("footerRow");

function buildTable() {
  while (headerRow.children.length > 3) headerRow.removeChild(headerRow.lastElementChild);
  while (footerRow.children.length > 1) footerRow.removeChild(footerRow.lastElementChild);
  COLORS.forEach((c, i) => {
    const th = document.createElement('th');
    th.className = 'sticky'; th.dataset.cidx = i; th.textContent = c;
    headerRow.appendChild(th);
    const td = document.createElement('td');
    td.className = 'tick'; td.id = 'count_' + i; td.textContent = '0';
    footerRow.appendChild(td);
  });

  tbody.innerHTML = "";
  rowsState = [];
  ROWS.forEach(r => {
    const tr = document.createElement('tr');

    const tdSel = document.createElement('td');
    const cb = document.createElement('input'); cb.type = 'checkbox';
    cb.addEventListener('change', () => { updateCounts(); applyFilters(); hideEmptyColorColumns(); });
    tdSel.appendChild(cb); tr.appendChild(tdSel);

    const tdModel = document.createElement('td'); tdModel.textContent = r.Model; tr.appendChild(tdModel);

    const tdUrl = document.createElement('td'); tdUrl.className = 'link';
    const a = document.createElement('a'); a.href = r.URL; a.target = '_blank'; a.rel = 'noopener noreferrer'; a.textContent = 'Abrir';
    tdUrl.appendChild(a); tr.appendChild(tdUrl);

    const tds = []; const cells = {};
    COLORS.forEach((c, i) => {
      const td = document.createElement('td'); td.className='tick'; td.dataset.cidx=i;
      const has = r.colors[c] ? 1 : 0; td.textContent = has ? '✓' : '';
      tds.push(td); cells[c] = has; tr.appendChild(td);
    });

    rowsState.push({ tr, cb, model: r.Model.toLowerCase(), modelName: r.Model, url: r.URL, cells, tds });
    tbody.appendChild(tr);
  });

  totalModelsEl.textContent = String(rowsState.length);
}

function getSelectedRows() { return rowsState.filter(r => r.cb.checked && r.tr.style.display !== 'none'); }

function updateCounts() {
  const counts = Array(COLORS.length).fill(0);
  rowsState.forEach(r => { if (r.cb.checked) COLORS.forEach((c,i)=> counts[i] += r.cells[c]); });
  counts.forEach((v,i) => { const td = document.getElementById('count_'+i); if (td) td.textContent = String(v); });
  selectedCountEl.textContent = String(rowsState.filter(r => r.cb.checked).length);
}

function applyFilters() {
  const q = (searchInput.value || '').toLowerCase();
  const only = onlySelected.checked;
  rowsState.forEach(r => { const show = r.model.includes(q) && (!only || r.cb.checked); r.tr.style.display = show ? '' : 'none'; });
}

function hideEmptyColorColumns() {
  const only = onlySelected.checked;
  const active = new Set();
  rowsState.forEach(r => {
    if ((!only || r.cb.checked) && r.tr.style.display !== 'none') {
      COLORS.forEach((c,i) => { if (r.cells[c]) active.add(i); });
    }
  });
  COLORS.forEach((c,i) => {
    const show = !only ? true : active.has(i);
    const th = document.querySelector('th[data-cidx="'+i+'"]'); if (th) th.style.display = show ? '' : 'none';
    rowsState.forEach(r => { const td = r.tds[i]; if (td) td.style.display = show ? '' : 'none'; });
    const ftd = document.getElementById('count_'+i); if (ftd) ftd.style.display = show ? '' : 'none';
  });
}

function csvEscape(val) { if (val == null) return ''; const s = String(val); return /[",\n]/.test(s) ? '"' + s.replace(/"/g,'""') + '"' : s; }
function download(filename, text) {
  const blob = new Blob([text], {type: 'text/csv;charset=utf-8;'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a'); a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

function exportMatrixCsv() {
  const sel = getSelectedRows();
  if (!sel.length) { alert('No hay modelos seleccionados.'); return; }
  const activeIdx = []; COLORS.forEach((c,i) => { if (sel.some(r => r.cells[c])) activeIdx.push(i); });
  const header = ['Model','URL', ...activeIdx.map(i => COLORS[i])];
  const lines = [header.map(csvEscape).join(',')];
  sel.forEach(r => {
    const row = [r.modelName, r.url];
    activeIdx.forEach(i => { const c = COLORS[i]; row.push(r.cells[c] ? '1' : '0'); });
    lines.push(row.map(csvEscape).join(','));
  });
  const counts = activeIdx.map(i => document.getElementById('count_'+i)?.textContent || '0');
  const footer = ['Usados en seleccionados','','', ...counts].slice(0, header.length);
  lines.push(footer.map(csvEscape).join(','));
  download('selected_matrix.csv', lines.join('\\n'));
}
function exportSummaryCsv() {
  const sel = getSelectedRows();
  if (!sel.length) { alert('No hay modelos seleccionados.'); return; }
  const counts = [];
  COLORS.forEach((c,i) => {
    const n = sel.reduce((acc, r) => acc + (r.cells[c] ? 1 : 0), 0);
    if (n > 0) counts.push([c, n]);
  });
  counts.sort((a,b) => b[1]-a[1] || a[0].localeCompare(b[0]));
  const lines = ['Color,Count', ...counts.map(([c,n]) => csvEscape(c)+','+n)];
  download('selected_summary.csv', lines.join('\\n'));
}

document.getElementById('selectAll').addEventListener('click', ()=>{ rowsState.forEach(r => r.cb.checked = true); updateCounts(); applyFilters(); hideEmptyColorColumns(); });
document.getElementById('selectNone').addEventListener('click', ()=>{ rowsState.forEach(r => r.cb.checked = false); updateCounts(); applyFilters(); hideEmptyColorColumns(); });
document.getElementById('searchInput').addEventListener('input', ()=>{ applyFilters(); hideEmptyColorColumns(); });
document.getElementById('onlySelected').addEventListener('change', ()=>{ applyFilters(); hideEmptyColorColumns(); });
document.getElementById('exportMatrixCsv').addEventListener('click', exportMatrixCsv);
document.getElementById('exportSummaryCsv').addEventListener('click', exportSummaryCsv);

document.getElementById('fileInput').addEventListener('change', (e) => {
  const file = e.target.files?.[0]; if (!file) return;
  const fr = new FileReader();
  fr.onload = () => buildFromCSV(String(fr.result || ''));
  fr.readAsText(file, 'utf-8');
});

document.getElementById('loadDemo').addEventListener('click', () => {
  const demo = 'model_name,model_url,colors\\n'
    + '1989 Batmobile Kit,https://example.com/batmobile,"Matte Ash Gray; Matte Charcoal Black; Matte Fossil Gray"\\n'
    + 'AT-AT Chunker Kit,https://example.com/at-at,"Matte Army Red; Matte Charcoal Black; Matte Cotton White; Matte Muted White; Matte Fossil Gray"\\n';
  buildFromCSV(demo);
});
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
