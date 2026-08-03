#!/bin/bash
cd /root/jailchecker/verificador
nohup python3 -m uvicorn app:app --host 0.0.0.0 --port 8000 > /tmp/uvicorn.log 2>&1 &
echo $! > /tmp/uvicorn.pid
