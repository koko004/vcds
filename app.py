import os
import uuid
import asyncio
import json
import shutil
import secrets
import time
import hashlib
import hmac
import base64
import requests
import threading
from datetime import datetime, timedelta
from collections import defaultdict
from fastapi import FastAPI, UploadFile, File, HTTPException, Body, Request, Response
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import bcrypt

from extractor import extraer_datos, extraer_datos_ocr, csv_con_formato_valido
from verificador_web import VerificadorWeb, _asegurar_display
from comparador import comparar
from importador import importar_documentos, detener_importacion, obtener_estado, import_progress

VERSION = "1.1.0"

app = FastAPI(title="Verificador de Certificados")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DIR_UPLOADS = os.path.join(os.path.dirname(__file__), "uploads")
DIR_ORIGINALES = os.path.join(os.path.dirname(__file__), "originales")
os.makedirs(DIR_UPLOADS, exist_ok=True)
os.makedirs(DIR_ORIGINALES, exist_ok=True)

app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")

verificaciones: dict[str, dict] = {}

AUTH_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "auth.json")
SESSION_SECRET = os.environ.get("VCDS_SESSION_SECRET", secrets.token_hex(32))
SESSION_COOKIE = "vcds_session"
SESSION_TTL = 3600 * 24 * 7  # 7 days
CSRF_TOKEN_TTL = 3600

_sessions: dict[str, dict] = {}
_csrf_tokens: dict[str, float] = {}
_login_attempts: dict[str, list[float]] = defaultdict(list)
LOGIN_RATE_LIMIT = 5
LOGIN_RATE_WINDOW = 300  # 5 minutes


def _load_auth_config():
    if os.path.exists(AUTH_CONFIG_PATH):
        with open(AUTH_CONFIG_PATH) as f:
            return json.load(f)
    return {}


def _extraer_datos_rapido(pdf_path, config=None):
    from extractor import extraer_csv_de_pdf_directo, extraer_nombre, extraer_dni, extraer_csv, extraer_fecha, extraer_no_consta
    import fitz
    csv_val = extraer_csv_de_pdf_directo(pdf_path)
    texto = ""
    try:
        doc = fitz.open(pdf_path)
        for page in doc:
            texto += page.get_text()
        doc.close()
    except Exception:
        pass
    if not texto.strip():
        try:
            import pdfplumber
            with pdfplumber.open(pdf_path) as pdf:
                for page in pdf.pages:
                    t = page.extract_text()
                    if t:
                        texto += t
        except Exception:
            pass
    nombre = extraer_nombre(texto) if texto else None
    dni = extraer_dni(texto) if texto else None
    if not csv_val:
        csv_val = extraer_csv(texto) if texto else None
    fecha = extraer_fecha(texto) if texto else None
    no_consta = extraer_no_consta(texto) if texto else False

    campos_faltan = not nombre or not dni or not csv_val
    if config and config.get("ai_ocr_fallback_enabled") and campos_faltan:
        omniroute_url = config.get("omniroute_url", "").rstrip("/")
        omniroute_key = config.get("omniroute_key", "")
        if omniroute_url and omniroute_key:
            try:
                doc = fitz.open(pdf_path)
                paginas_b64 = []
                for i in range(min(len(doc), 10)):
                    page = doc[i]
                    pix = page.get_pixmap(dpi=150)
                    img_bytes = pix.tobytes("png")
                    paginas_b64.append(base64.b64encode(img_bytes).decode("utf-8"))
                doc.close()

                modelo_chat = config.get("omniroute_model", "OCR")
                if len(paginas_b64) == 1:
                    texto_completo, _, _ = _ocr_pagina_ia(
                        omniroute_url, omniroute_key, paginas_b64[0], modelo_chat=modelo_chat)
                else:
                    # Multipágina en paralelo: OCR Mistral por página, con fallback a chat+visión
                    from concurrent.futures import ThreadPoolExecutor
                    textos = [None] * len(paginas_b64)

                    def _ocr_una(args):
                        i, b64 = args
                        t, _, _ = _ocr_pagina_ia(omniroute_url, omniroute_key, b64, modelo_chat=modelo_chat)
                        return i, t

                    with ThreadPoolExecutor(max_workers=4) as _ex:
                        for i, t in _ex.map(_ocr_una, enumerate(paginas_b64)):
                            textos[i] = t
                    texto_completo = "\n".join(textos)

                if not nombre:
                    nombre = extraer_nombre(texto_completo)
                if not dni:
                    dni = extraer_dni(texto_completo)
                if not csv_val:
                    csv_val = extraer_csv(texto_completo)
                if not fecha:
                    fecha = extraer_fecha(texto_completo)
                no_consta_ia = extraer_no_consta(texto_completo)
                if no_consta_ia is not None:
                    no_consta = no_consta_ia
            except Exception:
                pass

    return {
        "nombre": nombre,
        "dni": dni,
        "csv": csv_val,
        "fecha_emision": fecha,
        "no_consta": no_consta,
        "texto_completo": texto,
    }


def _save_auth_config(config):
    with open(AUTH_CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


def _ensure_default_user():
    config = _load_auth_config()
    if "username" not in config or "password_hash" not in config:
        default_user = "admin"
        default_pass = "vcds2024"
        config["username"] = default_user
        config["password_hash"] = bcrypt.hashpw(default_pass.encode(), bcrypt.gensalt()).decode()
        _save_auth_config(config)
        print(f"[AUTH] Usuario por defecto: {default_user} / {default_pass}")
    return config


_ensure_default_user()


def _verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def _create_session(username: str) -> str:
    sid = secrets.token_hex(32)
    _sessions[sid] = {
        "username": username,
        "created": time.time(),
        "expires": time.time() + SESSION_TTL,
    }
    return sid


def _validate_session(sid: str) -> bool:
    if not sid or sid not in _sessions:
        return False
    sess = _sessions[sid]
    if time.time() > sess["expires"]:
        del _sessions[sid]
        return False
    return True


def _generate_csrf() -> str:
    token = secrets.token_hex(32)
    _csrf_tokens[token] = time.time()
    return token


def _validate_csrf(token: str) -> bool:
    if not token or token not in _csrf_tokens:
        return False
    if time.time() - _csrf_tokens[token] > CSRF_TOKEN_TTL:
        del _csrf_tokens[token]
        return False
    del _csrf_tokens[token]
    return True


def _check_rate_limit(ip: str) -> bool:
    now = time.time()
    _login_attempts[ip] = [t for t in _login_attempts[ip] if now - t < LOGIN_RATE_WINDOW]
    if len(_login_attempts[ip]) >= LOGIN_RATE_LIMIT:
        return False
    _login_attempts[ip].append(now)
    return True


def _get_client_ip(request: Request) -> str:
    cf_ip = request.headers.get("CF-Connecting-IP")
    if cf_ip:
        return cf_ip.strip()
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def _require_auth(request: Request):
    sid = request.cookies.get(SESSION_COOKIE)
    if not _validate_session(sid):
        return False
    return True


async def _check_auth_or_redirect(request: Request):
    if not await _require_auth(request):
        raise HTTPException(status_code=401, detail="No autenticado")


def _make_log_fn(vid: str):
    def _log(msg: str):
        verificaciones.setdefault(vid, {}).setdefault("logs", []).append(msg)
    return _log


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not await _require_auth(request):
        return RedirectResponse(url="/login", status_code=302)
    ruta = os.path.join(os.path.dirname(__file__), "templates", "index.html")
    with open(ruta, encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if await _require_auth(request):
        return RedirectResponse(url="/", status_code=302)
    ruta = os.path.join(os.path.dirname(__file__), "templates", "login.html")
    with open(ruta, encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/favicon.ico")
async def favicon():
    ruta = os.path.join(os.path.dirname(__file__), "static", "favicon.ico")
    if os.path.exists(ruta):
        return FileResponse(ruta, media_type="image/x-icon")
    raise HTTPException(404)


@app.get("/api/auth/csrf")
async def get_csrf():
    token = _generate_csrf()
    return {"token": token}


@app.post("/api/auth/login")
async def login(request: Request, data: dict = Body(...)):
    ip = _get_client_ip(request)
    if not _check_rate_limit(ip):
        raise HTTPException(429, "Demasiados intentos. Espera 5 minutos.")

    username = data.get("username", "").strip()
    password = data.get("password", "")
    csrf_token = data.get("csrf_token", "")

    if not _validate_csrf(csrf_token):
        raise HTTPException(403, "Token CSRF inválido")

    config = _load_auth_config()
    if username != config.get("username") or not _verify_password(password, config.get("password_hash", "")):
        raise HTTPException(401, "Credenciales incorrectas")

    sid = _create_session(username)
    response = Response(content=json.dumps({"ok": True, "username": username}))
    response.set_cookie(
        key=SESSION_COOKIE,
        value=sid,
        httponly=True,
        samesite="strict",
        max_age=SESSION_TTL,
        path="/",
    )
    return response


@app.post("/api/auth/logout")
async def logout(request: Request):
    sid = request.cookies.get(SESSION_COOKIE)
    if sid and sid in _sessions:
        del _sessions[sid]
    response = Response(content=json.dumps({"ok": True}))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/api/auth/check")
async def check_auth(request: Request):
    sid = request.cookies.get(SESSION_COOKIE)
    if _validate_session(sid):
        return {"authenticated": True, "username": _sessions[sid]["username"]}
    return {"authenticated": False}


@app.post("/api/auth/change-password")
async def change_password(request: Request, data: dict = Body(...)):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")

    current_password = data.get("current_password", "")
    new_password = data.get("new_password", "")
    csrf_token = data.get("csrf_token", "")

    if not _validate_csrf(csrf_token):
        raise HTTPException(403, "Token CSRF inválido")

    if len(new_password) < 6:
        raise HTTPException(400, "La nueva contraseña debe tener al menos 6 caracteres")

    config = _load_auth_config()
    if not _verify_password(current_password, config.get("password_hash", "")):
        raise HTTPException(401, "Contraseña actual incorrecta")

    config["password_hash"] = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    _save_auth_config(config)

    return {"ok": True, "message": "Contraseña actualizada"}


@app.post("/api/upload")
async def upload_pdf(request: Request, file: UploadFile = File(...)):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Solo se aceptan archivos PDF")
    vid = uuid.uuid4().hex[:12]
    ext = os.path.splitext(file.filename)[1] or ".pdf"
    ruta_usuario = os.path.join(DIR_UPLOADS, f"{vid}{ext}")
    with open(ruta_usuario, "wb") as f:
        shutil.copyfileobj(file.file, f)
    datos = extraer_datos(ruta_usuario)

    datos_extraidos = {
        "nombre": datos.get("nombre"),
        "dni": datos.get("dni"),
        "csv": datos.get("csv"),
        "fecha_emision": datos.get("fecha_emision"),
        "no_consta": datos.get("no_consta", False),
    }

    config = _leer_config()
    ai_ocr_fallback = config.get("ai_ocr_fallback_enabled", False)
    campos_faltan = not datos_extraidos.get("nombre") or not datos_extraidos.get("dni") or not datos_extraidos.get("csv")

    if ai_ocr_fallback and campos_faltan:
        try:
            import fitz as _fitz
            omniroute_url = config.get("omniroute_url", "").rstrip("/")
            omniroute_key = config.get("omniroute_key", "")
            if omniroute_url and omniroute_key:
                doc = _fitz.open(ruta_usuario)
                pix = doc[0].get_pixmap(dpi=150)
                img_bytes = pix.tobytes("png")
                b64 = base64.b64encode(img_bytes).decode("utf-8")
                doc.close()
                modelo_chat = config.get("omniroute_model", "OCR")
                texto_completo, _, _ = _ocr_pagina_ia(omniroute_url, omniroute_key, b64, modelo_chat=modelo_chat)
                from extractor import extraer_nombre, extraer_dni, extraer_csv, extraer_fecha, extraer_no_consta
                if not datos_extraidos.get("nombre"): datos_extraidos["nombre"] = extraer_nombre(texto_completo)
                if not datos_extraidos.get("dni"): datos_extraidos["dni"] = extraer_dni(texto_completo)
                if not datos_extraidos.get("csv"): datos_extraidos["csv"] = extraer_csv(texto_completo)
                if not datos_extraidos.get("fecha_emision"): datos_extraidos["fecha_emision"] = extraer_fecha(texto_completo)
                no_consta_ia = extraer_no_consta(texto_completo)
                if no_consta_ia is not None: datos_extraidos["no_consta"] = no_consta_ia
        except Exception:
            pass

    verificaciones[vid] = {
        "id": vid,
        "nombre_archivo": file.filename,
        "ruta_usuario": ruta_usuario,
        "datos_extraidos": datos_extraidos,
        "estado": "extraido",
        "verificador": None,
        "ruta_original": None,
        "resultado": None,
    }
    return {"id": vid, "datos": datos_extraidos}


@app.post("/api/upload-sse")
async def upload_pdf_sse(request: Request, file: UploadFile = File(...)):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Solo se aceptan archivos PDF")
    vid = uuid.uuid4().hex[:12]
    ext = os.path.splitext(file.filename)[1] or ".pdf"
    ruta_usuario = os.path.join(DIR_UPLOADS, f"{vid}{ext}")
    with open(ruta_usuario, "wb") as f:
        shutil.copyfileobj(file.file, f)

    logs = []
    resultado = {}

    def on_log(msg):
        logs.append(msg)

    def _run_extraccion():
        try:
            resultado["datos"] = extraer_datos(ruta_usuario, log_fn=on_log)
        except Exception as e:
            resultado["error"] = str(e)
            on_log(f"Error en extracción: {e}")

    import threading
    hilo_extraccion = threading.Thread(target=_run_extraccion, daemon=True)
    hilo_extraccion.start()

    async def event_stream():
        idx = 0
        beat = 0
        while hilo_extraccion.is_alive():
            while idx < len(logs):
                yield f"data: {json.dumps({'type': 'log', 'message': logs[idx]})}\n\n"
                idx += 1
            beat += 1
            if beat % 30 == 0:
                yield ": ping\n\n"
            await asyncio.sleep(0.4)
        hilo_extraccion.join()
        while idx < len(logs):
            yield f"data: {json.dumps({'type': 'log', 'message': logs[idx]})}\n\n"
            idx += 1
        if "datos" not in resultado:
            yield f"data: {json.dumps({'type': 'error', 'message': resultado.get('error', 'Error desconocido en extracción')})}\n\n"
            return
        datos = resultado["datos"]

        datos_extraidos = {
            "nombre": datos.get("nombre"),
            "dni": datos.get("dni"),
            "csv": datos.get("csv"),
            "fecha_emision": datos.get("fecha_emision"),
            "no_consta": datos.get("no_consta", False),
        }

        config = _leer_config()
        ai_ocr_fallback = config.get("ai_ocr_fallback_enabled", False)
        campos_faltan = not datos_extraidos.get("nombre") or not datos_extraidos.get("dni") or not datos_extraidos.get("csv")

        verificaciones[vid] = {
            "id": vid,
            "nombre_archivo": file.filename,
            "ruta_usuario": ruta_usuario,
            "datos_extraidos": datos_extraidos,
            "estado": "extraido",
            "verificador": None,
            "ruta_original": None,
            "resultado": None,
        }

        ai_ocr_usado = False
        ai_queue = None

        if ai_ocr_fallback and campos_faltan:
            ai_queue = asyncio.Queue()

            def _run_ai_fallback():
                nonlocal ai_ocr_usado
                try:
                    import fitz as _fitz
                    omniroute_url = config.get("omniroute_url", "").rstrip("/")
                    omniroute_key = config.get("omniroute_key", "")
                    if omniroute_url and omniroute_key:
                        on_log("=== Fallback IA: Extracción primaria incompleta, intentando OCR con IA ===")
                        doc = _fitz.open(ruta_usuario)
                        paginas_b64 = []
                        for i in range(min(len(doc), 10)):
                            page = doc[i]
                            pix = page.get_pixmap(dpi=150)
                            img_bytes = pix.tobytes("png")
                            b64 = base64.b64encode(img_bytes).decode("utf-8")
                            paginas_b64.append(b64)
                        doc.close()

                    modelo_chat = config.get("omniroute_model", "OCR")
                    if len(paginas_b64) == 1:
                        texto_completo, _, _ = _ocr_pagina_ia(
                            omniroute_url, omniroute_key, paginas_b64[0], modelo_chat=modelo_chat)
                    else:
                        # Multipágina en paralelo con fallback a chat+visión por página
                        from concurrent.futures import ThreadPoolExecutor
                        on_log(f"OCR IA en paralelo: {len(paginas_b64)} páginas...")
                        textos = [None] * len(paginas_b64)

                        def _ocr_una(args):
                            i, b64 = args
                            t, _, _ = _ocr_pagina_ia(omniroute_url, omniroute_key, b64, modelo_chat=modelo_chat)
                            return i, t

                        with ThreadPoolExecutor(max_workers=4) as _ex:
                            for i, t in _ex.map(_ocr_una, enumerate(paginas_b64)):
                                textos[i] = t
                        texto_completo = "\n".join(textos)

                    from extractor import extraer_nombre, extraer_dni, extraer_csv, extraer_fecha, extraer_no_consta
                    nombre_ia = extraer_nombre(texto_completo)
                    dni_ia = extraer_dni(texto_completo)
                    csv_ia = extraer_csv(texto_completo)
                    fecha_ia = extraer_fecha(texto_completo)
                    no_consta_ia = extraer_no_consta(texto_completo)

                    on_log(f"IA resultado — Nombre: {nombre_ia or '(no encontrado)'}")
                    on_log(f"IA resultado — DNI: {dni_ia or '(no encontrado)'}")
                    on_log(f"IA resultado — CSV: {csv_ia or '(no encontrado)'}")
                    on_log(f"IA resultado — Fecha: {fecha_ia or '(no encontrado)'}")

                    if nombre_ia and not datos_extraidos.get("nombre"):
                        datos_extraidos["nombre"] = nombre_ia
                    if dni_ia and not datos_extraidos.get("dni"):
                        datos_extraidos["dni"] = dni_ia
                    if csv_ia and not datos_extraidos.get("csv"):
                        datos_extraidos["csv"] = csv_ia
                    if fecha_ia and not datos_extraidos.get("fecha_emision"):
                        datos_extraidos["fecha_emision"] = fecha_ia
                    if no_consta_ia is not None:
                        datos_extraidos["no_consta"] = no_consta_ia

                    ai_ocr_usado = True
                    on_log("=== Fallback IA completado ===")
                except Exception as e:
                    on_log(f"Error en fallback IA: {str(e)}")
                finally:
                    try:
                        ai_queue.put_nowait(True)
                    except Exception:
                        pass

            threading.Thread(target=_run_ai_fallback, daemon=True).start()

        yield f"data: {json.dumps({'type': 'done', 'id': vid, 'datos': datos_extraidos, 'ai_fallback': ai_ocr_usado})}\n\n"

        if ai_queue is not None:
            import time as _t
            _t0 = _t.time()
            while True:
                try:
                    await asyncio.wait_for(ai_queue.get(), timeout=10)
                    break
                except asyncio.TimeoutError:
                    if _t.time() - _t0 > 180:
                        break
                    yield ": ping\n\n"
            extra_logs = [l for l in logs if l not in [m for m in logs]]
            yield f"data: {json.dumps({'type': 'ai_updated', 'id': vid, 'datos': datos_extraidos, 'ai_fallback': ai_ocr_usado})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.put("/api/verify/{vid}/datos")
async def actualizar_datos(request: Request, vid: str, datos: dict = Body(...)):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    v = verificaciones.get(vid)
    if not v:
        raise HTTPException(404, "Verificación no encontrada")
    if v["estado"] != "extraido":
        raise HTTPException(400, f"Estado inválido: {v['estado']}")
    for key in ("nombre", "dni", "csv", "fecha_emision", "no_consta"):
        if key in datos:
            v["datos_extraidos"][key] = datos[key]
    return {"id": vid, "datos": v["datos_extraidos"]}


@app.post("/api/reextract-ocr/{vid}")
async def reextract_ocr(request: Request, vid: str):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    v = verificaciones.get(vid)
    if not v:
        raise HTTPException(404, "Verificación no encontrada")
    if v["estado"] != "extraido":
        raise HTTPException(400, f"Estado inválido: {v['estado']}")

    ruta = v["ruta_usuario"]
    if not os.path.exists(ruta):
        raise HTTPException(404, "Archivo PDF no encontrado")

    logs = []

    def on_log(msg):
        logs.append(msg)

    datos = extraer_datos_ocr(ruta, log_fn=on_log)

    for key in ("nombre", "dni", "csv", "fecha_emision", "no_consta"):
        v["datos_extraidos"][key] = datos.get(key)

    async def event_stream():
        for msg in logs:
            yield f"data: {json.dumps({'type': 'log', 'message': msg})}\n\n"
        yield f"data: {json.dumps({'type': 'done', 'id': vid, 'datos': v['datos_extraidos']})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/static-pdf/{filename}")
async def serve_pdf(filename: str):
    import re
    if not re.match(r'^[a-f0-9]+\.pdf$', filename):
        raise HTTPException(400, "Invalid filename")
    path = os.path.join(DIR_UPLOADS, filename)
    if not os.path.exists(path):
        raise HTTPException(404, "PDF not found")
    from fastapi.responses import FileResponse
    return FileResponse(path, media_type="application/pdf")


@app.get("/pdf-viewer/{vid}")
async def pdf_viewer(vid: str):
    from fastapi.responses import HTMLResponse
    pdf_url = f"/static-pdf/{vid}.pdf"
    html = f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8"><title>PDF Viewer</title>
<style>body{{margin:0;background:#525659;}}#viewer{{width:100vw;height:100vh;}}</style>
</head><body>
<canvas id="viewer"></canvas>
<script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js"></script>
<script>
pdfjsLib.GlobalWorkerOptions.workerSrc='https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';
async function render(){{
  const pdf=await pdfjsLib.getDocument('{pdf_url}').promise;
  const canvas=document.getElementById('viewer');
  const ctx=canvas.getContext('2d');
  let y=0;
  for(let i=1;i<=pdf.numPages;i++){{
    const pg=await pdf.getPage(i);
    const scale=2;
    const vp=pg.getViewport({{scale}});
    canvas.width=vp.width;
    canvas.height=vp.height;
    await pg.render({{canvasContext:ctx,viewport:vp}}).promise;
    y+=vp.height;
  }}
}}
render();
</script></body></html>"""
    return HTMLResponse(html)


@app.post("/api/chrome-ocr/{vid}")
async def chrome_ocr(request: Request, vid: str):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    v = verificaciones.get(vid)
    if not v:
        raise HTTPException(404, "Verificación no encontrada")
    if v["estado"] != "extraido":
        raise HTTPException(400, f"Estado inválido: {v['estado']}")

    ruta = v["ruta_usuario"]
    if not os.path.exists(ruta):
        raise HTTPException(404, "Archivo PDF no encontrado")
    if not _asegurar_display():
        v["estado"] = "error"
        v["mensaje"] = "No hay servidor X disponible"
        return {"ok": False, "mensaje": v["mensaje"]}

    abs_path = os.path.abspath(ruta)
    _log = _make_log_fn(vid)
    v["estado"] = "navegando"

    async def tarea_chrome_ocr():
        from playwright.async_api import async_playwright
        pw = None
        try:
            _asegurar_display()

            _log("Lanzando Google Chrome (headed + Xvfb)...")
            pw = await async_playwright().start()
            browser = await pw.chromium.launch(
                headless=False,
                channel="chrome",
                args=["--no-sandbox", "--disable-web-security", "--window-size=1280,1024"]
            )
            context = await browser.new_context(
                permissions=["clipboard-read", "clipboard-write"],
                viewport={"width": 1280, "height": 1024}
            )
            page = await context.new_page()

            file_url = f"file://{abs_path}"
            _log(f"Abriendo PDF en Google Chrome: {file_url}")
            await page.goto(file_url, timeout=30000)
            await page.wait_for_timeout(5000)

            _log("PDF abierto en Chrome")
            v["chrome_browser"] = browser
            v["chrome_context"] = context
            v["chrome_page"] = page

            _log("Clic en el visor para foco...")
            await page.mouse.click(700, 500)
            await page.wait_for_timeout(2000)

            _log("Ctrl+A - seleccionando texto...")
            await page.keyboard.press("Control+a")
            _log("Esperando 5s para ver selección...")
            await page.wait_for_timeout(5000)

            _log("Ctrl+C - copiando texto...")
            await page.keyboard.press("Control+c")
            _log("Esperando 5s para ver copia...")
            await page.wait_for_timeout(5000)

            _log("Leyendo texto seleccionado del DOM...")
            texto = await page.evaluate("""() => {
                const sel = window.getSelection();
                if (sel && sel.toString().trim()) return sel.toString();
                const spans = document.querySelectorAll('#viewer .page .textLayer span, #viewer span, .page span');
                let all = '';
                for (const s of spans) all += s.textContent + ' ';
                return all.trim();
            }""")

            if not texto or not texto.strip():
                _log("DOM selection vacía, intentando leer del portapapeles del sistema...")
                try:
                    import subprocess as _sp
                    env_clip = {**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":99")}
                    result_clip = _sp.run(
                        ["xsel", "--clipboard", "--output"],
                        capture_output=True, text=True, timeout=5, env=env_clip
                    )
                    if result_clip.stdout.strip():
                        texto = result_clip.stdout.strip()
                        _log(f"Portapapeles leído con xsel: {len(texto)} caracteres")
                except Exception:
                    pass

            if not texto or not texto.strip():
                _log("xsel falló, intentando leer vía CDP...")
                try:
                    cdp = await context.new_cdp_session(page)
                    await cdp.send("Browser.grantPermissions", {
                        "permissions": ["clipboardReadWrite", "clipboardSanitizedWrite"],
                    })
                    cdp_result = await cdp.send("Runtime.evaluate", {
                        "expression": "navigator.clipboard.readText()",
                        "awaitPromise": True,
                    })
                    cdp_texto = cdp_result.get("result", {}).get("value", "")
                    if cdp_texto and cdp_texto.strip():
                        texto = cdp_texto.strip()
                        _log(f"Portapapeles leído vía CDP: {len(texto)} caracteres")
                except Exception:
                    pass

            if not texto or not texto.strip():
                _log("Chrome OCR: no se pudo extraer texto (DOM, portapapeles y CDP fallaron)")
                v["estado"] = "error"
                v["mensaje"] = "No se pudo extraer texto. Intenta seleccionar manualmente con Ctrl+A y Ctrl+C."
                return

            _log(f"Texto extraído: {len(texto)} caracteres")
            _log(f"Vista previa: {texto[:300]}...")

            from extractor import extraer_nombre, extraer_dni, extraer_csv, extraer_fecha, extraer_no_consta
            nombre = extraer_nombre(texto)
            dni = extraer_dni(texto)

            pie = ""
            try:
                import fitz
                doc = fitz.open(ruta)
                for pagina in doc:
                    alto = pagina.height
                    umbral_y = alto * 0.80
                    bloques = pagina.get_text("dict", clip=fitz.Rect(0, umbral_y, pagina.rect.width, pagina.rect.height))
                    for bloque in bloques.get("blocks", []):
                        for linea in bloque.get("lines", []):
                            for span in linea.get("spans", []):
                                pie += span.get("text", "") + " "
                doc.close()
            except Exception:
                pass

            csv_val = extraer_csv(texto, pie)
            fecha = extraer_fecha(texto, pie)
            no_consta = extraer_no_consta(texto)

            _log(f"Nombre: {nombre or '(no encontrado)'}")
            _log(f"DNI: {dni or '(no encontrado)'}")
            _log(f"CSV: {csv_val or '(no encontrado)'}")
            _log(f"Fecha: {fecha or '(no encontrado)'}")
            _log(f"NO CONSTA: {no_consta}")

            v["datos_extraidos"] = {
                "nombre": nombre, "dni": dni, "csv": csv_val,
                "fecha_emision": fecha, "no_consta": no_consta,
            }
            v["estado"] = "completo"
            _log("Extracción Chrome OCR completada")

        except Exception as e:
            _log(f"Error Chrome OCR: {e}")
            v["estado"] = "error"
            v["mensaje"] = str(e)

    asyncio.create_task(tarea_chrome_ocr())
    return {"ok": True, "mensaje": "Google Chrome abierto con el PDF"}


@app.post("/api/chrome-ocr-compare/{vid}")
async def chrome_ocr_compare(request: Request, vid: str):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    v = verificaciones.get(vid)
    if not v:
        raise HTTPException(404, "Verificación no encontrada")
    if v["estado"] != "extraido":
        raise HTTPException(400, f"Estado inválido: {v['estado']}")

    ruta = v["ruta_usuario"]
    if not os.path.exists(ruta):
        raise HTTPException(404, "Archivo PDF no encontrado")
    if not _asegurar_display():
        v["estado"] = "error"
        v["mensaje"] = "No hay servidor X disponible"
        return {"ok": False, "mensaje": v["mensaje"]}

    abs_path = os.path.abspath(ruta)
    logs = []
    _log = _make_log_fn(vid)

    # Guardar datos primarios ANTES de OCR
    datos_primarios = dict(v["datos_extraidos"])

    async def tarea_compare():
        from playwright.async_api import async_playwright
        pw = None
        browser = None
        try:
            _asegurar_display()

            _log("=== COMPARACIÓN: Extracción Primaria vs Chrome OCR ===")
            _log(f"Datos primarios: CSV={datos_primarios.get('csv')}, DNI={datos_primarios.get('dni')}, Nombre={datos_primarios.get('nombre')}")

            _log("Lanzando Google Chrome para OCR...")
            pw = await async_playwright().start()
            browser = await pw.chromium.launch(
                headless=False,
                channel="chrome",
                args=["--no-sandbox", "--disable-web-security", "--window-size=1280,1024"]
            )
            context = await browser.new_context(
                permissions=["clipboard-read", "clipboard-write"],
                viewport={"width": 1280, "height": 1024}
            )
            page = await context.new_page()

            file_url = f"file://{abs_path}"
            _log(f"Abriendo PDF en Chrome: {file_url}")
            await page.goto(file_url, timeout=30000)
            await page.wait_for_timeout(5000)
            _log("PDF abierto en Chrome")

            _log("Ctrl+A - seleccionando texto...")
            await page.mouse.click(700, 500)
            await page.wait_for_timeout(2000)
            await page.keyboard.press("Control+a")
            await page.wait_for_timeout(3000)

            _log("Ctrl+C - copiando texto...")
            await page.keyboard.press("Control+c")
            await page.wait_for_timeout(3000)

            # --- Lectura del texto con fallbacks ---
            texto = await page.evaluate("""() => {
                const sel = window.getSelection();
                if (sel && sel.toString().trim()) return sel.toString();
                const spans = document.querySelectorAll('#viewer .page .textLayer span, #viewer span, .page span');
                let all = '';
                for (const s of spans) all += s.textContent + ' ';
                return all.trim();
            }""")

            if not texto or not texto.strip():
                _log("DOM vacío, intentando xsel...")
                try:
                    import subprocess as _sp
                    env_clip = {**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":99")}
                    r_clip = _sp.run(["xsel", "--clipboard", "--output"], capture_output=True, text=True, timeout=5, env=env_clip)
                    if r_clip.stdout.strip():
                        texto = r_clip.stdout.strip()
                        _log(f"Leído vía xsel: {len(texto)} caracteres")
                except Exception:
                    pass

            if not texto or not texto.strip():
                _log("xsel falló, intentando CDP...")
                try:
                    cdp = await context.new_cdp_session(page)
                    await cdp.send("Browser.grantPermissions", {"permissions": ["clipboardReadWrite", "clipboardSanitizedWrite"]})
                    cdp_r = await cdp.send("Runtime.evaluate", {"expression": "navigator.clipboard.readText()", "awaitPromise": True})
                    cdp_t = cdp_r.get("result", {}).get("value", "")
                    if cdp_t and cdp_t.strip():
                        texto = cdp_t.strip()
                        _log(f"Leído vía CDP: {len(texto)} caracteres")
                except Exception:
                    pass

            if not texto or not texto.strip():
                _log("Chrome OCR: no se pudo extraer texto")
                v["estado"] = "error"
                v["mensaje"] = "No se pudo extraer texto con Chrome OCR"
                return

            _log(f"Texto Chrome OCR: {len(texto)} caracteres")

            # Extraer campos del OCR
            from extractor import extraer_nombre, extraer_dni, extraer_csv, extraer_fecha, extraer_no_consta
            nombre_ocr = extraer_nombre(texto)
            dni_ocr = extraer_dni(texto)
            csv_ocr = extraer_csv(texto)
            fecha_ocr = extraer_fecha(texto)
            no_consta_ocr = extraer_no_consta(texto)

            _log(f"Chrome OCR - CSV: {csv_ocr or '(no encontrado)'}")
            _log(f"Chrome OCR - DNI: {dni_ocr or '(no encontrado)'}")
            _log(f"Chrome OCR - Nombre: {nombre_ocr or '(no encontrado)'}")
            _log(f"Chrome OCR - Fecha: {fecha_ocr or '(no encontrado)'}")

            # Comparación
            def _cmp(v1, v2):
                if v1 is None and v2 is None:
                    return True, "—", []
                if v1 is None or v2 is None:
                    return False, f"Primario: {v1 or 'N/A'} | OCR: {v2 or 'N/A'}", []
                if isinstance(v1, bool):
                    return v1 == v2, f"{'Sí' if v1 else 'No'} → {'Sí' if v2 else 'No'}", []
                s1 = v1.strip().upper()
                s2 = v2.strip().upper()
                coincide = s1 == s2
                diff_chars = []
                if not coincide:
                    max_len = max(len(s1), len(s2))
                    for i in range(max_len):
                        c1 = s1[i] if i < len(s1) else ''
                        c2 = s2[i] if i < len(s2) else ''
                        if c1 != c2:
                            diff_chars.append({"pos": i, "primario": c1, "ia": c2})
                return coincide, f"{v1} → {v2}", diff_chars

            comparaciones = {}
            for campo, val_prim, val_ocr in [
                ("csv", datos_primarios.get("csv"), csv_ocr),
                ("dni", datos_primarios.get("dni"), dni_ocr),
                ("nombre", datos_primarios.get("nombre"), nombre_ocr),
                ("fecha_emision", datos_primarios.get("fecha_emision"), fecha_ocr),
            ]:
                coincide, detalle, diff_chars = _cmp(val_prim, val_ocr)
                comparaciones[campo] = {"coincide": coincide, "detalle": detalle, "primario": val_prim, "ocr": val_ocr, "diff_chars": diff_chars}
                estado = "OK" if coincide else "DIFIERE"
                _log(f"Comparación {campo}: {estado} — {detalle}")

            csv_coincide = comparaciones.get("csv", {}).get("coincide", True)
            todos_ok = all(c["coincide"] for c in comparaciones.values())

            if todos_ok:
                _log("Todos los campos coinciden. Documento consistente.")
                veredicto_pre = "consistente"
            elif not csv_coincide:
                _log("ATENCIÓN: El CSV difiere entre extracción primaria y Chrome OCR.")
                veredicto_pre = "csv_difierente"
            else:
                _log("Algunos campos difieren (excepto CSV).")
                veredicto_pre = "diferencias_parciales"

            # Guardar resultados del OCR para uso posterior
            v["ocr_comparison"] = {
                "texto_ocr": texto,
                "datos_ocr": {
                    "nombre": nombre_ocr,
                    "dni": dni_ocr,
                    "csv": csv_ocr,
                    "fecha_emision": fecha_ocr,
                    "no_consta": no_consta_ocr,
                },
                "comparaciones": comparaciones,
                "veredicto_pre": veredicto_pre,
            }
            v["estado"] = "ocr_comparado"

        except Exception as e:
            _log(f"Error en comparación OCR: {e}")
            v["estado"] = "error"
            v["mensaje"] = str(e)
        finally:
            try:
                if browser:
                    await browser.close()
                if pw:
                    await pw.stop()
            except Exception:
                pass

    async def event_stream():
        logs_len = 0
        import asyncio as _aio
        task = _aio.create_task(tarea_compare())
        while not task.done():
            while logs_len < len(logs):
                yield f"data: {json.dumps({'type': 'log', 'message': logs[logs_len]})}\n\n"
                logs_len += 1
            await _aio.sleep(0.3)
        while logs_len < len(logs):
            yield f"data: {json.dumps({'type': 'log', 'message': logs[logs_len]})}\n\n"
            logs_len += 1

        ocr_data = v.get("ocr_comparison")
        if ocr_data:
            yield f"data: {json.dumps({'type': 'done', 'comparaciones': ocr_data['comparaciones'], 'veredicto_pre': ocr_data['veredicto_pre'], 'datos_ocr': ocr_data['datos_ocr']})}\n\n"
        else:
            yield f"data: {json.dumps({'type': 'error', 'mensaje': v.get('mensaje', 'Error desconocido')})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _ocr_omniroute(omniroute_url: str, omniroute_key: str, b64: str, timeout: int = 120, intentos: int = 3):
    """Llama a /ocr reintentando con backoff ante 429 (rate limit de OmniRoute)."""
    import time as _time
    last_resp = None
    for i in range(intentos):
        last_resp = requests.post(
            f"{omniroute_url}/ocr",
            headers={"Authorization": f"Bearer {omniroute_key}", "Content-Type": "application/json"},
            json={"model": "mistral/mistral-ocr-latest", "document": {"type": "document_url", "document_url": f"data:image/png;base64,{b64}"}},
            timeout=timeout,
        )
        if last_resp.status_code != 429:
            last_resp.raise_for_status()
            return last_resp.json()
        _time.sleep(5 * (i + 1))
    last_resp.raise_for_status()
    return last_resp.json()


def _transcribir_chat(omniroute_url: str, omniroute_key: str, modelo_chat: str, b64: str, timeout: int = 120) -> str:
    """Transcribe una página vía chat+visión (combo OCR) cuando Mistral /ocr no responde."""
    resp = requests.post(
        f"{omniroute_url}/chat/completions",
        headers={"Authorization": f"Bearer {omniroute_key}", "Content-Type": "application/json"},
        json={
            "model": modelo_chat,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "Transcribe fielmente todo el texto visible de esta imagen de un certificado oficial español, manteniendo el orden. El código CSV tiene formato estricto SD:XXXX-XXXX-XXXX-XXXX (letra S, letra D, dos puntos y 4 bloques de exactamente 4 caracteres): no omitas ni añadas caracteres en él. Responde SOLO con el texto transcrito, sin explicaciones."},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ]}],
            "max_tokens": 4000,
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip()


# Circuit breaker para Mistral /ocr: tras 3 fallos 429 seguidos se salta
# Mistral 10 min y va directo al fallback, evitando esperas inútiles.
_MISTRAL_CB = {"fallos": 0, "abierto_hasta": 0.0}
_MISTRAL_CB_LOCK = threading.Lock()


def _mistral_bloqueado() -> bool:
    with _MISTRAL_CB_LOCK:
        return time.time() < _MISTRAL_CB["abierto_hasta"]


def _mistral_ok() -> None:
    with _MISTRAL_CB_LOCK:
        _MISTRAL_CB["fallos"] = 0
        _MISTRAL_CB["abierto_hasta"] = 0.0


def _mistral_fallo(es_429: bool) -> None:
    with _MISTRAL_CB_LOCK:
        _MISTRAL_CB["fallos"] += 1
        if es_429 and _MISTRAL_CB["fallos"] >= 3:
            _MISTRAL_CB["abierto_hasta"] = time.time() + 600


def _ocr_pagina_ia(omniroute_url: str, omniroute_key: str, b64: str, modelo_chat: str = "OCR", timeout: int = 120):
    """OCR de una página: Mistral directo primero, fallback a transcripción chat+visión.
    Devuelve (texto, modelo_usado, proveedor)."""
    if not _mistral_bloqueado():
        try:
            data = _ocr_omniroute(omniroute_url, omniroute_key, b64, timeout=timeout, intentos=2)
            _mistral_ok()
            texto = "\n".join(p.get("markdown", "") for p in data.get("pages", []))
            return texto, data.get("model", "mistral-ocr-latest"), "mistral"
        except Exception as e:
            resp = getattr(e, "response", None)
            es429 = getattr(resp, "status_code", None) == 429
            _mistral_fallo(es429)
    texto = _transcribir_chat(omniroute_url, omniroute_key, modelo_chat, b64, timeout=timeout)
    return texto, modelo_chat, "chat-fallback"


@app.post("/api/ai-ocr/{vid}")
async def ai_ocr(request: Request, vid: str):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    v = verificaciones.get(vid)
    if not v:
        raise HTTPException(404, "Verificación no encontrada")

    ruta = v["ruta_usuario"]
    if not os.path.exists(ruta):
        raise HTTPException(404, "Archivo PDF no encontrado")

    config = _leer_config()
    omniroute_url = config.get("omniroute_url", "").rstrip("/")
    omniroute_key = config.get("omniroute_key", "")

    if not omniroute_url or not omniroute_key:
        raise HTTPException(400, "OmniRoute no configurado. Agrega omniroute_url y omniroute_key en config.")

    import fitz

    try:
        doc = fitz.open(ruta)
        paginas_b64 = []
        max_paginas = min(len(doc), 10)
        for i in range(max_paginas):
            page = doc[i]
            pix = page.get_pixmap(dpi=150)
            img_bytes = pix.tobytes("png")
            b64 = base64.b64encode(img_bytes).decode("utf-8")
            paginas_b64.append(b64)
        doc.close()

        modelo_chat = config.get("omniroute_model", "OCR")
        if len(paginas_b64) == 1:
            texto_completo, model_used, provider = _ocr_pagina_ia(
                omniroute_url, omniroute_key, paginas_b64[0], modelo_chat=modelo_chat)
        else:
            # Multipágina en paralelo con fallback a chat+visión por página
            from concurrent.futures import ThreadPoolExecutor
            textos = [None] * len(paginas_b64)
            modelos = [modelo_chat] * len(paginas_b64)

            def _ocr_una(args):
                i, b64 = args
                t, m, _ = _ocr_pagina_ia(omniroute_url, omniroute_key, b64, modelo_chat=modelo_chat)
                return i, t, m

            with ThreadPoolExecutor(max_workers=4) as _ex:
                for i, t, m in _ex.map(_ocr_una, enumerate(paginas_b64)):
                    textos[i] = t
                    modelos[i] = m
            texto_completo = "\n".join(textos)
            model_used = modelos[0] if modelos else modelo_chat
            provider = "mistral" if model_used.startswith("mistral") else "chat-fallback"

        from extractor import extraer_nombre, extraer_dni, extraer_csv, extraer_fecha, extraer_no_consta
        nombre_ia = extraer_nombre(texto_completo)
        dni_ia = extraer_dni(texto_completo)
        csv_ia = extraer_csv(texto_completo)
        fecha_ia = extraer_fecha(texto_completo)
        no_consta_ia = extraer_no_consta(texto_completo)

        datos_ia = {
            "nombre": nombre_ia,
            "dni": dni_ia,
            "csv": csv_ia,
            "fecha_emision": fecha_ia,
            "no_consta": no_consta_ia,
        }

        v["ai_ocr_data"] = datos_ia
        v["ai_ocr_model"] = model_used
        v["ai_ocr_texto"] = texto_completo

        return {
            "ok": True,
            "datos": datos_ia,
            "model": model_used,
            "provider": provider,
            "texto_extraido": texto_completo[:2000],
        }

    except requests.exceptions.RequestException as e:
        raise HTTPException(502, f"Error conectando con OmniRoute: {str(e)}")
    except Exception as e:
        raise HTTPException(500, f"Error en AI OCR: {str(e)}")


@app.post("/api/ai-ocr-compare/{vid}")
async def ai_ocr_compare(request: Request, vid: str):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    v = verificaciones.get(vid)
    if not v:
        raise HTTPException(404, "Verificación no encontrada")

    ai_data = v.get("ai_ocr_data")
    if not ai_data:
        raise HTTPException(400, "Primero ejecuta OCR con IA")

    datos_primarios = v.get("datos_extraidos", {})

    def _cmp(v1, v2):
        if v1 is None and v2 is None:
            return True, "—", []
        if v1 is None or v2 is None:
            return False, f"Primario: {v1 or 'N/A'} | IA: {v2 or 'N/A'}", []
        if isinstance(v1, bool):
            return v1 == v2, f"{'Sí' if v1 else 'No'} → {'Sí' if v2 else 'No'}", []
        s1 = v1.strip().upper()
        s2 = v2.strip().upper()
        coincide = s1 == s2
        diff_chars = []
        if not coincide:
            max_len = max(len(s1), len(s2))
            for i in range(max_len):
                c1 = s1[i] if i < len(s1) else ''
                c2 = s2[i] if i < len(s2) else ''
                if c1 != c2:
                    diff_chars.append({"pos": i, "primario": c1, "ia": c2})
        return coincide, f"{v1} → {v2}", diff_chars

    comparaciones = {}
    for campo in ["csv", "dni", "nombre", "fecha_emision", "no_consta"]:
        val_prim = datos_primarios.get(campo)
        val_ia = ai_data.get(campo)
        coincide, detalle, diff_chars = _cmp(val_prim, val_ia)
        comparaciones[campo] = {"coincide": coincide, "detalle": detalle, "primario": val_prim, "ia": val_ia, "diff_chars": diff_chars}

    todos_ok = all(c["coincide"] for c in comparaciones.values())
    csv_coincide = comparaciones.get("csv", {}).get("coincide", True)

    if todos_ok:
        veredicto = "consistente"
    elif not csv_coincide:
        veredicto = "csv_difierente"
    else:
        veredicto = "diferencias_parciales"

    return {
        "ok": True,
        "datos_primarios": datos_primarios,
        "datos_ia": ai_data,
        "comparaciones": comparaciones,
        "veredicto": veredicto,
        "model": v.get("ai_ocr_model", "unknown"),
    }


@app.post("/api/ai-ocr-apply/{vid}")
async def ai_ocr_apply(request: Request, vid: str):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    v = verificaciones.get(vid)
    if not v:
        raise HTTPException(404, "Verificación no encontrada")

    ai_data = v.get("ai_ocr_data")
    if not ai_data:
        raise HTTPException(400, "Primero ejecuta OCR con IA")

    v["datos_extraidos"].update({
        "nombre": ai_data.get("nombre") or v["datos_extraidos"].get("nombre"),
        "dni": ai_data.get("dni") or v["datos_extraidos"].get("dni"),
        "csv": ai_data.get("csv") or v["datos_extraidos"].get("csv"),
        "fecha_emision": ai_data.get("fecha_emision") or v["datos_extraidos"].get("fecha_emision"),
        "no_consta": ai_data.get("no_consta") if ai_data.get("no_consta") is not None else v["datos_extraidos"].get("no_consta"),
    })

    return {"ok": True, "datos": v["datos_extraidos"]}


@app.post("/api/abrir-ministerio/{vid}")
async def abrir_ministerio(request: Request, vid: str):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    v = verificaciones.get(vid)
    if not v:
        raise HTTPException(404, "Verificación no encontrada")
    datos = v["datos_extraidos"]
    csv = datos.get("csv")
    dni = datos.get("dni")
    if not csv:
        raise HTTPException(400, "No hay CSV disponible para abrir el Ministerio")
    if not dni:
        raise HTTPException(400, "No hay DNI disponible")
    if not _asegurar_display():
        v["estado"] = "error"
        v["mensaje"] = "No hay servidor X disponible"
        return {"ok": False, "mensaje": v["mensaje"]}

    v["estado"] = "navegando"
    verificador = VerificadorWeb(vid)

    async def tarea():
        try:
            await verificador.iniciar()
            await verificador.navegar(csv, dni)
            v["verificador"] = verificador
            v["estado"] = "esperando_captcha"
            v["mensaje"] = "Navegador listo. Resuelve el captcha manualmente en el visor."
        except Exception as e:
            v["estado"] = "error"
            v["mensaje"] = str(e)
            try:
                await verificador.cerrar()
            except Exception:
                pass

    asyncio.create_task(tarea())
    return {"ok": True, "mensaje": "Abriendo Ministerio en Playwright..."}


@app.post("/api/verify/{vid}")
async def iniciar_verificacion(request: Request, vid: str):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    v = verificaciones.get(vid)
    if not v:
        raise HTTPException(404, "Verificación no encontrada")
    if v["estado"] != "extraido":
        raise HTTPException(400, f"Estado inválido: {v['estado']}")
    datos = v["datos_extraidos"]
    if not datos.get("csv"):
        raise HTTPException(400, "No se encontró un CSV válido en el documento")
    if not csv_con_formato_valido(datos.get("csv")):
        raise HTTPException(400, f"CSV malformado ({datos.get('csv')}): norma SD: + 4 bloques de 4 caracteres")
    if not datos.get("dni"):
        raise HTTPException(400, "No se encontró un DNI/NIE válido en el documento")
    config_check = _leer_config()
    headless_on = config_check.get("headless_mode", False)
    if not headless_on and not _asegurar_display():
        v["estado"] = "error"
        v["mensaje"] = "No hay servidor X disponible"
        return {"id": vid, "estado": v["estado"], "mensaje": v["mensaje"]}
    v["estado"] = "navegando"
    verificador = VerificadorWeb(vid)

    async def tarea():
        try:
            await verificador.iniciar()

            config = _leer_config()
            api_key = config.get("captcha_key", "") if config.get("captcha_2captcha_enabled", False) else ""

            if api_key:
                v["estado"] = "resolviendo_captcha"
                v["mensaje"] = "Resolviendo captcha con2Captcha..."
                resultado_nav = await verificador.navegar_con_captcha(
                    datos["csv"], datos["dni"], api_key, log_fn=_make_log_fn(vid)
                )
                if resultado_nav == "error_captcha":
                    v["estado"] = "error"
                    v["mensaje"] = "2Captcha no pudo resolver el captcha"
                    return
            else:
                await verificador.navegar(datos["csv"], datos["dni"])

            v["verificador"] = verificador
            v["estado"] = "esperando_captcha"
            v["mensaje"] = "Navegador listo. Resuelve el captcha en el visor." if not api_key else "Esperando descarga del PDF original..."
            ruta_original = await verificador.esperar_descarga(timeout_s=300)
            v["verificador"] = None
            if not ruta_original:
                v["estado"] = "error"
                v["mensaje"] = "No se descargó el PDF original (timeout)"
                return
            v["ruta_original"] = ruta_original
            v["estado"] = "descargado"
            resultado = comparar(v["ruta_usuario"], ruta_original, v["datos_extraidos"])
            v["resultado"] = resultado
            v["estado"] = "completo"
        except Exception as e:
            v["estado"] = "error"
            v["mensaje"] = str(e)
        finally:
            try:
                await verificador.cerrar()
            except Exception:
                pass

    asyncio.create_task(tarea())
    return {"id": vid, "estado": v["estado"], "mensaje": "Verificación iniciada"}


@app.get("/api/verify/{vid}/status")
async def estado_verificacion(request: Request, vid: str):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    try:
        v = verificaciones.get(vid)
        if not v:
            raise HTTPException(404, "Verificación no encontrada")
        return {
            "id": vid,
            "estado": v["estado"],
            "mensaje": v.get("mensaje", ""),
            "logs": list(v.get("logs", [])),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Error interno de estado: {str(e)}")


@app.get("/api/verify/{vid}/screenshot")
async def capturar(request: Request, vid: str):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    v = verificaciones.get(vid)
    if not v:
        raise HTTPException(404, "Verificación no encontrada")

    if v.get("chrome_page"):
        try:
            import base64
            screenshot = await v["chrome_page"].screenshot()
            img_b64 = base64.b64encode(screenshot).decode()
            return {"imagen": f"data:image/png;base64,{img_b64}"}
        except Exception:
            return {"imagen": None}

    if not v.get("verificador"):
        return {"imagen": None}
    img = await v["verificador"].capturar_pantalla()
    if not img:
        return {"imagen": None}
    return {"imagen": f"data:image/png;base64,{img}"}


@app.post("/api/verify/{vid}/click")
async def click_en(request: Request, vid: str, data: dict = Body(...)):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    v = verificaciones.get(vid)
    if not v:
        raise HTTPException(404, "Verificación no encontrada")

    if v.get("chrome_page"):
        try:
            await v["chrome_page"].mouse.click(data["x"], data["y"])
            return {"ok": True}
        except Exception as e:
            raise HTTPException(500, f"Error click Chrome: {e}")

    if not v.get("verificador"):
        raise HTTPException(400, "Navegador no disponible")
    await v["verificador"].hacer_click(data["x"], data["y"])
    return {"ok": True}


@app.post("/api/verify/{vid}/type")
async def escribir_en(request: Request, vid: str, data: dict = Body(...)):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    v = verificaciones.get(vid)
    if not v:
        raise HTTPException(404, "Verificación no encontrada")

    if v.get("chrome_page"):
        try:
            await v["chrome_page"].keyboard.type(data["texto"])
            return {"ok": True}
        except Exception as e:
            raise HTTPException(500, f"Error typing Chrome: {e}")

    if not v.get("verificador"):
        raise HTTPException(400, "Navegador no disponible")
    await v["verificador"].escribir(data["texto"])
    return {"ok": True}


@app.post("/api/verify/{vid}/key")
async def tecla_en(request: Request, vid: str, data: dict = Body(...)):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    v = verificaciones.get(vid)
    if not v:
        raise HTTPException(404, "Verificación no encontrada")

    if v.get("chrome_page"):
        try:
            await v["chrome_page"].keyboard.press(data["tecla"])
            return {"ok": True}
        except Exception as e:
            raise HTTPException(500, f"Error key Chrome: {e}")

    if not v.get("verificador"):
        raise HTTPException(400, "Navegador no disponible")
    await v["verificador"].presionar_tecla(data["tecla"])
    return {"ok": True}


@app.post("/api/verify/{vid}/chrome-key")
async def chrome_key(request: Request, vid: str, data: dict = Body(...)):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    v = verificaciones.get(vid)
    if not v:
        raise HTTPException(404, "Verificación no encontrada")
    if not v.get("chrome_page"):
        raise HTTPException(400, "Chrome OCR no está activo")
    try:
        await v["chrome_page"].keyboard.press(data["tecla"])
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, f"Error: {e}")


@app.post("/api/verify/{vid}/chrome-click")
async def chrome_click(request: Request, vid: str, data: dict = Body(...)):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    v = verificaciones.get(vid)
    if not v:
        raise HTTPException(404, "Verificación no encontrada")
    if not v.get("chrome_page"):
        raise HTTPException(400, "Chrome OCR no está activo")
    try:
        await v["chrome_page"].mouse.click(data["x"], data["y"])
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, f"Error: {e}")


@app.get("/api/verify/{vid}/chrome-clipboard")
async def chrome_clipboard(request: Request, vid: str):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    v = verificaciones.get(vid)
    if not v:
        raise HTTPException(404, "Verificación no encontrada")
    if not v.get("chrome_page"):
        raise HTTPException(400, "Chrome OCR no está activo")
    page = v["chrome_page"]
    try:
        # 1) Intentar leer la selección del DOM (PDF viewer text layer)
        texto = await page.evaluate("""() => {
            const sel = window.getSelection();
            if (sel && sel.toString().trim()) return sel.toString();
            // Buscar en los spans del text layer del PDF viewer
            const spans = document.querySelectorAll('#viewer .page .textLayer span, #viewer span, .page span');
            let all = '';
            for (const s of spans) all += s.textContent + ' ';
            return all.trim();
        }""")
        if texto and texto.strip():
            return {"ok": True, "texto": texto.strip()}

        # 2) Intentar leer del portapapeles del sistema via CDP
        try:
            cdp = await v["chrome_context"].new_cdp_session(page)
            await cdp.send("Browser.grantPermissions", {
                "permissions": ["clipboardReadWrite", "clipboardSanitizedWrite"],
            })
            result = await cdp.send("Runtime.evaluate", {
                "expression": "navigator.clipboard.readText()",
                "awaitPromise": True,
            })
            texto = result.get("result", {}).get("value", "")
            if texto and texto.strip():
                return {"ok": True, "texto": texto.strip()}
        except Exception:
            pass

        # 3) Intentar via xsel del portapapeles del sistema
        import subprocess
        try:
            result = subprocess.run(
                ["xsel", "--clipboard", "--output"],
                capture_output=True, text=True, timeout=5,
                env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":99")}
            )
            if result.stdout.strip():
                return {"ok": True, "texto": result.stdout.strip()}
        except Exception:
            pass

        return {"ok": False, "texto": "", "error": "No se pudo leer el portapapeles ni la selección del DOM"}
    except Exception as e:
        return {"ok": False, "texto": "", "error": str(e)}


@app.get("/api/verify/{vid}/result")
async def resultado_verificacion(request: Request, vid: str):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    v = verificaciones.get(vid)
    if not v:
        raise HTTPException(404, "Verificación no encontrada")
    if v["estado"] != "completo":
        raise HTTPException(400, f"La verificación no ha finalizado. Estado: {v['estado']}")
    return {"id": vid, "resultado": v["resultado"]}


@app.get("/api/verify/{vid}/download-original")
async def descargar_original(request: Request, vid: str):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    v = verificaciones.get(vid)
    if not v or "ruta_original" not in v:
        raise HTTPException(404, "Original no disponible")
    return FileResponse(v["ruta_original"], media_type="application/pdf",
                        filename=f"original_{v.get('nombre_archivo', 'documento.pdf')}")


@app.get("/api/verify/{vid}/download-usuario")
async def descargar_usuario(request: Request, vid: str):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    v = verificaciones.get(vid)
    if not v:
        raise HTTPException(404, "Documento no encontrado")
    return FileResponse(v["ruta_usuario"], media_type="application/pdf",
                        filename=v.get("nombre_archivo", "documento.pdf"))


@app.get("/api/pdf/{vid}")
async def ver_pdf(request: Request, vid: str):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    v = verificaciones.get(vid)
    if not v:
        raise HTTPException(404, "Documento no encontrado")
    ruta = v.get("ruta_usuario") or v.get("ruta_original")
    if not ruta or not os.path.exists(ruta):
        raise HTTPException(404, "Archivo no encontrado")
    return FileResponse(ruta, media_type="application/pdf")


CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")


def _leer_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {"captcha_key": ""}


def _guardar_config(config):
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


@app.get("/api/version")
async def obtener_version():
    return {"version": VERSION}


@app.get("/api/config")
async def obtener_config(request: Request):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    config = _leer_config()
    key = config.get("captcha_key", "")
    masked = key[:8] + "..." + key[-4:] if len(key) > 12 else ("***" if key else "")
    return {"version": VERSION, "captcha_key_masked": masked, "has_key": bool(key)}


@app.get("/api/config/raw")
async def obtener_config_raw(request: Request):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    config = _leer_config()
    return {"captcha_key": config.get("captcha_key", "")}


@app.post("/api/config")
async def guardar_config(request: Request, data: dict = Body(...)):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    config = _leer_config()
    if "captcha_key" in data:
        config["captcha_key"] = data["captcha_key"].strip()
    if "extension_key" in data:
        config["extension_key"] = data["extension_key"].strip()
    if "scrape_do_token" in data:
        config["scrape_do_token"] = data["scrape_do_token"].strip()
    if "extension_enabled" in data:
        config["extension_enabled"] = bool(data["extension_enabled"])
    if "use_scrapedo" in data:
        config["use_scrapedo"] = bool(data["use_scrapedo"])
    if "captcha_2captcha_enabled" in data:
        config["captcha_2captcha_enabled"] = bool(data["captcha_2captcha_enabled"])
    if "headless_mode" in data:
        config["headless_mode"] = bool(data["headless_mode"])
    _guardar_config(config)
    key = config.get("captcha_key", "")
    masked = key[:8] + "..." + key[-4:] if len(key) > 12 else ("***" if key else "")
    return {
        "ok": True,
        "captcha_key_masked": masked,
        "has_key": bool(key),
        "extension_enabled": config.get("extension_enabled", False),
        "use_scrapedo": config.get("use_scrapedo", False),
        "captcha_2captcha_enabled": config.get("captcha_2captcha_enabled", False),
        "headless_mode": config.get("headless_mode", False),
    }


@app.get("/api/config/full")
async def obtener_config_full(request: Request):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    config = _leer_config()
    key = config.get("captcha_key", "")
    ext_key = config.get("extension_key", "")
    scrape_token = config.get("scrape_do_token", "")
    omni_key = config.get("omniroute_key", "")
    google_key = config.get("google_ai_key", "")
    opencode_key = config.get("opencode_key", "")
    key = config.get("captcha_key", "")
    masked = key[:8] + "..." + key[-4:] if len(key) > 12 else ("***" if key else "")
    return {
        "version": VERSION,
        "has_key": bool(key),
        "captcha_key_masked": masked,
        "has_ext_key": bool(ext_key),
        "has_scrape_token": bool(scrape_token),
        "has_omni_key": bool(omni_key),
        "has_google_key": bool(google_key),
        "has_opencode_key": bool(opencode_key),
        "extension_enabled": config.get("extension_enabled", False),
        "use_scrapedo": config.get("use_scrapedo", False),
        "captcha_2captcha_enabled": config.get("captcha_2captcha_enabled", False),
        "headless_mode": config.get("headless_mode", False),
        "ai_ocr_fallback_enabled": config.get("ai_ocr_fallback_enabled", False),
    }


@app.get("/api/config/captcha-balance")
async def captcha_balance(request: Request):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    config = _leer_config()
    key = config.get("captcha_key", "")
    if not key:
        return {"balance": None, "error": "No API key configured"}
    try:
        resp = requests.get(
            "https://api.2captcha.com/res.php",
            params={"key": key, "action": "getbalance", "json": 1},
            timeout=10,
        )
        data = resp.json()
        if data.get("request") and data["request"] != "ERROR_WRONG_USER_KEY":
            return {"balance": float(data["request"]), "currency": "USD"}
        return {"balance": None, "error": data.get("request", "Error")}
    except Exception as e:
        return {"balance": None, "error": str(e)}


@app.get("/api/config/omniroute")
async def obtener_config_omniroute(request: Request):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    config = _leer_config()
    key = config.get("omniroute_key", "")
    masked = key[:8] + "..." + key[-4:] if len(key) > 12 else ("***" if key else "")
    return {
        "omniroute_url": config.get("omniroute_url", ""),
        "omniroute_key_masked": masked,
        "has_key": bool(key),
        "omniroute_model": config.get("omniroute_model", "OCR"),
        "ai_ocr_fallback_enabled": config.get("ai_ocr_fallback_enabled", False),
    }


@app.post("/api/config/omniroute")
async def guardar_config_omniroute(request: Request, data: dict = Body(...)):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    config = _leer_config()
    if "omniroute_url" in data:
        config["omniroute_url"] = data["omniroute_url"].strip()
    if "omniroute_key" in data:
        config["omniroute_key"] = data["omniroute_key"].strip()
    if "omniroute_model" in data:
        config["omniroute_model"] = data["omniroute_model"].strip()
    if "ai_ocr_fallback_enabled" in data:
        config["ai_ocr_fallback_enabled"] = bool(data["ai_ocr_fallback_enabled"])
    _guardar_config(config)
    key = config.get("omniroute_key", "")
    masked = key[:8] + "..." + key[-4:] if len(key) > 12 else ("***" if key else "")
    return {
        "ok": True,
        "omniroute_url": config.get("omniroute_url", ""),
        "omniroute_key_masked": masked,
        "has_key": bool(key),
        "omniroute_model": config.get("omniroute_model", "OCR"),
        "ai_ocr_fallback_enabled": config.get("ai_ocr_fallback_enabled", False),
    }


@app.post("/api/config/omniroute/models")
async def listar_modelos_omniroute(request: Request, data: dict = Body({})):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    config = _leer_config()
    omniroute_url = (data.get("omniroute_url") or config.get("omniroute_url", "")).rstrip("/")
    omniroute_key = data.get("omniroute_key") or config.get("omniroute_key", "")
    if not omniroute_url or not omniroute_key:
        raise HTTPException(400, "OmniRoute no configurado")
    try:
        resp = requests.get(
            f"{omniroute_url}/models",
            headers={"Authorization": f"Bearer {omniroute_key}"},
            timeout=15,
        )
        resp.raise_for_status()
        data_resp = resp.json()
        models = [m["id"] for m in data_resp.get("data", [])]
        models.sort()
        return {"ok": True, "models": models}
    except Exception as e:
        raise HTTPException(502, f"Error al obtener modelos: {str(e)}")


@app.get("/api/config/omniroute/status")
async def omniroute_status(request: Request):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    config = _leer_config()
    omniroute_url = config.get("omniroute_url", "").rstrip("/")
    omniroute_key = config.get("omniroute_key", "")
    if not omniroute_url or not omniroute_key:
        return {"ok": False, "error": "No configurado"}
    try:
        resp = requests.get(
            f"{omniroute_url}/models",
            headers={"Authorization": f"Bearer {omniroute_key}"},
            timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json()
            models = [m["id"] for m in data.get("data", [])]
            return {"ok": True, "models": len(models), "model": config.get("omniroute_model", "OCR")}
        return {"ok": False, "error": f"HTTP {resp.status_code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:100]}


@app.get("/api/config/google")
async def obtener_config_google(request: Request):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    config = _leer_config()
    key = config.get("google_ai_key", "")
    masked = key[:8] + "..." + key[-4:] if len(key) > 12 else ("***" if key else "")
    return {
        "google_ai_key_masked": masked,
        "has_key": bool(key),
        "google_model": config.get("google_model", "gemini-2.0-flash"),
    }


@app.post("/api/config/google")
async def guardar_config_google(request: Request, data: dict = Body(...)):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    config = _leer_config()
    if "google_ai_key" in data:
        config["google_ai_key"] = data["google_ai_key"].strip()
    if "google_model" in data:
        config["google_model"] = data["google_model"].strip()
    _guardar_config(config)
    key = config.get("google_ai_key", "")
    masked = key[:8] + "..." + key[-4:] if len(key) > 12 else ("***" if key else "")
    return {"ok": True, "google_ai_key_masked": masked, "has_key": bool(key)}


@app.post("/api/config/google/test")
async def test_config_google(request: Request):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    config = _leer_config()
    key = config.get("google_ai_key", "")
    model = config.get("google_model", "gemini-2.0-flash")
    if not key:
        raise HTTPException(400, "API Key de Google AI no configurada")
    try:
        import google.generativeai as genai
        genai.configure(api_key=key)
        m = genai.GenerativeModel(model)
        resp = m.generate_content("Responde solo: OK")
        return {"ok": True, "model": model, "response": resp.text[:100]}
    except ImportError:
        raise HTTPException(500, "Paquete google-generativeai no instalado. Ejecuta: pip install google-generativeai")
    except Exception as e:
        raise HTTPException(502, f"Error de conexión: {str(e)}")


@app.get("/api/config/opencode")
async def obtener_config_opencode(request: Request):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    config = _leer_config()
    key = config.get("opencode_key", "")
    masked = key[:8] + "..." + key[-4:] if len(key) > 12 else ("***" if key else "")
    return {
        "opencode_key_masked": masked,
        "has_key": bool(key),
        "opencode_url": config.get("opencode_url", "https://api.opencode.ai/v1"),
        "opencode_model": config.get("opencode_model", "gpt-4o"),
    }


@app.get("/api/config/intranet")
async def obtener_config_intranet(request: Request):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    config = _leer_config()
    return {
        "intranet_user": config.get("intranet_user", ""),
        "has_pass": bool(config.get("intranet_pass", "")),
    }


@app.post("/api/config/intranet")
async def guardar_config_intranet(request: Request, data: dict = Body(...)):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    config = _leer_config()
    if "intranet_user" in data:
        config["intranet_user"] = data["intranet_user"].strip()
    if data.get("intranet_pass"):
        config["intranet_pass"] = data["intranet_pass"]
    _guardar_config(config)
    return {
        "ok": True,
        "intranet_user": config.get("intranet_user", ""),
        "has_pass": bool(config.get("intranet_pass", "")),
    }


@app.post("/api/config/opencode")
async def guardar_config_opencode(request: Request, data: dict = Body(...)):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    config = _leer_config()
    if "opencode_key" in data:
        config["opencode_key"] = data["opencode_key"].strip()
    if "opencode_url" in data:
        config["opencode_url"] = data["opencode_url"].strip()
    if "opencode_model" in data:
        config["opencode_model"] = data["opencode_model"].strip()
    _guardar_config(config)
    key = config.get("opencode_key", "")
    masked = key[:8] + "..." + key[-4:] if len(key) > 12 else ("***" if key else "")
    return {"ok": True, "opencode_key_masked": masked, "has_key": bool(key)}


@app.post("/api/config/opencode/test")
async def test_config_opencode(request: Request):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    config = _leer_config()
    key = config.get("opencode_key", "")
    url = config.get("opencode_url", "https://api.opencode.ai/v1")
    model = config.get("opencode_model", "gpt-4o")
    if not key:
        raise HTTPException(400, "API Key de OpenCode no configurada")
    try:
        resp = requests.post(
            f"{url}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model, "messages": [{"role": "user", "content": "Responde solo: OK"}], "max_tokens": 10},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return {"ok": True, "model": model, "response": data["choices"][0]["message"]["content"][:100]}
    except Exception as e:
        raise HTTPException(502, f"Error de conexión: {str(e)}")


@app.post("/api/verify/{vid}/open-extension")
async def abrir_extension(request: Request, vid: str):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    v = verificaciones.get(vid)
    if not v or not v.get("verificador"):
        raise HTTPException(400, "Navegador no disponible")
    result = await v["verificador"].abrir_popup_extension()
    return {"ok": True, "message": result}


@app.post("/api/verify/{vid}/restart")
async def reiniciar_navegador(request: Request, vid: str):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    v = verificaciones.get(vid)
    if not v:
        raise HTTPException(404, "Verificación no encontrada")
    verificador = v.get("verificador")
    if verificador:
        try:
            await verificador.cerrar()
        except Exception:
            pass
    datos = v["datos_extraidos"]
    nuevo_verificador = VerificadorWeb(vid)

    async def tarea():
        try:
            await nuevo_verificador.iniciar()

            config = _leer_config()
            api_key = config.get("captcha_key", "") if config.get("captcha_2captcha_enabled", False) else ""

            if api_key:
                v["estado"] = "resolviendo_captcha"
                v["mensaje"] = "Resolviendo captcha con2Captcha..."
                resultado_nav = await nuevo_verificador.navegar_con_captcha(
                    datos["csv"], datos["dni"], api_key, log_fn=_make_log_fn(vid)
                )
                if resultado_nav == "error_captcha":
                    v["estado"] = "error"
                    v["mensaje"] = "2Captcha no pudo resolver el captcha"
                    return
            else:
                await nuevo_verificador.navegar(datos["csv"], datos["dni"])

            v["verificador"] = nuevo_verificador
            v["estado"] = "esperando_captcha"
            v["mensaje"] = "Navegador reiniciado. Resuelve el captcha." if not api_key else "Esperando descarga del PDF original..."
            ruta_original = await nuevo_verificador.esperar_descarga(timeout_s=300)
            v["verificador"] = None
            if not ruta_original:
                v["estado"] = "error"
                v["mensaje"] = "No se descargó el PDF original (timeout)"
                return
            v["ruta_original"] = ruta_original
            v["estado"] = "descargado"
            resultado = comparar(v["ruta_usuario"], ruta_original, v["datos_extraidos"])
            v["resultado"] = resultado
            v["estado"] = "completo"
        except Exception as e:
            v["estado"] = "error"
            v["mensaje"] = str(e)
        finally:
            try:
                await nuevo_verificador.cerrar()
            except Exception:
                pass

    asyncio.create_task(tarea())
    v["estado"] = "reiniciando"
    return {"ok": True, "mensaje": "Navegador reiniciando"}


@app.post("/api/clear-uploads")
async def limpiar_uploads(request: Request):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    count = 0
    for f in os.listdir(DIR_UPLOADS):
        ruta = os.path.join(DIR_UPLOADS, f)
        if os.path.isfile(ruta):
            os.remove(ruta)
            count += 1
    for f in os.listdir(DIR_ORIGINALES):
        ruta = os.path.join(DIR_ORIGINALES, f)
        if os.path.isfile(ruta):
            os.remove(ruta)
            count += 1
    verificaciones.clear()
    return {"ok": True, "eliminados": count}


@app.post("/api/import/start")
async def iniciar_importacion(request: Request, data: dict = Body(...)):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    if import_progress.get("running"):
        raise HTTPException(400, "Ya hay una importación en curso")
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    if data.get("use_saved_creds"):
        cfg = _leer_config()
        if not username:
            username = cfg.get("intranet_user", "")
        if not password:
            password = cfg.get("intranet_pass", "")
    max_docs = min(int(data.get("max_docs", 50)), 200)
    headless = data.get("headless", True)
    if not username or not password:
        raise HTTPException(400, "Usuario y contraseña son obligatorios")
    import asyncio as _asyncio
    _asyncio.create_task(importar_documentos(username, password, max_docs, headless))
    return {"ok": True, "message": "Importación iniciada"}


@app.post("/api/import/stop")
async def detener_importacion_api(request: Request):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    detener_importacion()
    return {"ok": True, "message": "Solicitud de detención enviada"}


@app.get("/api/import/status")
async def estado_importacion(request: Request):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    return obtener_estado()


@app.get("/api/import/documents")
async def listar_documentos_importados(request: Request):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    from importador import DIR_DOWNLOADS
    docs = []
    if os.path.exists(DIR_DOWNLOADS):
        for f in sorted(os.listdir(DIR_DOWNLOADS)):
            ruta = os.path.join(DIR_DOWNLOADS, f)
            if os.path.isfile(ruta) and f.endswith(".pdf"):
                docs.append({
                    "filename": f,
                    "size": os.path.getsize(ruta),
                    "path": ruta,
                })
    return {"ok": True, "documents": docs, "count": len(docs)}


@app.post("/api/import/verify-all")
async def verificar_documentos_importados(request: Request):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    from importador import DIR_DOWNLOADS
    if not os.path.exists(DIR_DOWNLOADS):
        return {"ok": True, "queued": 0, "documents": []}
    pdfs = [f for f in os.listdir(DIR_DOWNLOADS) if f.endswith(".pdf")]
    if not pdfs:
        return {"ok": True, "queued": 0, "documents": []}

    loop = asyncio.get_event_loop()
    config = _leer_config()

    async def event_stream():
        documents = []
        for filename in pdfs:
            vid = uuid.uuid4().hex[:12]
            ruta = os.path.join(DIR_DOWNLOADS, filename)
            ruta_usuario = os.path.join(DIR_UPLOADS, f"{vid}.pdf")
            shutil.copy2(ruta, ruta_usuario)
            try:
                datos = await loop.run_in_executor(None, _extraer_datos_rapido, ruta_usuario, config)
                datos_extraidos = {
                    "nombre": datos.get("nombre"),
                    "dni": datos.get("dni"),
                    "csv": datos.get("csv"),
                    "fecha_emision": datos.get("fecha_emision"),
                    "no_consta": datos.get("no_consta", False),
                }
            except Exception:
                datos_extraidos = {"nombre": None, "dni": None, "csv": None, "fecha_emision": None, "no_consta": False}

            verificaciones[vid] = {
                "id": vid,
                "nombre_archivo": filename,
                "ruta_usuario": ruta_usuario,
                "datos_extraidos": datos_extraidos,
                "estado": "extraido",
                "verificador": None,
                "ruta_original": None,
                "resultado": None,
            }
            doc = {"id": vid, "name": filename, "datos": datos_extraidos}
            documents.append(doc)
            yield f"data: {json.dumps({'type': 'progress', 'current': len(documents), 'total': len(pdfs), 'document': doc})}\n\n"

        yield f"data: {json.dumps({'type': 'done', 'documents': documents})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


async def _ejecutar_verificacion(vid):
    v = verificaciones.get(vid)
    if not v or v["estado"] != "extraido":
        return
    datos = v["datos_extraidos"]
    if not datos.get("csv") or not datos.get("dni"):
        v["estado"] = "omitido"
        v["mensaje"] = "Campos incompletos (CSV/DNI)"
        return
    if not _asegurar_display():
        v["estado"] = "error"
        v["mensaje"] = "No hay servidor X disponible"
        return
    v["estado"] = "navegando"
    verificador = VerificadorWeb(vid)
    try:
        await verificador.iniciar()
        config = _leer_config()
        api_key = config.get("captcha_key", "") if config.get("captcha_2captcha_enabled", False) else ""
        if api_key:
            v["estado"] = "resolviendo_captcha"
            v["mensaje"] = "Resolviendo captcha con 2Captcha..."
            resultado_nav = await verificador.navegar_con_captcha(
                datos["csv"], datos["dni"], api_key, log_fn=_make_log_fn(vid)
            )
            if resultado_nav == "error_captcha":
                v["estado"] = "error"
                v["mensaje"] = "2Captcha no pudo resolver el captcha"
                return
        else:
            await verificador.navegar(datos["csv"], datos["dni"])
        v["verificador"] = verificador
        v["estado"] = "esperando_captcha"
        v["mensaje"] = "Navegador listo. Resuelve el captcha." if not api_key else "Esperando descarga del PDF original..."
        ruta_original = await verificador.esperar_descarga(timeout_s=300)
        v["verificador"] = None
        if not ruta_original:
            v["estado"] = "error"
            v["mensaje"] = "No se descargó el PDF original (timeout)"
            return
        v["ruta_original"] = ruta_original
        v["estado"] = "descargado"
        resultado = comparar(v["ruta_usuario"], ruta_original, v["datos_extraidos"])
        v["resultado"] = resultado
        v["estado"] = "completo"
    except Exception as e:
        v["estado"] = "error"
        v["mensaje"] = str(e)
    finally:
        try:
            await verificador.cerrar()
        except Exception:
            pass
