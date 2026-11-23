"""The speech synthesizer client: GPU server first, CPU server when it does not answer, 5 steps."""
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np
import soundfile as sf

import runtime_fakes  # noqa: F401

from agentkit.runtime.audio import TTSClient


def serve(bodies):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            bodies.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            out = io.BytesIO()
            sf.write(out, (np.sin(np.arange(4410) / 5) * 0.3).astype("float32"), 44100, format="WAV")
            data = out.getvalue()
            self.send_response(200); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

        def log_message(self, *args):
            pass
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_falls_back_to_the_cpu_server_and_asks_for_5_steps():
    bodies = []
    cpu = serve(bodies)
    client = TTSClient("http://127.0.0.1:9", fallback=f"http://127.0.0.1:{cpu.server_port}", steps=5)   # 9: nothing listens
    pcm = client.synthesize("안녕하세요!", "F1", "ko")
    assert len(pcm) > 0 and bodies[-1]["steps"] == 5 and bodies[-1]["voice"] == "F1"
    assert client._urls()[0].endswith(str(cpu.server_port))            # the GPU one is skipped for a while now
    cpu.shutdown()


def test_the_first_server_is_used_when_it_answers():
    gpu_bodies, cpu_bodies = [], []
    gpu, cpu = serve(gpu_bodies), serve(cpu_bodies)
    client = TTSClient(f"http://127.0.0.1:{gpu.server_port}", fallback=f"http://127.0.0.1:{cpu.server_port}", steps=5)
    client.synthesize("네.", "F1", "ko")
    assert len(gpu_bodies) == 1 and not cpu_bodies
    gpu.shutdown(); cpu.shutdown()
