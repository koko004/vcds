#!/usr/bin/env python3
import os
import sys
import signal
import resource

def daemonize():
    if os.fork() > 0:
        sys.exit(0)
    os.setsid()
    if os.fork() > 0:
        sys.exit(0)
    sys.stdout.flush()
    sys.stderr.flush()
    devnull = open(os.devnull, 'r')
    os.dup2(devnull.fileno(), sys.stdin.fileno())
    log = open('/tmp/uvicorn.log', 'a+')
    os.dup2(log.fileno(), sys.stdout.fileno())
    os.dup2(log.fileno(), sys.stderr.fileno())
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

if __name__ == '__main__':
    daemonize()
    os.chdir('/root/jailchecker/verificador')
    from uvicorn.main import main
    sys.argv = ['uvicorn', 'app:app', '--host', '0.0.0.0', '--port', '8000']
    main()
