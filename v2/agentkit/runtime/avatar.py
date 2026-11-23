"""
The video engine of the avatar channel: v1's `Engine` (FlashHead). Loading takes about 45 s (11 s to
read the model, 34 s to compile on the first chunks), so it is not done at start-up. It is loaded
when someone asks for it ahead of time (`warm()`, the "엔진 준비" button) or, failing that, by the
first avatar session; and it is unloaded again (`unload()`) by hand or after it sat unused for
config.ENGINE_IDLE_S, which gives its GPU memory back.

There is one engine and it carries one conversation's motion state, so the avatar channel takes one
session at a time: `acquire()` raises AvatarBusy while another session holds it. Voice-only sessions
never touch this module.

Settings (environment, all optional):
  AGENTKIT_AVATAR_CKPT        default <repo>/models/SoulX-FlashHead-1_3B
  AGENTKIT_AVATAR_WAV2VEC     default <repo>/models/wav2vec2-base-960h
  AGENTKIT_AVATAR_MODEL_TYPE  default lite  (only lite is faster than real time on one GPU)
  AGENTKIT_AVATAR_IMAGE       default examples/girl.png (used when the definition names no image)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import config

logger = logging.getLogger("agentkit.voice")

DEFAULT_IMAGE = "examples/girl.png"




def _free_gpu(why):
    """Gives back the GPU memory nothing refers to any more (compiled graphs keep models alive until reset)."""
    import gc
    try:
        import torch
        before = torch.cuda.memory_reserved()
        try:
            torch._dynamo.reset()
        except Exception:
            pass
        gc.collect()
        torch.cuda.empty_cache()
        logger.info("GPU memory %s: %.1f GB -> %.1f GB held by this process.", why, before / 2**30,
                    torch.cuda.memory_reserved() / 2**30)
    except Exception as e:
        gc.collect()
        logger.info("GPU memory %s (%s).", why, e)

class AvatarBusy(Exception):
    """Another avatar session is using the video engine."""


class AvatarUnavailable(Exception):
    """The video engine could not be loaded."""


def _env(name, default):
    return os.environ.get("AGENTKIT_AVATAR_" + name, default)


def _repo_path(path):
    path = Path(path)
    return str(path if path.is_absolute() else config.REPO_ROOT / path)


def _load_engine(image):
    """Runs on the worker thread. Imports v1 and builds its Engine (loads and warms up the model)."""
    root = str(config.REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    # flash_head.inference opens "flash_head/configs/infer_params.yaml" relative to the working
    # directory when it is imported, so the import has to happen from the repository root.
    previous = os.getcwd()
    os.chdir(root)
    try:
        from realtime_mic_app import Engine
    finally:
        os.chdir(previous)
    args = argparse.Namespace(
        ckpt_dir=_repo_path(_env("CKPT", "models/SoulX-FlashHead-1_3B")),
        wav2vec_dir=_repo_path(_env("WAV2VEC", "models/wav2vec2-base-960h")),
        model_type=_env("MODEL_TYPE", "lite"),
        cond_image=image,
        seed=9999,
        jpeg_quality=85,
        gate_db=None,
    )
    return Engine(args)


class AvatarHolder:
    """Process-wide owner of the video engine. All engine calls run on one worker thread."""

    def __init__(self, loader=_load_engine):
        self._loader = loader
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agentkit-avatar")
        self._guard = threading.Lock()
        self._busy = False
        self._error = None
        self._load = None       # concurrent Future of the load in progress (shared by warm() and acquire())
        self._load_started = None
        self.load_seconds = None    # how long the last load took
        self.last_used = None       # when it was loaded or a session let go of it
        self.engine = None
        self.image = None       # the reference image the engine currently shows

    @property
    def busy(self):
        return self._busy

    @property
    def loaded(self):
        return self.engine is not None

    async def _on_worker(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(self._pool, fn, *args)

    # ---- loading and unloading ------------------------------------------------------------------

    def _do_load(self, image):
        """Worker thread. One load at a time; whoever waits for it shares this call."""
        try:
            engine = self._loader(image)
        except BaseException as e:
            message = str(e) or type(e).__name__
            with self._guard:
                self._load, self._error = None, message
            # The traceback holds the frames of the half-built engine, and the Future would keep the exception:
            # a load that ran out of memory left 8 GB on the GPU with the state "unloaded" (2026-11-23). Drop it,
            # give the memory back, and raise a plain error with the message only.
            e.__traceback__ = None
            del e
            _free_gpu("after a failed load")
            raise AvatarUnavailable(message) from None
        with self._guard:
            self.engine, self.image = engine, image
            self.load_seconds = time.time() - self._load_started
            self.last_used = time.time()
            self._load, self._error = None, None
        logger.info("Video engine ready in %.0f s.", self.load_seconds)
        # Nothing is returned and the local is dropped: the finished Future (and this frame, while the
        # pool still refers to it) would otherwise keep the engine, and unload() could not free it
        # (measured: 4 GB of an 8 GB engine stayed on the GPU).
        engine = None

    def _start_load(self, image):
        with self._guard:
            if self.engine is not None:
                return None
            if self._load is None:
                logger.info("Loading the video engine...")
                self._load_started = time.time()
                self._load = self._pool.submit(self._do_load, image)
            return self._load

    def warm(self, image=None):
        """Starts loading ahead of the first conversation and returns at once (see status()).
        Does nothing if the engine is loaded or already loading. Raises AvatarUnavailable for a
        missing image."""
        image = _repo_path(image or _env("IMAGE", DEFAULT_IMAGE))
        if not os.path.exists(image):
            raise AvatarUnavailable(f"아바타 이미지를 찾을 수 없습니다: {image}")
        self._start_load(image)
        return self.status()

    def unload(self):
        """Drops the engine and gives its GPU memory back. False (and nothing happens) while a
        session holds it or it is still loading."""
        with self._guard:
            if self._busy or self._load is not None or self.engine is None:
                return False
            self.engine, self.image, self.last_used = None, None, None

        def free():
            # On the worker thread, after the last reference above is gone. The compiled graphs keep
            # the model alive until the compiler's caches are reset; only then can the memory go back.
            import gc
            try:
                import torch
                before = torch.cuda.memory_reserved()
                try:
                    torch._dynamo.reset()
                except Exception:
                    pass
                gc.collect()
                torch.cuda.empty_cache()
                logger.info("Video engine unloaded: GPU memory held by this process %.1f GB -> %.1f GB (%.1f GB still in use by tensors).",
                            before / 2**30, torch.cuda.memory_reserved() / 2**30, torch.cuda.memory_allocated() / 2**30)
            except Exception as e:
                gc.collect()
                logger.info("Video engine unloaded (%s).", e)
        self._pool.submit(free)
        return True

    def unload_if_idle(self, limit_s):
        """Unloads an engine nobody has used for limit_s seconds (0 or less: never)."""
        with self._guard:
            idle = self.engine is not None and not self._busy and self.last_used is not None \
                and limit_s > 0 and time.time() - self.last_used >= limit_s
        return self.unload() if idle else False

    def status(self):
        """{"state": "unloaded" | "loading" | "ready" | "in_use", ...} for the page and for callers of the API."""
        with self._guard:
            now = time.time()
            if self._load is not None:
                return {"state": "loading", "loading_s": round(now - self._load_started, 1),
                        "expected_s": round(self.load_seconds or 45)}
            if self.engine is None:
                return {"state": "unloaded", "expected_s": round(self.load_seconds or 45), "error": self._error}
            return {"state": "in_use" if self._busy else "ready", "load_s": round(self.load_seconds or 0, 1),
                    "idle_s": None if self._busy or self.last_used is None else round(now - self.last_used)}

    async def acquire(self, image=None):
        """Takes the engine for one session, loading it if this is the first avatar session.

        image: path of the reference image, relative to the repository root (None: the default).
        Raises AvatarBusy if a session holds the engine (also while it is still loading for that
        session), AvatarUnavailable if loading fails."""
        with self._guard:
            if self._busy:
                raise AvatarBusy("아바타는 한 번에 한 대화만 받을 수 있습니다. 다른 대화가 사용 중입니다.")
            self._busy = True
        try:
            image = _repo_path(image or _env("IMAGE", DEFAULT_IMAGE))
            if not os.path.exists(image):
                raise AvatarUnavailable(f"아바타 이미지를 찾을 수 없습니다: {image}")
            pending = self._start_load(image)      # None if it is loaded; else the load (ours or warm()'s)
            if pending is not None:
                await asyncio.wrap_future(pending)
            if image != self.image:
                await self._on_worker(self.engine.set_image, image, False)
                self.image = image
            await self._on_worker(self.engine.reset)
        except AvatarUnavailable:
            self._busy = False
            raise
        except BaseException as e:
            self._busy = False
            if isinstance(e, Exception):
                logger.warning("Video engine unavailable: %r", e)
                raise AvatarUnavailable(f"영상 엔진을 올리지 못했습니다: {e}") from e
            raise
        return AvatarSession(self)

    def _release(self):
        self._busy = False
        self.last_used = time.time()


class AvatarSession:
    """What one avatar conversation holds: the engine's geometry and `step`."""

    def __init__(self, holder):
        self._holder = holder
        engine = holder.engine
        self.sample_rate = engine.sample_rate
        self.fps = engine.fps
        self.width, self.height = engine.width, engine.height
        self.slice_frames = engine.slice_frames
        self.slice_samples = engine.slice_samples
        self.released = False

    async def step(self, audio):
        """audio: float32 [-1, 1], exactly slice_samples long. Returns one JPEG per frame."""
        return await self._holder._on_worker(self._holder.engine.step, audio)

    def release(self):
        if not self.released:
            self.released = True
            self._holder._release()


_holder = None
_holder_lock = threading.Lock()


def holder():
    """The process-wide AvatarHolder."""
    global _holder
    with _holder_lock:
        if _holder is None:
            _holder = AvatarHolder()
        return _holder
