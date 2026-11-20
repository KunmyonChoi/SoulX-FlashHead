"""Records of conversations with personal data masked (WP3). See ARCHITECTURE.md section 8.

    from agentkit.trace import get_recorder, get_store, get_masker
    recorder = get_recorder()        # contracts.Recorder, one per process
    store = get_store()              # sessions, metrics, documents, purge
    masker = get_masker()            # contracts.Masker

The three are bound to the data directory at first use: the environment variable
AGENTKIT_DATA_DIR if it is set at that moment, otherwise config.DATA_DIR. Tests call
`reset(tmp_path)` to point everything at a temporary directory.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Optional

from .bus import Bus
from .masker import RegexMasker, mask_text
from .recorder import TraceRecorder, install_exit_flush
from .store import Document, TraceStore, analysis_summary

__all__ = [
    "get_recorder", "get_store", "get_masker", "get_bus", "reset",
    "TraceRecorder", "TraceStore", "RegexMasker", "Bus", "Document", "mask_text", "analysis_summary",
]

_lock = threading.RLock()
_masker = RegexMasker()
_store: Optional[TraceStore] = None
_recorder: Optional[TraceRecorder] = None
_forced_dir: Optional[Path] = None


def _data_dir() -> Path:
    if _forced_dir is not None:
        return _forced_dir
    env = os.environ.get("AGENTKIT_DATA_DIR")
    if env:
        return Path(env)
    from agentkit import config
    return Path(config.DATA_DIR)


def get_masker() -> RegexMasker:
    return _masker


def get_store() -> TraceStore:
    global _store
    with _lock:
        if _store is None:
            _store = TraceStore(_data_dir(), masker=_masker)
        return _store


def get_recorder() -> TraceRecorder:
    global _recorder
    with _lock:
        if _recorder is None:
            _recorder = TraceRecorder(get_store())
            install_exit_flush(_recorder)
        return _recorder


def get_bus() -> Bus:
    """Live events of the current store (see api.py, /api/live)."""
    return get_store().bus


def reset(data_dir: Optional[os.PathLike | str] = None) -> None:
    """For tests: writes out and stops the current recorder and forgets the singletons. The next
    get_store()/get_recorder() binds to `data_dir`, or (None) to AGENTKIT_DATA_DIR / config.DATA_DIR."""
    global _store, _recorder, _forced_dir
    with _lock:
        if _recorder is not None:
            _recorder.close(2.0)
        _recorder = None
        _store = None
        _forced_dir = Path(data_dir) if data_dir is not None else None
