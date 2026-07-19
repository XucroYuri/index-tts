from __future__ import annotations

import ast
import importlib.util
import os
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "tts_more"
WORKFLOW = ROOT / ".github" / "workflows" / "portable-release.yml"
GATE = BUNDLE / "verify-release-asset-set.py"


def load_release_gate():
    spec = importlib.util.spec_from_file_location("index_release_asset_gate", GATE)
    if spec is None or spec.loader is None:
        raise AssertionError("release asset gate is not importable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def expected_names() -> list[str]:
    archive = "indextts-0.2.0-test-windows-x64-bootstrap.zip"
    return [
        archive,
        f"{archive}.sha256",
        f"{archive}.spdx.json",
        f"{archive}.licenses.json",
        f"{archive}.provenance.json",
        f"{archive}.acceptance.json",
    ]


class WorkerRuntimeContractTests(unittest.TestCase):
    def test_worker_status_uses_torch_uuid_without_spawning_nvidia_smi(self) -> None:
        sys.path.insert(0, str(BUNDLE))
        try:
            from app.workers import runtime
        finally:
            sys.path.pop(0)

        class FakeCuda:
            is_available = staticmethod(lambda: True)
            current_device = staticmethod(lambda: 0)
            memory_allocated = staticmethod(lambda _index: 0)
            memory_reserved = staticmethod(lambda _index: 0)
            mem_get_info = staticmethod(lambda _index: (1024, 2048))
            get_device_properties = staticmethod(
                lambda index: types.SimpleNamespace(uuid=f"GPU-logical-{index}")
            )

        fake_torch = types.SimpleNamespace(
            cuda=FakeCuda(), version=types.SimpleNamespace(cuda="12.8")
        )
        runtime._DEVICE_UUID_CACHE.clear()
        with mock.patch.dict(sys.modules, {"torch": fake_torch}):
            with mock.patch.object(
                subprocess,
                "run",
                side_effect=AssertionError("worker status must not spawn nvidia-smi"),
            ):
                with mock.patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("CUDA_VISIBLE_DEVICES", None)
                    status = runtime.worker_runtime_status(loaded=True, model="demo")

        self.assertEqual("GPU-logical-0", status["device_uuid"])

    def test_portable_launcher_and_worker_keep_artifacts_under_data_local(self) -> None:
        launcher = (BUNDLE / "Start-Worker.ps1").read_text(encoding="utf-8")
        worker = (BUNDLE / "app" / "workers" / "indextts_worker.py").read_text(encoding="utf-8")

        self.assertIn(
            '$env:TTS_MORE_ARTIFACT_ROOT = (Join-Path $Root "data\\local\\artifacts")',
            launcher,
        )
        self.assertIn('os.environ.get("TTS_MORE_ARTIFACT_ROOT")', worker)
        self.assertNotIn('ArtifactStore(REPO_DIR / "uploaded_ref")', worker)

    def test_indextts_worker_capabilities_match_portable_manifest_contract(self) -> None:
        source = (BUNDLE / "app" / "workers" / "indextts_worker.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        function = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "capabilities"
        )
        values = {
            node.value
            for node in ast.walk(function)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }

        self.assertTrue(
            {"tts", "reference_audio_voice", "emotion_text", "artifact-transfer"}.issubset(values)
        )


class ReleaseAssetGateContractTests(unittest.TestCase):
    def gate_args(self, names: list[str], *, tag: str = "v0.2.0-test") -> list[str]:
        return [
            "--repository",
            "XucroYuri/index-tts",
            "--tag",
            tag,
            *(arg for name in names for arg in ("--expected-name", name)),
        ]

    def test_workflow_has_exact_six_preflight_and_post_upload_gate(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        publish = workflow.split("- name: Publish bootstrap assets only", 1)[1]
        upload = 'gh release upload "$GITHUB_REF_NAME" "${assets[@]}" --clobber'
        gate_call = "python tts_more/verify-release-asset-set.py"

        self.assertIn("audit-release-assets --directory", workflow)
        self.assertIn("--expected-component indextts", workflow)
        self.assertIn("local_asset_names", publish)
        self.assertIn("remote_asset_names", publish)
        self.assertIn("comm -23", publish)
        self.assertNotIn("release delete-asset", publish)
        self.assertIn(upload, publish)
        self.assertIn(gate_call, publish)
        self.assertLess(publish.index(upload), publish.index(gate_call))
        gate_block = publish[publish.index(gate_call) :]
        self.assertIn('--repository "$GITHUB_REPOSITORY"', gate_block)
        self.assertIn('--tag "$GITHUB_REF_NAME"', gate_block)
        self.assertIn('"${verify_asset_args[@]}"', gate_block)
        self.assertIn(
            'verify_asset_args+=(--expected-name "$asset_name")', publish
        )

    def test_bootstrap_workflow_uses_locked_portable_build_tools_python(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        build = workflow.split("- name: Build bootstrap and prove full is prohibited", 1)[1]
        build = build.split("- uses: actions/upload-artifact", 1)[0]

        self.assertIn(
            '$env:UV_PROJECT_ENVIRONMENT = Join-Path $env:RUNNER_TEMP "tts-more-build-tools"',
            build,
        )
        self.assertIn("uv sync --locked --project tts_more/build-tools", build)
        self.assertIn(
            '$buildPython = Join-Path $env:UV_PROJECT_ENVIRONMENT "Scripts\\python.exe"',
            build,
        )
        self.assertIn('$env:TTS_MORE_BUILD_PYTHON = $buildPython', build)
        self.assertEqual(
            2,
            build.count("& $buildPython tts_more\\portable_packages.py audit-release"),
        )
        self.assertNotIn("python tts_more\\portable_packages.py", build)
        self.assertNotIn("python -m pip install", build)

    def test_full_refusal_uses_child_powershell_exit_code_and_output(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        build = workflow.split("- name: Build bootstrap and prove full is prohibited", 1)[1]
        build = build.split("- uses: actions/upload-artifact", 1)[0]

        self.assertIn("$hostPowerShell = (Get-Process -Id $PID).Path", build)
        self.assertIn(
            "$previousErrorActionPreference = $ErrorActionPreference",
            build,
        )
        self.assertIn('$ErrorActionPreference = "Continue"', build)
        self.assertIn("& $hostPowerShell -NoProfile -NonInteractive", build)
        self.assertIn("-File .\\Build-Package.ps1 -Profile Full", build)
        self.assertIn("$fullExit = $LASTEXITCODE", build)
        self.assertIn(
            "$ErrorActionPreference = $previousErrorActionPreference",
            build,
        )
        self.assertIn('$null -eq $fullExit', build)
        self.assertIn('$fullExit -eq 0', build)
        self.assertIn('$fullOutput -join "`n"', build)
        self.assertIn('-notmatch "profile=full"', build)
        self.assertIn("exit 0", build)
        validated_success_exit = (
            '            throw "full profile refusal probe failed:'
            " exit=$fullExit output=$($fullOutput -join ' | ')\"\n"
            "          }\n"
            "          exit 0"
        )
        self.assertIn(validated_success_exit, build)
        self.assertNotIn(
            "try { .\\Build-Package.ps1 -Profile Full",
            build,
        )

    def test_fake_gh_accepts_exact_six_and_percent_encodes_tag(self) -> None:
        gate = load_release_gate()
        expected = expected_names()
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(
                command, 0, stdout="\n".join(reversed(expected)) + "\n", stderr=""
            )

        self.assertEqual(0, gate.main(self.gate_args(expected, tag="v0.2.0/rc1"), run=fake_run))
        self.assertEqual(
            "repos/XucroYuri/index-tts/releases/tags/v0.2.0%2Frc1", calls[0][2]
        )

    def test_fake_gh_rejects_concurrent_seventh_asset(self) -> None:
        gate = load_release_gate()
        expected = expected_names()

        def fake_run(command: list[str], **_kwargs):
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="\n".join([*expected, "foreign-full.zip"]) + "\n",
                stderr="",
            )

        self.assertNotEqual(0, gate.main(self.gate_args(expected), run=fake_run))

    def test_fake_gh_rejects_replacement_with_six_assets(self) -> None:
        gate = load_release_gate()
        expected = expected_names()

        def fake_run(command: list[str], **_kwargs):
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="\n".join([*expected[:-1], "foreign.zip"]) + "\n",
                stderr="",
            )

        self.assertNotEqual(0, gate.main(self.gate_args(expected), run=fake_run))


if __name__ == "__main__":
    unittest.main()
