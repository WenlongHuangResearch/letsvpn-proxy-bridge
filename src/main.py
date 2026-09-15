from __future__ import annotations

import argparse
import http.client
import logging
import os
import queue
import re
import select
import socket
import subprocess
import sys
import threading
from http.client import HTTPMessage
from logging.handlers import RotatingFileHandler
from socketserver import ThreadingMixIn, TCPServer, StreamRequestHandler
from urllib.parse import urlsplit


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 10808
BUFFER_SIZE = 64 * 1024
CONNECT_TIMEOUT = 15


def configure_logging() -> logging.Logger:
    from pathlib import Path

    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
    logger = logging.getLogger("tricos-proxy")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        try:
            log_dir = base / "logs"
            log_dir.mkdir(exist_ok=True)
            handler = RotatingFileHandler(log_dir / "proxy.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
        except OSError:
            log_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "TRICOS Proxy" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(log_dir / "proxy.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        if sys.stderr is not None:
            console = logging.StreamHandler()
            console.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(console)
    return logger


def owner_details(port: int) -> tuple[str | None, str | None]:
    """Return (pid, process) for a listening local port when available."""
    try:
        output = subprocess.check_output(
            ["netstat", "-ano", "-p", "tcp"], text=True, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=5,
        )
        pattern = re.compile(rf"^\s*TCP\s+(?:127\.0\.0\.1|0\.0\.0\.0|\[::\]):{port}\s+\S+\s+LISTENING\s+(\d+)\s*$", re.I)
        pid = next((m.group(1) for line in output.splitlines() if (m := pattern.match(line))), None)
        if not pid:
            return None, None
        process = None
        try:
            ps = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", f"(Get-Process -Id {pid} -ErrorAction SilentlyContinue).ProcessName"],
                text=True, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=5,
            ).strip()
            process = f"{ps}.exe" if ps else None
        except Exception:
            pass
        return pid, process
    except Exception:
        return None, None


def is_probably_this_program(pid: str | None, process: str | None) -> bool:
    if not pid:
        return False
    name = (process or "").lower()
    if "proxylauncher" in name or name in {"main.exe", "python.exe", "pythonw.exe"}:
        try:
            cmd = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", f"(Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}').CommandLine"],
                text=True, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=5,
            ).lower()
            return "proxylauncher" in cmd or "src\\main.py" in cmd or "src/main.py" in cmd
        except Exception:
            return "proxylauncher" in name
    return False


def relay(left: socket.socket, right: socket.socket) -> None:
    sockets = [left, right]
    try:
        while True:
            readable, _, exceptional = select.select(sockets, [], sockets, 60)
            if exceptional or not readable:
                if not readable:
                    continue
                return
            for source in readable:
                data = source.recv(BUFFER_SIZE)
                if not data:
                    return
                target = right if source is left else left
                target.sendall(data)
    except (ConnectionError, OSError):
        return


class ProxyHandler(StreamRequestHandler):
    timeout = 30
    # Do not read tunnel bytes ahead into a file buffer before socket relay.
    rbufsize = 0

    def _read_headers(self) -> tuple[str, str, str, list[tuple[str, str]]] | None:
        line = self.rfile.readline(65536)
        if not line:
            return None
        try:
            method, target, version = line.decode("iso-8859-1").strip().split(" ", 2)
        except ValueError:
            self.wfile.write(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
            return None
        message = HTTPMessage()
        while True:
            header = self.rfile.readline(65536)
            if not header or header in (b"\r\n", b"\n"):
                break
            try:
                key, value = header.decode("iso-8859-1").split(":", 1)
                message[key] = value.strip()
            except ValueError:
                continue
        return method.upper(), target, version, list(message.items())

    def handle(self) -> None:
        request = self._read_headers()
        if request is None:
            return
        method, target, version, headers = request
        try:
            if method == "CONNECT":
                self._handle_connect(target)
            else:
                self._handle_http(method, target, version, headers)
        except Exception as exc:
            self.server.logger.info("request failed %s %s: %s", method, target, exc)
            try:
                self.wfile.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            except OSError:
                pass

    def _connect_target(self, host: str, port: int) -> socket.socket:
        remote = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
        remote.settimeout(None)
        return remote

    def _handle_connect(self, target: str) -> None:
        host, _, port_text = target.rpartition(":")
        if not host or not port_text.isdigit():
            self.wfile.write(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
            return
        remote = self._connect_target(host.strip("[]"), int(port_text))
        try:
            self.wfile.write(b"HTTP/1.1 200 Connection established\r\nProxy-Agent: TRICOS-Proxy\r\n\r\n")
            self.wfile.flush()
            relay(self.connection, remote)
        finally:
            remote.close()

    def _handle_http(self, method: str, target: str, version: str, headers: list[tuple[str, str]]) -> None:
        parsed = urlsplit(target)
        if parsed.scheme and parsed.hostname:
            host = parsed.hostname
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
        else:
            host_header = next((v for k, v in headers if k.lower() == "host"), "")
            host, _, port_text = host_header.rpartition(":")
            if not host:
                host = host_header
            port = int(port_text) if port_text.isdigit() else 80
            path = target or "/"
        remote = self._connect_target(host, port)
        try:
            filtered = [(k, v) for k, v in headers if k.lower() not in {"proxy-connection", "connection", "keep-alive"}]
            if not any(k.lower() == "host" for k, _ in filtered):
                filtered.append(("Host", host))
            request = f"{method} {path} {version}\r\n".encode("iso-8859-1")
            request += b"".join(f"{k}: {v}\r\n".encode("iso-8859-1") for k, v in filtered)
            request += b"Connection: close\r\n\r\n"
            remote.sendall(request)
            content_length = next((int(v) for k, v in headers if k.lower() == "content-length" and v.isdigit()), 0)
            while content_length:
                data = self.rfile.read(min(BUFFER_SIZE, content_length))
                if not data:
                    break
                remote.sendall(data)
                content_length -= len(data)
            relay(self.connection, remote)
        finally:
            remote.close()


class ProxyServer(ThreadingMixIn, TCPServer):
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, address: tuple[str, int], logger: logging.Logger):
        if address[0] != DEFAULT_HOST:
            raise ValueError("Only 127.0.0.1 is allowed.")
        self.logger = logger
        super().__init__(address, ProxyHandler)

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def internet_test(host: str, port: int, url: str, logger: logging.Logger) -> bool:
    connection = None
    try:
        target = urlsplit(url)
        if target.scheme not in {"http", "https"} or not target.hostname:
            raise ValueError("Test URL must be an HTTP or HTTPS URL")
        if target.scheme == "https":
            connection = http.client.HTTPSConnection(host, port, timeout=10)
            connection.set_tunnel(target.hostname, target.port or 443)
            path = target.path or "/"
            if target.query:
                path += "?" + target.query
        else:
            connection = http.client.HTTPConnection(host, port, timeout=10)
            path = url
        # Explicit local proxy connection; environment proxy bypass rules cannot skip it.
        connection.request("HEAD", path, headers={"Host": target.netloc, "Connection": "close"})
        with connection.getresponse() as response:
            logger.info("Internet Test HTTP status: %s", response.status)
            return 200 <= response.status < 400
    except Exception as exc:
        logger.info("internet test error: %s", exc)
        return False
    finally:
        if connection is not None:
            connection.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TRICOS localhost HTTP proxy")
    parser.add_argument("--host", choices=[DEFAULT_HOST], default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=int(os.environ.get("TRICOS_PROXY_PORT", DEFAULT_PORT)))
    parser.add_argument("--test-url", default="https://www.google.com")
    parser.add_argument("--no-test", action="store_true", help="skip the startup Internet test")
    parser.add_argument("--console", action="store_true", help="run the text console instead of the GUI")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535")
    return args


def console_main(args: argparse.Namespace, logger: logging.Logger) -> int:
    try:
        server = ProxyServer((args.host, args.port), logger)
    except OSError as exc:
        pid, process = owner_details(args.port)
        if is_probably_this_program(pid, process):
            print(f"Proxy is already running on {args.host}:{args.port}")
            logger.info("already running on %s:%s (PID %s)", args.host, args.port, pid)
        else:
            print(f"Port {args.port} is already in use.")
            if pid:
                print(f"PID: {pid}")
            if process:
                print(f"Process: {process}")
            logger.error("cannot bind %s:%s: %s (PID=%s Process=%s)", args.host, args.port, exc, pid, process)
        return 2

    logger.info("starting proxy on %s:%s", args.host, args.port)
    print("================================")
    print("TRICOS Proxy")
    print("================================")
    print(f"Proxy: {args.host}:{args.port}")
    print("Status: Running")
    print(f"Listen: {args.host}:{args.port}")
    serve_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    serve_thread.start()
    test_ok = True if args.no_test else internet_test(args.host, args.port, args.test_url, logger)
    print(f"Internet: {'Connected' if test_ok else 'FAILED'}")
    logger.info("Internet Test: %s", "OK" if test_ok else "FAILED")
    if not test_ok:
        print("Please make sure LetsVPN is connected.")
    print("Press Ctrl+C to stop.")
    try:
        while serve_thread.is_alive():
            serve_thread.join(timeout=1)
    except KeyboardInterrupt:
        print("Stopping proxy...")
    finally:
        server.shutdown()
        server.server_close()
        logger.info("proxy stopped on %s:%s", args.host, args.port)
    return 0


class ProxyApplication:
    """All Tk operations stay on the UI thread, including test completion."""

    def __init__(self, root, args: argparse.Namespace, logger: logging.Logger):
        import tkinter as tk
        from tkinter import ttk

        self.root, self.args, self.logger = root, args, logger
        self.server = None
        self.serve_thread = None
        self.closing = False
        self.testing = False
        self.events = queue.Queue()
        root.title("TRICOS Proxy")
        root.resizable(True, True)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.report_callback_exception = self.report_error
        self.status = tk.StringVar(value="Starting...")
        self.internet = tk.StringVar(value="Waiting for proxy")
        self.detail = tk.StringVar(value="Minimize to keep the proxy running. Close to stop it.")

        frame = ttk.Frame(root, padding=24)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text="TRICOS Proxy", font=("Segoe UI", 17, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 20))
        for row, title in enumerate(("Status", "Listen", "Internet"), 1):
            ttk.Label(frame, text=title, font=("Segoe UI", 10, "bold")).grid(row=row, column=0, sticky="nw", padx=(0, 24), pady=6)
        self.status_label = ttk.Label(frame, textvariable=self.status)
        self.status_label.grid(row=1, column=1, sticky="w", pady=6)
        ttk.Label(frame, text=f"{args.host}:{args.port}").grid(row=2, column=1, sticky="w", pady=6)
        self.internet_label = ttk.Label(frame, textvariable=self.internet)
        self.internet_label.grid(row=3, column=1, sticky="w", pady=6)
        ttk.Separator(frame).grid(row=4, column=0, columnspan=2, sticky="ew", pady=16)
        self.detail_label = ttk.Label(frame, textvariable=self.detail, wraplength=400, justify="left")
        self.detail_label.grid(row=5, column=0, columnspan=2, sticky="new")
        frame.rowconfigure(5, weight=1)
        buttons = ttk.Frame(frame)
        buttons.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(20, 0))
        self.test_button = ttk.Button(buttons, text="Test Connection", command=self.test_connection, state="disabled")
        self.test_button.pack(side="left")
        ttk.Button(buttons, text="Minimize", command=root.iconify).pack(side="left", padx=8)
        self.stop_button = ttk.Button(buttons, text="Stop Proxy", command=self.close)
        self.stop_button.pack(side="right")
        # Size from actual font/DPI requirements rather than clipping a fixed 250px window.
        root.update_idletasks()
        width, height = max(460, root.winfo_reqwidth()), max(340, root.winfo_reqheight() + 50)
        root.minsize(width, height)
        root.geometry(f"{width}x{height}")
        root.after(50, self.start)
        self.poll_id = root.after(100, self.poll_events)

    def start(self):
        if self.closing:
            return
        try:
            self.server = ProxyServer((self.args.host, self.args.port), self.logger)
            self.serve_thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
            self.serve_thread.start()
            self.status.set("Running")
            self.status_label.configure(foreground="#16803c")
            self.logger.info("starting proxy on %s:%s", self.args.host, self.args.port)
            self.test_button.configure(state="normal")
            if self.args.no_test:
                self.internet.set("Not tested")
            else:
                self.test_connection()
        except OSError as exc:
            self.status.set("Unable to start")
            self.status_label.configure(foreground="#b42318")
            self.internet.set("Not tested")
            self.stop_button.configure(text="Close")
            self.detail.set("Checking port ownership...")
            self.logger.exception("cannot bind %s:%s", self.args.host, self.args.port)
            threading.Thread(target=self.describe_bind_error, args=(str(exc),), daemon=True).start()

    def describe_bind_error(self, error):
        pid, process = owner_details(self.args.port)
        own = is_probably_this_program(pid, process)
        if own:
            detail = f"Proxy is already running on {self.args.host}:{self.args.port}"
            status = "Already running"
        elif pid:
            detail = f"Port {self.args.port} is already in use."
            status = "Port in use"
        else:
            detail = f"Unable to listen on {self.args.host}:{self.args.port}.\n{error}"
            status = "Unable to start"
        if pid:
            detail += f"\nPID: {pid}\nProcess: {process or 'Unknown'}"
        self.events.put(("bind_error", (status, detail)))

    def test_connection(self):
        if self.server is None or self.closing or self.testing:
            return
        self.testing = True
        self.test_button.configure(state="disabled")
        self.internet.set("Testing...")
        self.internet_label.configure(foreground="#777777")
        threading.Thread(target=self.test_worker, daemon=True).start()

    def test_worker(self):
        ok = internet_test(self.args.host, self.args.port, self.args.test_url, self.logger)
        self.logger.info("Internet Test: %s", "OK" if ok else "FAILED")
        self.events.put(("test", ok))

    def poll_events(self):
        if self.closing:
            return
        while not self.events.empty():
            kind, result = self.events.get_nowait()
            if kind == "bind_error":
                self.status.set(result[0])
                self.detail.set(result[1])
            elif kind == "test":
                self.testing = False
                self.test_button.configure(state="normal")
                self.internet.set("Connected" if result else "FAILED")
                self.internet_label.configure(foreground="#16803c" if result else "#b42318")
                self.detail.set("Minimize to keep the proxy running. Close to stop it." if result else "Proxy is running. Internet test failed.\nPlease make sure LetsVPN is connected, then test again.")
        self.poll_id = self.root.after(100, self.poll_events)

    def report_error(self, exc_type, exc, traceback):
        from tkinter import messagebox
        self.logger.error("GUI error", exc_info=(exc_type, exc, traceback))
        messagebox.showerror("TRICOS Proxy", f"An error occurred: {exc}\nSee proxy.log for details.", parent=self.root)

    def close(self):
        if self.closing:
            return
        self.closing = True
        self.root.after_cancel(self.poll_id)
        if self.server is not None:
            if self.serve_thread is not None and self.serve_thread.is_alive():
                self.server.shutdown()
                self.serve_thread.join(timeout=2)
            self.server.server_close()
            self.logger.info("proxy stopped on %s:%s", self.args.host, self.args.port)
        self.root.destroy()


def gui_main(args: argparse.Namespace, logger: logging.Logger) -> int:
    import tkinter as tk
    root = tk.Tk()
    app = ProxyApplication(root, args, logger)
    try:
        root.mainloop()
    finally:
        app.close()
    return 0


def main() -> int:
    args = parse_args()
    if args.console and sys.stdout is None and os.name == "nt":
        import ctypes
        # --windowed has no streams; explicitly requested console mode needs them.
        if not ctypes.windll.kernel32.AttachConsole(-1):
            ctypes.windll.kernel32.AllocConsole()
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", buffering=1)
        sys.stdin = open("CONIN$", "r", encoding="utf-8")
    logger = configure_logging()
    if args.console:
        return console_main(args, logger)
    return gui_main(args, logger)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        logging.getLogger("tricos-proxy").exception("fatal startup error")
        if os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, f"TRICOS Proxy could not start.\n\n{exc}\n\nSee logs/proxy.log for details.", "TRICOS Proxy", 0x10)
        else:
            raise
