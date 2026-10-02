import os
import asyncio
import json
import time
import random
from playwright.async_api import async_playwright

URL_BASE = "https://intranet.asturfutbol.es/nfg/"
URL_PENDIENTES = "https://intranet.asturfutbol.es/nfg/NPcd/NFG_GC_GestionValidacion_PdtesTipo?&Sch_Fecha_Hasta=&cod_primaria=5000190&modo=2&coddelegacion=&territorial=&nacional=&noextranjeros=&codigo_tipo_fichero=3509445&Sch_Categoria_principal=&Sch_tipo_juego=&Sch_Procedencia_Principal=&Sch_Fecha_Desde=&Sch_Revisados=2"

DIR_DOWNLOADS = os.path.join(os.path.dirname(__file__), "imports")

import_progress = {
    "running": False,
    "total": 0,
    "processed": 0,
    "downloaded": 0,
    "errors": 0,
    "current": "",
    "logs": [],
    "documents": [],
}


def _log(msg):
    ts = time.strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    import_progress["logs"].append(entry)
    if len(import_progress["logs"]) > 200:
        import_progress["logs"] = import_progress["logs"][-200:]
    print(f"[Importador] {msg}")


def _delay(min_s=1.5, max_s=4.0):
    return random.uniform(min_s, max_s)


async def _handle_alerts(page, timeout_ms=5000):
    try:
        while True:
            dialog = await page.wait_for_event("dialog", timeout=timeout_ms)
            if dialog:
                _log(f"  Alert: {dialog.message[:80]}")
                await dialog.accept()
                await asyncio.sleep(0.3)
    except Exception:
        pass


async def _wait_for_table(page, timeout=30):
    for _ in range(timeout * 2):
        try:
            count = await page.eval_on_selector_all(
                'a[href*="ValidarDocumento"]',
                "els => els.length"
            )
            if count > 0:
                return count
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return 0


async def _extract_documents(page):
    return await page.evaluate("""() => {
        const links = document.querySelectorAll('a[title="Validar sólo este documento"]');
        const results = [];
        links.forEach((a, i) => {
            const href = a.getAttribute('href') || '';
            const match = href.match(/ValidarDocumento\\((\\d+),(\\d+),(\\d+),"(\\d+)","(\\d+)"(?:,"(\\d+)")?\\)/);
            if (match) {
                results.push({
                    index: i,
                    codigo_barras: parseInt(match[1]),
                    cod_personal: parseInt(match[2]),
                    tipo_validar: parseInt(match[3]),
                    param1: match[4],
                    param2: match[5],
                    cod_tipo_fichero: match[6] || '',
                });
            }
        });
        return results;
    }""")


async def _ir_a_pagina_siguiente(page) -> bool:
    """Avanza a la siguiente página de la tabla si existe control de paginación."""
    selectores = [
        'a:text-is("Siguiente")',
        'a:text-is("»")',
        'a:text-is(">")',
        'input[value="Siguiente"]',
        'input[value=">"]',
        'a[href*="Page$Next"]',
    ]
    for sel in selectores:
        try:
            loc = page.locator(sel).first
            if await loc.count() == 0:
                continue
            if not await loc.is_visible():
                continue
            disabled = await loc.get_attribute("disabled")
            cls = (await loc.get_attribute("class")) or ""
            if disabled or "disabled" in cls or "aspNetDisabled" in cls:
                continue
            await loc.click()
            await asyncio.sleep(_delay(2, 4))
            return True
        except Exception:
            continue
    return False


async def _download_pdf(page, popup, doc, idx):
    """Click en 'Descargar Pdf Original' o 'Descargar documento' usando waitForEvent('download')."""
    filename = f"doc_{doc['codigo_barras']}_{idx + 1}.pdf"
    filepath = os.path.join(DIR_DOWNLOADS, filename)

    await asyncio.sleep(_delay(2, 4))

    # Buscar link de descarga: primero "Descargar Pdf Original", luego "Descargar documento"
    download_link = None
    for link_name in ["Descargar Pdf Original", "Descargar documento"]:
        try:
            download_link = popup.get_by_role("link", name=link_name)
            count = await download_link.count()
            if count > 0:
                _log(f"  Link '{link_name}' encontrado")
                break
            download_link = None
        except Exception:
            download_link = None

    if not download_link:
        _log("  No se encontró link de descarga (Descargar documento / Pdf Original)")
        return None

    try:
        async with popup.expect_download(timeout=30000) as download_info:
            await download_link.click()
        download = await download_info.value
        await download.save_as(filepath)
        file_size = os.path.getsize(filepath)
        _log(f"  ✅ Descargado: {filename} ({file_size} bytes)")
        return {"filename": filename, "filepath": filepath, "size": file_size}
    except Exception as e:
        _log(f"  Error en download event: {e}")
        return None


async def importar_documentos(username, password, max_docs=50, headless=True):
    os.makedirs(DIR_DOWNLOADS, exist_ok=True)
    for f in os.listdir(DIR_DOWNLOADS):
        if f.endswith(".pdf"):
            os.remove(os.path.join(DIR_DOWNLOADS, f))

    import_progress.update({
        "running": True,
        "total": 0,
        "processed": 0,
        "downloaded": 0,
        "errors": 0,
        "current": "",
        "logs": [],
        "documents": [],
    })

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=headless,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ],
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                viewport={"width": 1366, "height": 768},
                locale="es-ES",
                timezone_id="Europe/Madrid",
                accept_downloads=True,
            )

            page = await context.new_page()
            await page.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")

            _log("Navegando a intranet...")
            await page.goto(URL_BASE, wait_until="networkidle", timeout=45000)
            await asyncio.sleep(_delay(2, 4))

            # Login exactamente como el test grabado
            _log("Rellenando credenciales de intranet...")
            await page.get_by_role("textbox", name="Usuario").click()
            await page.get_by_role("textbox", name="Usuario").fill(username)
            await page.get_by_role("textbox", name="Usuario").press("Tab")
            await asyncio.sleep(0.5)
            await page.get_by_role("textbox", name="Clave").fill(password)
            await asyncio.sleep(_delay(1, 2))
            await page.get_by_role("link", name="Entrar").click()
            _log("Login enviado")
            await asyncio.sleep(_delay(3, 5))

            # Verificar login
            current_title = await page.title()
            _log(f"Título post-login: {current_title}")
            if "Login" in current_title or "Acceso" in current_title:
                _log("ERROR: Login fallido")
                import_progress["running"] = False
                return

            _log("Login OK, navegando a documentos pendientes...")
            await page.goto(URL_PENDIENTES, wait_until="networkidle", timeout=45000)
            await asyncio.sleep(_delay(3, 5))

            # Esperar tabla
            doc_count = await _wait_for_table(page, timeout=30)
            _log(f"Documentos encontrados: {doc_count}")

            if not doc_count:
                _log("ERROR: No se encontraron documentos")
                import_progress["running"] = False
                return

            documents = await _extract_documents(page)
            _log(f"Documentos extraídos (página 1): {len(documents)}")

            if not documents:
                _log("ERROR: No se pudo extraer información de documentos")
                import_progress["running"] = False
                return

            vistos_cb = set()
            total_procesados = 0
            pagina_num = 1
            import_progress["total"] = min(max(doc_count, len(documents)), max_docs)
            _log(f"Procesando hasta {import_progress['total']} documentos (máx {max_docs})...")

            while True:
                if pagina_num > 1:
                    await _wait_for_table(page, timeout=15)
                    documents = await _extract_documents(page)
                    _log(f"Página {pagina_num}: {len(documents)} documentos en tabla")
                    if not documents:
                        break

                for doc in documents:
                    if total_procesados >= max_docs or not import_progress["running"]:
                        break
                    if doc["codigo_barras"] in vistos_cb:
                        continue
                    vistos_cb.add(doc["codigo_barras"])
                    idx = total_procesados
                    total_procesados += 1
                    import_progress["processed"] = idx + 1
                    import_progress["current"] = f"CB:{doc['codigo_barras']} ({idx + 1}/{import_progress['total']})"
                    _log(f"--- [{idx + 1}/{import_progress['total']}] CB:{doc['codigo_barras']} ---")

                    try:
                        alert_task = asyncio.create_task(_handle_alerts(page, timeout_ms=3000))

                        # 1. Preparar listener de popup ANTES de click
                        popup_promise = page.wait_for_event("popup", timeout=15000)

                        # 2. Click en "Validar sólo este documento" (por índice)
                        links = page.get_by_role("link", name="Validar sólo este documento")
                        await links.nth(doc["index"]).click()
                        _log(f"  Click en ValidarDocumento, esperando popup...")

                        # 3. Obtener popup
                        popup = await popup_promise
                        _log(f"  Popup abierto: {popup.url[:80]}")

                        # 4. Descargar PDF usando waitForEvent('download') como en el test
                        result = await _download_pdf(page, popup, doc, idx)

                        # 5. Cerrar popup
                        try:
                            await popup.close()
                        except Exception:
                            pass

                        alert_task.cancel()
                        try:
                            await alert_task
                        except asyncio.CancelledError:
                            pass

                        if result:
                            import_progress["downloaded"] += 1
                            import_progress["documents"].append({
                                "codigo_barras": doc["codigo_barras"],
                                "filename": result["filename"],
                                "filepath": result["filepath"],
                                "size": result["size"],
                            })
                        else:
                            import_progress["errors"] += 1

                        await asyncio.sleep(_delay(1, 3))

                    except Exception as e:
                        _log(f"❌ Error: {e}")
                        import_progress["errors"] += 1
                        alert_task.cancel()
                        try:
                            await alert_task
                        except asyncio.CancelledError:
                            pass

                if total_procesados >= max_docs:
                    _log(f"Límite alcanzado ({max_docs} documentos)")
                    break
                if not import_progress["running"]:
                    _log("Detenido por el usuario")
                    break
                _log(f"Página {pagina_num} completada, buscando más documentos...")
                if not await _ir_a_pagina_siguiente(page):
                    _log("No hay más páginas de documentos")
                    import_progress["total"] = total_procesados
                    break
                pagina_num += 1

            await browser.close()

    except Exception as e:
        _log(f"❌ ERROR GENERAL: {e}")

    import_progress["running"] = False
    _log(f"Finalizado. Descargados: {import_progress['downloaded']}, Errores: {import_progress['errors']}")


def detener_importacion():
    import_progress["running"] = False


def obtener_estado():
    return dict(import_progress)
