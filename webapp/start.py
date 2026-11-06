from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser


WEBAPP_DIR = Path(__file__).resolve().parent
REPO_ROOT = WEBAPP_DIR.parent
FRONTEND_DIR = WEBAPP_DIR / "frontend"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start STEP web frontend and backend together.")
    parser.add_argument("--python", default=os.getenv("DEFEATURE_PYTHON") or sys.executable)
    parser.add_argument("--backend-port", type=int, default=8000)
    parser.add_argument("--frontend-port", type=int, default=5173)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--reload", action="store_true", help="Enable Uvicorn reload mode")
    return parser.parse_args(argv)


def port_is_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(0.25)
        return connection.connect_ex(("127.0.0.1", port)) == 0


def wait_http(url: str, process: subprocess.Popen, timeout: float = 45.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("process exited with code {} before {} became ready".format(process.returncode, url))
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                if response.status < 500:
                    return
        except Exception:
            time.sleep(0.25)
    raise TimeoutError("timed out waiting for {}".format(url))


def terminate_tree(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        process.terminate()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    python = Path(args.python).expanduser().resolve()
    if not python.is_file():
        raise FileNotFoundError("Python executable not found: {}".format(python))
    if not (FRONTEND_DIR / "node_modules").is_dir():
        raise RuntimeError("frontend dependencies missing; run npm install in {}".format(FRONTEND_DIR))
    occupied = [port for port in (args.backend_port, args.frontend_port) if port_is_open(port)]
    if occupied:
        raise RuntimeError(
            "port(s) already in use: {}. Stop the existing webapp or choose other ports.".format(
                ", ".join(str(port) for port in occupied)
            )
        )

    environment = os.environ.copy()
    creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    backend_command = [
        str(python), "-m", "uvicorn", "webapp.backend.app.main:app",
        "--host", "127.0.0.1", "--port", str(args.backend_port),
    ]
    if args.reload:
        backend_command.append("--reload")
    frontend_command = [
        "npm.cmd" if os.name == "nt" else "npm",
        "run", "dev", "--", "--host", "127.0.0.1", "--port", str(args.frontend_port),
    ]

    backend = frontend = None
    try:
        print("Starting backend: {}".format(subprocess.list2cmdline(backend_command)), flush=True)
        backend = subprocess.Popen(backend_command, cwd=REPO_ROOT, env=environment, creationflags=creation_flags)
        wait_http("http://127.0.0.1:{}/api/health".format(args.backend_port), backend)
        time.sleep(0.5)
        if backend.poll() is not None:
            raise RuntimeError("new backend exited during startup with code {}".format(backend.returncode))
        print("Backend ready: http://127.0.0.1:{}".format(args.backend_port), flush=True)

        print("Starting frontend: {}".format(subprocess.list2cmdline(frontend_command)), flush=True)
        frontend = subprocess.Popen(frontend_command, cwd=FRONTEND_DIR, env=environment, creationflags=creation_flags)
        url = "http://127.0.0.1:{}".format(args.frontend_port)
        wait_http(url, frontend)
        print("Frontend ready: {}".format(url), flush=True)
        print("Press Ctrl+C to stop both services.", flush=True)
        if not args.no_browser:
            webbrowser.open(url)

        while True:
            if backend.poll() is not None:
                raise RuntimeError("backend exited with code {}".format(backend.returncode))
            if frontend.poll() is not None:
                raise RuntimeError("frontend exited with code {}".format(frontend.returncode))
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("Stopping services...", flush=True)
        return 0
    finally:
        terminate_tree(frontend)
        terminate_tree(backend)


if __name__ == "__main__":
    raise SystemExit(main())
