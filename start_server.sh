#!/bin/bash
cd /root/jailchecker/verificador
exec python3 -m uvicorn app:app --host 0.0.0.0 --port 8000
