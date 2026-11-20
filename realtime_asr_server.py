"""
Speech recognition service for realtime_mic_app.py, running Qwen3-ASR.

Kept in a process (and Python environment) of its own, like the TTS server: the `qwen-asr`
package pins its own transformers and torch, which must not disturb the avatar's environment.

    /data/miniforge3/envs/qwenasr/bin/python realtime_asr_server.py --port 7790
    # faster: the vLLM engine (its own environment; reserves GPU memory up front)
    /data/miniforge3/envs/qwenasr-vllm/bin/python realtime_asr_server.py --backend vllm --gpu_memory 0.25

POST /transcribe?language=ko|en|auto   body: raw 16-bit mono PCM at 16 kHz
  -> {"text": ..., "language": "ko" | "en", "ms": ...}
GET /health -> {"model": ...}
"""
import argparse
import time

import numpy as np
import torch
import uvicorn
from fastapi import FastAPI, Request
from qwen_asr import Qwen3ASRModel

NAMES = {"ko": "Korean", "en": "English"}
CODES = {name: code for code, name in NAMES.items()}


def create_app(args):
    if args.backend == "vllm":
        # vLLM takes its share of the GPU at start-up and keeps it: say how much
        model = Qwen3ASRModel.LLM(
            model=args.model, gpu_memory_utilization=args.gpu_memory,
            max_inference_batch_size=1, max_new_tokens=256,
        )
    else:
        model = Qwen3ASRModel.from_pretrained(
            args.model, dtype=torch.bfloat16, device_map=args.device,
            max_inference_batch_size=1, max_new_tokens=256,
        )
    # the first call compiles kernels; do it before the first real utterance
    model.transcribe(audio=(np.zeros(16000, dtype=np.float32), 16000), language="Korean")
    app = FastAPI()

    @app.get("/health")
    def health():
        return {"model": args.model, "backend": args.backend}

    @app.post("/transcribe")
    async def transcribe(request: Request, language: str = "ko"):
        audio = np.frombuffer(await request.body(), dtype="<i2").astype(np.float32) / 32768.0
        started = time.perf_counter()
        if language in NAMES:
            result = model.transcribe(audio=(audio, 16000), language=NAMES[language])[0]
            heard_in = language
        else:
            # auto: let the model name the language; anything but English is taken as Korean,
            # the only other language the rest of the pipeline speaks
            result = model.transcribe(audio=(audio, 16000), language=None)[0]
            heard_in = CODES.get(getattr(result, "language", None) or "", "ko")
            if getattr(result, "language", None) not in CODES:
                result = model.transcribe(audio=(audio, 16000), language="Korean")[0]
        return {"text": (result.text or "").strip(), "language": heard_in,
                "ms": (time.perf_counter() - started) * 1000}

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="Qwen/Qwen3-ASR-1.7B")
    parser.add_argument("--backend", default="transformers", choices=["transformers", "vllm"])
    parser.add_argument("--gpu_memory", type=float, default=0.25,
                        help="vLLM only: fraction of the GPU's memory to reserve.")
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7790)
    args = parser.parse_args()
    uvicorn.run(create_app(args), host=args.host, port=args.port, log_level="warning")
