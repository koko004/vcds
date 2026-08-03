#!/usr/bin/env python3
import subprocess, os, time, signal, sys

xvfb = subprocess.Popen(
    ['Xvfb', ':99', '-screen', '0', '1280x1024x24'],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
)
os.environ['DISPLAY'] = ':99'
time.sleep(1)

proc = subprocess.Popen([
    'python3', '-m', 'uvicorn', 'app:app',
    '--host', '0.0.0.0', '--port', '8000'
])

def handle(sig, frame):
    proc.terminate()
    xvfb.terminate()
    sys.exit(0)

signal.signal(signal.SIGTERM, handle)
signal.signal(signal.SIGINT, handle)
proc.wait()
