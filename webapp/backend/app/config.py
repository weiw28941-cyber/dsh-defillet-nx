from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any


BACKEND_DIR = Path(__file__).resolve().parents[1]
WEBAPP_DIR = BACKEND_DIR.parent
REPO_ROOT = WEBAPP_DIR.parent


def _opencode_provider() -> dict[str, Any]:
    configured_path = os.getenv("OPENCODE_CONFIG_PATH", "").strip()
    if not configured_path:
        return {}
    path = Path(configured_path).expanduser()
    if not path.is_file():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        model_ref = str(document.get("model") or "")
        provider_name, _, model_name = model_ref.partition("/")
        provider = (document.get("provider") or {}).get(provider_name) or {}
        options = provider.get("options") or {}
        return {
            "path": str(path.resolve()),
            "provider": provider_name,
            "base_url": str(options.get("baseURL") or ""),
            "api_key": str(options.get("apiKey") or ""),
            "model": model_name,
            "headers": {str(key): str(value) for key, value in (options.get("headers") or {}).items()},
        }
    except (OSError, ValueError, TypeError):
        return {}


_OPENCODE = _opencode_provider()


@dataclass(frozen=True)
class Settings:
    jobs_root: Path = Path(os.getenv("STEP_WEB_JOBS_ROOT", WEBAPP_DIR / "jobs"))
    max_upload_bytes: int = int(os.getenv("STEP_WEB_MAX_UPLOAD_BYTES", str(100 * 1024 * 1024)))
    openai_base_url: str = os.getenv("OPENAI_BASE_URL", _OPENCODE.get("base_url", "https://api.openai.com/v1")).rstrip("/")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", _OPENCODE.get("api_key", ""))
    openai_model: str = os.getenv("OPENAI_MODEL", _OPENCODE.get("model", ""))
    openai_headers: dict[str, str] | None = None
    opencode_config_path: str = _OPENCODE.get("path", "")
    nx_base: Path = Path(os.getenv("UGII_BASE_DIR") or Path(os.getenv("ProgramFiles", "C:/Program Files")) / "Siemens" / "NX2406")
    nx_timeout_seconds: int = int(os.getenv("STEP_WEB_NX_TIMEOUT_SECONDS", "600"))
    nx_global_timeout_seconds: int = int(os.getenv("STEP_WEB_NX_GLOBAL_TIMEOUT_SECONDS", "3600"))
    max_delete_attempts: int = max(1, min(12, int(os.getenv("STEP_WEB_MAX_DELETE_ATTEMPTS", "6"))))
    mesh_deflection: float = float(os.getenv("STEP_WEB_MESH_DEFLECTION", "0.25"))
    mesh_angle: float = float(os.getenv("STEP_WEB_MESH_ANGLE", "0.5"))

    @property
    def run_journal(self) -> Path:
        override = os.getenv("NX_RUN_JOURNAL", "").strip()
        return Path(override) if override else self.nx_base / "NXBIN" / "run_journal.exe"

    @property
    def planner_headers(self) -> dict[str, str]:
        headers = dict(_OPENCODE.get("headers") or {})
        if os.getenv("OPENAI_AIMP_BIZ_ID"):
            headers["Aimp-Biz-Id"] = os.environ["OPENAI_AIMP_BIZ_ID"]
        if os.getenv("OPENAI_AIGC_USER"):
            headers["AIGC-USER"] = os.environ["OPENAI_AIGC_USER"]
        return headers


settings = Settings()
settings.jobs_root.mkdir(parents=True, exist_ok=True)
