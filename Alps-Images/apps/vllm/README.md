# vLLM App Image

This image builds vLLM `v0.30.0` from source on top of `pytorch-cuda:26.07-py3` (CUDA) and `pytorch-rocm:rocm10.0-ubuntu24.04-py3.12-torch2.13` (ROCm).

vLLM `v0.30.0` targets Torch `2.13`, which exposes the stable-libtorch APIs (`Tensor::layout()`, the `from_blob` deleter overload, `torch._opaque_base`) that the previous NVIDIA Torch `2.11.0a0` snapshots were missing. Two narrow compatibility patches remain: both images disable CUDA graphs for the DeepEP high-throughput backend before the compilation-mode early return, and the CUDA image treats the optional `ll_bf16` kernel as unavailable when QuACK is not installed.
The ROCm image installs the matching upstream `amd-aiter` `0.1.21.post2` release from its pinned commit and sets both `PYTORCH_ROCM_ARCH` and `GPU_ARCHS` to `gfx942`, the ISA used by MI300A and MI300X. AITER's full build-time precompilation is disabled because its FlyDSL AOT path ignores `GPU_ARCHS` and compiles targets from multi-architecture CSV data. AITER remains enabled in vLLM and JIT-compiles required kernels for `gfx942` at runtime.

Both accelerator variants build UCCL's expert-parallel extension from upstream commit `e22a3e8fcd636f7efbbd90900458087a41ff73ce`, install its `uccl.ep` module and DeepEP-compatible `deep_ep` wrapper, and default the transport to Slingshot CXI. CXI domains are selected from each worker's node-wide physical GPU rank rather than its process-local visible-device index, so Ray workers that each see one logical GPU use the corresponding `cxi0` through `cxi3` device. The CUDA image builds the native extension for SM90, explicitly selects its Grace Hopper memory paths even when container builds cannot see a GPU, and uses libfabric's `FI_HMEM_CUDA` interface. The ROCm image applies five app-local compatibility fixes for the ROCm 10/CXI stack: use `FI_HMEM_ROCR` for AMD GPU allocations, select HIP's extended kernel-launch types and API, restore device math overloads removed in ROCm 10, snapshot volatile shared-memory heads before minimum comparisons, and check the runtime top-k rather than the compile-time maximum against MI300's wave64 dispatch capacity. `libibverbs-dev` and the other development-only packages are removed after the extension is built.

The CUDA image also installs Run:ai Model Streamer `0.16.1` and upgrades NIXL to vLLM's pinned `1.4.1` package. The CUDA CI smoke test loads the native NIXL bindings through vLLM, creates one agent per Ray worker, and validates that each agent publishes metadata. This checks package selection and runtime initialization without assuming a prefill/decode topology.

Both builds constrain Python package installation to the Torch and Triton versions supplied by their canonical base images; the CUDA build also preserves base-provided TorchVision and TorchAudio versions. A final image check rejects dependency resolution that replaces any of those packages. Runtime Python ignores user-site packages (`PYTHONNOUSERSITE=1`). Build-time package installation uses `package-helpers.sh`, which routes pip through the CSCS PyPI proxy without forcing that index on image users. Image labels record the pinned UCCL revision and, where applicable, the AITER revision, Model Streamer version, and NIXL version.

The CUDA source build uses 16 build jobs and four NVCC threads. This limits peak compiler concurrency relative to the previous 32-job configuration while retaining explicit build-argument overrides for controlled builders.

The two-node CUDA and ROCm CI tests import both UCCL modules, construct a low-latency UCCL-EP buffer across eight GPUs, and verify an identity dispatch/combine round trip. This exercises CXI endpoint setup, GPU-memory registration, and the UCCL data path in addition to the Ray NCCL/RCCL all-reduce.

The ROCm variant keeps a GPU-less-safe build-time smoke check: `import vllm` runs the platform probe and the module-level GCN-arch query, which initializes CUDA and fails on build containers without a visible GPU. The build only imports torch and locates the vLLM package via `importlib.util.find_spec`; the real ROCm `import vllm` coverage lives in the `ray-rccl-uccl-ep` test job.

Older vLLM releases may need fewer changes. Releases `0.22` and older are suspected to work without these compatibility patches, but that still needs to be verified against the Alps CUDA/HPC stack.

## Mooncake (CXI KV transfer)

Both variants additionally install the [Mooncake](https://github.com/kvcache-ai/Mooncake) transfer engine with the HPE Slingshot (CXI) backend (`USE_CXI`, [kvcache-ai/Mooncake#2535](https://github.com/kvcache-ai/Mooncake/pull/2535)). It is built from a pinned tag by `sources/install-mooncake.sh` (which is part of the app content hash) and installed as the `mooncake-transfer-engine` wheel, providing the `mooncake.engine` and `mooncake.store` modules used by vLLM's `MooncakeConnector` and `MooncakeStoreConnector` KV connectors.

Mooncake is compiled against the libfabric installed by the Alps base image (`/usr`), so it always matches the stack's libfabric version and shares a single in-process libfabric instance with the aws-ofi-nccl NCCL plugin. This is deliberate: a second, bundled libfabric would race for the CXI devices and leave one consumer with an empty provider list.

On Slingshot, select the CXI transport in the extra config instead of the default `rdma` protocol, e.g.:

```bash
vllm serve <model> \
    --kv-transfer-config \
    '{"kv_connector": "MooncakeConnector", "kv_role": "kv_producer",
      "kv_connector_extra_config": {"mooncake_protocol": "cxi"}}'
```

`device_name` may be left empty; Mooncake auto-discovers CXI devices. The image also ships the `mooncake_master` store service and the `transfer_engine_bench` utility for transport debugging. A single-node CXI smoke test runs in CI (`mooncake_smoke.sh`).
