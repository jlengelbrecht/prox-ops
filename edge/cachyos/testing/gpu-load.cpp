// Simulated competing GPU workload, for exercising the interactive guard
// (STORY-035-6 AC4) without waiting for a human to launch a game.
//
// It does the two things a real interactive client does that the guard looks
// for: it holds VRAM, and it keeps the card busy. Run it while
// scripts/edge-interactive-guard.sh is watching and the node should withdraw
// within (EDGE_GUARD_INTERVAL x EDGE_GUARD_TRIP_SAMPLES) seconds plus the
// drain, then come back on its own once this exits and the release hold-down
// expires.
//
// This is a HIP program, so the guard sees it as a *compute* client — it trips
// the "more compute clients than models loaded" rule. A real game is a
// *graphics* client and trips the VRAM rule instead. The two paths are
// deliberately separate; see README.md §"Interactive priority".
//
// Arguments: <vram_mb> <seconds>   (defaults: 1024 MB, 60 s). Keep vram_mb
// inside the headroom a loaded model leaves — about a gigabyte on this card —
// or hipMalloc simply fails and the drill measures nothing.
//
// On a host with ROCm installed, from edge/cachyos:
//
//   hipcc --offload-arch=gfx1100 -O2 -o /tmp/gpu-load testing/gpu-load.cpp
//   GPU_LOAD_BIN=/tmp/gpu-load testing/interactive-drill.sh
//
// Without a host toolchain — the llama.cpp ROCm image ships the full ROCm SDK,
// so building and drilling can happen in one container:
//
//   docker run --rm --network host --device /dev/kfd --device /dev/dri \
//     -v "$EDGE_STATE_HOST_DIR":/edge/state:ro -v "$PWD":/work -w /work \
//     -e EDGE_BIND_ADDR=<edge-lan-ip> -e EDGE_PORT=8443 \
//     -e EDGE_STATE_DIR=/edge/state -e GPU_LOAD_BIN=/tmp/gpu-load \
//     --entrypoint bash ghcr.io/ggml-org/llama.cpp:server-rocm-b9917 -c \
//     'hipcc --offload-arch=gfx1100 -O2 -o /tmp/gpu-load testing/gpu-load.cpp &&
//      testing/interactive-drill.sh --vram-mb 512 --seconds 60'
//
// The compiled binary is a build artifact and does not belong in Git; build it
// to /tmp, not into this directory.

#include <hip/hip_runtime.h>
#include <cstdio>
#include <cstdlib>
#include <ctime>

__global__ void burn(float *p, long n, int iters) {
    long i = (long)blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= n) return;
    float v = p[i];
    for (int k = 0; k < iters; ++k) {
        v = v * 1.0000001f + 1e-7f;
    }
    p[i] = v;
}

#define CHECK(expr)                                                            \
    do {                                                                       \
        hipError_t _e = (expr);                                                \
        if (_e != hipSuccess) {                                                \
            fprintf(stderr, "hip error: %s\n", hipGetErrorString(_e));         \
            return 1;                                                          \
        }                                                                      \
    } while (0)

int main(int argc, char **argv) {
    long mb = (argc > 1) ? atol(argv[1]) : 1024;
    int secs = (argc > 2) ? atoi(argv[2]) : 60;
    size_t bytes = (size_t)mb * 1024 * 1024;

    float *d = nullptr;
    CHECK(hipMalloc(&d, bytes));
    CHECK(hipMemset(d, 0, bytes));

    long n = (long)(bytes / sizeof(float));
    printf("holding %ld MB of VRAM and burning the card for %ds\n", mb, secs);
    fflush(stdout);

    time_t deadline = time(nullptr) + secs;
    while (time(nullptr) < deadline) {
        hipLaunchKernelGGL(burn, dim3((n + 255) / 256), dim3(256), 0, 0, d, n, 400);
        CHECK(hipDeviceSynchronize());
    }

    CHECK(hipFree(d));
    printf("done\n");
    return 0;
}
