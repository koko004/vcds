import re
import io
import os
import asyncio
import fitz
import pdfplumber
import pytesseract
from PIL import Image
from typing import Callable


CSV_CARACTERES_VALIDOS = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"


def _normalizar_texto(texto: str) -> str:
    texto = texto.strip()
    texto = re.sub(r'[ \t]+', ' ', texto)
    texto = re.sub(r'\n{3,}', '\n\n', texto)
    return texto


def _log(log_fn: Callable | None, msg: str):
    if log_fn:
        log_fn(msg)


def _extraer_con_pdfplumber(pdf_path: str, log_fn: Callable | None = None) -> str:
    _log(log_fn, "Extracting text with pdfplumber")
    texto = ""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for pagina in pdf.pages:
                t = pagina.extract_text(layout=True)
                if t:
                    texto += t + "\n"
    except Exception:
        _log(log_fn, "pdfplumber failed")
    return texto


def _extraer_pie_pdfplumber(pdf_path: str, log_fn: Callable | None = None) -> str:
    _log(log_fn, "Extracting footer with pdfplumber")
    texto_pie = ""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for pagina in pdf.pages:
                alto = pagina.height
                umbral_y = alto * 0.80
                chars = pagina.chars
                chars_pie = [c for c in chars if c.get("top", 0) >= umbral_y]
                chars_pie.sort(key=lambda c: (c.get("top", 0), c.get("x0", 0)))
                linea_actual = ""
                ultimo_top = None
                for c in chars_pie:
                    top = c.get("top", 0)
                    if ultimo_top is not None and abs(top - ultimo_top) > 5:
                        texto_pie += linea_actual.strip() + "\n"
                        linea_actual = ""
                    linea_actual += c.get("text", "")
                    ultimo_top = top
                if linea_actual.strip():
                    texto_pie += linea_actual.strip() + "\n"
    except Exception:
        _log(log_fn, "pdfplumber footer extraction failed")
    return texto_pie


def _extraer_con_fitz(pdf_path: str, log_fn: Callable | None = None) -> str:
    _log(log_fn, "Extracting text with PyMuPDF (fitz)")
    texto = ""
    doc = fitz.open(pdf_path)
    for pagina in doc:
        texto += pagina.get_text() + "\n"
    doc.close()
    return texto


def _normalizar_con_ghostscript(pdf_path: str, log_fn: Callable | None = None) -> str | None:
    _log(log_fn, "Normalizing PDF with Ghostscript (regenerating ToUnicode CMap)")
    try:
        import subprocess
        import tempfile
        normalized = os.path.join(tempfile.gettempdir(), f"gs_norm_{os.getpid()}.pdf")
        result = subprocess.run(
            ["gs", "-o", normalized, "-sDEVICE=pdfwrite",
             "-dPDFSETTINGS=/prepress", "-dNOPAUSE", "-dQUIET", "-dBATCH", pdf_path],
            capture_output=True, timeout=30
        )
        if result.returncode == 0 and os.path.exists(normalized):
            texto = ""
            doc = fitz.open(normalized)
            for pagina in doc:
                texto += pagina.get_text() + "\n"
            doc.close()
            os.remove(normalized)
            return texto if texto.strip() else None
        else:
            _log(log_fn, f"Ghostscript failed: {result.stderr.decode('latin-1', errors='replace')[:200]}")
    except Exception as e:
        _log(log_fn, f"Ghostscript error: {e}")
    return None


def _extraer_con_playwright(pdf_path: str, log_fn: Callable | None = None) -> str:
    _log(log_fn, "Extracting text with Chromium via PDF.js")
    texto = ""
    try:
        from playwright.sync_api import sync_playwright
        from http.server import HTTPServer, SimpleHTTPRequestHandler
        import json
        import threading
        import socket
        import shutil

        abs_path = os.path.abspath(pdf_path)
        pdf_name = os.path.basename(abs_path)
        static_dir = os.path.join(os.path.dirname(__file__), "static")

        def find_free_port():
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(('', 0))
                return s.getsockname()[1]

        port = find_free_port()

        tmp_dir = os.path.join(os.path.dirname(__file__), ".pdfjs_tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        shutil.copy2(abs_path, os.path.join(tmp_dir, pdf_name))

        html_src = os.path.join(static_dir, "pdfjs_extract.html")
        with open(html_src, "r") as f:
            html = f.read()
        html = html.replace(
            "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js",
            f"http://127.0.0.1:{port}/static/pdf.worker.min.js"
        )
        html = html.replace(
            "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js",
            f"http://127.0.0.1:{port}/static/pdf.min.js"
        )
        tmp_index = os.path.join(tmp_dir, "index.html")
        with open(tmp_index, "w") as f:
            f.write(html)

        class MuxHandler(SimpleHTTPRequestHandler):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
            def log_message(self, *a): pass
            def translate_path(self, path):
                import urllib.parse
                path = urllib.parse.unquote(path.split('?')[0].split('#')[0])
                if path.startswith('/static/'):
                    return os.path.join(static_dir, path[8:])
                return os.path.join(tmp_dir, path.lstrip('/'))

        server = HTTPServer(("127.0.0.1", port), MuxHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        _log(log_fn, f"Local server started on port {port}")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            page.goto(f"http://127.0.0.1:{port}/index.html", timeout=10000)
            page.wait_for_load_state("networkidle")

            page.evaluate(f"""
                (async () => {{
                    try {{
                        const resp = await fetch('/{pdf_name}');
                        const ct = resp.headers.get('content-type');
                        const data = await resp.arrayBuffer();
                        if (data.byteLength < 100) {{
                            window.__pdfResult = JSON.stringify({{ok: false, error: 'PDF too small: ' + data.byteLength}});
                            return;
                        }}
                        const uint8Array = new Uint8Array(data);
                        const loadingTask = pdfjsLib.getDocument({{data: uint8Array}});
                        const pdf = await loadingTask.promise;
                        let allText = [];
                        for (let i = 1; i <= pdf.numPages; i++) {{
                            const pg = await pdf.getPage(i);
                            const content = await pg.getTextContent();
                            allText.push(content.items.map(item => item.str).join(' '));
                        }}
                        window.__pdfResult = JSON.stringify({{ok: true, pages: allText.length, text: allText.join('\\n')}});
                    }} catch(e) {{
                        window.__pdfResult = JSON.stringify({{ok: false, error: e.message}});
                    }}
                }})();
            """)

            page.wait_for_function("window.__pdfResult !== undefined", timeout=30000)
            result_json = page.evaluate("window.__pdfResult")
            browser.close()

        server.shutdown()
        shutil.rmtree(tmp_dir, ignore_errors=True)

        result = json.loads(result_json)
        if result.get("ok"):
            texto = result.get("text", "")
            _log(log_fn, f"PDF.js extracted {len(texto)} chars via Chromium")
        else:
            _log(log_fn, f"PDF.js error: {result.get('error', 'unknown')}")
    except Exception as e:
        _log(log_fn, f"Playwright extraction failed: {e}")
    return texto


def _extraer_con_screenshot_ocr(pdf_path: str, log_fn: Callable | None = None) -> str:
    _log(log_fn, "Starting Playwright screenshot + Tesseract OCR extraction")
    texto = ""
    try:
        from playwright.sync_api import sync_playwright
        import tempfile

        abs_path = os.path.abspath(pdf_path)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1280, "height": 1024},
                device_scale_factor=2
            )
            page = context.new_page()

            file_url = f"file://{abs_path}"
            _log(log_fn, f"Opening PDF in Chromium: {abs_path}")
            page.goto(file_url, wait_until="load", timeout=30000)
            page.wait_for_timeout(3000)

            num_pages = page.evaluate("""() => {
                const pages = document.querySelectorAll('.page, [data-page-number], .pdfPage');
                return pages.length || document.querySelector('#viewer')?.children?.length || 1;
            }""")
            _log(log_fn, f"PDF has {num_pages} page(s)")

            for i in range(num_pages):
                _log(log_fn, f"Processing page {i+1}/{num_pages}")

                page.evaluate(f"""() => {{
                    const pages = document.querySelectorAll('.page, [data-page-number], .pdfPage');
                    const viewer = document.querySelector('#viewer');
                    const targets = pages.length ? pages : (viewer?.children ? viewer.children : []);
                    if (targets[{i}]) targets[{i}].scrollIntoView();
                }}""")
                page.wait_for_timeout(1000)

                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                    tmp_path = tmp.name

                page.screenshot(path=tmp_path, full_page=False)

                img = Image.open(tmp_path)
                gray = img.convert("L")
                config = "--psm 4 --oem 1"

                texto_bin = pytesseract.image_to_string(
                    gray.point(lambda p: 255 if p > 120 else 0),
                    lang="spa", config=config
                )
                texto_raw = pytesseract.image_to_string(gray, lang="spa", config=config)

                pagina_texto = texto_raw if len(texto_raw.strip()) > len(texto_bin.strip()) else texto_bin
                texto += pagina_texto + "\n"
                _log(log_fn, f"Page {i+1}: extracted {len(pagina_texto)} chars via OCR")

                os.remove(tmp_path)

            browser.close()

        _log(log_fn, f"Screenshot OCR complete: {len(texto)} total chars")
    except Exception as e:
        _log(log_fn, f"Screenshot OCR failed: {e}")
    return texto


def _ocr_pagina(pagina, log_fn: Callable | None = None) -> str:
    _log(log_fn, "Rendering page to image at 400 DPI")
    pix = pagina.get_pixmap(dpi=400)
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    gray = img.convert("L")
    config = "--psm 4 --oem 1"

    texto_bin = pytesseract.image_to_string(gray.point(lambda p: 255 if p > 120 else 0), lang="spa", config=config)
    texto_raw = pytesseract.image_to_string(gray, lang="spa", config=config)

    if len(texto_raw.strip()) > len(texto_bin.strip()):
        _log(log_fn, "Running OCR with Tesseract (grayscale, no binarization)...")
        return texto_raw
    _log(log_fn, "Running OCR with Tesseract (binarized)...")
    return texto_bin


def _ocr_csv_bottom_region(pagina, log_fn: Callable | None = None) -> str | None:
    pw, ph = pagina.rect.width, pagina.rect.height
    region = fitz.Rect(0, ph * 0.75, pw, ph)
    pix = pagina.get_pixmap(matrix=fitz.Matrix(400/72, 400/72), clip=region)
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    gray = img.convert("L")
    _log(log_fn, "OCR on bottom region (grayscale) for CSV...")
    return pytesseract.image_to_string(gray, lang="spa", config="--psm 4 --oem 1")


def _extraer_con_ocr(pdf_path: str, log_fn: Callable | None = None) -> str:
    _log(log_fn, "No selectable text found — falling back to OCR")
    texto = ""
    doc = fitz.open(pdf_path)
    for pagina in doc:
        texto += _ocr_pagina(pagina, log_fn) + "\n"
    if not re.search(r'SD[:\-]', texto):
        _log(log_fn, "CSV not found in full OCR — trying bottom region...")
        for pagina in doc:
            texto_bottom = _ocr_csv_bottom_region(pagina, log_fn)
            if texto_bottom:
                texto += "\n" + texto_bottom
    doc.close()
    return texto


def extraer_texto(pdf_path: str, log_fn: Callable | None = None) -> str:
    texto = _extraer_con_fitz(pdf_path, log_fn)
    if texto.strip():
        _log(log_fn, f"Extracted {len(texto)} chars with fitz (selectable text)")
    else:
        _log(log_fn, "fitz returned no text")
        texto = _extraer_con_pdfplumber(pdf_path, log_fn)
        if texto.strip():
            _log(log_fn, f"Extracted {len(texto)} chars with pdfplumber")
        else:
            _log(log_fn, "pdfplumber returned no text — trying Ghostscript normalization")
            texto_gs = _normalizar_con_ghostscript(pdf_path, log_fn)
            if texto_gs and texto_gs.strip():
                texto = texto_gs
                _log(log_fn, f"Extracted {len(texto)} chars after Ghostscript normalization")
            else:
                _log(log_fn, "Ghostscript produced no text — trying PDF.js via Chromium")
                texto = _extraer_con_playwright(pdf_path, log_fn)
                if texto.strip():
                    _log(log_fn, f"Extracted {len(texto)} chars with PDF.js (Chromium)")
                else:
                    _log(log_fn, "PDF.js returned no text — falling back to OCR")
                    texto = _extraer_con_ocr(pdf_path, log_fn)
                    _log(log_fn, f"Extracted {len(texto)} chars with OCR")
    return _normalizar_texto(texto)


def extraer_pie(pdf_path: str, log_fn: Callable | None = None, texto_ya_extraido: str = "") -> str:
    pie = _extraer_pie_pdfplumber(pdf_path, log_fn)
    if not pie.strip():
        if texto_ya_extraido.strip():
            _log(log_fn, "Footer not found in pdfplumber, using last lines of already extracted text")
            lineas = texto_ya_extraido.split('\n')
            pie = '\n'.join(lineas[-15:])
        else:
            _log(log_fn, "Footer not found in pdfplumber, extracting full text for footer")
            texto = extraer_texto(pdf_path, log_fn)
            lineas = texto.split('\n')
            pie = '\n'.join(lineas[-15:])
    return _normalizar_texto(pie)


PATRON_NOMBRE = re.compile(
    r'relativos(?:/as)?\s+a:\s*\n\s*(?:D\.?\/?D?ª?\.?\s*)?(.+?),\s*nacional de',
    re.IGNORECASE
)

PATRON_NIF_NIE = re.compile(
    r'\b(?:NIF|NIE|PERSONA\s+FISICA)\s+([0-9]{8}[A-Z]|[XYZ][0-9]{7}[A-Z])\b',
    re.IGNORECASE
)

PATRON_DNI_SIMPLE = re.compile(r'\b(\d{8}[A-Z])\b')
PATRON_NIE_SIMPLE = re.compile(r'\b([XYZ]\d{7}[A-Z])\b')

PATRON_FECHA_CABECERA = re.compile(
    r'Fecha de\s*expedición\s*(\d{2}/\d{2}/\d{4})'
)
PATRON_FECHA_PIE = re.compile(
    r'(\d{2}-\d{2}-\d{4})'
)
PATRON_FECHA_TEXTO = re.compile(
    r'(\d{2}/\d{2}/\d{4})'
)
PATRON_FECHA_CUERPO = re.compile(
    r'(?:Madrid|madrid),?\s*a\s+(\d{1,2})\s+de\s+(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre)\s+de\s+(\d{4})',
    re.IGNORECASE
)

MESES = {
    "enero": "01", "febrero": "02", "marzo": "03", "abril": "04",
    "mayo": "05", "junio": "06", "julio": "07", "agosto": "08",
    "septiembre": "09", "octubre": "10", "noviembre": "11", "diciembre": "12",
}


def _extraer_csv_de_url(texto: str) -> str | None:
    m = re.search(
        r'CSV=(?:5?[SDsd][:;]?)?([A-Za-z0-9]{4}-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}-[A-Za-z0-9]{4})',
        texto
    )
    if m:
        return m.group(1)
    m = re.search(
        r'CSV=5?[SDsd][:;]?\s*([A-Za-z0-9.\-]+)',
        texto
    )
    if m:
        raw = m.group(1)
        raw = re.sub(r'(?<=[A-Za-z0-9])\.(?=[A-Za-z0-9])', '', raw)
        partes = [p for p in raw.split('-') if p]
        if len(partes) >= 4:
            return '-'.join(partes[:4])
    return None


def _extraer_csv_de_bloque(texto: str) -> str | None:
    m = re.search(
        r'(?:Código Seguro de\s*Verificación|Verificación)\s*\|?\s*(?:SD:)?([A-Za-z0-9]{4}-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}-[A-Za-z0-9]{4})',
        texto
    )
    if m:
        return m.group(1)
    return None


def _extraer_csv_generico(texto: str) -> str | None:
    m = re.search(
        r'(?:SD[:;])\s*([A-Za-z0-9]{4}-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}-[A-Za-z0-9]{4})',
        texto
    )
    if m:
        return m.group(1)
    return None


def _extraer_csv_ocr(texto: str) -> str | None:
    m = re.search(
        r'S5D([a-zA-Z0-9]{3,6})[\-\.]([a-zA-Z0-9]{3,6})[\-\.]([a-zA-Z0-9]{3,6})[\-\.]([a-zA-Z0-9]{2,6})',
        texto
    )
    if m:
        parts = [g[:4] for g in m.groups()]
        return '-'.join(parts)
    return None


def _sanitizar_csv(csv_limpio: str) -> str:
    csv_limpio = csv_limpio.replace(' ', '')
    partes = csv_limpio.split('-')
    partes_corregidas = []
    for parte in partes:
        p = parte
        p = re.sub(r'(?<=\d)B(?=\d)', '6', p)
        p = re.sub(r'(?<=[a-z])B(?=[a-z0-9])', '', p)
        p = re.sub(r'(?<=[a-z0-9])B(?=[a-z])', '', p)
        p = p[:4]
        partes_corregidas.append(p)
    return '-'.join(partes_corregidas)


def _corregir_csv_ocr(csv_raw: str) -> str:
    partes = csv_raw.split('-')
    corregidas = []
    for parte in partes:
        p = parte
        p = p.replace('Pa', 'Fa')
        p = p.replace('58', '5B')
        p = re.sub(r'(\d)8(\d)', r'\1B\2', p)
        p = p.replace('.0', '')
        if len(p) < 4:
            p = p.ljust(4, 'c')
        corregidas.append(p[:4])
    return '-'.join(corregidas)


def extraer_csv(texto: str, pie: str = "") -> str | None:
    busqueda = pie + "\n" + texto

    csv_raw = _extraer_csv_de_url(busqueda)
    if not csv_raw:
        csv_raw = _extraer_csv_de_bloque(pie)
    if not csv_raw:
        csv_raw = _extraer_csv_de_bloque(texto)
    if not csv_raw:
        csv_raw = _extraer_csv_generico(busqueda)
    if not csv_raw:
        csv_raw = _extraer_csv_ocr(busqueda)
        if csv_raw:
            csv_raw = _corregir_csv_ocr(csv_raw)
    if not csv_raw:
        return None

    csv_final = _sanitizar_csv(csv_raw)

    if len(csv_final.replace('-', '')) >= 12:
        return f"SD:{csv_final}"
    return None


def extraer_nombre(texto: str) -> str | None:
    m = PATRON_NOMBRE.search(texto)
    if m:
        return re.sub(r'\s+', ' ', m.group(1)).strip().title()
    return None


def extraer_dni(texto: str) -> str | None:
    m = PATRON_NIF_NIE.search(texto)
    if m:
        return m.group(1)
    m = PATRON_DNI_SIMPLE.search(texto)
    if m:
        return m.group(1)
    m = PATRON_NIE_SIMPLE.search(texto)
    return m.group(1) if m else None


def extraer_fecha(texto: str, pie: str = "") -> str | None:
    m = PATRON_FECHA_CABECERA.search(texto)
    if m:
        return m.group(1)
    m = PATRON_FECHA_PIE.search(pie)
    if m:
        return m.group(1).replace('-', '/')
    m = PATRON_FECHA_PIE.search(texto)
    if m:
        return m.group(1).replace('-', '/')
    m = PATRON_FECHA_TEXTO.search(texto)
    if m:
        return m.group(1)
    m = PATRON_FECHA_CUERPO.search(texto)
    if m:
        dia = m.group(1).zfill(2)
        mes = MESES.get(m.group(2).lower(), "01")
        anio = m.group(3)
        return f"{dia}/{mes}/{anio}"
    return None


def extraer_no_consta(texto: str) -> bool:
    return "NO CONSTAN" in texto


def extraer_datos(pdf_path: str, log_fn: Callable | None = None) -> dict:
    _log(log_fn, "Starting PDF data extraction")
    texto = extraer_texto(pdf_path, log_fn)
    pie = extraer_pie(pdf_path, log_fn, texto_ya_extraido=texto)
    _log(log_fn, "Parsing fields: name, DNI, CSV, date, NO CONSTA")
    nombre = extraer_nombre(texto)
    dni = extraer_dni(texto)
    csv_val = extraer_csv(texto, pie)
    fecha = extraer_fecha(texto, pie)
    no_consta = extraer_no_consta(texto)
    _log(log_fn, f"Name: {nombre or '(not found)'}")
    _log(log_fn, f"DNI: {dni or '(not found)'}")
    _log(log_fn, f"CSV: {csv_val or '(not found)'}")
    _log(log_fn, f"Date: {fecha or '(not found)'}")
    _log(log_fn, f"NO CONSTA: {no_consta}")
    _log(log_fn, "Extraction complete")
    return {
        "nombre": nombre,
        "dni": dni,
        "csv": csv_val,
        "fecha_emision": fecha,
        "no_consta": no_consta,
        "texto_completo": texto,
    }


def extraer_datos_ocr(pdf_path: str, log_fn: Callable | None = None) -> dict:
    _log(log_fn, "Starting OCR-based extraction (Playwright screenshot + Tesseract)")
    texto = _extraer_con_screenshot_ocr(pdf_path, log_fn)
    if not texto.strip():
        _log(log_fn, "Screenshot OCR returned no text")
        return {
            "nombre": None,
            "dni": None,
            "csv": None,
            "fecha_emision": None,
            "no_consta": False,
            "texto_completo": "",
        }
    pie = _extraer_pie_pdfplumber(pdf_path, log_fn)
    if not pie.strip():
        lineas = texto.split('\n')
        pie = '\n'.join(lineas[-15:])
    _log(log_fn, "Parsing fields from OCR text")
    nombre = extraer_nombre(texto)
    dni = extraer_dni(texto)
    csv_val = extraer_csv(texto, pie)
    fecha = extraer_fecha(texto, pie)
    no_consta = extraer_no_consta(texto)
    _log(log_fn, f"OCR Name: {nombre or '(not found)'}")
    _log(log_fn, f"OCR DNI: {dni or '(not found)'}")
    _log(log_fn, f"OCR CSV: {csv_val or '(not found)'}")
    _log(log_fn, f"OCR Date: {fecha or '(not found)'}")
    _log(log_fn, f"OCR NO CONSTA: {no_consta}")
    _log(log_fn, "OCR extraction complete")
    return {
        "nombre": nombre,
        "dni": dni,
        "csv": csv_val,
        "fecha_emision": fecha,
        "no_consta": no_consta,
        "texto_completo": texto,
    }
