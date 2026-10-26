"""Closed synthetic SMTP sink. Never forwards, never logs message data."""

import json
import socketserver
from threading import Event, Lock, Thread


class Sink(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int]) -> None:
        self.messages: list[str] = []
        self.mode = "accept"
        self.lock = Lock()
        self.release = Event()
        super().__init__(address, SMTPHandler)


class SMTPHandler(socketserver.StreamRequestHandler):
    server: Sink

    def handle(self) -> None:
        def reply(line: bytes) -> None:
            if self.server.mode == "slow":
                self.server.release.wait(0.04)
            self.wfile.write(line + b"\r\n")
            self.wfile.flush()

        reply(b"220 synthetic.invalid ESMTP")
        while line := self.rfile.readline(65537):
            command = line.upper()
            if command.startswith((b"EHLO", b"HELO")):
                reply(b"250 synthetic.invalid")
            elif command.startswith((b"MAIL", b"RCPT", b"RSET")):
                reply(b"250 OK")
            elif command.startswith(b"DATA"):
                if self.server.mode == "reject":
                    reply(b"451 synthetic failure")
                    continue
                reply(b"354 End with dot")
                data = []
                while (part := self.rfile.readline(65537)) not in (b".\r\n", b""):
                    data.append(part[1:] if part.startswith(b"..") else part)
                if self.server.mode == "timeout":
                    self.server.release.wait(30)
                    return
                with self.server.lock:
                    self.server.messages.append(b"".join(data).decode("ascii"))
                reply(b"250 Accepted")
            elif command.startswith(b"QUIT"):
                reply(b"221 Bye")
                return
            else:
                reply(b"500 Unsupported")


def start_sink() -> tuple[Sink, Thread]:
    sink = Sink(("127.0.0.1", 0))
    thread = Thread(target=sink.serve_forever, daemon=True)
    thread.start()
    return sink, thread


if __name__ == "__main__":
    sink = Sink(("0.0.0.0", 1025))

    class Control(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            command = json.loads(self.rfile.readline(1024))
            with sink.lock:
                if mode := command.get("mode"):
                    if mode not in {"accept", "reject", "timeout"}:
                        return
                    sink.mode = mode
                self.wfile.write(json.dumps(sink.messages).encode() + b"\n")

    control = socketserver.ThreadingTCPServer(("0.0.0.0", 1026), Control)
    Thread(target=control.serve_forever, daemon=True).start()
    sink.serve_forever()
