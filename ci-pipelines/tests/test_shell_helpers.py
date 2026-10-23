import json
import os
import re
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def run_bash(script, *, env=None, check=True, cwd=ROOT):
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=cwd,
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


def run_meta(command, *, check=True, cwd=ROOT, **extra_env):
    return run_bash(
        f"source ci-pipelines/helpers/skopeo.sh; source ci-pipelines/helpers/meta.sh; {command}",
        env=meta_env(**extra_env), check=check, cwd=cwd,
    )


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


@pytest.fixture()
def named_app_root(tmp_path):
    (tmp_path / "ci-pipelines").symlink_to(ROOT / "ci-pipelines", target_is_directory=True)
    images = tmp_path / "Alps-Images"
    images.mkdir()
    for name in ("NGC", "ROCm", "common", "patches"):
        (images / name).symlink_to(ROOT / "Alps-Images" / name, target_is_directory=True)
    app = images / "apps" / "apertus-2"
    app.mkdir(parents=True)
    targets = {
        "megatron-base-cuda": ("pytorch-cuda:25.12-py3", "Containerfile.megatron-base", "tests/megatron-base"),
        "megatron-cuda": ("pytorch-cuda:26.06-py3", "Containerfile.megatron", "tests/megatron"),
        "megatron-rocm": (
            "pytorch-rocm:rocm10.0-ubuntu24.04-py3.12-torch2.13",
            "Containerfile.megatron.rocm",
            "tests/rocm",
        ),
    }
    profile = [f'APP_VARIANTS="{" ".join(targets)}"']
    for target, (base, recipe, test_dir) in targets.items():
        prefix = target.upper().replace("-", "_")
        profile.extend([
            f"{prefix}_BASE_IMAGE={base}",
            f"{prefix}_CONTAINERFILE={recipe}",
            f"{prefix}_TEST_DIR={test_dir}",
        ])
        (app / recipe).write_text("ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\n")
        tests = app / test_dir
        tests.mkdir(parents=True)
        (tests / "check-stack.py").write_text("assert True\n")
    (app / "profile.env").write_text("\n".join(profile) + "\n")
    (app / "ci.yaml").write_text("tests: {}\n")
    return tmp_path


def write_named_target_profile(root, targets):
    profile = root / "Alps-Images/apps/apertus-2/profile.env"
    lines = [f'APP_VARIANTS="{" ".join(targets)}"']
    for target, base in targets.items():
        prefix = target.upper().replace("-", "_")
        lines.extend([
            f"{prefix}_BASE_IMAGE={base}",
            f"{prefix}_CONTAINERFILE=Containerfile.megatron",
            f"{prefix}_TEST_DIR=tests/megatron",
        ])
    profile.write_text("\n".join(lines) + "\n")


def test_meta_named_targets_use_the_selected_base(named_app_root):
    targets = {
        "megatron-base-cuda": ("cuda", "ngc_base_refs pytorch 25.12-py3", 3, "Containerfile.megatron-base"),
        "megatron-cuda": ("cuda", "ngc_base_refs pytorch 26.06-py3", 3, "Containerfile.megatron"),
        "megatron-rocm": (
            "rocm",
            "rocm_base_refs pytorch rocm10.0-ubuntu24.04-py3.12-torch2.13",
            2,
            "Containerfile.megatron.rocm",
        ),
    }
    for target, (family, command, ref_index, recipe) in targets.items():
        output = named_app_root / f"{target}.env"
        run_meta(f"write_app_build_env {output} apertus-2 {target}", cwd=named_app_root)
        data = dict(line.split("=", 1) for line in output.read_text().splitlines())
        base_ref = run_meta(command, cwd=named_app_root).stdout.split()[ref_index]

        assert data["FAMILY"] == family
        assert data["VARIANT"] == target
        assert data["BASE_IMAGE"] == base_ref
        assert data["DOCKERFILE"] == f"Alps-Images/apps/apertus-2/{recipe}"
        assert f"apertus-2-{target}:" in data["CANON_IMAGE_REF"]
        assert data["TESTED_IMAGE_REF"] == f'{data["CANON_IMAGE_REF"]}-tested-{data["VALIDATION_HASH"]}'


@pytest.mark.parametrize(
    "variants",
    ["megatron-base-cuda megatron_base-cuda", "megatron-cuda MEGATRON-cuda", "megatron-cuda megatron-cuda"],
)
@pytest.mark.parametrize("helper", ["app_refs", "app_validation_hash"])
def test_meta_rejects_colliding_target_prefixes(named_app_root, variants, helper):
    profile = named_app_root / "Alps-Images/apps/apertus-2/profile.env"
    profile.write_text(f'APP_VARIANTS="{variants}"\n')

    result = run_meta(f"{helper} apertus-2 {variants.split()[0]}", cwd=named_app_root, check=False)

    assert result.returncode != 0
    assert "share profile prefix" in result.stderr


@pytest.mark.parametrize(
    "target",
    ["megatron", "megatron-base", "megatron-tpu", "cuda-debug", "-cuda", "megatron.rocm"],
)
def test_meta_app_refs_reject_invalid_target_names(named_app_root, target):
    write_named_target_profile(named_app_root, {target: "pytorch-cuda:26.06-py3"})

    result = run_meta(f"app_refs apertus-2 {target}", cwd=named_app_root, check=False)

    assert result.returncode != 0
    assert target in result.stderr


@pytest.mark.parametrize("helper", ["app_refs", "app_validation_hash"])
def test_meta_consumers_reject_undeclared_target(named_app_root, helper):
    write_named_target_profile(named_app_root, {"megatron-cuda": "pytorch-cuda:26.06-py3"})

    result = run_meta(f"{helper} apertus-2 megatron-rocm", cwd=named_app_root, check=False)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "megatron-rocm" in result.stderr


@pytest.mark.parametrize(
    "target,family,base",
    [
        ("cuda", "cuda", "pytorch-cuda:26.06-py3"),
        ("rocm", "rocm", "pytorch-rocm:rocm10.0-ubuntu24.04-py3.12-torch2.13"),
        ("_team-rocm-stage-cuda", "cuda", "pytorch-cuda:26.06-py3"),
        ("team-cuda-stage-rocm", "rocm", "pytorch-rocm:rocm10.0-ubuntu24.04-py3.12-torch2.13"),
    ],
)
def test_meta_build_env_accepts_family_only_and_multi_part_targets(named_app_root, target, family, base):
    write_named_target_profile(named_app_root, {target: base})
    output = named_app_root / "build.env"

    run_meta(f"write_app_build_env {output} apertus-2 {target}", cwd=named_app_root)

    data = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert data["VARIANT"] == target
    assert data["FAMILY"] == family
    assert f"/pytorch-{family}:" in data["BASE_IMAGE"]
    assert f"/apertus-2-{target}:" in data["CANON_IMAGE_REF"]
    assert data["VALIDATION_HASH"]
    assert data["TESTED_IMAGE_REF"] == f'{data["CANON_IMAGE_REF"]}-tested-{data["VALIDATION_HASH"]}'


@pytest.mark.parametrize(
    "helper,target,base",
    [
        ("app_refs", "cuda-stage-rocm", "pytorch-cuda:26.06-py3"),
        ("app_refs", "cuda", "pytorch-rocm:rocm10.0-ubuntu24.04-py3.12-torch2.13"),
        ("app_validation_hash", "rocm-stage-cuda", "pytorch-rocm:rocm10.0-ubuntu24.04-py3.12-torch2.13"),
        ("app_validation_hash", "rocm", "pytorch-cuda:26.06-py3"),
    ],
)
def test_meta_consumers_reject_mismatches_in_unselected_targets(named_app_root, helper, target, base):
    write_named_target_profile(
        named_app_root,
        {"megatron-cuda": "pytorch-cuda:26.06-py3", target: base},
    )

    result = run_meta(f"{helper} apertus-2 megatron-cuda", cwd=named_app_root, check=False)

    assert result.returncode != 0
    assert target in result.stderr
    assert base in result.stderr


@pytest.mark.parametrize("failure", ["base-family-mismatch", "missing-validation-input"])
def test_meta_write_app_build_env_propagates_failures(named_app_root, failure):
    if failure == "base-family-mismatch":
        write_named_target_profile(
            named_app_root,
            {"megatron-cuda": "pytorch-rocm:rocm10.0-ubuntu24.04-py3.12-torch2.13"},
        )
    else:
        (named_app_root / "Alps-Images/apps/apertus-2/ci.yaml").unlink()
    output = named_app_root / "build.env"

    result = run_meta(f"write_app_build_env {output} apertus-2 megatron-cuda", cwd=named_app_root, check=False)

    assert result.returncode != 0
    assert result.stderr
    assert not output.exists()


@pytest.mark.parametrize(
    "target,recipe,test_dir",
    [
        ("megatron-base-cuda", "Containerfile.megatron-base", "tests/megatron-base"),
        ("megatron-cuda", "Containerfile.megatron", "tests/megatron"),
        ("megatron-rocm", "Containerfile.megatron.rocm", "tests/rocm"),
    ],
)
def test_megatron_hashes_use_the_selected_recipe_and_test_group(named_app_root, target, recipe, test_dir):
    app = named_app_root / "Alps-Images/apps/apertus-2"
    targets = ("megatron-base-cuda", "megatron-cuda", "megatron-rocm")
    refs = {name: run_meta(f"app_refs apertus-2 {name}", cwd=named_app_root).stdout for name in targets}
    validations = {
        name: run_meta(f"app_validation_hash apertus-2 {name}", cwd=named_app_root).stdout
        for name in targets
    }
    for relative in (recipe, f"{test_dir}/check-stack.py"):
        path = app / relative
        original = path.read_text()
        path.write_text(original + "\n# Changed hash input.\n")
        for name in targets:
            current_ref = run_meta(f"app_refs apertus-2 {name}", cwd=named_app_root).stdout
            current_validation = run_meta(f"app_validation_hash apertus-2 {name}", cwd=named_app_root).stdout
            assert (current_ref != refs[name]) == (name == target)
            assert (current_validation != validations[name]) == (name == target and relative.startswith("tests/"))
        path.write_text(original)


@pytest.mark.parametrize("helper", ["rocm_base_refs pytorch", "write_base_build_env"])
def test_rocm_base_helpers_preserve_optional_library_variable_scope(tmp_path, helper):
    variant = "rocm7.14-ubuntu24.04-py3.12-torch2.11"
    output = tmp_path / "base.env"
    command = (
        f"rocm_base_refs pytorch {variant}"
        if helper == "rocm_base_refs pytorch"
        else f"write_base_build_env {output} rocm pytorch {variant}"
    )
    result = run_meta(
        f'{command} && printf "\\nLIBRARY_SCOPE=%s,%s\\n" "$ROCM_LIBRARIES_REPO" "$ROCM_LIBRARIES_COMMIT"',
        ROCM_LIBRARIES_REPO="outer-repo",
        ROCM_LIBRARIES_COMMIT="outer-commit",
    )

    assert result.stdout.splitlines()[-1] == "LIBRARY_SCOPE=outer-repo,outer-commit"
    if output.exists():
        data = dict(line.split("=", 1) for line in output.read_text().splitlines())
        profile = f"Alps-Images/ROCm/pytorch-{variant}/profile.env"
        for key in ("ROCM_LIBRARIES_REPO", "ROCM_LIBRARIES_COMMIT"):
            assert data[key] == run_meta(f"profile_value {profile} {key}").stdout.strip()


def test_manual_build_generates_both_megatron_targets(tmp_path):
    for target, recipe in (
        ("megatron-base-cuda", "Containerfile.megatron-base"),
        ("megatron-cuda", "Containerfile.megatron"),
    ):
        script = generate_manual("app", "apertus-2", target, output=tmp_path / f"{target}.sh")

        assert f"Alps-Images/apps/apertus-2/{recipe}" in script
        assert re.search(
            r'--build-arg BASE_IMAGE="localhost/alps-images/pytorch-cuda:.*-[0-9a-f]{32}"',
            script,
        )
