from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("aosp_test_prep", ROOT / "aosp_test_prep.py")
assert SPEC and SPEC.loader
aosp_test_prep = importlib.util.module_from_spec(SPEC)
sys.modules["aosp_test_prep"] = aosp_test_prep
SPEC.loader.exec_module(aosp_test_prep)

PROFILE_ROOT = ROOT / "aosp_profiles"
LEGACY_PROFILE_KEYS = {"ssh", "serial", "runner_serial", "cvd_manager_path", "adb_path", "python_path"}


def archived_profiles() -> list[Path]:
    return sorted(PROFILE_ROOT.glob("aosp*/aosp_test_profile*.json"))


def test_archived_profiles_load_with_current_schema():
    paths = archived_profiles()
    assert paths

    for path in paths:
        profile = aosp_test_prep.Profile.load(str(path))
        assert profile.build_host
        assert profile.aosp_root
        assert profile.adb_serial_on_harness_host
        assert profile.adb_serial_on_build_host
        assert profile.adb_path_on_harness_host
        assert profile.adb_path_on_build_host
        assert profile.python_path_on_harness_host
        assert profile.python_path_on_build_host

        if profile.device_kind == "cvd":
            assert profile.cvd_manager_execution == "local"
            assert profile.cvd_manager_path_on_harness_host
            assert profile.cvd_host in {"r743", "pro0", "pro1"}
            assert profile.instances
            assert profile.allowed_instances
        else:
            assert profile.device_kind == "real"


def test_archived_profiles_use_explicit_harness_and_build_host_names():
    for path in archived_profiles():
        raw = json.loads(path.read_text())
        assert not (LEGACY_PROFILE_KEYS & raw.keys()), path
        assert raw["adb_serial_on_harness_host"]
        assert raw["adb_serial_on_build_host"]
        assert raw["adb_path_on_harness_host"]
        assert raw["adb_path_on_build_host"]
        assert raw["python_path_on_harness_host"]
        assert raw["python_path_on_build_host"]


def test_remote_cvd_hosts_are_not_named_local():
    for path in archived_profiles():
        raw = json.loads(path.read_text())
        if raw.get("device_kind", "cvd") != "cvd":
            continue
        assert raw["cvd_manager_execution"] == "local"
        assert raw["cvd_host"] != "local"
