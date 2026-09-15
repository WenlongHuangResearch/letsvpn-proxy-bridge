import http.client
import logging
import socket
import sys
import threading
import unittest
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from main import ProxyServer, internet_test


class Origin(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(503 if self.path == "/fail" else 204)
        self.end_headers()

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.log = logging.getLogger("proxy-tests")
        self.log.addHandler(logging.NullHandler())
        # The OS chooses temporary ports. Tests never bind 10808.
        self.origin = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
        self.proxy = ProxyServer(("127.0.0.1", 0), self.log)
        self.threads = []
        for server in (self.origin, self.proxy):
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
            thread.start()
            self.threads.append(thread)
        self.port = self.proxy.server_address[1]
        self.origin_port = self.origin.server_address[1]

    def tearDown(self):
        for server in (self.proxy, self.origin):
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(timeout=2)

    def test_health_check_runs_through_proxy(self):
        url = f"http://127.0.0.1:{self.origin_port}/"
        self.assertTrue(internet_test("127.0.0.1", self.port, url, self.log))
        # Origin still works, but a stopped local proxy must cause test failure.
        self.proxy.shutdown()
        self.proxy.server_close()
        self.assertFalse(internet_test("127.0.0.1", self.port, url, self.log))

    def test_failed_internet_does_not_stop_proxy(self):
        self.assertFalse(internet_test("127.0.0.1", self.port, f"http://127.0.0.1:{self.origin_port}/fail", self.log))
        self.assertTrue(internet_test("127.0.0.1", self.port, f"http://127.0.0.1:{self.origin_port}/", self.log))

    def test_http_post_body(self):
        with closing(http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)) as connection:
            payload = b"proxy-body-test" * 10000
            connection.request("POST", f"http://127.0.0.1:{self.origin_port}/", payload)
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), payload)

    def test_connect_tunnel_with_early_payload(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=3) as connection:
            connection.settimeout(3)
            # Send data together with CONNECT; parser must not swallow tunnel bytes.
            connection.sendall((f"CONNECT 127.0.0.1:{self.origin_port} HTTP/1.1\r\n\r\n"
                                "HEAD / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n").encode())
            data = b""
            while chunk := connection.recv(4096):
                data += chunk
            self.assertIn(b"200 Connection established", data)
            self.assertIn(b"204 No Content", data)

    def test_conflict_preserves_existing_listener(self):
        with self.assertRaises(OSError):
            ProxyServer(("127.0.0.1", self.port), self.log)
        self.assertTrue(internet_test("127.0.0.1", self.port, f"http://127.0.0.1:{self.origin_port}/", self.log))

    def test_close_releases_port(self):
        self.proxy.shutdown()
        self.proxy.server_close()
        with ProxyServer(("127.0.0.1", self.port), self.log) as replacement:
            self.assertEqual(replacement.server_address, ("127.0.0.1", self.port))

    def test_rejects_non_loopback_binding(self):
        with self.assertRaisesRegex(ValueError, "127.0.0.1"):
            ProxyServer(("0.0.0.0", 0), self.log)


if __name__ == "__main__":
    unittest.main()
