# AGENTS.md — Verificador de Certificados (VCDS)

## Project Overview

FastAPI app that verifies Spanish sexual offense certificates (Certificados de Delitos de Naturaleza Sexual) by extracting data from PDFs, comparing against the Ministerio de Justicia portal, and detecting manipulated documents.

**Tech stack**: Python 3.13 + FastAPI + Playwright (headless Chromium) + PyMuPDF + pdfplumber + Tesseract OCR + Ghostscript + 2Captcha

**Version**: `1.0.12` (defined in `app.py:22`)

## Repository Structure

```
verificador/
├── app.py                  # FastAPI backend, auth, all API endpoints
├── verificador_web.py      # Playwright automation, Ministry portal interaction
├── extractor.py            # PDF extraction pipeline (PyMuPDF/pdfplumber/Tesseract)
├── comparador.py           # PDF comparison logic
├── start_app.py            # Docker entrypoint (Xvfb + uvicorn)
├── start.sh                # Local dev launcher (nohup uvicorn)
├── config.json             # 2Captcha key, extension/scrapedo toggles
├── auth.json               # bcrypt-hashed credentials (gitignored)
├── templates/              # HTML (login, index with batch queue)
├── static/                 # CSS, JS, favicon
├── Dockerfile              # Multi-stage build
└── docker-compose.yml      # vcds + watchtower (auto-update)
```

## Development Commands

### Local (without Docker)
```bash
# Start server (background, log to /tmp/uvicorn.log)
cd /root/jailchecker/verificador
bash start.sh

# Or run foreground
python3 -m uvicorn app:app --host 0.0.0.0 --port 8000
```

**Note**: Playwright needs Xvfb if no display. `verificador_web.py` auto-starts it if `$DISPLAY` is unset.

### Docker
```bash
docker compose up -d          # Start with watchtower auto-updates
docker compose pull && docker compose up -d  # Force update
docker logs vcds              # Check logs
```

## Critical Gotchas

1. **Playwright profile dirs are unique per VID** — each verification creates a temp Chrome profile in `chrome_profile/`. Old profiles are cleaned before new runs.

2. **CSV extraction from OCR PDFs** — When PyMuPDF/pdfplumber fail to find text (scanned PDFs), Tesseract OCR is used. CSV pattern: `SD:XXXX-XXXX-XXXX-XXXX`.

3. **Ministry site uses reCAPTCHA v2** — NOT Enterprise. Sitekey: `6LfvHcsfAAAAAJ_hASi7O0_diq5kGKVHvBmWEbMo`. Callback: `capcha_filled`. Submit via `submitFormulario()` JS function.

4. **2Captcha integration** — HTTP direct calls to `api.2captcha.com`, task type `NoCaptchaTaskProxyless`, polls every 3s, timeout 60 attempts. Token: stored in `config.json` key `captcha_key`.

5. **Batch processing bug (fixed in 1.0.12)** — `waitForVerification()` was checking `rData.ok` (undefined) instead of `rData.resultado`. Same fix in `showDetailResult()`: `data.ok` → `res.ok && data.resultado`.

6. **Docker: No bash in slim image** — Dockerfile uses `start_app.py` (Python) as entrypoint, NOT bash inline. Do not use bash-style commands in CMD.

7. **Auth** — Default credentials: `admin / vcds2024`. Stored in `auth.json` (bcrypt). Sessions use `vcds_session` cookie, 7-day TTL, CSRF protection, rate limiting (5 attempts/5min/IP).

8. **SESSION_SECRET is ephemeral** — Generated fresh each start unless `VCDS_SESSION_SECRET` env var is set. All sessions invalidate on restart.

## Docker Image

- **Registry**: `koko004/vcds`
- **Tags**: `latest`, `v1.0.12`
- **Base**: `python:3.13-slim` + tesseract-ocr-spa + ghostscript + xvfb + chromium
- **Watchtower**: auto-updates every hour

## Configuration

`config.json` keys:
- `captcha_key` — 2Captcha API key
- `captcha_2captcha_enabled` — Use 2Captcha for reCAPTCHA solving (default: true)
- `extension_enabled` — Use Chrome extension for captcha (default: false)
- `use_scrapedo` — Use scrape.do proxy for requests (default: false)

## Git

- `auth.json` and `config.json` are in `.gitignore` — do not commit secrets
- Runtime dirs excluded: `uploads/`, `originales/`, `chrome_profile/`, `__pycache__/`
- GitHub repo: `https://github.com/koko004/vcds`
