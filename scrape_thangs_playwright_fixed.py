#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Playwright-based scraper for Thangs > The Kit Kiln to extract "Polymaker Matte ... PLA" colors.

Usage:
  python scrape_thangs_playwright_fixed.py [designer_url]

Setup (first time):
  pip install playwright beautifulsoup4 lxml
  playwright install chromium
"""
import csv
import re
import sys
import time
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

BASE_DESIGNER = "https://thangs.com/designer/The%20Kit%20Kiln"

MODEL_LINK_RE = re.compile(r"/designer/The%20Kit%20Kiln/3d-model/[^?\s]+-\d+$")
POLY_LINE_RE  = re.compile(r"Polymaker\s+Matte\s+.*?PLA", re.IGNORECASE)


def discover_model_urls(page, designer_url):
    """Scroll/paginate through the designer page and collect model URLs."""
    urls = set()
    page.goto(designer_url, wait_until="domcontentloaded", timeout=60000)

    last_height = 0
    stagnant = 0

    # Try to scroll multiple times to trigger lazy-loading
    for _ in range(25):
        # Collect links on current viewport
        anchors = page.locator("a[href]").all()
        for a in anchors:
            try:
                href = a.get_attribute("href") or ""
                if MODEL_LINK_RE.search(href):
                    urls.add(urljoin("https://thangs.com", href))
            except Exception:
                pass

        # Scroll to bottom
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass
        time.sleep(1.0)

        # Detect if height no longer changes (we reached the end)
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

    return sorted(urls)


def extract_polymaker_colors(page, model_url):
    """Open page and parse visible text for color lines."""
    try:
        page.goto(model_url, wait_until="domcontentloaded", timeout=60000)
    except PwTimeout:
        page.goto(model_url, wait_until="networkidle", timeout=60000)

    time.sleep(1.2)  # allow late content

    html = page.content()
    soup = BeautifulSoup(html, "lxml")

    # Title
    title_node = soup.find(["h1", "title"])
    if title_node:
        title_text = title_node.get_text(strip=True)
    else:
        title_text = model_url.rsplit("/", 1)[-1]

    # Full text for regex search
    text = soup.get_text("\n", strip=True)

    raw_matches = POLY_LINE_RE.findall(text)

    colors = []
    for m in raw_matches:
        t = re.sub(r"(?i)^Polymaker\s+", "", m)
        t = re.sub(r"(?i)\s*PLA\s*$", "", t)
        t = t.strip()
        tl = t.lower()
        if tl not in [c.lower() for c in colors]:
            colors.append(t)

    return title_text, colors


def main():
    designer = BASE_DESIGNER
    if len(sys.argv) > 1:
        designer = sys.argv[1]

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
        model_urls = discover_model_urls(page, designer)
        print(f"[+] Modelos detectados: {len(model_urls)}")

        if not model_urls:
            print("[!] No se encontraron modelos; puede requerir login/captcha o cambió el layout.")
            browser.close()
            return

        total = len(model_urls)
        for i, url in enumerate(model_urls, 1):
            print(f"[{i}/{total}] {url}")
            try:
                title, colors = extract_polymaker_colors(page, url)
            except Exception as e:
                print(f"[WARN] {url}: {e}")
                time.sleep(1.0)
                continue

            rows.append({
                "model_name": title,
                "model_url": url,
                "colors": "; ".join(colors)
            })
            for c in colors:
                color_to_models.setdefault(c, []).append(title)

            time.sleep(0.8)

        browser.close()

    # Write CSVs
    with open("models_colors.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["model_name", "model_url", "colors"])
        w.writeheader()
        w.writerows(rows)

    with open("color_counts.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["color", "count", "models"])
        for color, models in sorted(color_to_models.items(), key=lambda x: (-len(x[1]), x[0].lower())):
            w.writerow([color, len(models), "; ".join(models)])

    print("[✓] Listo. Archivos: models_colors.csv, color_counts.csv")
    if color_to_models:
        top = sorted(color_to_models.items(), key=lambda x: -len(x[1]))[:10]
        print("Top colores:")
        for color, models in top:
            print(f"  {color}: {len(models)} usos")


if __name__ == "__main__":
    main()
