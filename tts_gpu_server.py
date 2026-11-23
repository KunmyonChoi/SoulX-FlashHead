"""supertonic serve on the GPU.

supertonic hard-codes the CPU (config.DEFAULT_ONNX_PROVIDERS = ["CPUExecutionProvider"]). On the CPU a reply's first
piece took 360-714 ms to synthesise at 5 steps; on an RTX 6000 Ada 23-27 ms (measured 2026-11-23). The avatar waits
for that piece, so this is most of what remained between the reply on the page and the avatar's first sound.

Needs an environment with supertonic and onnxruntime-gpu[cuda,cudnn] (here: conda env supertonic-gpu, a clone of
supertonic). Usage, same options as `supertonic serve`:

    CUDA_VISIBLE_DEVICES=3 /data/miniforge3/envs/supertonic-gpu/bin/python tts_gpu_server.py --port 7789
"""
import sys

import onnxruntime

onnxruntime.preload_dlls()          # the CUDA and cuDNN libraries pip installed into the environment

import supertonic.loader as loader  # noqa: E402

loader.DEFAULT_ONNX_PROVIDERS = ["CUDAExecutionProvider", "CPUExecutionProvider"]

from supertonic.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.argv = ["supertonic", "serve", *sys.argv[1:]]
    sys.exit(main())
