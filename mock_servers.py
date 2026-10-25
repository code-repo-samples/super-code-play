#!/usr/bin/env python3
"""
mock_servers.py - local SFTP + HTTP servers for smoke-testing sftp_load.py without touching real systems.

    python mock_servers.py            # SFTP on 127.0.0.1:2222 (user test / password test), HTTP on 127.0.0.1:8080
    python mock_servers.py --root mock_remote

Then point .env at it:  SFTP_HOST=127.0.0.1  SFTP_PORT=2222  SFTP_USER=test  SFTP_PASSWORD=test
and use http://127.0.0.1:8080/anything as an http_url.  Uploaded files land under the --root folder.
Needs: pip install paramiko
"""
import argparse
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import paramiko


class Auth(paramiko.ServerInterface):
    def check_auth_password(self, u, p):
        return paramiko.AUTH_SUCCESSFUL if (u, p) == ("test", "test") else paramiko.AUTH_FAILED

    def get_allowed_auths(self, u):
        return "password"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


class Handle(paramiko.SFTPHandle):
    def stat(self):
        try:
            return paramiko.SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def chattr(self, attr):
        return paramiko.SFTP_OK


class Stub(paramiko.SFTPServerInterface):
    ROOT = "."

    def _p(self, path):
        return os.path.join(self.ROOT, path.lstrip("/"))

    def canonicalize(self, path):
        return "/" + path.lstrip("/")

    def list_folder(self, path):
        try:
            return [paramiko.SFTPAttributes.from_stat(os.stat(os.path.join(self._p(path), f)), f)
                    for f in os.listdir(self._p(path))]
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def stat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(os.stat(self._p(path)))
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    lstat = stat

    def open(self, path, flags, attr):
        p = self._p(path)
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            fd = os.open(p, flags | getattr(os, "O_BINARY", 0), 0o644)
            mode = "wb" if flags & os.O_WRONLY else ("r+b" if flags & os.O_RDWR else "rb")
            f = os.fdopen(fd, mode)
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)
        h = Handle(flags)
        h.filename, h.readfile, h.writefile = p, f, f
        return h

    def remove(self, path):
        try:
            os.remove(self._p(path))
            return paramiko.SFTP_OK
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def mkdir(self, path, attr):
        os.makedirs(self._p(path), exist_ok=True)
        return paramiko.SFTP_OK


def sftp_server(port, key):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen(50)

    def serve(conn):
        t = paramiko.Transport(conn)
        t.add_server_key(key)
        t.set_subsystem_handler("sftp", paramiko.SFTPServer, Stub)
        t.start_server(server=Auth())

    while True:
        c, _ = s.accept()
        threading.Thread(target=serve, args=(c,), daemon=True).start()


class Http(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(f'{{"ok":true,"bytes":{len(body)}}}'.encode())

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="mock_remote")
    ap.add_argument("--sftp-port", type=int, default=2222)
    ap.add_argument("--http-port", type=int, default=8080)
    a = ap.parse_args()
    Stub.ROOT = os.path.abspath(a.root)
    os.makedirs(Stub.ROOT, exist_ok=True)
    key = paramiko.RSAKey.generate(2048)
    threading.Thread(target=sftp_server, args=(a.sftp_port, key), daemon=True).start()
    print(f"SFTP 127.0.0.1:{a.sftp_port} (test/test) root={Stub.ROOT}\nHTTP 127.0.0.1:{a.http_port}\nCtrl+C to stop")
    ThreadingHTTPServer(("127.0.0.1", a.http_port), Http).serve_forever()


if __name__ == "__main__":
    main()
