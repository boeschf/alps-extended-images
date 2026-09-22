# vLLM App Image

This image builds vLLM `v0.30.0` from source on top of `pytorch-cuda:26.07-py3` (CUDA) and `pytorch-rocm:rocm10.0-ubuntu24.04-py3.12-torch2.13` (ROCm).

vLLM `v0.30.0` targets Torch `2.13`, which exposes the stable-libtorch APIs (`Tensor::layout()`, the `from_blob` deleter overload, `torch._opaque_base`) that the previous NVIDIA Torch `2.11.0a0` snapshots were missing. The compatibility patches that earlier versions needed are therefore gone.
The ROCm image installs the matching upstream `amd-aiter` `0.1.21.post2` release from its pinned commit and sets both `PYTORCH_ROCM_ARCH` and `GPU_ARCHS` to `gfx942`, the ISA used by MI300A and MI300X. AITER's full build-time precompilation is disabled because its FlyDSL AOT path ignores `GPU_ARCHS` and compiles targets from multi-architecture CSV data. AITER remains enabled in vLLM and JIT-compiles required kernels for `gfx942` at runtime.

The ROCm image also builds only UCCL's expert-parallel extension from upstream commit `e22a3e8fcd636f7efbbd90900458087a41ff73ce`, installs its `uccl.ep` module and DeepEP-compatible `deep_ep` wrapper, and defaults the transport to Slingshot CXI. Upstream supports ROCm and publishes MI300X results, but the pinned commit needs five app-local compatibility fixes with the ROCm 10/CXI stack: use libfabric's `FI_HMEM_ROCR` interface for AMD GPU allocations, select HIP's extended kernel-launch types and API, restore device math overloads removed in ROCm 10, snapshot volatile shared-memory heads before minimum comparisons, and check the runtime top-k rather than the compile-time maximum against MI300's wave64 dispatch capacity. `libibverbs-dev` and the other development packages are build-only and are removed after the extension is linked; the versioned verbs runtime library remains because UCCL-EP links it.

The two-node ROCm CI test imports both modules, constructs a low-latency UCCL-EP buffer across eight MI300 GPUs, and verifies an identity dispatch/combine round trip. This exercises CXI endpoint setup, GPU-memory registration, and the UCCL data path in addition to the existing Ray/RCCL all-reduce.

The ROCm variant keeps a GPU-less-safe build-time smoke check: `import vllm` runs the platform probe and the module-level GCN-arch query, which initializes CUDA and fails on build containers without a visible GPU. The build only imports torch and locates the vLLM package via `importlib.util.find_spec`; the real `import vllm` coverage lives in the `ray-rccl-uccl-ep` test job.

Older vLLM releases may need fewer changes. Releases `0.22` and older are suspected to work without these compatibility patches, but that still needs to be verified against the Alps CUDA/HPC stack.
