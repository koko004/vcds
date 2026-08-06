import os
import uuid
import asyncio
import json
import shutil
import secrets
import time
import hashlib
import hmac
from datetime import datetime, timedelta
from collections import defaultdict
from fastapi import FastAPI, UploadFile, File, HTTPException, Body, Request, Response
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import bcrypt

from extractor import extraer_datos, extraer_datos_ocr
from verificador_web import VerificadorWeb, _asegurar_display
from comparador import comparar

VERSION = "1.0.14"

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
    verificaciones[vid] = {
        "id": vid,
        "nombre_archivo": file.filename,
        "ruta_usuario": ruta_usuario,
        "datos_extraidos": {
            "nombre": datos.get("nombre"),
            "dni": datos.get("dni"),
            "csv": datos.get("csv"),
            "fecha_emision": datos.get("fecha_emision"),
            "no_consta": datos.get("no_consta", False),
        },
        "estado": "extraido",
        "verificador": None,
        "ruta_original": None,
        "resultado": None,
    }
    return {"id": vid, "datos": verificaciones[vid]["datos_extraidos"]}


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

    def on_log(msg):
        logs.append(msg)

    datos = extraer_datos(ruta_usuario, log_fn=on_log)

    verificaciones[vid] = {
        "id": vid,
        "nombre_archivo": file.filename,
        "ruta_usuario": ruta_usuario,
        "datos_extraidos": {
            "nombre": datos.get("nombre"),
            "dni": datos.get("dni"),
            "csv": datos.get("csv"),
            "fecha_emision": datos.get("fecha_emision"),
            "no_consta": datos.get("no_consta", False),
        },
        "estado": "extraido",
        "verificador": None,
        "ruta_original": None,
        "resultado": None,
    }

    async def event_stream():
        for msg in logs:
            yield f"data: {json.dumps({'type': 'log', 'message': msg})}\n\n"
        yield f"data: {json.dumps({'type': 'done', 'id': vid, 'datos': verificaciones[vid]['datos_extraidos']})}\n\n"

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

    logs = []

    def on_log(msg):
        logs.append(msg)

    abs_path = os.path.abspath(ruta)

    def _chrome_ocr_sync():
        from playwright.sync_api import sync_playwright
        texto = ""
        on_log("Iniciando Chrome OCR (Playwright + portapapeles)...")
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(
                    permissions=["clipboard-read", "clipboard-write"],
                    viewport={"width": 1280, "height": 1024},
                )
                page = context.new_page()

                file_url = f"file://{abs_path}"
                on_log(f"Abriendo PDF en Chromium: {abs_path}")
                page.goto(file_url, wait_until="load", timeout=30000)
                page.wait_for_timeout(2500)

                on_log("Haciendo clic en el documento para enfocar...")
                page.mouse.click(640, 512)
                page.wait_for_timeout(500)

                on_log("Seleccionando todo el texto (Ctrl+A)...")
                page.keyboard.press("Control+a")
                page.wait_for_timeout(500)

                on_log("Copiando al portapapeles (Ctrl+C)...")
                page.keyboard.press("Control+c")
                page.wait_for_timeout(500)

                on_log("Leyendo portapapeles...")
                texto = ""
                try:
                    texto = page.evaluate("navigator.clipboard.readText()")
                except Exception as e:
                    on_log(f"Portapapeles no disponible: {e}")

                if not texto or not texto.strip():
                    on_log("Fallback: intentando con window.getSelection()...")
                    try:
                        texto = page.evaluate("""() => {
                            const sel = window.getSelection();
                            return sel ? sel.toString() : '';
                        }""")
                    except Exception:
                        pass

                if not texto or not texto.strip():
                    on_log("Fallback: intentando con document.getSelection()...")
                    try:
                        texto = page.evaluate("""() => {
                            const sel = document.getSelection();
                            return sel ? sel.toString() : '';
                        }""")
                    except Exception:
                        pass

                if not texto or not texto.strip():
                    on_log("Fallback: seleccionando por rango de caracteres...")
                    try:
                        texto = page.evaluate("""() => {
                            const range = document.createRange();
                            const body = document.body;
                            if (!body) return '';
                            range.selectNodeContents(body);
                            const sel = window.getSelection();
                            sel.removeAllRanges();
                            sel.addRange(range);
                            return sel.toString();
                        }""")
                    except Exception:
                        pass

                if texto and texto.strip():
                    on_log(f"Chrome OCR extraído: {len(texto)} caracteres")
                else:
                    on_log("Chrome OCR no pudo extraer texto (portapapeles restringido en headless)")

                browser.close()
        except Exception as e:
            on_log(f"Chrome OCR error: {e}")
        return texto

    try:
        texto = await asyncio.get_event_loop().run_in_executor(None, _chrome_ocr_sync)
    except Exception as e:
        raise HTTPException(500, f"Error en Chrome OCR: {e}")

    if not texto or not texto.strip():
        async def event_stream():
            for msg in logs:
                yield f"data: {json.dumps({'type': 'log', 'message': msg})}\n\n"
                yield f"data: {json.dumps({'type': 'error', 'message': 'Chrome OCR no pudo extraer texto. El portapapeles puede estar restringido en modo headless.'})}\n\n"
        return StreamingResponse(event_stream(), media_type="text/event-stream")

    from extractor import extraer_nombre, extraer_dni, extraer_csv, extraer_fecha, extraer_no_consta
    on_log("Parseando campos del texto extraído...")
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
    on_log(f"Nombre: {nombre or '(no encontrado)'}")
    on_log(f"DNI: {dni or '(no encontrado)'}")
    on_log(f"CSV: {csv_val or '(no encontrado)'}")
    on_log(f"Fecha: {fecha or '(no encontrado)'}")
    on_log(f"NO CONSTA: {no_consta}")

    v["datos_extraidos"] = {
        "nombre": nombre,
        "dni": dni,
        "csv": csv_val,
        "fecha_emision": fecha,
        "no_consta": no_consta,
    }

    async def event_stream():
        for msg in logs:
            yield f"data: {json.dumps({'type': 'log', 'message': msg})}\n\n"
        yield f"data: {json.dumps({'type': 'done', 'id': vid, 'datos': v['datos_extraidos']})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


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
    if not datos.get("dni"):
        raise HTTPException(400, "No se encontró un DNI/NIE válido en el documento")
    if not _asegurar_display():
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
    v = verificaciones.get(vid)
    if not v:
        raise HTTPException(404, "Verificación no encontrada")
    return {
        "id": vid,
        "estado": v["estado"],
        "mensaje": v.get("mensaje", ""),
        "logs": v.get("logs", []),
    }


@app.get("/api/verify/{vid}/screenshot")
async def capturar(request: Request, vid: str):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    v = verificaciones.get(vid)
    if not v:
        raise HTTPException(404, "Verificación no encontrada")
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
    if not v or not v.get("verificador"):
        raise HTTPException(400, "Navegador no disponible")
    await v["verificador"].hacer_click(data["x"], data["y"])
    return {"ok": True}


@app.post("/api/verify/{vid}/type")
async def escribir_en(request: Request, vid: str, data: dict = Body(...)):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    v = verificaciones.get(vid)
    if not v or not v.get("verificador"):
        raise HTTPException(400, "Navegador no disponible")
    await v["verificador"].escribir(data["texto"])
    return {"ok": True}


@app.post("/api/verify/{vid}/key")
async def tecla_en(request: Request, vid: str, data: dict = Body(...)):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    v = verificaciones.get(vid)
    if not v or not v.get("verificador"):
        raise HTTPException(400, "Navegador no disponible")
    await v["verificador"].presionar_tecla(data["tecla"])
    return {"ok": True}


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
    }


@app.get("/api/config/full")
async def obtener_config_full(request: Request):
    if not await _require_auth(request):
        raise HTTPException(401, "No autenticado")
    config = _leer_config()
    key = config.get("captcha_key", "")
    ext_key = config.get("extension_key", "")
    scrape_token = config.get("scrape_do_token", "")
    masked = key[:8] + "..." + key[-4:] if len(key) > 12 else ("***" if key else "")
    masked_ext = ext_key[:8] + "..." + ext_key[-4:] if len(ext_key) > 12 else ("***" if ext_key else "")
    masked_scrape = scrape_token[:8] + "..." + scrape_token[-4:] if len(scrape_token) > 12 else ("***" if scrape_token else "")
    return {
        "version": VERSION,
        "captcha_key_masked": masked,
        "has_key": bool(key),
        "extension_key_masked": masked_ext,
        "has_ext_key": bool(ext_key),
        "scrape_do_token_masked": masked_scrape,
        "has_scrape_token": bool(scrape_token),
        "extension_enabled": config.get("extension_enabled", False),
        "use_scrapedo": config.get("use_scrapedo", False),
        "captcha_2captcha_enabled": config.get("captcha_2captcha_enabled", False),
    }


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
