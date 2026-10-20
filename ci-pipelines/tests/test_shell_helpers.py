import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def run_bash(script, *, env=None, check=True):
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=ROOT,
        env=merged_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"command failed: {script}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    return result


def fake_skopeo_env(tmp_path, digests):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    copy_log = tmp_path / "copy.log"
    login_log = tmp_path / "login.log"
    skopeo = bin_dir / "skopeo"
    skopeo.write_text(
        r"""#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
digests = json.loads(os.environ.get('FAKE_SKOPEO_DIGESTS', '{}'))
if args[0] == 'login':
    with open(os.environ['FAKE_SKOPEO_LOGIN_LOG'], 'a', encoding='utf-8') as handle:
        handle.write('ARGS=' + ' '.join(args[1:]) + '\n')
        handle.write('STDIN=' + sys.stdin.read() + '\n')
    raise SystemExit(0)
if args[0] == 'inspect':
    ref = args[-1]
    if ref.startswith('docker://'):
        ref = ref[len('docker://'):]
    digest = digests.get(ref, '')
    if digest == '__ERROR__':
        print('unauthorized: authentication required', file=sys.stderr)
        raise SystemExit(7)
    if digest == '__MISSING_FATAL__':
        print('time="2026-08-20T08:09:50Z" level=fatal msg="Error parsing image name \\\"docker://' + ref + '\\\": reading manifest tag in repo: manifest unknown: The named manifest is not known to the registry."', file=sys.stderr)
        raise SystemExit(2)
    if digest:
        print(digest)
        raise SystemExit(0)
    print('manifest unknown', file=sys.stderr)
    raise SystemExit(1)
if args[0] == 'copy':
    with open(os.environ['FAKE_SKOPEO_COPY_LOG'], 'a', encoding='utf-8') as handle:
        handle.write(' '.join(args[1:]) + '\n')
    raise SystemExit(0)
print('unsupported skopeo call: ' + ' '.join(args), file=sys.stderr)
raise SystemExit(2)
"""
    )
    skopeo.chmod(0o755)
    return {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_SKOPEO_DIGESTS": json.dumps(digests),
        "FAKE_SKOPEO_COPY_LOG": str(copy_log),
        "FAKE_SKOPEO_LOGIN_LOG": str(login_log),
    }, copy_log


def source_skopeo(command):
    return f"source ci-pipelines/helpers/skopeo.sh; {command}"


def meta_env(**extra):
    env = {
        "IMAGE_PREFIX": "localhost/alps-images",
        "GHCR_IMAGE_PREFIX": "localhost/ghcr",
        "ALPS_REV": "alps7-dev",
        "CI_COMMIT_SHORT_SHA": "abc123",
        "CI_COMMIT_SHA": "abc123",
        "CSCS_CI_ORIG_CLONE_URL": "https://example.invalid/repo.git",
    }
    env.update(extra)
    return env


def test_skopeo_img_digest_returns_digest_for_existing_ref(tmp_path):
    env, _ = fake_skopeo_env(tmp_path, {"registry/image:tag": "sha256:123"})
    result = run_bash(source_skopeo("img_digest registry/image:tag"), env=env)
    assert result.stdout.strip() == "sha256:123"


def test_skopeo_img_digest_returns_empty_for_missing_ref(tmp_path):
    env, _ = fake_skopeo_env(tmp_path, {})
    result = run_bash(source_skopeo("img_digest registry/missing:tag"), env=env)
    assert result.stdout == ""


def test_skopeo_img_digest_returns_empty_for_jfrog_missing_manifest(tmp_path):
    env, _ = fake_skopeo_env(tmp_path, {"registry/missing:tag": "__MISSING_FATAL__"})
    result = run_bash(source_skopeo("img_digest registry/missing:tag"), env=env)
    assert result.stdout == ""


def test_skopeo_img_digest_fails_closed_on_unexpected_error(tmp_path):
    env, _ = fake_skopeo_env(tmp_path, {"registry/private:tag": "__ERROR__"})
    result = run_bash(source_skopeo("img_digest registry/private:tag"), env=env, check=False)
    assert result.returncode != 0
    assert "failed to inspect image" in result.stderr


def test_skopeo_login_passes_password_on_stdin(tmp_path):
    env, _ = fake_skopeo_env(tmp_path, {})
    env.update({"IMAGE_PREFIX": "registry.example/ns", "JFROG_USER": "user", "JFROG_KEY": "secret-token"})

    run_bash(source_skopeo("skopeo_login"), env=env)

    login_log = Path(env["FAKE_SKOPEO_LOGIN_LOG"]).read_text()
    assert "--password-stdin" in login_log
    assert "--password secret-token" not in login_log
    assert "STDIN=secret-token" in login_log


def test_skopeo_require_tested_marker_valid_requires_marker_exists(tmp_path):
    env, _ = fake_skopeo_env(tmp_path, {"registry/image:canon": "sha256:123"})
    result = run_bash(
        source_skopeo("require_tested_marker_valid registry/image:canon registry/image:canon-tested-hash"),
        env=env,
        check=False,
    )
    assert result.returncode != 0
    assert "tested marker missing" in result.stderr


def test_skopeo_require_tested_marker_valid_rejects_mismatch(tmp_path):
    env, _ = fake_skopeo_env(tmp_path, {"registry/image:canon": "sha256:123", "registry/image:canon-tested-hash": "sha256:old"})
    result = run_bash(
        source_skopeo("require_tested_marker_valid registry/image:canon registry/image:canon-tested-hash"),
        env=env,
        check=False,
    )
    assert result.returncode != 0
    assert "different digest" in result.stderr


def test_skopeo_mark_tested_creates_missing_marker(tmp_path):
    env, copy_log = fake_skopeo_env(tmp_path, {"registry/image:canon": "sha256:123"})
    run_bash(source_skopeo("mark_tested registry/image:canon registry/image:canon-tested-hash"), env=env)
    assert copy_log.read_text().strip() == "docker://registry/image:canon docker://registry/image:canon-tested-hash"


def test_skopeo_ref_url_preserves_existing_prefix():
    result = run_bash(source_skopeo("_ref_url docker://registry/image:tag"))
    assert result.stdout.strip() == "docker://registry/image:tag"


def test_skopeo_ref_url_adds_docker_prefix():
    result = run_bash(source_skopeo("_ref_url registry/image:tag"))
    assert result.stdout.strip() == "docker://registry/image:tag"


def test_skopeo_tested_ref_for_accepts_registry_port():
    result = run_bash(source_skopeo("tested_ref_for registry:5000/ns/image:tag hash"))
    assert result.stdout.strip() == "registry:5000/ns/image:tag-tested-hash"


def test_skopeo_tested_ref_for_rejects_untagged_registry_port():
    result = run_bash(source_skopeo("tested_ref_for registry:5000/ns/image hash"), check=False)
    assert result.returncode != 0
    assert "must include a tag" in result.stderr


def test_skopeo_mark_tested_noops_matching_marker(tmp_path):
    env, copy_log = fake_skopeo_env(tmp_path, {"registry/image:canon": "sha256:123", "registry/image:canon-tested-hash": "sha256:123"})
    result = run_bash(source_skopeo("mark_tested registry/image:canon registry/image:canon-tested-hash"), env=env)
    assert "No-op" in result.stdout
    assert not copy_log.exists()


def test_skopeo_mark_tested_rejects_mismatch(tmp_path):
    env, _ = fake_skopeo_env(tmp_path, {"registry/image:canon": "sha256:123", "registry/image:canon-tested-hash": "sha256:old"})
    result = run_bash(source_skopeo("mark_tested registry/image:canon registry/image:canon-tested-hash"), env=env, check=False)
    assert result.returncode != 0
    assert "different digest" in result.stderr


def test_skopeo_promote_strict_copies_missing_stable(tmp_path):
    env, copy_log = fake_skopeo_env(tmp_path, {"registry/image:canon": "sha256:123"})
    run_bash(source_skopeo("promote_strict registry/image:canon registry/image:stable"), env=env)
    assert copy_log.read_text().strip() == "docker://registry/image:canon docker://registry/image:stable"


def test_skopeo_promote_strict_noops_matching_stable(tmp_path):
    env, copy_log = fake_skopeo_env(tmp_path, {"registry/image:canon": "sha256:123", "registry/image:stable": "sha256:123"})
    result = run_bash(source_skopeo("promote_strict registry/image:canon registry/image:stable"), env=env)
    assert "no-op" in result.stdout
    assert not copy_log.exists()


def test_skopeo_promote_strict_rejects_different_stable(tmp_path):
    env, _ = fake_skopeo_env(tmp_path, {"registry/image:canon": "sha256:123", "registry/image:stable": "sha256:old"})
    result = run_bash(source_skopeo("promote_strict registry/image:canon registry/image:stable"), env=env, check=False)
    assert result.returncode != 0
    assert "refusing to overwrite" in result.stderr


def run_meta(command, *, check=True, **extra_env):
    return run_bash(f"source ci-pipelines/helpers/skopeo.sh; source ci-pipelines/helpers/meta.sh; {command}", env=meta_env(**extra_env), check=check)


def test_meta_base_refs_return_expected_fields():
    ngc_fields = run_meta("ngc_base_refs pytorch 25.12-py3").stdout.split()
    rocm_fields = run_meta("rocm_base_refs pytorch rocm7.14-ubuntu24.04-py3.12-torch2.11").stdout.split()
    assert len(ngc_fields) == 5
    assert len(rocm_fields) == 4


def test_meta_app_refs_use_canonical_base_ref():
    fields = run_meta("app_refs vllm cuda").stdout.split()
    assert len(fields) == 4
    assert re.match(r"localhost/alps-images/pytorch-cuda:26\.07-py3-alps7-dev-[0-9a-f]{32}$", fields[0])


def test_meta_parse_base_image_rejects_unsupported_format():
    result = run_meta("parse_base_image pytorch:plain", check=False)
    assert result.returncode != 0
    assert "expected BASE_IMAGE" in result.stderr


def test_meta_validate_rocm_profile_rejects_invalid_rebuild_flag(tmp_path):
    profile = tmp_path / "profile.env"
    profile.write_text(
        "\n".join(
            [
                'ROCM_PYPI_INDEX_URL="https://example.invalid"',
                'ROCM_REBUILD_RCCL="maybe"',
                'ROCM_SYSTEMS_REPO="https://example.invalid/repo.git"',
                'ROCM_SYSTEMS_COMMIT="abc123"',
                'RCCL_GPU_TARGETS="gfx942"',
                'RCCL_TESTS_GPU_TARGETS="gfx942"',
            ]
        )
    )
    result = run_meta(f"load_rocm_profile {profile}", check=False)
    assert result.returncode != 0
    assert "ROCM_REBUILD_RCCL must be 0 or 1" in result.stderr


def test_meta_profile_value_accepts_quoted_value_with_inline_comment(tmp_path):
    profile = tmp_path / "profile.env"
    profile.write_text('APP_VARIANTS="cuda rocm" # supported variants\n')

    result = run_meta(f"profile_value {profile} APP_VARIANTS")

    assert result.stdout.strip() == "cuda rocm"


def test_meta_refs_are_deterministic():
    first = run_meta("app_refs vllm cuda").stdout
    second = run_meta("app_refs vllm cuda").stdout
    assert first == second


def test_meta_commit_sha_does_not_change_canonical_refs():
    first = run_meta("ngc_base_refs pytorch 25.12-py3", CI_COMMIT_SHORT_SHA="one", CI_COMMIT_SHA="one").stdout
    second = run_meta("ngc_base_refs pytorch 25.12-py3", CI_COMMIT_SHORT_SHA="two", CI_COMMIT_SHA="two").stdout
    assert first == second


def test_meta_ngc_canonical_ref_changes_with_profile_inputs():
    profile = ROOT / "Alps-Images" / "NGC" / "pytorch-25.12-py3" / "profile.env"
    original = profile.read_text()
    baseline = run_meta("ngc_base_refs pytorch 25.12-py3").stdout.split()
    try:
        profile.write_text(original + '\nREMOVE_HPCX_DIRS="${REMOVE_HPCX_DIRS} /tmp/extra-hpcx"\n')
        changed_remove_hpcx = run_meta("ngc_base_refs pytorch 25.12-py3").stdout.split()
        profile.write_text(original + '\nNVCR_PREFIX=custom\n')
        changed_nvcr_prefix = run_meta("ngc_base_refs pytorch 25.12-py3").stdout.split()
    finally:
        profile.write_text(original)

    assert baseline[3] != changed_remove_hpcx[3]
    assert baseline[3] != changed_nvcr_prefix[3]


def test_meta_app_profile_is_not_executed(tmp_path):
    profile = ROOT / "Alps-Images" / "apps" / "vllm" / "profile.env"
    marker = tmp_path / "profile-executed"
    original = profile.read_text()
    try:
        profile.write_text(original + f"\ntouch {marker}\n")
        run_meta("app_refs vllm cuda")
        run_meta("app_validation_hash vllm cuda")
    finally:
        profile.write_text(original)

    assert not marker.exists()


def test_meta_write_build_env_includes_validation_marker_refs(tmp_path):
    output = tmp_path / "build.env"
    run_meta(f"write_base_build_env {output} cuda pytorch 25.12-py3")
    base_env = output.read_text()
    run_meta(f"write_app_build_env {output} vllm cuda")
    app_env = output.read_text()
    assert "VALIDATION_HASH=" in base_env
    assert "TESTED_IMAGE_REF=" in base_env
    assert "OCI_CREATED=" not in base_env
    assert "VALIDATION_HASH=" in app_env
    assert "TESTED_IMAGE_REF=" in app_env
    assert "OCI_CREATED=" not in app_env


def test_meta_validation_hashes_include_helper_scripts():
    result = run_meta("validation_helper_paths")
    paths = result.stdout.split()
    assert "ci-pipelines/helpers/meta.sh" in paths
    assert "ci-pipelines/helpers/skopeo.sh" in paths


def generate_manual(*args, output):
    env = meta_env()
    run_bash(f"manual-build/manual-build.sh {' '.join(args)} {output}", env=env)
    run_bash(f"bash -n {output}")
    return output.read_text()


def test_manual_build_generates_valid_cuda_base_script(tmp_path):
    script = generate_manual("base", "cuda", "pytorch", "25.12-py3", output=tmp_path / "base-cuda.sh")
    assert "podman build" in script
    assert "NGC_VARIANT_DIR" in script


def test_manual_build_generates_valid_rocm_base_script(tmp_path):
    script = generate_manual("base", "rocm", "pytorch", "rocm7.14-ubuntu24.04-py3.12-torch2.11", output=tmp_path / "base-rocm.sh")
    assert "podman build" in script
    assert "ROCM_VARIANT_DIR" in script
    assert re.search(r'--build-arg ROCM_SYSTEMS_COMMIT="[0-9a-f]{40}"', script)
    assert "ROCM_LIBRARIES_COMMIT" in script


def test_manual_build_generates_valid_cuda_app_script(tmp_path):
    script = generate_manual("app", "vllm", "cuda", output=tmp_path / "app-cuda.sh")
    assert "Alps-Images/apps/vllm/Containerfile" in script
    assert re.search(r'--build-arg BASE_IMAGE="localhost/alps-images/pytorch-cuda:.*-[0-9a-f]{32}"', script)


def test_manual_build_generates_valid_rocm_app_script(tmp_path):
    script = generate_manual("app", "vllm", "rocm", output=tmp_path / "app-rocm.sh")
    assert "Alps-Images/apps/vllm/Containerfile.rocm" in script
    assert re.search(r'--build-arg BASE_IMAGE="localhost/alps-images/pytorch-rocm:.*-[0-9a-f]{32}"', script)


def source_rocm(command):
    return (
        "set -e; source Alps-Images/ROCm/install-alps-rocm-components.sh; "
        'die() { printf "%s\\n" "$*" >&2; exit 1; }; '
        + command
    )


@pytest.mark.parametrize("kind", ["directory", "symlink", "dangling-symlink", "marker-symlink"])
def test_rocm_compat_root_refuses_unmanaged_layout(tmp_path, kind):
    root = tmp_path / "rocm"
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    sentinel = vendor / "preserve"
    sentinel.write_text("vendor data")
    if kind == "directory":
        root.mkdir()
        (root / "preserve").write_text("user data")
    elif kind == "marker-symlink":
        root.mkdir()
        marker = tmp_path / "marker"
        marker.write_text("alps-rocm-compat-v1\n")
        (root / ".alps-rocm-compat").symlink_to(marker)
    else:
        root.symlink_to(vendor if kind == "symlink" else tmp_path / "absent")
    result = run_bash(source_rocm('recreate_rocm_compat_root "$TEST_ROOT"'), env={"TEST_ROOT": str(root)}, check=False)
    assert result.returncode != 0
    assert "Refusing to replace unmanaged ROCm" in result.stderr
    assert sentinel.read_text() == "vendor data"
    if kind == "directory":
        assert (root / "preserve").read_text() == "user data"
    elif kind.endswith("symlink") and kind != "marker-symlink":
        assert root.is_symlink()


def test_rocm_compat_root_recreates_only_owned_tree(tmp_path):
    root = tmp_path / "rocm"
    root.mkdir()
    (root / ".alps-rocm-compat").write_text("alps-rocm-compat-v1\n")
    (root / "obsolete").write_text("owned")
    run_bash(source_rocm('recreate_rocm_compat_root "$TEST_ROOT"'), env={"TEST_ROOT": str(root)})
    assert not (root / "obsolete").exists()
    assert (root / ".alps-rocm-compat").read_text() == "alps-rocm-compat-v1\n"
    assert (root / "include").is_dir()


def fake_rocm_distributions(tmp_path, versions):
    site = tmp_path / "site"
    site.mkdir()
    for name, version in versions.items():
        dist = site / f"{name.replace('-', '_')}-{version}.dist-info"
        dist.mkdir()
        (dist / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
    return {"ROCM_PYTHON": sys.executable, "PYTHONPATH": str(site), "ROCM_VERSION": "7.14.0"}


@pytest.mark.parametrize(
    ("installed", "status"),
    [("7.14.0", 0), ("7.14.0+git.abc123", 0), ("7.14.0a20261015", 0), ("7.14.0.dev0+abc123", 0), ("7.14.0rc2", 0), ("7.13.0", 2), ("7.14.1", 2)],
)
def test_rocm_sdk_validates_distribution_release(tmp_path, installed, status):
    env = fake_rocm_distributions(tmp_path, {"rocm-sdk-core": installed})
    result = run_bash(source_rocm("rocm_sdk_distribution_present rocm-sdk-core"), env=env, check=False)
    assert result.returncode == status
    if status:
        assert f"installed {installed}, expected ROCm 7.14.0" in result.stderr


def test_rocm_sdk_detects_device_distribution_without_importable_module(tmp_path):
    env = fake_rocm_distributions(tmp_path, {"rocm-sdk-device-gfx942": "7.14.0"})
    result = run_bash(source_rocm("rocm_sdk_distribution_present rocm-sdk-device-gfx942"), env=env)
    assert result.returncode == 0
    missing = run_bash(source_rocm("rocm_sdk_distribution_present rocm-sdk-device-gfx90a"), env=env, check=False)
    assert missing.returncode == 1


@pytest.mark.parametrize("fail_install", [False, True])
def test_rocm_sdk_installs_absent_devel_and_propagates_network_failure(tmp_path, fail_install):
    versions = {name: "7.14.0" for name in ("rocm", "rocm-sdk-core", "rocm-sdk-libraries", "rocm-sdk-device-gfx942")}
    env = fake_rocm_distributions(tmp_path, versions)
    site = Path(env["PYTHONPATH"])
    pip = site / "pip"
    pip.mkdir()
    (pip / "__init__.py").write_text("")
    (pip / "__main__.py").write_text(
        "import os, sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['PIP_LOG']).write_text(' '.join(sys.argv[1:]))\n"
        "if os.environ['FAIL_INSTALL'] == '1':\n"
        "    sys.exit('network unavailable')\n"
        "site = Path(os.environ['PYTHONPATH'])\n"
        "dist = site / 'rocm_sdk_devel-7.14.0.dist-info'\n"
        "dist.mkdir()\n"
        "(dist / 'METADATA').write_text('Metadata-Version: 2.1\\nName: rocm-sdk-devel\\nVersion: 7.14.0\\n')\n"
    )
    log = tmp_path / "pip.log"
    env.update({
        "RCCL_GPU_TARGETS": "gfx942",
        "ROCM_PYPI_INDEX_URL": "https://example.invalid",
        "ROCM_LIBRARIES_REPO": "",
        "ROCM_LIBRARIES_COMMIT": "",
        "PIP_LOG": str(log),
        "FAIL_INSTALL": "1" if fail_install else "0",
    })
    result = run_bash(source_rocm('install_rocm_sdk_wheels; printf "devel=%s\\n" "$ROCM_HAVE_DEVEL"'), env=env, check=False)
    assert "rocm-sdk-devel==7.14.0" in log.read_text()
    if fail_install:
        assert result.returncode != 0
        assert "network unavailable" in result.stderr
        assert "Failed to install required ROCm SDK wheels" in result.stderr
    else:
        assert result.returncode == 0
        assert "devel=yes" in result.stdout


def test_rocm_sdk_source_pins_explicitly_allow_missing_devel(tmp_path):
    versions = {name: "7.14.0" for name in ("rocm", "rocm-sdk-core", "rocm-sdk-libraries", "rocm-sdk-device-gfx942")}
    env = fake_rocm_distributions(tmp_path, versions)
    env.update({"RCCL_GPU_TARGETS": "gfx942", "ROCM_LIBRARIES_REPO": "https://example.invalid/pinned.git", "ROCM_LIBRARIES_COMMIT": "a" * 40})
    result = run_bash(source_rocm('install_rocm_sdk_wheels; printf "devel=%s\\n" "$ROCM_HAVE_DEVEL"'), env=env)
    assert "devel=no" in result.stdout


def test_rocm_build_prefix_order_does_not_change_runtime_order():
    env = {"ROCM_CORE_PREFIX": "/wheel/core", "ROCM_LIBRARIES_PREFIX": "/wheel/libraries", "ROCM_DEVEL_PREFIX": "/wheel/devel"}
    runtime = run_bash(source_rocm("rocm_sdk_prefix_candidates"), env=env).stdout.splitlines()
    build = run_bash(source_rocm("rocm_sdk_prefix_candidates build"), env=env).stdout.splitlines()
    assert runtime[:3] == ["/wheel/core", "/wheel/libraries", "/opt/rocm"]
    assert build[:4] == ["/wheel/core", "/wheel/libraries", "/wheel/devel", "/opt/rocm"]


def test_rocm_714_rccl_test_sources_remain_unchanged(tmp_path):
    source = tmp_path / "projects" / "rccl-tests" / "src"
    source.mkdir(parents=True)
    cmake = source / "CMakeLists.txt"
    original = "add_library(rccl_common OBJECT ${HIP_COMMON_SOURCES})\n"
    cmake.write_text(original)
    run_bash(source_rocm("patch_rocm_rccl_test_sources"), env={"ROCM_VERSION": "7.14.0", "ROCM_SYSTEMS_SRC_DIR": str(tmp_path)})
    assert cmake.read_text() == original


def test_rocm_component_versions_come_from_source_templates(tmp_path):
    source = tmp_path / "source"
    root = tmp_path / "rocm"
    templates = {
        "hipblas": ("library/include/hipblas-version.h.in", "hipblas/hipblas-version.h"),
        "hipblaslt": ("library/include/hipblaslt-version.h.in", "hipblaslt/hipblaslt-version.h"),
        "hipsparselt": ("library/include/hipsparselt-version.h.in", "hipsparselt/hipsparselt-version.h"),
        "hipsparse": ("library/include/hipsparse-version.h.in", "hipsparse/hipsparse-version.h"),
        "hipsolver": ("library/include/internal/hipsolver-version.h.in", "hipsolver/internal/hipsolver-version.h"),
        "rocprim": ("rocprim/include/rocprim/rocprim_version.hpp.in", "rocprim/rocprim_version.hpp"),
        "hipcub": ("hipcub/include/hipcub/hipcub_version.hpp.in", "hipcub/hipcub_version.hpp"),
        "rocthrust": ("thrust/rocthrust_version.hpp.in", "thrust/rocthrust_version.hpp"),
        "rocrand": ("library/include/rocrand/rocrand_version.h.in", "rocrand/rocrand_version.h"),
        "hiprand": ("library/include/hiprand/hiprand_version.h.in", "hiprand/hiprand_version.h"),
    }
    for package in [*templates, "rocblas", "rocsolver", "miopen", "hipfft"]:
        project = source / "projects" / package
        project.mkdir(parents=True)
        declaration = 'set(VERSION_STRING "8.2.3")\n'
        if package == "hipsparselt":
            declaration = "project(hipsparselt VERSION 0.2.9 LANGUAGES CXX)\n"
        elif package == "hipblaslt":
            declaration = 'set(HIPBLASLT_PROJECT_VERSION "1.4.1" CACHE STRING "version")\n'
        elif package == "miopen":
            declaration = "rocm_setup_version(VERSION 3.6.0)\n"
        elif package == "hipfft":
            declaration = '\n'.join(f'set(HIPFFT_VERSION_{key} "{value}")' for key, value in zip(("MAJOR", "MINOR", "PATCH"), (1, 0, 25)))
        if package == "hipcub":
            declaration += "\n".join(f"set(HIPCUB_CCCL_VERSION_{key} {value})" for key, value in zip(("MAJOR", "MINOR", "PATCH"), (2, 8, 2)))
        if package == "hipsparse":
            declaration += '\noption(HIPSPARSE_ENABLE_SPMV_BSR "feature" ON)\n'
        (project / "CMakeLists.txt").write_text(declaration)
        if package in templates:
            template = project / templates[package][0]
            template.parent.mkdir(parents=True, exist_ok=True)
            content = f"version=@{package}_VERSION_MAJOR@.@{package}_VERSION_MINOR@.@{package}_VERSION_PATCH@\nnumber=@{package}_VERSION_NUMBER@\n"
            if package == "hipcub":
                content += "cccl=@HIPCUB_CCCL_VERSION_NUMBER@\n"
            if package == "hipsparse":
                content += "#cmakedefine HIPSPARSE_WITH_SPMV_BSR\n"
                (template.parent / "CMakeLists.txt").write_text("set(HIPSPARSE_WITH_SPMV_BSR ${HIPSPARSE_ENABLE_SPMV_BSR})\n")
            template.write_text(content)
    env = {"SOURCE": str(source), "TEST_ROOT": str(root), "ROCM_PYTHON": sys.executable}
    result = run_bash(source_rocm('generate_rocm_component_metadata "$SOURCE" "$TEST_ROOT"'), env=env)
    assert "hipsparselt=0.2.9" in result.stdout
    assert "hipblaslt=1.4.1" in result.stdout
    assert "hipfft=1.0.25" in result.stdout
    assert "number=800203" in (root / "include/rocprim/rocprim_version.hpp").read_text()
    assert "cccl=200802" in (root / "include/hipcub/hipcub_version.hpp").read_text()
    assert "#define HIPSPARSE_WITH_SPMV_BSR" in (root / "include/hipsparse/hipsparse-version.h").read_text()
    template = source / "projects/hipblas" / templates["hipblas"][0]
    template.write_text("@UNKNOWN_VERSION_FIELD@\n")
    failed = run_bash(source_rocm('generate_rocm_component_metadata "$SOURCE" "$TEST_ROOT"'), env=env, check=False)
    assert failed.returncode != 0
    assert "Unknown version template field" in failed.stderr


def test_rocm_development_link_is_created_at_real_wheel_location(tmp_path):
    wheel = tmp_path / "wheel" / "lib"
    compat = tmp_path / "rocm" / "lib"
    wheel.mkdir(parents=True)
    compat.mkdir(parents=True)
    library = wheel / "libamdhip64.so.7.0"
    library.write_text("runtime library")
    (wheel / "libamdhip64.so.7").symlink_to(library.name)
    runtime_link = compat / "libamdhip64.so.7"
    runtime_link.symlink_to(wheel / "libamdhip64.so.7")
    dev_link = compat / "libamdhip64.so"
    run_bash(
        source_rocm('link_rocm_development_library "$RUNTIME_LINK" "$DEV_LINK"'),
        env={"RUNTIME_LINK": str(runtime_link), "DEV_LINK": str(dev_link)},
    )
    assert dev_link.resolve() == library
    assert (wheel / "libamdhip64.so").resolve() == library


@pytest.mark.parametrize("mismatch", ["rocm", "rocm-sdk-core", "rocm-sdk-libraries", "rocm-sdk-device-gfx942"])
def test_rocm_sdk_refuses_mismatched_preinstalled_wheels(tmp_path, mismatch):
    versions = {name: "7.14.0" for name in ("rocm", "rocm-sdk-core", "rocm-sdk-libraries", "rocm-sdk-device-gfx942")}
    versions[mismatch] = "7.13.0"
    env = fake_rocm_distributions(tmp_path, versions)
    env.update({"RCCL_GPU_TARGETS": "gfx942", "ROCM_LIBRARIES_REPO": "https://example.invalid/pinned.git", "ROCM_LIBRARIES_COMMIT": "a" * 40})
    result = run_bash(source_rocm("install_rocm_sdk_wheels"), env=env, check=False)
    assert result.returncode != 0
    assert f"Incompatible {mismatch}: installed 7.13.0, expected ROCm 7.14.0" in result.stderr
