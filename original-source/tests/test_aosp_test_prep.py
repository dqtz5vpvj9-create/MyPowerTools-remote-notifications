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


EXPECTED_FAILURE_TYPES = {
    "pass",
    "doctor_ssh_failure",
    "doctor_missing_tool",
    "doctor_bad_profile",
    "remote_lock_held",
    "prod_instance_guard",
    "build_compile_error",
    "build_timeout",
    "deploy_failure",
    "cvd_failure",
    "infra_adb_failure",
    "boot_timeout",
    "avb_failure",
    "zygote_crash",
    "system_server_crash",
    "test_failure",
    "instrumentation_timeout",
    "unknown",
}


def base_profile(**overrides):
    profile = {
        "build_host": "local-test",
        "aosp_root": "/tmp",
        "out_dir": "cf_out",
        "adb_serial_on_harness_host": "0.0.0.0:6534",
        "adb_serial_on_build_host": "0.0.0.0:6534",
        "instances": [15],
        "allowed_instances": [15],
        "prod_instance_guard": True,
        "cvd_host": "local",
        "cvd_aosp_profile": "aosp14",
        "cvd_manager_execution": "local",
        "build_modules": [],
        "image_modules": [],
        "deploy_strategy": "none",
        "test_strategy": "none",
        "cg_cf_path": "/bin/true",
        "cvd_manager_path_on_harness_host": "/bin/true",
        "adb_path_on_harness_host": "/bin/true",
        "adb_path_on_build_host": "/bin/true",
        "python_path_on_harness_host": sys.executable,
        "python_path_on_build_host": sys.executable,
        "adbkeyboard": {
            "enabled": False,
            "apk_path": "/tmp/keyboardservice-debug.apk",
            "package_name": "com.android.adbkeyboard",
            "ime_id": "com.android.adbkeyboard/.AdbIME",
        },
    }
    profile.update(overrides)
    return profile


def write_profile(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(data))
    return path


def make_harness(tmp_path: Path, profile_data: dict):
    profile_path = write_profile(tmp_path, profile_data)
    profile = aosp_test_prep.Profile.load(str(profile_path))
    artifacts = aosp_test_prep.ArtifactManager("unit-run", str(tmp_path / "runs"))
    harness = aosp_test_prep.AospTestPrepHarness(profile, artifacts)
    local_runner = aosp_test_prep.LocalRunner(artifacts, aosp_test_prep.CommandRecorder(artifacts))
    harness.runner = local_runner
    harness.build_runner = local_runner
    harness.local_runner = local_runner
    harness.remote = aosp_test_prep.RemoteFS(local_runner)
    return harness


class RecordingRunner(aosp_test_prep.Runner):
    def __init__(self, artifacts, recorder):
        self.artifacts = artifacts
        self.recorder = recorder
        self.calls = []

    def run(
        self,
        step,
        argv,
        cwd=None,
        timeout=None,
        stream=False,
        execution_class=None,
        execution_host=None,
        resource_host=None,
        adb_serial="",
        requires_remote_resource=None,
    ):
        stdout_path = self.artifacts.log_path(step, "stdout")
        stderr_path = self.artifacts.log_path(step, "stderr")
        stdout_path.write_text("")
        stderr_path.write_text("")
        result = aosp_test_prep.CommandResult(
            step=step,
            exit_code=0,
            start_ms=0,
            end_ms=1,
            duration_ms=1,
            stdout_path=self.artifacts.rel(stdout_path),
            stderr_path=self.artifacts.rel(stderr_path),
            first_error_line="",
            remote_cmd=" ".join(argv),
            argv=list(argv),
            cwd=cwd,
            execution_class=execution_class or "recording",
            execution_host=execution_host or "recording_host",
            resource_host=resource_host or "recording_resource",
            adb_serial=adb_serial,
            requires_remote_resource=bool(requires_remote_resource),
        )
        self.calls.append(result)
        self.recorder.record(result)
        return result


def test_failure_type_contract_is_exact():
    assert aosp_test_prep.FAILURE_TYPES == EXPECTED_FAILURE_TYPES


def test_bad_profile_writes_local_result_and_commands_file(tmp_path):
    profile_path = write_profile(tmp_path, {"build_host": "missing-most-fields"})

    rc = aosp_test_prep.main(
        [
            "doctor",
            "--profile",
            str(profile_path),
            "--run-id",
            "bad-profile",
            "--runs-dir",
            str(tmp_path / "runs"),
        ]
    )

    result_path = tmp_path / "runs" / "bad-profile" / "result.json"
    commands_path = tmp_path / "runs" / "bad-profile" / "commands.jsonl"
    result = json.loads(result_path.read_text())
    assert rc == 1
    assert result["gate"] == "BLOCKED"
    assert result["failure_type"] == "doctor_bad_profile"
    assert commands_path.exists()


def test_remote_fs_and_runner_write_commands_jsonl(tmp_path):
    artifacts = aosp_test_prep.ArtifactManager("commands", str(tmp_path / "runs"))
    recorder = aosp_test_prep.CommandRecorder(artifacts)
    runner = aosp_test_prep.LocalRunner(artifacts, recorder)
    remote = aosp_test_prep.RemoteFS(runner)

    result = runner.run("unit.echo", ["/bin/echo", "ok"])
    assert result.exit_code == 0
    assert remote.exists("/bin/sh", step="unit.exists")

    lines = [json.loads(line) for line in artifacts.commands_jsonl.read_text().splitlines()]
    assert [line["step"] for line in lines] == ["unit.echo", "unit.exists"]
    for line in lines:
        assert set(
            [
                "ts",
                "run_id",
                "step",
                "cwd",
                "argv",
                "remote_cmd",
                "exit_code",
                "duration_ms",
                "stdout_path",
                "stderr_path",
                "first_error_line",
            ]
        ).issubset(line)


def test_command_json_records_heartbeat_fields(tmp_path):
    artifacts = aosp_test_prep.ArtifactManager("heartbeat-fields", str(tmp_path / "runs"))
    recorder = aosp_test_prep.CommandRecorder(artifacts)
    runner = aosp_test_prep.LocalRunner(artifacts, recorder)

    runner.run("unit.echo", ["/bin/echo", "ok"])

    line = json.loads(artifacts.commands_jsonl.read_text().splitlines()[0])
    assert line["heartbeat_count"] == 0
    assert line["max_output_gap_ms"] >= 0


def test_artifact_manager_blocks_run_id_reuse_by_default(tmp_path):
    aosp_test_prep.ArtifactManager("reuse", str(tmp_path / "runs"))

    try:
        aosp_test_prep.ArtifactManager("reuse", str(tmp_path / "runs"))
    except FileExistsError as exc:
        assert "run_id already exists" in str(exc)
    else:
        raise AssertionError("expected run_id reuse to be blocked")


def test_artifact_manager_overwrite_recreates_artifact_dir(tmp_path):
    first = aosp_test_prep.ArtifactManager("reuse", str(tmp_path / "runs"))
    stale = first.logs_dir / "stale.log"
    stale.write_text("old")

    second = aosp_test_prep.ArtifactManager("reuse", str(tmp_path / "runs"), overwrite=True)

    assert second.root == first.root
    assert not stale.exists()
    assert second.commands_jsonl.read_text() == ""


def test_doctor_blocks_instance_outside_allowlist_before_remote_commands(tmp_path):
    harness = make_harness(
        tmp_path,
        base_profile(
            instances=[14],
            allowed_instances=[15],
            adb_serial_on_harness_host="0.0.0.0:6533",
            adb_serial_on_build_host="0.0.0.0:6533",
        ),
    )

    result = harness.phase_doctor()

    assert result.ok is False
    assert result.reason == "prod_instance_guard"
    assert result.details["instances_not_allowed"] == [14]
    assert harness.artifacts.commands_jsonl.read_text() == ""


def test_doctor_blocks_bare_cvd_tools_before_remote_commands(tmp_path):
    harness = make_harness(
        tmp_path,
        base_profile(
            deploy_strategy="image_update",
            cvd_manager_path_on_harness_host="stop_cvd",
        ),
    )

    result = harness.phase_doctor()

    assert result.ok is False
    assert result.reason == "prod_instance_guard"
    assert "bare stop_cvd" in result.details["error"]
    assert harness.artifacts.commands_jsonl.read_text() == ""


def test_doctor_success_uses_local_lock_and_releases(tmp_path):
    instance = 15
    harness = make_harness(
        tmp_path,
        base_profile(instances=[instance], allowed_instances=[instance]),
    )

    result = harness.phase_doctor()
    assert result.ok is True
    assert harness.lock_acquired is True
    assert Path(harness.profile.lock_path).exists()
    harness.release_remote_lock()
    assert not Path(harness.profile.lock_path).exists()


def test_cvd_manager_is_invoked_through_profile_python(tmp_path):
    harness = make_harness(
        tmp_path,
        base_profile(
            python_path_on_harness_host="/bin/echo",
            cvd_manager_path_on_harness_host="/remote/cvd_manager.py",
        ),
    )

    result = harness.run_cvd_manager("unit.cvd.status", "status")

    assert result.exit_code == 0
    lines = [json.loads(line) for line in harness.artifacts.commands_jsonl.read_text().splitlines()]
    assert lines[-1]["argv"] == [
        "env",
        "CVD_AOSP_ROOT=/tmp",
        "CVD_CF_OUT=/tmp/cf_out",
        "CVD_PRODUCT_OUT=/tmp/cf_out/target/product/vsoc_x86_64",
        "/bin/echo",
        "/remote/cvd_manager.py",
        "--aosp-profile",
        "aosp14",
        "--host",
        "local",
        "status",
    ]


def test_image_update_passes_profile_cvd_start_extra_args(tmp_path):
    harness = make_harness(
        tmp_path,
        base_profile(
            deploy_strategy="image_update",
            cvd_start_extra_args=["--no-overlay"],
        ),
    )
    runner = RecordingRunner(harness.artifacts, aosp_test_prep.CommandRecorder(harness.artifacts))
    harness.local_runner = runner

    result = harness.deploy_image_update()

    assert result.ok is True
    start_call = next(call for call in runner.calls if call.step == "cvd.start")
    assert start_call.argv[-3:] == ["--instances", "15", "--no-overlay"]


def test_profile_rejects_serial_instance_port_mismatch(tmp_path):
    profile_path = write_profile(
        tmp_path,
        base_profile(
            instances=[15],
            adb_serial_on_harness_host="0.0.0.0:6533",
            adb_serial_on_build_host="0.0.0.0:6533",
        ),
    )

    try:
        aosp_test_prep.Profile.load(str(profile_path))
    except ValueError as exc:
        assert "adb_serial_on_harness_host port 6533 does not match instance 15" in str(exc)
    else:
        raise AssertionError("expected profile validation failure")


def test_profile_rejects_cvd_name_instances(tmp_path):
    profile_path = write_profile(tmp_path, base_profile(instances=["cvd-15"]))

    try:
        aosp_test_prep.Profile.load(str(profile_path))
    except ValueError as exc:
        assert "instances must contain integer CVD instance numbers" in str(exc)
    else:
        raise AssertionError("expected profile validation failure")


def test_profile_requires_adbkeyboard_config(tmp_path):
    data = base_profile()
    data.pop("adbkeyboard")
    profile_path = write_profile(tmp_path, data)

    try:
        aosp_test_prep.Profile.load(str(profile_path))
    except ValueError as exc:
        assert "adbkeyboard" in str(exc)
    else:
        raise AssertionError("expected profile validation failure")


def test_profile_accepts_build_host_without_ssh_alias(tmp_path):
    data = base_profile()
    data.pop("ssh", None)
    profile_path = write_profile(tmp_path, data)

    profile = aosp_test_prep.Profile.load(str(profile_path))

    assert profile.build_host == "local-test"
    assert profile.ssh == "local-test"


def test_profile_keeps_legacy_ssh_alias_compatible(tmp_path):
    data = base_profile()
    data["ssh"] = data.pop("build_host")
    profile_path = write_profile(tmp_path, data)

    profile = aosp_test_prep.Profile.load(str(profile_path))

    assert profile.build_host == "local-test"
    assert profile.ssh == "local-test"


def test_real_device_profile_loads_without_cvd_fields(tmp_path):
    data = {
        "device_kind": "real",
        "build_host": "r743",
        "aosp_root": "/tmp",
        "out_dir": "out",
        "adb_serial_on_harness_host": "26071FDH3000DV",
        "adb_serial_on_build_host": "26071FDH3000DV",
        "build_modules": [],
        "image_modules": ["systemimage"],
        "deploy_strategy": "none",
        "test_strategy": "none",
        "cg_cf_path": "/bin/true",
        "adb_path_on_harness_host": "/bin/true",
        "adb_path_on_build_host": "/bin/true",
        "python_path_on_harness_host": sys.executable,
        "python_path_on_build_host": sys.executable,
        "adbkeyboard": {"enabled": False},
    }
    profile_path = write_profile(tmp_path, data)
    profile = aosp_test_prep.Profile.load(str(profile_path))

    assert profile.device_kind == "real"
    assert profile.instances == []
    assert profile.cvd_manager_path_on_harness_host == ""
    assert profile.expected_serial() == "26071FDH3000DV"
    assert "26071FDH3000DV" in profile.lock_path


def test_real_device_profile_rejects_non_none_deploy(tmp_path):
    data = {
        "device_kind": "real",
        "build_host": "r743",
        "aosp_root": "/tmp",
        "out_dir": "out",
        "adb_serial_on_harness_host": "26071FDH3000DV",
        "adb_serial_on_build_host": "26071FDH3000DV",
        "build_modules": [],
        "image_modules": ["systemimage"],
        "deploy_strategy": "image_update",
        "test_strategy": "none",
        "cg_cf_path": "/bin/true",
        "adb_path_on_harness_host": "/bin/true",
        "adb_path_on_build_host": "/bin/true",
        "python_path_on_harness_host": sys.executable,
        "python_path_on_build_host": sys.executable,
        "adbkeyboard": {"enabled": False},
    }
    profile_path = write_profile(tmp_path, data)
    try:
        aosp_test_prep.Profile.load(str(profile_path))
    except ValueError as exc:
        assert 'deploy_strategy must be "none" when device_kind="real"' in str(exc)
    else:
        raise AssertionError("expected validation failure")


def test_real_device_profile_rejects_non_none_test(tmp_path):
    data = {
        "device_kind": "real",
        "build_host": "r743",
        "aosp_root": "/tmp",
        "out_dir": "out",
        "adb_serial_on_harness_host": "26071FDH3000DV",
        "adb_serial_on_build_host": "26071FDH3000DV",
        "build_modules": [],
        "image_modules": ["systemimage"],
        "deploy_strategy": "none",
        "test_strategy": "atest",
        "tests": ["DummyTest"],
        "cg_cf_path": "/bin/true",
        "adb_path_on_harness_host": "/bin/true",
        "adb_path_on_build_host": "/bin/true",
        "python_path_on_harness_host": sys.executable,
        "python_path_on_build_host": sys.executable,
        "adbkeyboard": {"enabled": False},
    }
    profile_path = write_profile(tmp_path, data)
    try:
        aosp_test_prep.Profile.load(str(profile_path))
    except ValueError as exc:
        assert 'test_strategy must be "none" when device_kind="real"' in str(exc)
    else:
        raise AssertionError("expected validation failure")


def test_real_device_doctor_skips_cvd_checks(tmp_path):
    data = {
        "device_kind": "real",
        "build_host": "local-test",
        "aosp_root": "/tmp",
        "out_dir": "out",
        "adb_serial_on_harness_host": "26071FDH3000DV",
        "adb_serial_on_build_host": "26071FDH3000DV",
        "build_modules": [],
        "image_modules": ["systemimage"],
        "deploy_strategy": "none",
        "test_strategy": "none",
        "cg_cf_path": "/bin/true",
        "adb_path_on_harness_host": "/bin/true",
        "adb_path_on_build_host": "/bin/true",
        "python_path_on_harness_host": sys.executable,
        "python_path_on_build_host": sys.executable,
        "adbkeyboard": {"enabled": False},
    }
    harness = make_harness(tmp_path, data)

    result = harness.phase_doctor()
    assert result.ok is True
    assert harness.lock_acquired is True
    assert Path(harness.profile.lock_path).exists()
    harness.release_remote_lock()


def test_phase_test_prepares_adbkeyboard_before_atest(tmp_path):
    adb_script = tmp_path / "fake_adb.sh"
    adb_script.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> " + str(tmp_path / "adb_calls.txt") + "\n"
        "case \"$*\" in\n"
        "  *'pm path com.android.adbkeyboard'*) echo package:/data/app/ADBKeyBoard/base.apk ;;\n"
        "  *'settings get secure default_input_method'*) echo com.android.adbkeyboard/.AdbIME ;;\n"
        "esac\n"
        "exit 0\n"
    )
    adb_script.chmod(0o755)
    cg_cf = tmp_path / "fake_cg_cf.sh"
    cg_cf.write_text("#!/bin/sh\nexit 0\n")
    cg_cf.chmod(0o755)
    apk = tmp_path / "keyboardservice-debug.apk"
    apk.write_text("apk")

    harness = make_harness(
        tmp_path,
        base_profile(
            test_strategy="atest",
            tests=["UiWaitTypeE2ETest"],
            adb_path_on_harness_host=str(adb_script),
            adb_path_on_build_host=str(adb_script),
            cg_cf_path=str(cg_cf),
            adbkeyboard={
                "enabled": True,
                "apk_path": str(apk),
                "package_name": "com.android.adbkeyboard",
                "ime_id": "com.android.adbkeyboard/.AdbIME",
            },
        ),
    )

    result = harness.phase_test()

    assert result.ok is True
    steps = [json.loads(line)["step"] for line in harness.artifacts.commands_jsonl.read_text().splitlines()]
    assert steps[:6] == [
        "test.adbkeyboard.install.adb_connect",
        "test.adbkeyboard.install",
        "test.adbkeyboard.ime_enable",
        "test.adbkeyboard.ime_set",
        "test.adbkeyboard.pm_path",
        "test.adbkeyboard.verify_default_ime",
    ]
    assert steps[-1] == "test.atest.UiWaitTypeE2ETest"


def test_bundle_sync_creates_remote_dirs_and_records_cvd_host(tmp_path):
    harness = make_harness(
        tmp_path,
        base_profile(
            aosp_root="/tmp/aosp",
            out_dir="cf_out",
            cvd_host="pro0",
            instances=[1],
            allowed_instances=[1],
            adb_serial_on_harness_host="pro0:6520",
            adb_serial_on_build_host="pro0:6520",
            deploy_strategy="image_update",
        ),
    )
    recorder = aosp_test_prep.CommandRecorder(harness.artifacts)
    runner = RecordingRunner(harness.artifacts, recorder)
    harness.build_runner = runner
    harness.remote = aosp_test_prep.RemoteFS(runner)

    result = harness.sync_cvd_bundle_if_needed()

    assert result.ok is True
    call = runner.calls[-1]
    script = call.argv[-1]
    assert "ssh pro0 mkdir -p /tmp/aosp/cf_out/host/linux-x86 /tmp/aosp/cf_out/target/product/vsoc_x86_64" in script
    assert "pro0:/tmp/aosp/cf_out/host/linux-x86/" in script
    assert "pro0:/tmp/aosp/cf_out/target/product/vsoc_x86_64/" in script
    line = json.loads(harness.artifacts.commands_jsonl.read_text().splitlines()[-1])
    assert line["execution_class"] == "remote_artifact"
    assert line["execution_host"] == "local-test"
    assert line["resource_host"] == "pro0"


def test_partition_sync_connects_build_host_adb_before_sync(tmp_path):
    harness = make_harness(
        tmp_path,
        base_profile(
            cvd_host="pro1",
            instances=[1],
            allowed_instances=[1],
            adb_serial_on_harness_host="pro1:6520",
            adb_serial_on_build_host="pro1:6520",
            deploy_strategy="partition_sync",
            sync_partitions=["system"],
        ),
    )
    recorder = aosp_test_prep.CommandRecorder(harness.artifacts)
    runner = RecordingRunner(harness.artifacts, recorder)
    harness.build_runner = runner
    harness.remote = aosp_test_prep.RemoteFS(runner)

    result = harness.deploy_partition_sync()

    assert result.ok is True
    steps = [json.loads(line)["step"] for line in harness.artifacts.commands_jsonl.read_text().splitlines()]
    assert steps.index("deploy.sync.build_host_adb_connect") < steps.index("deploy.sync.system")
