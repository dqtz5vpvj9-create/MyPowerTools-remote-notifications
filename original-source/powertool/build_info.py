import json
import os
import sys
from datetime import datetime
from pathlib import Path

from powertool.frozen_path import bundled_dir


def load_build_info() -> dict[str, str]:
    for path in _candidate_paths():
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except Exception:
            continue
    return {}


def build_display_text() -> str:
    info = load_build_info()
    built_at = info.get("built_at_local") or info.get("built_at_utc")
    if built_at:
        return f"Build {built_at}"
    return f"Source {source_mtime_text()}"


def source_mtime_text() -> str:
    try:
        mtime = os.path.getmtime(sys.argv[0])
        return datetime.fromtimestamp(mtime).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    except Exception:
        return "unknown"


def _candidate_paths() -> list[Path]:
    paths = [Path(bundled_dir()) / "build_info.json"]
    try:
        repo_root = Path(__file__).resolve().parents[1]
        paths.append(repo_root / "build" / "generated" / "build_info.json")
    except Exception:
        pass
    return paths
