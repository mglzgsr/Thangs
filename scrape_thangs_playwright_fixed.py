# scrape_thangs_playwright_fixed.py
# -*- coding: utf-8 -*-
import csv, re, sys, time, os
from pathlib import Path
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

BASE_DESIGNER = "https://thangs.com/designer/The%20Kit%20Kiln"

MODEL_LINK_RE = re.compile(r"/designer/[^/]+/3d-model/[^?\s]+-\d+$", re.IGNORECASE)
POLY_LINE_RE  = re.compile(r"Polymaker\s+.*?PLA", re.IGNORECASE)
HEADER_RE = re.compile(
    r"(Want your.*?Shop the filament we used on the Polymaker Website|Shop the filament we used on the Polymaker Website)",
    re.IGNORECASE | re.DOTALL
)
DEBUG_DIR = Path("debug"); DEBUG_DIR.mkdir(exist_ok=True)

def _extract_from_poly_block(soup):
    """
    Intenta localizar el bloque de 'Shop the filament...' y extrae SOLO ahí.
    Busca el texto del encabezado, sube a un contenedor y recorre
    hermanos inmediatos (p, li, a, div cortos) hasta topar con otra sección.
    """
    # 1) localizar nodo de texto con el header
    header_node = soup.find(string=HEADER_RE)
    if not header_node:
        return []

    # 2) sube a un contenedor razonable
    container = header_node.find_parent(["section", "div", "article", "main"]) or header_node.parent

    # 3) crea una lista lineal de nodos 'después del header' pero cerca
    # heurística: siguientes 10-15 nodos de texto/enlaces/list items
    colors = []
    def consider_text(txt):
        for m in POLY_ITEM_RE.findall(txt or ""):
            c = m.strip()
            if c and c.lower() not in [x.lower() for x in colors]:
                colors.append(c)

    # recoger enlaces y textos cercanos
    # a) enlaces dentro del mismo contenedor cercano al header
    for tag in container.find_all(["a", "li", "p", "div"], limit=50):
        # corta si aparece otra cabecera o un separator fuerte
        if tag.name in ("h1", "h2", "h3", "hr"):
            break
        t = tag.get_text(" ", strip=True)
        # prioridad: anchors que apunten a Polymaker
        if tag.name == "a":
            href = (tag.get("href") or "").lower()
            if "polymaker" in href:
                consider_text(tag.get_text(" ", strip=True))
                continue
        # si no hay anchor a Polymaker, igualmente considera el texto del tag
        consider_text(t)

    return colors

def dump_debug(page, name):
    try:
        page.screenshot(path=str(DEBUG_DIR / f"{name}.png"), full_page=True)
    except Exception:
        pass
    try:
        (DEBUG_DIR / f"{name}.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass

def collect_links_from_html(html):
    soup = BeautifulSoup(html, "lxml")
    urls = set()

    # 1) Preferido: enlaces de modelo con patrón completo /designer/<name>/3d-model/<slug>-<id>
    for a in soup.find_all("a", href=True):
        href = a["href"] or ""
        if MODEL_LINK_RE.search(href):
            urls.add(urljoin("https://thangs.com", href))

    # 2) Fallback: cualquier /3d-model/ (por si el HTML cambia o vienen de colecciones)
    if not urls:
        for a in soup.find_all("a", href=True):
            href = a["href"] or ""
            if "/3d-model/" in href:
                urls.add(urljoin("https://thangs.com", href))

    return urls

def discover_model_urls_scroll(page, listing_url):
    urls = set()
    print("[*] Intentando scroll infinito…")
    page.goto(listing_url, wait_until="networkidle", timeout=90000)
    time.sleep(1.0)
    # Relajamos el selector: cualquier enlace a /3d-model/
    try:
        page.wait_for_selector('a[href*="/3d-model/"]', timeout=15000)
    except PwTimeout:
        print("[!] No aparecieron enlaces tras networkidle; probamos domcontentloaded + debug dump")
        dump_debug(page, "designer_initial")
        page.goto(listing_url, wait_until="domcontentloaded", timeout=90000)
        time.sleep(2.0)

    last_height = 0
    stagnant = 0
    for _ in range(30):
        html = page.content()
        urls |= collect_links_from_html(html)  # usa collect_links_from_html actualizado abajo

        # scroll
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass
        time.sleep(1.0)

        try:
            height = page.evaluate("document.body.scrollHeight")
        except Exception:
            height = last_height

        if height == last_height:
            stagnant += 1
        else:
            stagnant = 0
        last_height = height
        if stagnant >= 3:
            break

    print(f"[*] Scroll recogió {len(urls)} enlaces")
    return sorted(urls)

def discover_model_urls_paged(page, listing_url, max_pages=20):
    print("[*] Intentando paginación ?page=N…")
    urls = set()
    for n in range(1, max_pages+1):
        url = listing_url if n == 1 else f"{listing_url}?page={n}"
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except PwTimeout:
            page.goto(url, wait_until="networkidle", timeout=60000)
        time.sleep(1.0)
        html = page.content()
        found = collect_links_from_html(html)
        print(f"    - page {n}: {len(found)} enlaces")
        urls |= found
        if not found:  # si ya no hay más, paramos
            break
    print(f"[*] Paginación recogió {len(urls)} enlaces")
    return sorted(urls)

def extract_polymaker_colors(page, model_url):
    try:
        page.goto(model_url, wait_until="domcontentloaded", timeout=60000)
    except PwTimeout:
        page.goto(model_url, wait_until="networkidle", timeout=60000)

    time.sleep(1.2)
    html = page.content()
    soup = BeautifulSoup(html, "lxml")

    # Título
    title_node = soup.find(["h1", "title"])
    title_text = title_node.get_text(strip=True) if title_node else model_url.rsplit("/", 1)[-1]

    # 1) Primero: intentar en el bloque acotado
    colors = _extract_from_poly_block(soup)

    # 2) Fallback: si no encontramos el bloque, usar el método global
    if not colors:
        text = soup.get_text("\n", strip=True)
        raw_matches = POLY_LINE_RE.findall(text)  # tu regex anterior (Polymaker Matte ... PLA)
        for m in raw_matches:
            c = m.strip()
            if c and c.lower() not in [x.lower() for x in colors]:
                colors.append(c)

    # Limpieza final (igual que ya haces en tu pipeline)
    clean_colors = []
    for c in colors:
        t = re.sub(r"(?i)^Matte\s+", "Matte ", c)   # normaliza prefijo
        t = re.sub(r"(?i)\s*PLA\s*$", "", t)        # quita 'PLA' sobrante si aparece
        t = t.strip(" -–—·.")
        if t and t.lower() != "matte" and len(t.split()) >= 2:
            if t.lower() not in [x.lower() for x in clean_colors]:
                clean_colors.append(t)

    return title_text, clean_colors

def main():
    designer = sys.argv[1] if len(sys.argv) > 1 else BASE_DESIGNER

    rows = []
    color_to_models = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        ))
        page = context.new_page()

        print(f"[+] Cargando diseñador: {designer}")
        urls = discover_model_urls_scroll(page, designer)

        if not urls:
            # dump extra debug y probar paginación
            dump_debug(page, "designer_after_scroll")
            urls = discover_model_urls_paged(page, designer)

        if not urls:
            print("[X] No se encontraron modelos. Subiendo debug/ para inspeccionar.")
            browser.close()
            # crear archivos vacíos para no fallar el job
            Path("models_colors.csv").write_text("model_name,model_url,colors\n", encoding="utf-8")
            Path("color_counts.csv").write_text("color,count,models\n", encoding="utf-8")
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

            time.sleep(0.5)

        browser.close()

    # CSVs
    with open("models_colors.csv","w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=["model_name","model_url","colors"])
        w.writeheader(); w.writerows(rows)

    with open("color_counts.csv","w",newline="",encoding="utf-8") as f:
        w=csv.writer(f); w.writerow(["color","count","models"])
        for color,models in sorted(color_to_models.items(), key=lambda x:(-len(x[1]), x[0].lower())):
            w.writerow([color,len(models),"; ".join(models)])

    print("[✓] Listo. Archivos: models_colors.csv, color_counts.csv")
    if color_to_models:
        top = sorted(color_to_models.items(), key=lambda x: -len(x[1]))[:10]
        print("Top colores:")
        for color, models in top:
            print(f"  {color}: {len(models)} usos")

if __name__ == "__main__":
    main()
