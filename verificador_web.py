import os
import json
import asyncio
import base64
import glob as glob_mod
import random
import signal
import subprocess
import atexit
import time
import requests
from playwright.async_api import async_playwright

URL_BASE = "https://sede.mjusticia.gob.es/sedecsvbroker/FormularioVerificacion.action"
DIR_DESCARGAS = os.path.join(os.path.dirname(__file__), "originales")
DIR_EXTENSION = os.path.join(os.path.dirname(__file__), "extension", "captcha_solver")
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
USER_DATA = os.path.join(os.path.dirname(__file__), "chrome_profile")
EXT_ID = "hlifkpholllijblknnmbfagnkjneagid"

SCRAPE_DO_TOKEN = "705a6492448a4c878e7925e1c85161683798eecd0db"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]

_XVFB_PROC = None


def _asegurar_display():
    global _XVFB_PROC
    if os.environ.get("DISPLAY"):
        return True
    try:
        _XVFB_PROC = subprocess.Popen(
            ["Xvfb", ":99", "-screen", "0", "1280x1024x24"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        os.environ["DISPLAY"] = ":99"
        atexit.register(lambda: _XVFB_PROC and _XVFB_PROC.terminate())
        return True
    except FileNotFoundError:
        return False


def _cargar_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {"captcha_key": ""}


def _limpiar_profile(profile_dir: str):
    """Kill old Chromium using this profile and remove lock files."""
    try:
        subprocess.run(
            ["pkill", "-9", "-f", f"user-data-dir={profile_dir}"],
            capture_output=True, timeout=3,
        )
    except Exception:
        pass
    for lock_file in glob_mod.glob(os.path.join(profile_dir, "SingletonLock")) + \
                     glob_mod.glob(os.path.join(profile_dir, "SingletonSocket")) + \
                     glob_mod.glob(os.path.join(profile_dir, "SingletonCookie")):
        try:
            os.remove(lock_file)
        except Exception:
            pass
    for lock_file in glob_mod.glob(os.path.join(profile_dir, "**", "lockfile")) + \
                     glob_mod.glob(os.path.join(profile_dir, "**", "Lock")):
        try:
            os.remove(lock_file)
        except Exception:
            pass


class VerificadorWeb:
    def __init__(self, vid: str = None):
        self._vid = vid or str(random.randint(10000, 99999))
        self._pw = None
        self._context = None
        self._page = None
        self._ruta_descargada = None
        self._descargado = asyncio.Event()

    async def _movimiento_ratón_realista(self, x_final: int, y_final: int):
        """Simula un movimiento de ratón realista con curva Bézier."""
        if not self._page:
            return
        try:
            estado = await self._page.evaluate("() => ({ x: window._lastMouseX || 400, y: window._lastMouseY || 300 })")
            x_inicio = estado.get("x", 400)
            y_inicio = estado.get("y", 300)

            puntos = random.randint(8, 15)
            cp1x = x_inicio + (x_final - x_inicio) * 0.3 + random.randint(-30, 30)
            cp1y = y_inicio + (y_final - y_inicio) * 0.1 + random.randint(-20, 20)
            cp2x = x_inicio + (x_final - x_inicio) * 0.7 + random.randint(-20, 20)
            cp2y = y_inicio + (y_final - y_inicio) * 0.9 + random.randint(-15, 15)

            for i in range(puntos + 1):
                t = i / puntos
                t2 = t * t
                t3 = t2 * t
                mt = 1 - t
                mt2 = mt * mt
                mt3 = mt2 * mt

                x = mt3 * x_inicio + 3 * mt2 * t * cp1x + 3 * mt * t2 * cp2x + t3 * x_final
                y = mt3 * y_inicio + 3 * mt2 * t * cp1y + 3 * mt * t2 * cp2y + t3 * y_final

                await self._page.mouse.move(x, y)
                await asyncio.sleep(random.uniform(0.008, 0.025))

            await self._page.evaluate(f"() => {{ window._lastMouseX = {x_final}; window._lastMouseY = {y_final}; }}")
        except Exception:
            pass

    async def _delay_realista(self, min_s: float = 0.5, max_s: float = 2.0):
        """Espera un tiempo aleatorio realista."""
        await asyncio.sleep(random.uniform(min_s, max_s))

    async def _mover_ratón_aleatorio(self):
        """Mueve el ratón a una posición aleatoria en la página."""
        if not self._page:
            return
        try:
            x = random.randint(100, 1100)
            y = random.randint(100, 700)
            await self._movimiento_ratón_realista(x, y)
        except Exception:
            pass

    async def iniciar(self):
        os.makedirs(DIR_DESCARGAS, exist_ok=True)
        os.makedirs(USER_DATA, exist_ok=True)
        config = _cargar_config()
        captcha_key = config.get("captcha_key", "")
        extension_enabled = config.get("extension_enabled", False)
        use_scrapedo = config.get("use_scrapedo", False)
        captcha_2captcha = config.get("captcha_2captcha_enabled", False)

        headless = captcha_2captcha and not extension_enabled

        if not headless:
            _asegurar_display()

        args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-infobars",
            "--disable-breakpad",
            "--metrics-recording-only",
            "--disable-background-networking",
        ]

        if extension_enabled and os.path.isdir(DIR_EXTENSION):
            args.append(f"--disable-extensions-except={DIR_EXTENSION}")
            args.append(f"--load-extension={DIR_EXTENSION}")

        proxy_config = None
        if use_scrapedo:
            proxy_config = {
                "server": f"http://proxy.scrape.do:8080",
                "username": SCRAPE_DO_TOKEN,
                "password": f"render=false&super=true&geoCode=es",
            }

        user_agent = random.choice(USER_AGENTS)

        user_data = os.path.join(USER_DATA, f"profile_{self._vid}")
        os.makedirs(user_data, exist_ok=True)
        _limpiar_profile(user_data)

        self._pw = await async_playwright().start()
        self._context = await self._pw.chromium.launch_persistent_context(
            user_data_dir=user_data,
            headless=headless,
            args=args,
            accept_downloads=True,
            locale="es-ES",
            timezone_id="Europe/Madrid",
            viewport={"width": 1280, "height": 900},
            user_agent=user_agent,
            proxy=proxy_config,
            ignore_https_errors=True,
        )

        self._ext_id = None
        if extension_enabled and captcha_key:
            await self._configurar_extension(captcha_key)

        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()
        self._page.on("download", self._on_download)

        await self._page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'languages', { get: () => ['es-ES', 'es', 'en'] });
            Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
            Object.defineProperty(navigator, 'plugins', {
                get: () => {
                    const plugins = [
                        { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
                        { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
                        { name: 'Native Client', filename: 'internal-nacl-plugin' },
                    ];
                    plugins.length = 3;
                    return plugins;
                }
            });
            window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){} };
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (params) => (
                params.name === 'notifications' ?
                    Promise.resolve({ state: Notification.permission }) :
                    originalQuery(params)
            );
        """)

    async def _configurar_extension(self, key: str):
        try:
            sw = None
            if self._context.service_workers:
                sw = self._context.service_workers[0]
            else:
                sw = await self._context.wait_for_event("serviceworker", timeout=10000)

            if not sw:
                print("[Extension] No service worker found")
                return

            sw_url = sw.url
            self._ext_id = sw_url.split("/")[2] if "chrome-extension://" in sw_url else None
            print(f"[Extension] ID: {self._ext_id}")

            await sw.evaluate("""(key) => {
                chrome.storage.local.set({
                    creditKey: key,
                    enableCaptchaSolver: true
                });
            }""", key)

            if self._ext_id:
                page = await self._context.new_page()
                try:
                    await page.goto(f"chrome-extension://{self._ext_id}/popup/popup.html", timeout=10000)
                    await asyncio.sleep(2)

                    await page.evaluate("""(key) => {
                        chrome.storage.local.set({
                            creditKey: key,
                            enableCaptchaSolver: true
                        });
                    }""", key)

                    try:
                        await page.wait_for_selector("#creditKey, input[placeholder*='key'], input[placeholder*='Key']", timeout=3000)
                        key_input = page.locator("#creditKey, input[placeholder*='key'], input[placeholder*='Key']").first
                        if await key_input.count() > 0:
                            await key_input.fill(key)
                            await asyncio.sleep(0.5)
                    except Exception:
                        pass

                    buttons = await page.query_selector_all("button")
                    for btn in buttons:
                        text = (await btn.inner_text()).strip().lower()
                        if any(w in text for w in ["bind", "key", "save", "guardar", "activate", "activar"]):
                            await btn.click()
                            print(f"[Extension] Clicked: {text}")
                            await asyncio.sleep(1)
                            break

                except Exception as e:
                    print(f"[Extension] Popup error: {e}")
                finally:
                    try:
                        await page.close()
                    except Exception:
                        pass
        except Exception as e:
            print(f"[Extension] Error: {e}")

    async def abrir_popup_extension(self):
        if not self._ext_id:
            return "No hay extensión activa"
        try:
            await self._page.goto(f"chrome-extension://{self._ext_id}/popup/popup.html", timeout=10000)
            return "Popup abierto en el visor"
        except Exception as e:
            return f"Error: {e}"

    async def _on_download(self, download):
        ruta = os.path.join(DIR_DESCARGAS, download.suggested_filename)
        await download.save_as(ruta)
        self._ruta_descargada = ruta
        self._descargado.set()

    async def navegar(self, csv: str, dni: str) -> str:
        url = f"{URL_BASE}?CSV={csv}"

        await self._mover_ratón_aleatorio()
        await self._delay_realista(1.0, 2.5)

        await self._page.goto(url, wait_until="domcontentloaded")
        await self._delay_realista(3.0, 5.0)

        await self._mover_ratón_aleatorio()
        await self._delay_realista(1.0, 2.0)

        filled = False
        selectors = [
            "input#iddnitext",
            "input[name='formulario.dni']",
            "input#docIdentidad",
            "input[name='docIdentidad']",
            "input[placeholder*='identidad']",
            "input[placeholder*='Documento']",
            "input[placeholder*='DNI']",
            "input[placeholder*='NIF']",
        ]
        for sel in selectors:
            try:
                el = self._page.locator(sel)
                if await el.count() > 0:
                    box = await el.first.bounding_box()
                    if box:
                        await self._movimiento_ratón_realista(
                            int(box["x"] + box["width"] / 2 + random.randint(-5, 5)),
                            int(box["y"] + box["height"] / 2 + random.randint(-3, 3)),
                        )
                        await self._delay_realista(0.3, 0.8)
                    await el.first.click()
                    await self._delay_realista(0.2, 0.5)
                    for char in dni:
                        await self._page.keyboard.type(char, delay=random.randint(50, 150))
                        await asyncio.sleep(random.uniform(0.02, 0.08))
                    filled = True
                    break
            except Exception:
                continue
        if not filled:
            try:
                inputs = self._page.locator("input[type='text']")
                count = await inputs.count()
                if count >= 2:
                    box = await inputs.nth(1).bounding_box()
                    if box:
                        await self._movimiento_ratón_realista(
                            int(box["x"] + box["width"] / 2),
                            int(box["y"] + box["height"] / 2),
                        )
                        await self._delay_realista(0.3, 0.6)
                    await inputs.nth(1).click()
                    await self._delay_realista(0.2, 0.5)
                    for char in dni:
                        await self._page.keyboard.type(char, delay=random.randint(50, 150))
                        await asyncio.sleep(random.uniform(0.02, 0.08))
                    filled = True
            except Exception:
                pass

        await self._mover_ratón_aleatorio()
        await self._delay_realista(1.0, 2.0)
        return "ok"

    async def capturar_pantalla(self) -> str | None:
        if not self._page:
            return None
        try:
            screenshot = await self._page.screenshot(type="png")
            return base64.b64encode(screenshot).decode()
        except Exception:
            return None

    async def hacer_click(self, x: int, y: int):
        if not self._page:
            return
        try:
            await self._page.mouse.click(x, y)
        except Exception:
            pass

    async def escribir(self, texto: str):
        if not self._page:
            return
        try:
            await self._page.keyboard.type(texto)
        except Exception:
            pass

    async def presionar_tecla(self, tecla: str):
        if not self._page:
            return
        try:
            await self._page.keyboard.press(tecla)
        except Exception:
            pass

    async def esperar_descarga(self, timeout_s: int = 300) -> str | None:
        self._descargado = asyncio.Event()
        self._ruta_descargada = None
        try:
            await asyncio.wait_for(self._descargado.wait(), timeout=timeout_s)
            return self._ruta_descargada
        except asyncio.TimeoutError:
            return None

    async def cerrar(self):
        try:
            if self._context:
                await self._context.close()
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass

    # ── 2Captcha integration ──────────────────────────────────────────

    SITEKEY = "6LfvHcsfAAAAAJ_hASi7O0_diq5kGKVHvBmWEbMo"
    CALLBACK = "capcha_filled"
    URL_MINISTERIO = "https://sede.mjusticia.gob.es/sedecsvbroker/FormularioVerificacion.action"

    def _solucionar_captcha_2captcha(self, api_key: str, page_url: str) -> str | None:
        """Send reCAPTCHA to2Captcha and return the response token."""
        API_BASE = "https://api.2captcha.com"

        # Step 1: createTask
        payload = {
            "clientKey": api_key,
            "task": {
                "type": "NoCaptchaTaskProxyless",
                "websiteURL": page_url,
                "websiteKey": self.SITEKEY,
            },
        }
        try:
            r = requests.post(f"{API_BASE}/createTask", json=payload, timeout=30)
            data = r.json()
        except Exception as e:
            print(f"[2Captcha] createTask request failed: {e}")
            return None

        if data.get("errorId", 0) != 0:
            print(f"[2Captcha] createTask error: {data.get('errorDescription', data)}")
            return None

        task_id = data.get("taskId")
        if not task_id:
            print(f"[2Captcha] No taskId in response: {data}")
            return None
        print(f"[2Captcha] Task created: {task_id}")

        # Step 2: poll getTaskResult
        poll_payload = {"clientKey": api_key, "taskId": task_id}
        for attempt in range(60):
            time.sleep(3)
            try:
                r = requests.post(f"{API_BASE}/getTaskResult", json=poll_payload, timeout=30)
                result = r.json()
            except Exception as e:
                print(f"[2Captcha] getTaskResult request failed: {e}")
                continue

            if result.get("errorId", 0) != 0:
                print(f"[2Captcha] getTaskResult error: {result.get('errorDescription', result)}")
                return None

            status = result.get("status")
            if status == "ready":
                token = result.get("solution", {}).get("gRecaptchaResponse", "")
                print(f"[2Captcha] Solved in {attempt * 3}s, token length: {len(token)}")
                return token

            if attempt % 5 == 0:
                print(f"[2Captcha] Polling... attempt {attempt + 1}/60, status: {status}")

        print("[2Captcha] Timeout waiting for solution")
        return None

    async def navegar_con_captcha(self, csv: str, dni: str, api_key: str, log_fn=None) -> str:
        """Navigate, fill DNI, solve captcha via2Captcha, and trigger download."""
        def _log(msg):
            if log_fn:
                log_fn(msg)
            print(f"[2Captcha] {msg}")

        url = f"{self.URL_MINISTERIO}?CSV={csv}"

        # Navigate
        _log(f"Navigating to Ministry page...")
        await self._mover_ratón_aleatorio()
        await self._delay_realista(1.0, 2.0)
        await self._page.goto(url, wait_until="domcontentloaded")
        await self._delay_realista(3.0, 5.0)
        await self._mover_ratón_aleatorio()
        await self._delay_realista(1.0, 2.0)

        # Fill DNI
        _log("Filling DNI...")
        filled = False
        selectors = [
            "input#iddnitext",
            "input[name='formulario.dni']",
            "input#docIdentidad",
            "input[name='docIdentidad']",
            "input[placeholder*='identidad']",
            "input[placeholder*='Documento']",
            "input[placeholder*='DNI']",
            "input[placeholder*='NIF']",
        ]
        for sel in selectors:
            try:
                el = self._page.locator(sel)
                if await el.count() > 0:
                    box = await el.first.bounding_box()
                    if box:
                        await self._movimiento_ratón_realista(
                            int(box["x"] + box["width"] / 2 + random.randint(-5, 5)),
                            int(box["y"] + box["height"] / 2 + random.randint(-3, 3)),
                        )
                        await self._delay_realista(0.3, 0.8)
                    await el.first.click()
                    await self._delay_realista(0.2, 0.5)
                    for char in dni:
                        await self._page.keyboard.type(char, delay=random.randint(50, 150))
                        await asyncio.sleep(random.uniform(0.02, 0.08))
                    filled = True
                    break
            except Exception:
                continue

        if not filled:
            _log("WARNING: Could not find DNI input field")

        await self._delay_realista(2.0, 3.0)

        # Wait for reCAPTCHA to load
        _log("Waiting for reCAPTCHA to load...")
        try:
            await self._page.wait_for_selector(".g-recaptcha", timeout=15000)
            await self._delay_realista(2.0, 4.0)
        except Exception:
            _log("WARNING: reCAPTCHA div not found, proceeding anyway...")

        # Solve captcha via2Captcha
        _log(f"Sending captcha to2Captcha (sitekey: {self.SITEKEY[:15]}...)")
        current_url = self._page.url
        token = await asyncio.get_event_loop().run_in_executor(
            None, self._solucionar_captcha_2captcha, api_key, current_url
        )

        if not token:
            _log("ERROR:2Captcha failed to solve captcha")
            return "error_captcha"

        _log(f"Token received ({len(token)} chars). Injecting...")

        # Inject token, call callback, and submit form
        injected = await self._page.evaluate(f"""() => {{
            // Set token in hidden input
            const hidden = document.querySelector('#g-recaptcha-response');
            if (hidden) {{
                hidden.value = '{token}';
            }}

            // Set token in textarea (reCAPTCHA widget)
            const textarea = document.querySelector('textarea[name="g-recaptcha-response"]');
            if (textarea) {{
                textarea.value = '{token}';
                textarea.style.display = 'block';
                textarea.style.height = '1px';
                textarea.style.visibility = 'visible';
            }}

            // Call the callback to hide error message
            if (typeof {self.CALLBACK} === 'function') {{
                {self.CALLBACK}('{token}');
            }}

            // Submit the form via the page's own function
            if (typeof submitFormulario === 'function') {{
                submitFormulario();
                return 'form_submitted';
            }}

            // Fallback: submit the form directly
            if (document.forms && document.forms[0]) {{
                document.forms[0].action = 'enviarFormularioVerificacion.action';
                document.forms[0].submit();
                return 'form_submitted_direct';
            }}

            return 'no_action';
        }}""")

        _log(f"Injection result: {injected}")

        if injected in ("form_submitted", "form_submitted_direct"):
            _log("Form submitted successfully")
        elif injected == "callback_called":
            _log("Callback triggered (form should auto-submit)")
        else:
            _log("WARNING: Form may not have been submitted")

        await self._delay_realista(3.0, 5.0)
        _log("Waiting for download...")
        return "ok"
