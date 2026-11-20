"""Where things are. Environment variables override the defaults (AGENTKIT_<NAME>)."""
import os
from pathlib import Path

V2_ROOT = Path(__file__).resolve().parent.parent          # .../SoulX-FlashHead/v2
REPO_ROOT = V2_ROOT.parent                                # v1 lives here and stays untouched


def _env(name, default):
    return os.environ.get("AGENTKIT_" + name, default)


AGENTS_DIR = Path(_env("AGENTS_DIR", V2_ROOT / "agents"))     # <agent_id>/versions/<n>/*.yaml
TOOLS_DIR = Path(_env("TOOLS_DIR", V2_ROOT / "tools"))        # <tool name>.yaml
DATA_DIR = Path(_env("DATA_DIR", V2_ROOT / "data"))           # trace.sqlite3, audio/, runs/
WEB_DIR = Path(_env("WEB_DIR", V2_ROOT / "web"))

LLM_URL = _env("LLM_URL", "http://127.0.0.1:11434")           # Ollama
LLM_MODEL = _env("LLM_MODEL", "gemma4:26b")
TTS_URL = _env("TTS_URL", "http://127.0.0.1:7788")            # supertonic serve
ASR_URL = _env("ASR_URL", "http://127.0.0.1:7790")            # realtime_asr_server.py (Qwen3-ASR)
WEKNORA_URL = _env("WEKNORA_URL", "http://127.0.0.1/api/v1")
WEKNORA_KEY_FILE = _env("WEKNORA_KEY_FILE", "~/.weknora_rt_key")
WHISPER_MODEL = _env("WHISPER_MODEL", "mobiuslabsgmbh/faster-whisper-large-v3-turbo")
SMART_TURN_MODEL = _env("SMART_TURN_MODEL", str(REPO_ROOT / "models/smart-turn/smart-turn-v3.2-cpu.onnx"))

# Seconds the video engine and the speech recogniser may sit unused before they are unloaded and
# their GPU memory given back (0: never). They load again on demand, or ahead of time through
# POST /api/engines/warmup.
ENGINE_IDLE_S = float(_env("ENGINE_IDLE_S", 1800))

SERVER_PORT = int(_env("PORT", 7871))                         # v1 keeps 7861
RETENTION_DAYS = int(_env("RETENTION_DAYS", 30))              # records older than this are purged (R-OBS-6)
TIMEZONE = _env("TIMEZONE", "Asia/Seoul")
