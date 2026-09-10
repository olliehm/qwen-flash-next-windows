# Building the engine on Windows (gfx1151 / HIP)

Qwen3.8-Flash-Next's architecture (`qwen4exp`) with MTP speculative decoding is
not in stock llama.cpp yet. The required patches exist as
[stew675/llama-cpp-rdna-boosts](https://github.com/stew675/llama-cpp-rdna-boosts) —
a mailbox-patch delivery repo (all patches authored by Stew Forster), carrying
the qwen4exp/MTP work from upstream PRs
[#27836](https://github.com/ggml-org/llama.cpp/pull/27836), #28243 and #28118
plus RDNA perf blocks. This page is the Windows build recipe for that patch set.

Nothing binary is redistributed here — you build from source and copy runtime
DLLs from your own ROCm SDK.

## Toolchain

| Piece | What we used | Notes |
|---|---|---|
| MSVC | VS 2022 Build Tools | ROCm's clang targets the **MSVC ABI** — `vcvars64.bat` must be active so `cl`/`link`/STL/WinSDK resolve |
| ROCm SDK | TheRock nightly, `therock-dist-windows-gfx1151-10.1.0a20260822.tar.gz` | [ROCm/TheRock](https://github.com/ROCm/TheRock) Windows dist for gfx1151; extract anywhere (ours: `D:\rocm-sdk\rocm`) |
| CMake | 3.30.5 | portable zip is fine |
| Ninja | any recent | |

## Get the source

```powershell
git clone https://github.com/ggml-org/llama.cpp
git clone https://github.com/stew675/llama-cpp-rdna-boosts
cd llama.cpp
git checkout <fork-point recorded in the patch repo>   # see its README
..\llama-cpp-rdna-boosts\scripts\apply-all.sh          # or: git am the blocks in order
```

The patch repo records the exact upstream fork point it was rebased on; apply
to that commit, not to master-of-the-day.

## Configure

`configure.bat` (adjust the three roots):

```bat
@echo off
call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
set "ROCM_PATH=D:\rocm-sdk\rocm"
set "HIP_PATH=D:\rocm-sdk\rocm"
set "PATH=D:\rocm-sdk\rocm\bin;D:\rocm-sdk\rocm\lib\llvm\bin;<cmake>\bin;<ninja>;%PATH%"
cd /d <llama.cpp checkout>

cmake -S . -B build -G Ninja ^
  -DGGML_HIP=ON ^
  -DGGML_HIP_RCCL=OFF ^
  -DGPU_TARGETS=gfx1151 ^
  -DAMDGPU_TARGETS=gfx1151 ^
  -DGGML_HIP_GRAPHS=ON ^
  -DGGML_NATIVE=ON ^
  -DCMAKE_BUILD_TYPE=Release ^
  -DCMAKE_C_COMPILER=D:/rocm-sdk/rocm/lib/llvm/bin/clang.exe ^
  -DCMAKE_CXX_COMPILER=D:/rocm-sdk/rocm/lib/llvm/bin/clang++.exe ^
  -DCMAKE_HIP_COMPILER=D:/rocm-sdk/rocm/lib/llvm/bin/clang++.exe ^
  -DCMAKE_PREFIX_PATH=D:/rocm-sdk/rocm ^
  -DLLAMA_CURL=OFF ^
  -DLLAMA_BUILD_TESTS=OFF ^
  -DLLAMA_BUILD_EXAMPLES=OFF
```

## Build

```bat
@echo off
call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
set "ROCM_PATH=D:\rocm-sdk\rocm"
set "HIP_PATH=D:\rocm-sdk\rocm"
set "HIP_DEVICE_LIB_PATH=D:\rocm-sdk\rocm\lib\llvm\amdgcn\bitcode"
set "PATH=D:\rocm-sdk\rocm\bin;D:\rocm-sdk\rocm\lib\llvm\bin;<cmake>\bin;<ninja>;%PATH%"
cd /d <llama.cpp checkout>
cmake --build build --target llama-server llama-cli llama-bench -j 16
```

`HIP_DEVICE_LIB_PATH` matters at build time: without it the HIP compiler can
fail to find the amdgcn bitcode device libraries.

## Package the runtime DLLs

To run the binaries outside the build tree (or hand them to Lemonade), copy
these next to the exes **from your own SDK**:

```
amdhip64_7.dll   amd_comgr.dll     hipblas.dll      libhipblaslt.dll
hiprtc0716.dll   hiprtc-builtins0716.dll            rocblas.dll
rocsolver.dll    rocm_kpack.dll    origami.dll
rocblas\library\   (tensile kernels dir)
hipblaslt\library\ (hipblaslt kernels dir)
```

(Exact hiprtc/amdhip64 version suffixes track your SDK version.)

## Verify — with a stripped PATH and real inference

A build that works in your shell can still be broken on deployment: if the
ROCm SDK is on your PATH, the exes silently resolve DLLs from there and you
have not tested the package you are about to ship. And `--version` proves
nothing about GPU bring-up.

```powershell
$env:PATH = "C:\Windows\System32"        # strip everything
cd <packaged engine dir>
.\llama-cli.exe -m <small.gguf> -ngl 99 -p "2+2=" -n 8
```

Real tokens out on the GPU with only System32 on PATH = the package is
self-contained.

## Vulkan warning

On this machine (96 GB carve / 32 GB host split), the Vulkan backend places
model buffers host-side, which invites the WDDM-spill freeze described in the
shim README. HIP/ROCm only. Also never pass `-md` (draft model) to a stock
engine that lacks the qwen4exp arch — it fails at load with
`unknown model architecture: 'qwen4exp'`.
