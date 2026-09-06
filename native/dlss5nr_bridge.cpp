// SPDX-License-Identifier: MIT
// Copyright (c) 2026 ComfyUI-DLSS5-NR contributors

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <d3d11.h>
#include <d3d12.h>
#include <dxgi1_4.h>
#include <wrl/client.h>

#include <algorithm>
#include <cmath>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <string>
#include <vector>

using Microsoft::WRL::ComPtr;
using NGXResult = int;
static constexpr NGXResult NGX_SUCCESS = 1;
static constexpr int NR_FEATURE_ID = 18;
static constexpr int DLSS_FEATURE_ID = 1;
// Feature 18 is a post-pass in the Merserk/RenoDX integration.  The
// performance selector belongs to the ordinary DLSS carrier; the neural
// snippet is created at its own fixed post-pass quality value.
static constexpr int NR_POSTPASS_PERF_QUALITY = 6;
// The DLSSNR 310.8 reference snippet used by the Linux/Wine test runtime is
// registered under the same project id as the RenoDX carrier.  Keeping these
// identifiers configurable is useful because NGX applications are normally
// issued their own id, and a snippet may reject an unrelated project with
// FAIL_OutOfDate/FAIL_Denied even though D3D12 itself is healthy.
static constexpr unsigned long long APP_ID = 141959980ULL;
static constexpr unsigned long long GENERIC_APP_ID = 0x24480451ULL;
static constexpr const char* PROJECT_ID = "53f803cc-a12f-4d69-90d5-19b7599cad19";

struct NGXHandle { unsigned int Id; };

// Minimal ABI-compatible interface used by the NVIDIA NGX parameter object.
struct NGXParameter {
    virtual void Set(const char*, unsigned long long) = 0;
    virtual void Set(const char*, float) = 0;
    virtual void Set(const char*, double) = 0;
    virtual void Set(const char*, unsigned int) = 0;
    virtual void Set(const char*, int) = 0;
    virtual void Set(const char*, ID3D11Resource*) = 0;
    virtual void Set(const char*, ID3D12Resource*) = 0;
    virtual void Set(const char*, void*) = 0;
    virtual NGXResult Get(const char*, unsigned long long*) const = 0;
    virtual NGXResult Get(const char*, float*) const = 0;
    virtual NGXResult Get(const char*, double*) const = 0;
    virtual NGXResult Get(const char*, unsigned int*) const = 0;
    virtual NGXResult Get(const char*, int*) const = 0;
    virtual NGXResult Get(const char*, ID3D11Resource**) const = 0;
    virtual NGXResult Get(const char*, ID3D12Resource**) const = 0;
    virtual NGXResult Get(const char*, void**) const = 0;
    virtual void Reset() = 0;
};

struct NGXPathListInfo {
    wchar_t const* const* Path;
    unsigned int Length;
};
enum NGXLoggingLevel { NGX_LOG_OFF = 0, NGX_LOG_ON = 1, NGX_LOG_VERBOSE = 2 };
using NGXLogCallback = void(__cdecl*)(const char*, NGXLoggingLevel, int);
struct NGXLoggingInfo {
    // SDK 0x14 layout: callback first, then the minimum level.  Earlier
    // revisions of this bridge used the old internal ordering here, which
    // made the R580 core read a function pointer as a logging enum and reject
    // initialization with FAIL_OutOfDate under Wine.
    NGXLogCallback LoggingCallback;
    NGXLoggingLevel MinimumLoggingLevel;
    bool DisableOtherLoggingSinks;
};
struct NGXFeatureCommonInfoInternal;
struct NGXFeatureCommonInfo {
    NGXPathListInfo PathListInfo;
    NGXFeatureCommonInfoInternal* InternalData;
    NGXLoggingInfo LoggingInfo;
};

using InitExtFn = NGXResult(__cdecl*)(unsigned long long, const wchar_t*, ID3D12Device*, int, const void*);
using SnippetInitFn = NGXResult(__cdecl*)(unsigned long long, const wchar_t*, ID3D12Device*, const void*, int);
using InitProjectIdFn = NGXResult(__cdecl*)(const char*, int, const char*, const wchar_t*, ID3D12Device*, int, const void*);
using AllocParamsFn = NGXResult(__cdecl*)(NGXParameter**);
using GetCapabilityParamsFn = NGXResult(__cdecl*)(NGXParameter**);
using CreateFeatureFn = NGXResult(__cdecl*)(ID3D12GraphicsCommandList*, int, NGXParameter*, NGXHandle**);
using EvaluateFeatureFn = NGXResult(__cdecl*)(ID3D12GraphicsCommandList*, const NGXHandle*, const NGXParameter*, void*);
using ReleaseFeatureFn = NGXResult(__cdecl*)(NGXHandle*);
using ShutdownFn = NGXResult(__cdecl*)();

using ShimInitFn = NGXResult(__cdecl*)(void*, unsigned long long, const wchar_t*, ID3D12Device*, int, const void*);
using ShimCreateFn = NGXResult(__cdecl*)(void*, ID3D12GraphicsCommandList*, int, NGXParameter*, NGXHandle**);
using ShimEvaluateFn = NGXResult(__cdecl*)(void*, ID3D12GraphicsCommandList*, const NGXHandle*, const NGXParameter*, void*);
using ShimReleaseFn = NGXResult(__cdecl*)(void*, NGXHandle*);
using ShimShutdownFn = NGXResult(__cdecl*)(void*);

static std::mutex g_mutex;
static std::string g_last_error;
static std::wstring g_runtime_dir;
static int g_gpu_index = 0;
static std::string g_gpu_name = "unknown";
static bool g_initialized = false;

static HMODULE g_core_mod = nullptr;
static HMODULE g_nr_mod = nullptr;
static HMODULE g_shim_mod = nullptr;
static HMODULE g_nvapi_mod = nullptr;
static InitExtFn g_core_init_ext = nullptr;
static InitProjectIdFn g_core_init_project = nullptr;
static AllocParamsFn g_alloc_params = nullptr;
static GetCapabilityParamsFn g_get_capability_params = nullptr;
static CreateFeatureFn g_core_create = nullptr;
static EvaluateFeatureFn g_core_eval = nullptr;
static ReleaseFeatureFn g_core_release = nullptr;
static ShutdownFn g_core_shutdown = nullptr;
static SnippetInitFn g_nr_init = nullptr;
static CreateFeatureFn g_nr_create = nullptr;
static EvaluateFeatureFn g_nr_eval = nullptr;
static ReleaseFeatureFn g_nr_release = nullptr;
static ShutdownFn g_nr_shutdown = nullptr;
static ShimInitFn g_shim_init = nullptr;
static ShimCreateFn g_shim_create = nullptr;
static ShimEvaluateFn g_shim_eval = nullptr;
static ShimReleaseFn g_shim_release = nullptr;
static ShimShutdownFn g_shim_shutdown = nullptr;
using NvapiInitializeFn = int(__cdecl*)();
using NvapiUnloadFn = int(__cdecl*)();
static NvapiInitializeFn g_nvapi_initialize = nullptr;
static NvapiUnloadFn g_nvapi_unload = nullptr;

static ComPtr<ID3D12Device> g_device;
static ComPtr<ID3D12CommandQueue> g_queue;
static ComPtr<ID3D12CommandAllocator> g_cmd_alloc;
static ComPtr<ID3D12GraphicsCommandList> g_cmd;
static ComPtr<ID3D12Fence> g_fence;
static UINT64 g_fence_value = 0;

static NGXParameter* g_params = nullptr;
static NGXHandle* g_feature = nullptr;
static NGXHandle* g_dlss_feature = nullptr;
static ComPtr<ID3D12Resource> g_color;
static ComPtr<ID3D12Resource> g_mvec;
static ComPtr<ID3D12Resource> g_depth;
static ComPtr<ID3D12Resource> g_dlss_output;
static ComPtr<ID3D12Resource> g_output;
static ComPtr<ID3D12Resource> g_color_upload;
static ComPtr<ID3D12Resource> g_mvec_upload;
static ComPtr<ID3D12Resource> g_depth_upload;
static ComPtr<ID3D12Resource> g_output_readback;
static UINT g_input_width = 0, g_input_height = 0;
static UINT g_output_width = 0, g_output_height = 0;
static UINT g_color_row_pitch = 0, g_mvec_row_pitch = 0, g_depth_row_pitch = 0, g_output_row_pitch = 0;
static UINT64 g_color_bytes = 0, g_mvec_bytes = 0, g_depth_bytes = 0, g_output_bytes = 0;
static bool g_upscale_active = false;
static bool g_dlss_ready = false;
static bool g_dlss_output_readable = false;
static UINT64 g_neural_evaluations = 0;
static int g_feature_style = -999;
static int g_feature_preset = -999;
static int g_feature_perf_quality = -999;
static int g_float_set_slot = -1;
static bool g_capability_params = false;

static unsigned long long EnvU64(const char* name, unsigned long long fallback) {
    char buf[128] = {};
    DWORD n = GetEnvironmentVariableA(name, buf, static_cast<DWORD>(sizeof(buf)));
    if (n == 0 || n >= sizeof(buf)) return fallback;
    char* end = nullptr;
    unsigned long long value = _strtoui64(buf, &end, 0);
    return end != buf ? value : fallback;
}

static int EnvInt(const char* name, int fallback) {
    char buf[64] = {};
    DWORD n = GetEnvironmentVariableA(name, buf, static_cast<DWORD>(sizeof(buf)));
    if (n == 0 || n >= sizeof(buf)) return fallback;
    char* end = nullptr;
    long value = strtol(buf, &end, 0);
    return end != buf ? static_cast<int>(value) : fallback;
}

static std::string EnvString(const char* name, const char* fallback) {
    char buf[512] = {};
    DWORD n = GetEnvironmentVariableA(name, buf, static_cast<DWORD>(sizeof(buf)));
    return n == 0 || n >= sizeof(buf) ? std::string(fallback) : std::string(buf, n);
}

static void __cdecl NGXLog(const char* message, NGXLoggingLevel level, int source) {
    if (!message) return;
    std::fprintf(stderr, "[ngx source=%d level=%d] %s\n", source, static_cast<int>(level), message);
    std::fflush(stderr);
}

static void SetError(const char* fmt, ...) {
    char buf[4096];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    g_last_error = buf;
}

static void CopyError(char* dst, int cap) {
    if (!dst || cap <= 0) return;
    const size_t n = std::min<size_t>(g_last_error.size(), static_cast<size_t>(cap - 1));
    memcpy(dst, g_last_error.data(), n);
    dst[n] = '\0';
}

static std::wstring Join(const std::wstring& a, const std::wstring& b) {
    if (a.empty()) return b;
    wchar_t c = a.back();
    if (c == L'\\' || c == L'/') return a + b;
    return a + L"\\" + b;
}

static bool FileExists(const std::wstring& p) {
    DWORD a = GetFileAttributesW(p.c_str());
    return a != INVALID_FILE_ATTRIBUTES && !(a & FILE_ATTRIBUTE_DIRECTORY);
}

static unsigned long long FileTimeKey(const FILETIME& ft) {
    ULARGE_INTEGER u{};
    u.LowPart = ft.dwLowDateTime;
    u.HighPart = ft.dwHighDateTime;
    return u.QuadPart;
}

static HMODULE LoadCoreNGX(const std::wstring& runtime) {
    // 1) Explicit local override. This is also the quickest workaround if
    // DriverStore auto-discovery ever misses a vendor-specific INF name.
    const std::wstring local = Join(runtime, L"_nvngx.dll");
    if (FileExists(local)) {
        if (HMODULE m = LoadLibraryW(local.c_str())) return m;
    }

    // 2) Normal loader search (works on systems where NVIDIA exposes it).
    if (HMODULE m = LoadLibraryW(L"_nvngx.dll")) return m;

    // 3) NVIDIA ships NGX core inside the active display-driver package in
    // DriverStore. The INF prefix is NOT always nv_dispi: depending on OEM,
    // notebook/desktop package and driver generation it can be nvddi, nvaci,
    // nvhmui, etc. Scan every NVIDIA-looking *.inf_* package instead.
    wchar_t windows_dir[MAX_PATH] = {};
    UINT windows_len = GetWindowsDirectoryW(windows_dir, MAX_PATH);
    if (windows_len == 0 || windows_len >= MAX_PATH) return nullptr;
    const std::wstring repo = std::wstring(windows_dir) + L"\\System32\\DriverStore\\FileRepository";
    const std::wstring pat = repo + L"\\nv*.inf_*";

    struct Candidate {
        std::wstring path;
        unsigned long long stamp;
    };
    std::vector<Candidate> candidates;

    WIN32_FIND_DATAW fd{};
    HANDLE h = FindFirstFileW(pat.c_str(), &fd);
    if (h != INVALID_HANDLE_VALUE) {
        do {
            if (!(fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)) continue;
            if (wcscmp(fd.cFileName, L".") == 0 || wcscmp(fd.cFileName, L"..") == 0) continue;

            std::wstring candidate = repo + L"\\" + fd.cFileName + L"\\_nvngx.dll";
            WIN32_FILE_ATTRIBUTE_DATA fad{};
            if (GetFileAttributesExW(candidate.c_str(), GetFileExInfoStandard, &fad) &&
                !(fad.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)) {
                candidates.push_back({candidate, FileTimeKey(fad.ftLastWriteTime)});
            }
        } while (FindNextFileW(h, &fd));
        FindClose(h);
    }

    // Prefer the newest package. DriverStore often retains older drivers after
    // updates, and loading a stale NGX core is worse than not finding one.
    std::sort(candidates.begin(), candidates.end(),
              [](const Candidate& a, const Candidate& b) { return a.stamp > b.stamp; });

    for (const Candidate& c : candidates) {
        if (HMODULE m = LoadLibraryW(c.path.c_str())) return m;
    }

    return nullptr;
}

static ComPtr<ID3D12Device> CreateDevice(int nvidia_index) {
    ComPtr<IDXGIFactory4> factory;
    if (FAILED(CreateDXGIFactory1(IID_PPV_ARGS(&factory)))) return nullptr;

    int seen = 0;
    for (UINT i = 0;; ++i) {
        ComPtr<IDXGIAdapter1> adapter;
        if (factory->EnumAdapters1(i, &adapter) == DXGI_ERROR_NOT_FOUND) break;
        DXGI_ADAPTER_DESC1 desc{};
        adapter->GetDesc1(&desc);
        if ((desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) || desc.VendorId != 0x10DE) continue;
        if (seen++ != nvidia_index) continue;
        char gpu_utf8[512] = {};
        WideCharToMultiByte(CP_UTF8, 0, desc.Description, -1, gpu_utf8, static_cast<int>(sizeof(gpu_utf8)), nullptr, nullptr);
        if (gpu_utf8[0]) g_gpu_name = gpu_utf8;
        ComPtr<ID3D12Device> d;
        // Wine's D3D12 implementation can expose a fully usable D3D12 device
        // while rejecting the 12_0 feature-level probe.  The NGX snippets use
        // resource/queue interfaces rather than shader-model feature queries,
        // so retrying at 11_0 is a valid compatibility fallback and keeps the
        // native Windows path unchanged.
        if (SUCCEEDED(D3D12CreateDevice(adapter.Get(), D3D_FEATURE_LEVEL_12_0, IID_PPV_ARGS(&d))) ||
            SUCCEEDED(D3D12CreateDevice(adapter.Get(), D3D_FEATURE_LEVEL_11_0, IID_PPV_ARGS(&d))))
            return d;
        return nullptr;
    }
    return nullptr;
}

static bool SetupD3D12() {
    g_device = CreateDevice(g_gpu_index);
    if (!g_device) { SetError("Could not create a D3D12 device for NVIDIA GPU index %d", g_gpu_index); return false; }

    D3D12_COMMAND_QUEUE_DESC q{};
    q.Type = D3D12_COMMAND_LIST_TYPE_DIRECT;
    if (FAILED(g_device->CreateCommandQueue(&q, IID_PPV_ARGS(&g_queue)))) { SetError("CreateCommandQueue failed"); return false; }
    if (FAILED(g_device->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, IID_PPV_ARGS(&g_cmd_alloc)))) { SetError("CreateCommandAllocator failed"); return false; }
    if (FAILED(g_device->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, g_cmd_alloc.Get(), nullptr, IID_PPV_ARGS(&g_cmd)))) { SetError("CreateCommandList failed"); return false; }
    if (FAILED(g_device->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&g_fence)))) { SetError("CreateFence failed"); return false; }
    return true;
}

static bool ExecuteAndWait() {
    HRESULT hr = g_cmd->Close();
    if (FAILED(hr)) { SetError("CommandList::Close failed (0x%08X)", static_cast<unsigned>(hr)); return false; }
    ID3D12CommandList* lists[] = { g_cmd.Get() };
    g_queue->ExecuteCommandLists(1, lists);
    ++g_fence_value;
    hr = g_queue->Signal(g_fence.Get(), g_fence_value);
    if (FAILED(hr)) { SetError("Queue::Signal failed (0x%08X)", static_cast<unsigned>(hr)); return false; }
    if (g_fence->GetCompletedValue() < g_fence_value) {
        HANDLE ev = CreateEventW(nullptr, FALSE, FALSE, nullptr);
        if (!ev) { SetError("CreateEvent failed"); return false; }
        g_fence->SetEventOnCompletion(g_fence_value, ev);
        DWORD w = WaitForSingleObject(ev, 30000);
        CloseHandle(ev);
        if (w != WAIT_OBJECT_0) { SetError("Timed out waiting for DLSS5 NR GPU work"); return false; }
    }
    g_cmd_alloc->Reset();
    g_cmd->Reset(g_cmd_alloc.Get(), nullptr);
    return true;
}

static void WaitQueueIdle() {
    if (!g_queue || !g_fence) return;
    ++g_fence_value;
    if (SUCCEEDED(g_queue->Signal(g_fence.Get(), g_fence_value)) && g_fence->GetCompletedValue() < g_fence_value) {
        HANDLE ev = CreateEventW(nullptr, FALSE, FALSE, nullptr);
        if (ev) {
            g_fence->SetEventOnCompletion(g_fence_value, ev);
            WaitForSingleObject(ev, 30000);
            CloseHandle(ev);
        }
    }
}

static D3D12_RESOURCE_BARRIER Barrier(ID3D12Resource* r, D3D12_RESOURCE_STATES before, D3D12_RESOURCE_STATES after) {
    D3D12_RESOURCE_BARRIER b{};
    b.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    b.Transition.pResource = r;
    b.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
    b.Transition.StateBefore = before;
    b.Transition.StateAfter = after;
    return b;
}

static ComPtr<ID3D12Resource> CreateTexture(
    UINT w, UINT h, DXGI_FORMAT format, D3D12_RESOURCE_STATES state, D3D12_RESOURCE_FLAGS flags) {
    D3D12_RESOURCE_DESC d{};
    d.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    d.Width = w; d.Height = h; d.DepthOrArraySize = 1; d.MipLevels = 1;
    d.Format = format;
    d.SampleDesc.Count = 1;
    d.Layout = D3D12_TEXTURE_LAYOUT_UNKNOWN;
    d.Flags = flags;
    D3D12_HEAP_PROPERTIES hp{};
    hp.Type = D3D12_HEAP_TYPE_DEFAULT;
    ComPtr<ID3D12Resource> r;
    if (FAILED(g_device->CreateCommittedResource(&hp, D3D12_HEAP_FLAG_NONE, &d, state, nullptr, IID_PPV_ARGS(&r))))
        return nullptr;
    return r;
}

static ComPtr<ID3D12Resource> CreateLinearBuffer(UINT64 bytes, D3D12_HEAP_TYPE type, D3D12_RESOURCE_STATES state) {
    D3D12_RESOURCE_DESC d{};
    d.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    d.Width = bytes; d.Height = 1; d.DepthOrArraySize = 1; d.MipLevels = 1;
    d.SampleDesc.Count = 1; d.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    D3D12_HEAP_PROPERTIES hp{};
    hp.Type = type;
    ComPtr<ID3D12Resource> r;
    if (FAILED(g_device->CreateCommittedResource(&hp, D3D12_HEAP_FLAG_NONE, &d, state, nullptr, IID_PPV_ARGS(&r))))
        return nullptr;
    return r;
}

static void DiscoverFloatSetter() {
    if (g_float_set_slot >= 0 || !g_params) return;
    // The NGX parameter object is built with the MSVC ABI.  Its overloaded
    // methods are grouped by argument family rather than emitted in the
    // declaration order reproduced by a MinGW caller: the shipping R580
    // consumer uses Set(ID3D12Resource*) at slot 0, Set(int/uint) at slot 3,
    // and Set(float) at slot 6.  Calling slot 1 (the apparent public-SDK
    // position) silently invokes a different overload and leaves every float
    // parameter at an invalid/default value, which the DLSS carrier reports
    // as 0xBAD00005.  Keep the mapping deterministic and ABI-safe.
    g_float_set_slot = 6;
    std::fprintf(stderr, "[dlss5nr] capability float setter vtable slot=%d (MSVC NGX ABI)\n", g_float_set_slot);
}

// The core-owned capability map used by the R580/Linux runtime does not always
// expose the public C++ overloads in the order declared by the SDK headers.
// Keep all writes in one place so the ordinary DLSS carrier and the signed
// feature-18 snippet use exactly the same ABI-safe setters.
static void SetParamUInt(const char* key, unsigned int value) {
    if (!g_params) return;
    void** vt = *reinterpret_cast<void***>(g_params);
    using Fn = void(__thiscall*)(void*, const char*, unsigned int);
    // NGX's MSVC parameter ABI exposes the signed/unsigned 32-bit setter in
    // the same integer slot.  The bit pattern is identical for the values
    // used by feature contracts (dimensions, flags and booleans).
    reinterpret_cast<Fn>(vt[3])(g_params, key, value);
}

static void SetParamFloat(const char* key, float value) {
    if (!g_params) return;
    void** vt = *reinterpret_cast<void***>(g_params);
    using Fn = void(__thiscall*)(void*, const char*, float);
    const int slot = g_float_set_slot >= 0 ? g_float_set_slot : 1;
    reinterpret_cast<Fn>(vt[slot])(g_params, key, value);
}

static void SetParamResource(const char* key, ID3D12Resource* value) {
    if (!g_params) return;
    void** vt = *reinterpret_cast<void***>(g_params);
    using Fn = void(__thiscall*)(void*, const char*, unsigned long long);
    reinterpret_cast<Fn>(vt[0])(g_params, key, reinterpret_cast<unsigned long long>(value));
}

static void SetParamPointer(const char* key, void* value) {
    if (!g_params) return;
    void** vt = *reinterpret_cast<void***>(g_params);
    using Fn = void(__thiscall*)(void*, const char*, void*);
    // Pointer-valued Set overloads share the resource family immediately
    // after Set(ID3D12Resource*) in the MSVC vtable.  Slot 2 is the generic
    // void* overload used for callbacks such as
    // DLSSNRComputeScalingRatioCallback.
    reinterpret_cast<Fn>(vt[2])(g_params, key, value);
}

static NGXResult __cdecl ScalingRatioCallback(NGXParameter* parameters) {
    if (!parameters) return static_cast<NGXResult>(0xBAD00005);
    // The callback is invoked by Feature 18 while it validates its create
    // contract.  It must see a post-pass (1x) ratio because the preceding
    // ordinary DLSS carrier already performed the requested enlargement.
    if (parameters == g_params) SetParamFloat("DLSSNR.ScalingRatio", 1.0f);
    else {
        void** vt = *reinterpret_cast<void***>(parameters);
        using Fn = void(__thiscall*)(void*, const char*, float);
        const int slot = g_float_set_slot >= 0 ? g_float_set_slot : 1;
        reinterpret_cast<Fn>(vt[slot])(parameters, "DLSSNR.ScalingRatio", 1.0f);
    }
    return NGX_SUCCESS;
}

static uint16_t FloatToHalf(float f) {
    uint32_t x; memcpy(&x, &f, sizeof(x));
    uint32_t s = (x >> 16) & 0x8000u;
    int32_t e = static_cast<int32_t>((x >> 23) & 0xff) - 127 + 15;
    uint32_t m = x & 0x7fffffu;
    if (e <= 0) {
        if (e < -10) return static_cast<uint16_t>(s);
        m = (m | 0x800000u) >> (1 - e);
        return static_cast<uint16_t>(s | (m >> 13));
    }
    if (e >= 31) return static_cast<uint16_t>(s | 0x7c00u);
    return static_cast<uint16_t>(s | (static_cast<uint32_t>(e) << 10) | (m >> 13));
}

// Feature 18 does not accept arbitrary scaling ratios.  The 310.8 runtime
// derives its network shape from PerfQualityValue and ScalingRatio, using the
// same fixed modes exposed by the Merserk worker.  Keep this mapping in the
// native boundary too, so a caller cannot accidentally pair (for example)
// the Performance quality selector with a slightly different geometric
// ratio caused by even-pixel rounding.
static bool FixedScalingRatio(int perf_quality, float* ratio) {
    if (!ratio) return false;
    switch (perf_quality) {
        case 5: *ratio = 1.0f; break;   // DLAA / native
        case 2: *ratio = 1.5f; break;   // Quality
        case 1: *ratio = 1.724f; break; // Balanced
        case 0: *ratio = 2.0f; break;   // Performance
        case 3: *ratio = 3.0f; break;   // Ultra Performance
        default: return false;
    }
    return true;
}

static float HalfToFloat(uint16_t h) {
    uint32_t s = (h >> 15) & 1, e = (h >> 10) & 0x1f, m = h & 0x3ff, x;
    if (e == 0) {
        if (m == 0) x = s << 31;
        else {
            e = 1;
            while (!(m & 0x400)) { m <<= 1; --e; }
            m &= 0x3ff;
            x = (s << 31) | ((e + 112) << 23) | (m << 13);
        }
    } else if (e == 0x1f) x = (s << 31) | 0x7f800000u | (m << 13);
    else x = (s << 31) | ((e + 112) << 23) | (m << 13);
    float f; memcpy(&f, &x, sizeof(f)); return f;
}

static void ReleaseFeatureAndResources() {
    WaitQueueIdle();
    if (g_feature) {
        if (g_nr_release && g_shim_release) g_shim_release(reinterpret_cast<void*>(g_nr_release), g_feature);
        else if (g_core_release) g_core_release(g_feature);
        g_feature = nullptr;
    }
    if (g_dlss_feature) {
        if (g_core_release) g_core_release(g_dlss_feature);
        g_dlss_feature = nullptr;
    }
    g_color.Reset(); g_mvec.Reset(); g_depth.Reset(); g_dlss_output.Reset(); g_output.Reset();
    g_color_upload.Reset(); g_mvec_upload.Reset(); g_depth_upload.Reset(); g_output_readback.Reset();
    g_input_width = g_input_height = 0;
    g_output_width = g_output_height = 0;
    g_color_row_pitch = g_mvec_row_pitch = g_depth_row_pitch = g_output_row_pitch = 0;
    g_color_bytes = g_mvec_bytes = g_depth_bytes = g_output_bytes = 0;
    g_upscale_active = false;
    g_dlss_output_readable = false;
    g_neural_evaluations = 0;
    g_feature_style = -999; g_feature_preset = -999;
    g_feature_perf_quality = -999;
}

static bool AllocateFrameResources(UINT input_w, UINT input_h, UINT output_w, UINT output_h) {
    g_color = CreateTexture(input_w, input_h, DXGI_FORMAT_R16G16B16A16_FLOAT,
                            D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,
                            D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS);
    g_mvec = CreateTexture(input_w, input_h, DXGI_FORMAT_R16G16_FLOAT,
                           D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,
                           D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS);
    // Video has no scene depth.  A constant, typed depth surface is still
    // required by the ordinary DLSS carrier contract; with a flat depth field
    // the carrier behaves as a stable image upscaler instead of rejecting the
    // evaluate for a missing input.  The matching upload is initialized once
    // and copied into the texture during the first frame.
    g_depth = CreateTexture(input_w, input_h, DXGI_FORMAT_R32_FLOAT,
                            D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,
                            D3D12_RESOURCE_FLAG_NONE);
    if (g_upscale_active) {
        g_dlss_output = CreateTexture(output_w, output_h, DXGI_FORMAT_R16G16B16A16_FLOAT,
                                       D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
                                       D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS);
    }
    g_output = CreateTexture(output_w, output_h, DXGI_FORMAT_R16G16B16A16_FLOAT,
                             D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
                             D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS);
    if (!g_color || !g_mvec || !g_depth || !g_output || (g_upscale_active && !g_dlss_output)) {
        SetError("Failed to create DLSSNR input/output/motion textures"); return false;
    }

    g_color_row_pitch = (input_w * 8u + 255u) & ~255u;
    g_mvec_row_pitch = (input_w * 4u + 255u) & ~255u;
    g_depth_row_pitch = (input_w * 4u + 255u) & ~255u;
    g_output_row_pitch = (output_w * 8u + 255u) & ~255u;
    g_color_bytes = static_cast<UINT64>(g_color_row_pitch) * input_h;
    g_mvec_bytes = static_cast<UINT64>(g_mvec_row_pitch) * input_h;
    g_depth_bytes = static_cast<UINT64>(g_depth_row_pitch) * input_h;
    g_output_bytes = static_cast<UINT64>(g_output_row_pitch) * output_h;
    g_color_upload = CreateLinearBuffer(g_color_bytes, D3D12_HEAP_TYPE_UPLOAD, D3D12_RESOURCE_STATE_GENERIC_READ);
    g_mvec_upload = CreateLinearBuffer(g_mvec_bytes, D3D12_HEAP_TYPE_UPLOAD, D3D12_RESOURCE_STATE_GENERIC_READ);
    g_depth_upload = CreateLinearBuffer(g_depth_bytes, D3D12_HEAP_TYPE_UPLOAD, D3D12_RESOURCE_STATE_GENERIC_READ);
    g_output_readback = CreateLinearBuffer(g_output_bytes, D3D12_HEAP_TYPE_READBACK, D3D12_RESOURCE_STATE_COPY_DEST);
    if (!g_color_upload || !g_mvec_upload || !g_depth_upload || !g_output_readback) {
        SetError("Failed to create DLSSNR upload/readback buffers"); return false;
    }
    g_input_width = input_w; g_input_height = input_h;
    g_output_width = output_w; g_output_height = output_h;
    return true;
}

static void SetDLSSCarrierParams(int perf_quality, int reset) {
    if (!g_params) return;

    // This is the ordinary DLSS Super Resolution contract.  It is deliberately
    // kept separate from the DLSSNR.* aliases below: the carrier must be
    // created/evaluated with render-sized Color and output-sized Output before
    // Feature 18 sees the frame.
    SetParamUInt("CreationNodeMask", 1);
    SetParamUInt("VisibilityNodeMask", 1);
    SetParamUInt("Width", g_input_width);
    SetParamUInt("Height", g_input_height);
    SetParamUInt("OutWidth", g_output_width);
    SetParamUInt("OutHeight", g_output_height);
    SetParamUInt("ResourceWidth", g_input_width);
    SetParamUInt("ResourceHeight", g_input_height);
    SetParamUInt("ResourceOutWidth", g_output_width);
    SetParamUInt("ResourceOutHeight", g_output_height);
    SetParamUInt("PerfQualityValue", static_cast<unsigned int>(std::max(0, perf_quality)));

    // A video frame has no depth buffer, but DLSS still requires the resource
    // and the feature-create flags.  The bridge uploads a constant R32_FLOAT
    // surface (0.0); with the inverted-depth flag this represents the far
    // plane and keeps the carrier contract deterministic.
    constexpr unsigned int kMvLowRes = 1u << 1;
    constexpr unsigned int kDepthInverted = 1u << 3;
    SetParamUInt("DLSS.Feature.Create.Flags", kMvLowRes | kDepthInverted);
    SetParamUInt("DLSS.Enable.Output.Subrects", 0);
    SetParamUInt("DLSS.Input.Color.Subrect.Base.X", 0);
    SetParamUInt("DLSS.Input.Color.Subrect.Base.Y", 0);
    SetParamUInt("DLSS.Input.Depth.Subrect.Base.X", 0);
    SetParamUInt("DLSS.Input.Depth.Subrect.Base.Y", 0);
    SetParamUInt("DLSS.Input.MV.Subrect.Base.X", 0);
    SetParamUInt("DLSS.Input.MV.Subrect.Base.Y", 0);
    SetParamUInt("DLSS.Output.Subrect.Base.X", 0);
    SetParamUInt("DLSS.Output.Subrect.Base.Y", 0);
    SetParamUInt("DLSS.Render.Subrect.Dimensions.Width", g_input_width);
    SetParamUInt("DLSS.Render.Subrect.Dimensions.Height", g_input_height);
    SetParamFloat("DLSS.Pre.Exposure", 1.0f);
    SetParamFloat("DLSS.Exposure.Scale", 1.0f);
    SetParamFloat("Jitter.Offset.X", 0.0f);
    SetParamFloat("Jitter.Offset.Y", 0.0f);
    SetParamFloat("Sharpness", 0.0f);
    SetParamFloat("MV.Scale.X", 1.0f);
    SetParamFloat("MV.Scale.Y", 1.0f);
    SetParamUInt("Reset", static_cast<unsigned int>(reset ? 1 : 0));
    SetParamResource("Color", g_color.Get());
    SetParamResource("MotionVectors", g_mvec.Get());
    SetParamResource("Depth", g_depth.Get());
    SetParamResource("Output", g_dlss_output.Get());
}

static void SetNeuralParams(
    int style, int preset, int perf_quality,
    float intensity, float tone, float structure, float skin, int automask, int reset) {
    if (!g_params) return;

    // The neural snippet is a post-pass, not a second spatial upscaler.  In
    // upscale mode its Color is the high-resolution carrier result and both
    // ColorSubrect and OutputSubrect are the output dimensions.  Only the
    // guide subrects remain at the original render dimensions.
    const UINT neural_color_width = g_upscale_active ? g_output_width : g_input_width;
    const UINT neural_color_height = g_upscale_active ? g_output_height : g_input_height;
    const int neural_quality = g_upscale_active ? NR_POSTPASS_PERF_QUALITY : perf_quality;

    SetParamUInt("DLSSNR.Width", g_output_width);
    SetParamUInt("DLSSNR.Height", g_output_height);
    SetParamUInt("DLSSNR.InputWidth", neural_color_width);
    SetParamUInt("DLSSNR.InputHeight", neural_color_height);
    SetParamUInt("DLSSNR.OutputWidth", g_output_width);
    SetParamUInt("DLSSNR.OutputHeight", g_output_height);
    SetParamUInt("DLSSNR.Output.Width", g_output_width);
    SetParamUInt("DLSSNR.Output.Height", g_output_height);
    SetParamUInt("PerfQualityValue", static_cast<unsigned int>(std::max(0, neural_quality)));
    SetParamUInt("DLSSNR.Enabled", 1);
    SetParamUInt("DLSSNR.Reset", static_cast<unsigned int>(reset ? 1 : 0));
    SetParamUInt("DLSSNR.Style", static_cast<unsigned int>(std::max(0, style)));
    SetParamUInt("DLSSNR.Hint.Render.Preset", static_cast<unsigned int>(std::max(0, preset)));
    SetParamUInt("DLSSNR.Upscaling", 0);
    SetParamFloat("DLSSNR.Intensity", intensity);
    SetParamFloat("DLSSNR.LocalToneStrength", tone);
    SetParamFloat("DLSSNR.LocalStructureStrength", structure);
    SetParamFloat("DLSSNR.GlobalToneStrength", 1.0f);
    SetParamFloat("DLSSNR.SkinStructureStrength", skin);
    SetParamUInt("DLSSNR.UseAutoMask", static_cast<unsigned int>(automask ? 1 : 0));
    SetParamUInt("DLSSNR.UICorrection", 0);
    SetParamUInt("DLSSNR.DepthInverted", 1);
    SetParamFloat("DLSSNR.ScalingRatio", 1.0f);
    SetParamFloat("DLSSNR.Scale", 1.0f);
    SetParamFloat("DLSSNR.MVecScaleX", 1.0f);
    SetParamFloat("DLSSNR.MVecScaleY", 1.0f);
    SetParamPointer("DLSSNRComputeScalingRatioCallback", reinterpret_cast<void*>(&ScalingRatioCallback));

    ID3D12Resource* color = g_upscale_active ? g_dlss_output.Get() : g_color.Get();
    SetParamResource("DLSSNR.Color", color);
    SetParamResource("DLSSNR.MVec", g_mvec.Get());
    SetParamResource("DLSSNR.Depth", g_depth.Get());
    SetParamResource("DLSSNR.Output", g_output.Get());
    // NR blends against Backbuffer below full intensity. Using its destination
    // here blends with an empty or previous frame instead of the current color.
    SetParamResource("DLSSNR.Backbuffer", color);
    SetParamUInt("DLSSNR.ColorSubrectBaseX", 0);
    SetParamUInt("DLSSNR.ColorSubrectBaseY", 0);
    SetParamUInt("DLSSNR.ColorSubrectWidth", neural_color_width);
    SetParamUInt("DLSSNR.ColorSubrectHeight", neural_color_height);
    SetParamUInt("DLSSNR.MVecSubrectBaseX", 0);
    SetParamUInt("DLSSNR.MVecSubrectBaseY", 0);
    SetParamUInt("DLSSNR.MVecSubrectWidth", g_input_width);
    SetParamUInt("DLSSNR.MVecSubrectHeight", g_input_height);
    SetParamUInt("DLSSNR.DepthSubrectBaseX", 0);
    SetParamUInt("DLSSNR.DepthSubrectBaseY", 0);
    SetParamUInt("DLSSNR.DepthSubrectWidth", g_input_width);
    SetParamUInt("DLSSNR.DepthSubrectHeight", g_input_height);
    SetParamUInt("DLSSNR.OutputSubrectBaseX", 0);
    SetParamUInt("DLSSNR.OutputSubrectBaseY", 0);
    SetParamUInt("DLSSNR.OutputSubrectWidth", g_output_width);
    SetParamUInt("DLSSNR.OutputSubrectHeight", g_output_height);
}

static bool EnsureFeature(
    UINT input_w, UINT input_h, UINT output_w, UINT output_h,
    int style, int preset, int perf_quality,
    float intensity, float tone, float structure, float skin, int automask) {
    // Match this node pack's DLAA/SR carrier at every scale, including 1x.
    const bool requested_upscale = true;
    const bool rebuild = !g_feature || input_w != g_input_width || input_h != g_input_height ||
        output_w != g_output_width || output_h != g_output_height ||
        style != g_feature_style || preset != g_feature_preset || perf_quality != g_feature_perf_quality ||
        requested_upscale != g_upscale_active;
    if (!rebuild) {
        if (g_upscale_active) SetDLSSCarrierParams(perf_quality, 0);
        SetNeuralParams(style, preset, perf_quality, intensity, tone, structure, skin, automask, 0);
        return true;
    }

    ReleaseFeatureAndResources();
    g_upscale_active = requested_upscale;
    // Use NGX core for the carrier.  In a ProjectID session the core has
    // already discovered nvngx_dlss.dll (the runtime log reports the 310.8
    // snippet under the DLSS feature), and its Create/Evaluate entry points
    // preserve the internal session metadata that a direct snippet Init_Ext
    // call cannot recreate.  The carrier is deliberately routed through the
    // core rather than called through a separately loaded snippet.
    if (g_upscale_active && !g_dlss_ready) {
        SetError("DLSS Super Resolution carrier is unavailable: NGX core D3D12 Create/Evaluate/Release exports are missing");
        g_upscale_active = false;
        return false;
    }
    if (!AllocateFrameResources(input_w, input_h, output_w, output_h)) return false;

    if (g_upscale_active) {
        SetDLSSCarrierParams(perf_quality, 1);
        NGXResult carrier = g_core_create(g_cmd.Get(), DLSS_FEATURE_ID, g_params, &g_dlss_feature);
        std::fprintf(stderr, "[dlss5nr] DLSS carrier CreateFeature(1) -> 0x%08X handle=%p (%ux%u -> %ux%u, perf=%d)\n",
                     static_cast<unsigned>(carrier), static_cast<void*>(g_dlss_feature),
                     input_w, input_h, output_w, output_h, perf_quality);
        if (carrier != NGX_SUCCESS || !g_dlss_feature) {
            SetError("DLSS carrier CreateFeature(1) failed: 0x%08X. Check nvngx_dlss.dll and driver support.",
                     static_cast<unsigned>(carrier));
            ReleaseFeatureAndResources();
            return false;
        }
        // DLSS creation may upload model state to the command list. Submit it
        // before creating/evaluating the signed feature-18 post-pass, matching
        // the ordering used by the RenoDX carrier.
        if (!ExecuteAndWait()) {
            ReleaseFeatureAndResources();
            return false;
        }
    }

    SetNeuralParams(style, preset, perf_quality, intensity, tone, structure, skin, automask, 1);

    NGXResult r;
    if (g_nr_create && g_shim_create)
        r = g_shim_create(reinterpret_cast<void*>(g_nr_create), g_cmd.Get(), NR_FEATURE_ID, g_params, &g_feature);
    else
        r = g_core_create(g_cmd.Get(), NR_FEATURE_ID, g_params, &g_feature);
    std::fprintf(stderr, "[dlss5nr] Feature18 CreateFeature(18) -> 0x%08X handle=%p (%ux%u -> %ux%u, carrier=%d)\n",
                 static_cast<unsigned>(r), static_cast<void*>(g_feature),
                 input_w, input_h, output_w, output_h, g_upscale_active ? 1 : 0);
    if (r != NGX_SUCCESS || !g_feature) {
        SetError("CreateFeature(18) failed: 0x%08X. Check GPU support, driver, nvngx_dlssnr.dll, and caller shim.", static_cast<unsigned>(r));
        ReleaseFeatureAndResources();
        return false;
    }
    // CreateFeature may enqueue model/network initialization on the command
    // list.  The reference player submits and waits for that work before its
    // first EvaluateFeature; evaluating on the still-unsubmitted list makes
    // the 310.8 snippet report the otherwise opaque 0xBAD00005
    // (InvalidParameter).  ExecuteAndWait also leaves g_cmd open and clean for
    // the frame upload/evaluate sequence below.
    if (!ExecuteAndWait()) {
        ReleaseFeatureAndResources();
        return false;
    }
    g_feature_style = style;
    g_feature_preset = preset;
    g_feature_perf_quality = perf_quality;
    return true;
}

static bool LoadNGX() {
    // The Linux DXVK-NVAPI implementation is lazy: unlike the Windows
    // display driver it does not necessarily initialize its adapter registry
    // when a client DLL is loaded.  NGX's core asks NvAPI for the D3D12-device
    // LUID during Init, so explicitly initialize the compatibility layer first
    // when it is present.  On native Windows this is harmless and simply
    // returns the driver's normal status.
    g_nvapi_mod = LoadLibraryW(L"nvapi64.dll");
    if (g_nvapi_mod) {
        g_nvapi_initialize = reinterpret_cast<NvapiInitializeFn>(GetProcAddress(g_nvapi_mod, "NvAPI_Initialize"));
        g_nvapi_unload = reinterpret_cast<NvapiUnloadFn>(GetProcAddress(g_nvapi_mod, "NvAPI_Unload"));
        if (g_nvapi_initialize) {
            const int nr = g_nvapi_initialize();
            std::fprintf(stderr, "[dlss5nr] NvAPI_Initialize -> 0x%08X\n", static_cast<unsigned>(nr));
        }
    }
    g_core_mod = LoadCoreNGX(g_runtime_dir);
    if (!g_core_mod) {
        SetError("Could not load NVIDIA NGX core _nvngx.dll. Tried runtime\\_nvngx.dll, normal DLL search, and NVIDIA DriverStore packages matching nv*.inf_*. You can copy the _nvngx.dll from your active NVIDIA DriverStore folder into runtime\\_nvngx.dll as an explicit override.");
        return false;
    }

    const std::wstring nr_path = Join(g_runtime_dir, L"nvngx_dlssnr.dll");
    if (!FileExists(nr_path)) { SetError("nvngx_dlssnr.dll not found in runtime folder"); return false; }
    g_nr_mod = LoadLibraryW(nr_path.c_str());
    if (!g_nr_mod) { SetError("LoadLibrary(nvngx_dlssnr.dll) failed: Win32 %lu", GetLastError()); return false; }

    std::wstring shim_path = Join(Join(g_runtime_dir, L"caller"), L"nvngx.dll_comfy.dll");
    if (!FileExists(shim_path)) {
        // Backward-compatible fallback for older builds.
        shim_path = Join(Join(g_runtime_dir, L"caller"), L"nvngx.dll");
    }
    if (!FileExists(shim_path)) { SetError("caller shim not found (expected caller\\nvngx.dll_comfy.dll)"); return false; }
    g_shim_mod = LoadLibraryW(shim_path.c_str());
    if (!g_shim_mod) { SetError("LoadLibrary(caller shim) failed: Win32 %lu", GetLastError()); return false; }

    g_core_init_ext = reinterpret_cast<InitExtFn>(GetProcAddress(g_core_mod, "NVSDK_NGX_D3D12_Init_Ext"));
    g_core_init_project = reinterpret_cast<InitProjectIdFn>(GetProcAddress(g_core_mod, "NVSDK_NGX_D3D12_Init_ProjectID"));
    g_alloc_params = reinterpret_cast<AllocParamsFn>(GetProcAddress(g_core_mod, "NVSDK_NGX_D3D12_AllocateParameters"));
    g_get_capability_params = reinterpret_cast<GetCapabilityParamsFn>(GetProcAddress(g_core_mod, "NVSDK_NGX_D3D12_GetCapabilityParameters"));
    g_core_create = reinterpret_cast<CreateFeatureFn>(GetProcAddress(g_core_mod, "NVSDK_NGX_D3D12_CreateFeature"));
    g_core_eval = reinterpret_cast<EvaluateFeatureFn>(GetProcAddress(g_core_mod, "NVSDK_NGX_D3D12_EvaluateFeature"));
    g_core_release = reinterpret_cast<ReleaseFeatureFn>(GetProcAddress(g_core_mod, "NVSDK_NGX_D3D12_ReleaseFeature"));
    g_core_shutdown = reinterpret_cast<ShutdownFn>(GetProcAddress(g_core_mod, "NVSDK_NGX_D3D12_Shutdown"));

    g_nr_init = reinterpret_cast<SnippetInitFn>(GetProcAddress(g_nr_mod, "NVSDK_NGX_D3D12_Init_Ext"));
    g_nr_create = reinterpret_cast<CreateFeatureFn>(GetProcAddress(g_nr_mod, "NVSDK_NGX_D3D12_CreateFeature"));
    g_nr_eval = reinterpret_cast<EvaluateFeatureFn>(GetProcAddress(g_nr_mod, "NVSDK_NGX_D3D12_EvaluateFeature"));
    g_nr_release = reinterpret_cast<ReleaseFeatureFn>(GetProcAddress(g_nr_mod, "NVSDK_NGX_D3D12_ReleaseFeature"));
    g_nr_shutdown = reinterpret_cast<ShutdownFn>(GetProcAddress(g_nr_mod, "NVSDK_NGX_D3D12_Shutdown"));

    g_shim_init = reinterpret_cast<ShimInitFn>(GetProcAddress(g_shim_mod, "DLSSNR_CallInit"));
    g_shim_create = reinterpret_cast<ShimCreateFn>(GetProcAddress(g_shim_mod, "DLSSNR_CallCreate"));
    g_shim_eval = reinterpret_cast<ShimEvaluateFn>(GetProcAddress(g_shim_mod, "DLSSNR_CallEvaluate"));
    g_shim_release = reinterpret_cast<ShimReleaseFn>(GetProcAddress(g_shim_mod, "DLSSNR_CallRelease"));
    g_shim_shutdown = reinterpret_cast<ShimShutdownFn>(GetProcAddress(g_shim_mod, "DLSSNR_CallShutdown"));

    if ((!g_core_init_ext && !g_core_init_project) ||
        (!g_alloc_params && !g_get_capability_params) || !g_core_create ||
        !g_core_eval || !g_core_release || !g_core_shutdown) {
        SetError("Required NGX core exports are missing (need Init_Ext or Init_ProjectID, parameter allocation, and D3D12 Create/Evaluate/Release/Shutdown)");
        return false;
    }
    if (!g_nr_init || !g_nr_create || !g_nr_eval || !g_nr_release || !g_nr_shutdown) {
        SetError("Required DLSSNR exports are missing from nvngx_dlssnr.dll"); return false;
    }
    if (!g_shim_init || !g_shim_create || !g_shim_eval || !g_shim_release || !g_shim_shutdown) {
        SetError("Required caller shim exports are missing"); return false;
    }
    return true;
}

static bool InitNGXSession() {
    const wchar_t* paths[1] = { g_runtime_dir.c_str() };
    NGXPathListInfo pli{ paths, 1 };
    NGXFeatureCommonInfo fci{};
    fci.PathListInfo = pli;
    fci.LoggingInfo.LoggingCallback = NGXLog;
    fci.LoggingInfo.MinimumLoggingLevel = NGX_LOG_VERBOSE;
    // The host uses stdout as a binary frame transport. NGX's application
    // callback writes to stderr, while its optional file/console sinks are
    // controlled explicitly so they can never corrupt the protocol stream.
    // Set DLSS5NR_DISABLE_OTHER_SINKS=1 for a quieter production run.
    fci.LoggingInfo.DisableOtherLoggingSinks = EnvInt("DLSS5NR_DISABLE_OTHER_SINKS", 0) != 0;

    bool core_ok = false;
    NGXResult last_project_result = 0;
    NGXResult last_ext_result = 0;
    const int ver = EnvInt("DLSS5NR_SDK_VERSION", 0x15);
    const unsigned long long app_id = EnvU64("DLSS5NR_APP_ID", APP_ID);
    const unsigned long long generic_id = EnvU64("DLSS5NR_GENERIC_APP_ID", GENERIC_APP_ID);
    // Keep each value in its own string.  Returning a pointer to one shared
    // temporary buffer made a pair of environment overrides overwrite one
    // another (the project id silently became the engine version).
    const std::string project_id = EnvString("DLSS5NR_PROJECT_ID", PROJECT_ID);
    const std::string engine_version = EnvString("DLSS5NR_ENGINE_VERSION", "0.1");
    const std::string init_mode = EnvString("DLSS5NR_INIT_MODE", "project");

    // A failed NGX init is not safely retryable in the same core instance on
    // every driver build.  The default therefore makes one deliberate call;
    // set DLSS5NR_INIT_MODE=ext or =all for compatibility probing in a fresh
    // process.  ProjectID is the route used by the DLSS5 carrier.
    if ((init_mode == "project" || init_mode == "all") && g_core_init_project) {
        NGXResult r = g_core_init_project(project_id.c_str(), 0, engine_version.c_str(), g_runtime_dir.c_str(), g_device.Get(), ver, &fci);
        last_project_result = r;
        core_ok = (r == NGX_SUCCESS);
        std::fprintf(stderr, "[dlss5nr] Init_ProjectID project=%s app=%llu sdk=0x%X -> 0x%08X\n",
                     project_id.c_str(), app_id, ver, static_cast<unsigned>(r));
    }
    const bool project_export_missing = !g_core_init_project;
    if (!core_ok &&
        ((init_mode == "ext" || init_mode == "all") ||
         (init_mode == "project" && project_export_missing)) &&
        g_core_init_ext) {
        NGXResult r = g_core_init_ext(app_id, g_runtime_dir.c_str(), g_device.Get(), ver, &fci);
        last_ext_result = r;
        core_ok = (r == NGX_SUCCESS);
        std::fprintf(stderr, "[dlss5nr] Init_Ext app=%llu sdk=0x%X -> 0x%08X\n",
                     app_id, ver, static_cast<unsigned>(r));
    }
    if (!core_ok && init_mode == "generic" && g_core_init_ext) {
        NGXResult r = g_core_init_ext(generic_id, g_runtime_dir.c_str(), g_device.Get(), ver, &fci);
        last_ext_result = r;
        core_ok = (r == NGX_SUCCESS);
        std::fprintf(stderr, "[dlss5nr] Init_Ext generic app=0x%llX sdk=0x%X -> 0x%08X\n",
                     generic_id, ver, static_cast<unsigned>(r));
    }
    if (!core_ok) {
        SetError("NGX core initialization failed (mode=%s, project=0x%08X, ext=0x%08X)",
                 init_mode.c_str(), static_cast<unsigned>(last_project_result),
                 static_cast<unsigned>(last_ext_result));
        return false;
    }

    // A normal DLSS snippet is initialized and selected by NGX core as part of
    // the ProjectID session.  The carrier below therefore uses
    // g_core_create/g_core_eval, exactly as a regular NGX application would;
    // calling nvngx_dlss.dll's Init_Ext directly bypasses that routing and
    // returns 0xBAD00002 on the 310.8 runtime.
    g_dlss_ready = g_core_create && g_core_eval && g_core_release;
    std::fprintf(stderr, "[dlss5nr] DLSS carrier routed through NGX core (ready=%d)\n",
                 g_dlss_ready ? 1 : 0);

    NGXResult sr = g_shim_init(reinterpret_cast<void*>(g_nr_init), app_id, g_runtime_dir.c_str(), g_device.Get(), ver, &fci);
    if (sr != NGX_SUCCESS) {
        wchar_t shim_self[MAX_PATH] = L"<unknown>";
        GetModuleFileNameW(g_shim_mod, shim_self, MAX_PATH);
        char shim_utf8[MAX_PATH * 3] = {};
        WideCharToMultiByte(CP_UTF8, 0, shim_self, -1, shim_utf8, static_cast<int>(sizeof(shim_utf8)), nullptr, nullptr);
        SetError("DLSSNR snippet Init_Ext via caller shim failed: 0x%08X; loaded shim=%s", static_cast<unsigned>(sr), shim_utf8);
        return false;
    }

    // Feature 18 is not self-contained: the signed snippet expects the
    // capability map returned by the core, which carries its callbacks and
    // feature metadata.  A fresh AllocateParameters map can still let
    // CreateFeature succeed but then returns InvalidParameter at Evaluate.
    NGXResult ar = 0;
    if (g_get_capability_params) {
        ar = g_get_capability_params(&g_params);
        g_capability_params = (ar == NGX_SUCCESS && g_params != nullptr);
        std::fprintf(stderr, "[dlss5nr] GetCapabilityParameters -> 0x%08X (%p)\n",
                     static_cast<unsigned>(ar), static_cast<void*>(g_params));
    }
    if (!g_capability_params && g_alloc_params) {
        ar = g_alloc_params(&g_params);
        g_capability_params = false;
        std::fprintf(stderr, "[dlss5nr] AllocateParameters fallback -> 0x%08X (%p)\n",
                     static_cast<unsigned>(ar), static_cast<void*>(g_params));
    }
    if (ar != NGX_SUCCESS || !g_params) {
        SetError("NGX parameter map allocation failed: 0x%08X", static_cast<unsigned>(ar));
        return false;
    }
    DiscoverFloatSetter();
    return true;
}

static void ShutdownUnlocked() {
    ReleaseFeatureAndResources();
    // NR is initialized directly, outside NGX core's feature ownership.
    // Omitting its shutdown leaves worker threads alive and FreeLibrary hangs.
    if (g_initialized && g_nr_shutdown && g_shim_shutdown) {
        g_shim_shutdown(reinterpret_cast<void*>(g_nr_shutdown));
    }
    if (g_core_shutdown) g_core_shutdown();
    g_params = nullptr;
    g_capability_params = false;
    g_float_set_slot = -1;
    g_device.Reset(); g_queue.Reset(); g_cmd_alloc.Reset(); g_cmd.Reset(); g_fence.Reset();
    if (g_shim_mod) FreeLibrary(g_shim_mod);
    if (g_nr_mod) FreeLibrary(g_nr_mod);
    if (g_core_mod) FreeLibrary(g_core_mod);
    if (g_nvapi_unload) g_nvapi_unload();
    if (g_nvapi_mod) FreeLibrary(g_nvapi_mod);
    g_shim_mod = g_nr_mod = g_core_mod = nullptr;
    g_nvapi_mod = nullptr;

    g_core_init_ext = nullptr;
    g_core_init_project = nullptr;
    g_alloc_params = nullptr;
    g_get_capability_params = nullptr;
    g_core_create = nullptr;
    g_core_eval = nullptr;
    g_core_release = nullptr;
    g_core_shutdown = nullptr;
    g_dlss_ready = false;
    g_nr_init = nullptr;
    g_nr_create = nullptr;
    g_nr_eval = nullptr;
    g_nr_release = nullptr;
    g_nr_shutdown = nullptr;
    g_shim_init = nullptr;
    g_shim_create = nullptr;
    g_shim_eval = nullptr;
    g_shim_release = nullptr;
    g_shim_shutdown = nullptr;
    g_nvapi_initialize = nullptr;
    g_nvapi_unload = nullptr;

    g_initialized = false;
}

extern "C" {

__declspec(dllexport) const char* __cdecl dlss5nr_version() {
    return "0.4.0-neural-upscale-motion";
}

__declspec(dllexport) const char* __cdecl dlss5nr_gpu_name() {
    return g_gpu_name.c_str();
}

__declspec(dllexport) int __cdecl dlss5nr_init(int gpu_index, const wchar_t* runtime_dir, char* err, int err_cap) {
    std::lock_guard<std::mutex> guard(g_mutex);
    g_last_error.clear();
    if (g_initialized) { CopyError(err, err_cap); return 1; }
    if (!runtime_dir || !*runtime_dir) { SetError("runtime_dir is empty"); CopyError(err, err_cap); return 0; }

    g_gpu_index = gpu_index;
    g_runtime_dir = runtime_dir;
    HRESULT co = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    (void)co; // RPC_E_CHANGED_MODE is harmless for this use.

    if (!SetupD3D12() || !LoadNGX() || !InitNGXSession()) {
        ShutdownUnlocked();
        CopyError(err, err_cap);
        return 0;
    }
    g_initialized = true;
    CopyError(err, err_cap);
    return 1;
}

static int ProcessFrame(
    const float* rgb_in, const uint16_t* mvec_in, float* rgb_out,
    int input_width, int input_height, int output_width, int output_height,
    int style, int preset, int perf_quality,
    float intensity, float tone, float structure, float skin,
    int automask, int reset, char* err, int err_cap) {

    std::lock_guard<std::mutex> guard(g_mutex);
    g_last_error.clear();
    if (!g_initialized) { SetError("DLSS5 NR bridge is not initialized"); CopyError(err, err_cap); return 0; }
    if (!rgb_in || !mvec_in || !rgb_out || input_width <= 0 || input_height <= 0 ||
        output_width <= 0 || output_height <= 0) {
        SetError("Invalid image/motion buffer or dimensions"); CopyError(err, err_cap); return 0;
    }
    if (input_width > 16384 || input_height > 16384 || output_width > 16384 || output_height > 16384) {
        SetError("Image dimensions are unreasonably large"); CopyError(err, err_cap); return 0;
    }
    const float ratio_x = static_cast<float>(output_width) / static_cast<float>(input_width);
    const float ratio_y = static_cast<float>(output_height) / static_cast<float>(input_height);
    if (ratio_x <= 0.0f || ratio_y <= 0.0f || !std::isfinite(ratio_x) || !std::isfinite(ratio_y) ||
        std::fabs(ratio_x - ratio_y) > 0.03f) {
        SetError("DLSSNR input/output dimensions must preserve aspect ratio");
        CopyError(err, err_cap); return 0;
    }
    float scaling_ratio = 0.0f;
    if (!FixedScalingRatio(perf_quality, &scaling_ratio)) {
        SetError("Unsupported DLSSNR PerfQualityValue %d (expected 0, 1, 2, 3, or 5)", perf_quality);
        CopyError(err, err_cap); return 0;
    }
    if (std::fabs(ratio_x - scaling_ratio) > 0.03f || std::fabs(ratio_y - scaling_ratio) > 0.03f) {
        SetError("Output dimensions do not match PerfQualityValue %d (requested %.3fx%.3f, mode %.3f)",
                 perf_quality, ratio_x, ratio_y, scaling_ratio);
        CopyError(err, err_cap); return 0;
    }

    if (!EnsureFeature(static_cast<UINT>(input_width), static_cast<UINT>(input_height),
                       static_cast<UINT>(output_width), static_cast<UINT>(output_height),
                       style, preset, perf_quality,
                       intensity, tone, structure, skin, automask)) {
        CopyError(err, err_cap); return 0;
    }
    // Set the carrier's public parameters before uploading the render-sized
    // frame.  In native mode there is no carrier and the feature-18 params are
    // ready immediately; in upscale mode they are written after the carrier
    // has produced its high-resolution surface below.
    if (g_upscale_active) SetDLSSCarrierParams(perf_quality, reset ? 1 : 0);
    else SetNeuralParams(style, preset, perf_quality, intensity, tone, structure, skin, automask, reset ? 1 : 0);

    void* mapped = nullptr;
    HRESULT hr = g_color_upload->Map(0, nullptr, &mapped);
    if (FAILED(hr) || !mapped) { SetError("Upload buffer Map failed: 0x%08X", static_cast<unsigned>(hr)); CopyError(err, err_cap); return 0; }
    memset(mapped, 0, static_cast<size_t>(g_color_bytes));
    auto* dst_base = static_cast<uint8_t*>(mapped);
    for (int y = 0; y < input_height; ++y) {
        auto* row = reinterpret_cast<uint16_t*>(dst_base + static_cast<size_t>(y) * g_color_row_pitch);
        const float* src = rgb_in + static_cast<size_t>(y) * input_width * 3;
        for (int x = 0; x < input_width; ++x) {
            row[x * 4 + 0] = FloatToHalf(std::clamp(src[x * 3 + 0], 0.0f, 1.0f));
            row[x * 4 + 1] = FloatToHalf(std::clamp(src[x * 3 + 1], 0.0f, 1.0f));
            row[x * 4 + 2] = FloatToHalf(std::clamp(src[x * 3 + 2], 0.0f, 1.0f));
            row[x * 4 + 3] = FloatToHalf(1.0f);
        }
    }
    g_color_upload->Unmap(0, nullptr);

    mapped = nullptr;
    hr = g_mvec_upload->Map(0, nullptr, &mapped);
    if (FAILED(hr) || !mapped) { SetError("Motion upload buffer Map failed: 0x%08X", static_cast<unsigned>(hr)); CopyError(err, err_cap); return 0; }
    memset(mapped, 0, static_cast<size_t>(g_mvec_bytes));
    auto* mvec_dst = static_cast<uint8_t*>(mapped);
    const size_t mvec_row_bytes = static_cast<size_t>(input_width) * 2u * sizeof(uint16_t);
    for (int y = 0; y < input_height; ++y) {
        memcpy(mvec_dst + static_cast<size_t>(y) * g_mvec_row_pitch,
               mvec_in + static_cast<size_t>(y) * input_width * 2u,
               mvec_row_bytes);
    }
    g_mvec_upload->Unmap(0, nullptr);

    mapped = nullptr;
    hr = g_depth_upload->Map(0, nullptr, &mapped);
    if (FAILED(hr) || !mapped) { SetError("Depth upload buffer Map failed: 0x%08X", static_cast<unsigned>(hr)); CopyError(err, err_cap); return 0; }
    // A flat reversed-depth field is deterministic and avoids feeding an
    // uninitialised texture to DLSS.  The corresponding create flag and NR
    // parameter advertise the inverted convention.
    auto* depth_dst = static_cast<float*>(mapped);
    for (int y = 0; y < input_height; ++y) {
        float* row = reinterpret_cast<float*>(reinterpret_cast<uint8_t*>(depth_dst) + static_cast<size_t>(y) * g_depth_row_pitch);
        std::fill(row, row + input_width, 0.0f);
    }
    g_depth_upload->Unmap(0, nullptr);

    D3D12_RESOURCE_BARRIER before_copy[] = {
        Barrier(g_color.Get(), D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, D3D12_RESOURCE_STATE_COPY_DEST),
        Barrier(g_mvec.Get(), D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, D3D12_RESOURCE_STATE_COPY_DEST),
        Barrier(g_depth.Get(), D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, D3D12_RESOURCE_STATE_COPY_DEST),
    };
    g_cmd->ResourceBarrier(3, before_copy);

    D3D12_TEXTURE_COPY_LOCATION color_dst{};
    color_dst.pResource = g_color.Get(); color_dst.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
    D3D12_TEXTURE_COPY_LOCATION color_src{};
    color_src.pResource = g_color_upload.Get(); color_src.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
    color_src.PlacedFootprint.Footprint.Format = DXGI_FORMAT_R16G16B16A16_FLOAT;
    color_src.PlacedFootprint.Footprint.Width = static_cast<UINT>(input_width);
    color_src.PlacedFootprint.Footprint.Height = static_cast<UINT>(input_height);
    color_src.PlacedFootprint.Footprint.Depth = 1;
    color_src.PlacedFootprint.Footprint.RowPitch = g_color_row_pitch;
    g_cmd->CopyTextureRegion(&color_dst, 0, 0, 0, &color_src, nullptr);

    D3D12_TEXTURE_COPY_LOCATION mvec_dst_loc{};
    mvec_dst_loc.pResource = g_mvec.Get(); mvec_dst_loc.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
    D3D12_TEXTURE_COPY_LOCATION mvec_src_loc{};
    mvec_src_loc.pResource = g_mvec_upload.Get(); mvec_src_loc.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
    mvec_src_loc.PlacedFootprint.Footprint.Format = DXGI_FORMAT_R16G16_FLOAT;
    mvec_src_loc.PlacedFootprint.Footprint.Width = static_cast<UINT>(input_width);
    mvec_src_loc.PlacedFootprint.Footprint.Height = static_cast<UINT>(input_height);
    mvec_src_loc.PlacedFootprint.Footprint.Depth = 1;
    mvec_src_loc.PlacedFootprint.Footprint.RowPitch = g_mvec_row_pitch;
    g_cmd->CopyTextureRegion(&mvec_dst_loc, 0, 0, 0, &mvec_src_loc, nullptr);

    D3D12_TEXTURE_COPY_LOCATION depth_dst_loc{};
    depth_dst_loc.pResource = g_depth.Get(); depth_dst_loc.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
    D3D12_TEXTURE_COPY_LOCATION depth_src_loc{};
    depth_src_loc.pResource = g_depth_upload.Get(); depth_src_loc.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
    depth_src_loc.PlacedFootprint.Footprint.Format = DXGI_FORMAT_R32_FLOAT;
    depth_src_loc.PlacedFootprint.Footprint.Width = static_cast<UINT>(input_width);
    depth_src_loc.PlacedFootprint.Footprint.Height = static_cast<UINT>(input_height);
    depth_src_loc.PlacedFootprint.Footprint.Depth = 1;
    depth_src_loc.PlacedFootprint.Footprint.RowPitch = g_depth_row_pitch;
    g_cmd->CopyTextureRegion(&depth_dst_loc, 0, 0, 0, &depth_src_loc, nullptr);

    D3D12_RESOURCE_BARRIER after_copy[] = {
        Barrier(g_color.Get(), D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE),
        Barrier(g_mvec.Get(), D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE),
        Barrier(g_depth.Get(), D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE),
    };
    g_cmd->ResourceBarrier(3, after_copy);

    NGXResult er = NGX_SUCCESS;
    if (g_upscale_active) {
        if (g_dlss_output_readable) {
            auto carrier_uav = Barrier(g_dlss_output.Get(), D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,
                                       D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
            g_cmd->ResourceBarrier(1, &carrier_uav);
            g_dlss_output_readable = false;
        }
        er = g_core_eval(g_cmd.Get(), g_dlss_feature, g_params, nullptr);
        if (er != NGX_SUCCESS) {
            SetError("DLSS carrier EvaluateFeature failed: 0x%08X", static_cast<unsigned>(er));
            ExecuteAndWait();
            CopyError(err, err_cap); return 0;
        }
        // Feature 18 consumes the carrier output as a shader resource.  A UAV
        // barrier before the transition also covers runtimes that enqueue
        // multiple compute dispatches internally.
        D3D12_RESOURCE_BARRIER carrier_barriers[2]{};
        carrier_barriers[0].Type = D3D12_RESOURCE_BARRIER_TYPE_UAV;
        carrier_barriers[0].UAV.pResource = g_dlss_output.Get();
        carrier_barriers[1] = Barrier(g_dlss_output.Get(), D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
                                      D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
        g_cmd->ResourceBarrier(2, carrier_barriers);
        g_dlss_output_readable = true;

        SetNeuralParams(style, preset, perf_quality, intensity, tone, structure, skin, automask, reset ? 1 : 0);
        er = g_shim_eval(reinterpret_cast<void*>(g_nr_eval), g_cmd.Get(), g_feature, g_params, nullptr);
    } else {
        er = g_shim_eval(reinterpret_cast<void*>(g_nr_eval), g_cmd.Get(), g_feature, g_params, nullptr);
    }
    if (er != NGX_SUCCESS) {
        SetError("DLSSNR EvaluateFeature failed: 0x%08X", static_cast<unsigned>(er));
        // Reset command list to a clean state before returning.
        ExecuteAndWait();
        CopyError(err, err_cap); return 0;
    }
    ++g_neural_evaluations;
    if (g_neural_evaluations == 1 || (g_neural_evaluations % 100) == 0) {
        std::fprintf(stderr, "[dlss5nr] Feature18 EvaluateFeature succeeded (frame=%llu, carrier=%d)\n",
                     static_cast<unsigned long long>(g_neural_evaluations), g_upscale_active ? 1 : 0);
    }

    auto b3 = Barrier(g_output.Get(), D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_SOURCE);
    g_cmd->ResourceBarrier(1, &b3);
    D3D12_TEXTURE_COPY_LOCATION rd{};
    rd.pResource = g_output_readback.Get(); rd.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
    rd.PlacedFootprint.Footprint.Format = DXGI_FORMAT_R16G16B16A16_FLOAT;
    rd.PlacedFootprint.Footprint.Width = static_cast<UINT>(output_width);
    rd.PlacedFootprint.Footprint.Height = static_cast<UINT>(output_height);
    rd.PlacedFootprint.Footprint.Depth = 1; rd.PlacedFootprint.Footprint.RowPitch = g_output_row_pitch;
    D3D12_TEXTURE_COPY_LOCATION rs{};
    rs.pResource = g_output.Get(); rs.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
    g_cmd->CopyTextureRegion(&rd, 0, 0, 0, &rs, nullptr);
    auto b4 = Barrier(g_output.Get(), D3D12_RESOURCE_STATE_COPY_SOURCE, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    g_cmd->ResourceBarrier(1, &b4);

    if (!ExecuteAndWait()) { CopyError(err, err_cap); return 0; }

    void* rmap = nullptr;
    hr = g_output_readback->Map(0, nullptr, &rmap);
    if (FAILED(hr) || !rmap) { SetError("Readback Map failed: 0x%08X", static_cast<unsigned>(hr)); CopyError(err, err_cap); return 0; }
    const auto* base = static_cast<const uint8_t*>(rmap);
    for (int y = 0; y < output_height; ++y) {
        const auto* row = reinterpret_cast<const uint16_t*>(base + static_cast<size_t>(y) * g_output_row_pitch);
        float* dstf = rgb_out + static_cast<size_t>(y) * output_width * 3;
        for (int x = 0; x < output_width; ++x) {
            // Return the resource channels exactly as stored. Some stock/reference
            // DLSSNR builds have been observed to produce B,G,R,A while patched
            // Ada builds may produce R,G,B,A. Python selects/auto-detects the
            // correct interpretation instead of hard-coding a swap here.
            dstf[x * 3 + 0] = std::clamp(HalfToFloat(row[x * 4 + 0]), 0.0f, 1.0f);
            dstf[x * 3 + 1] = std::clamp(HalfToFloat(row[x * 4 + 1]), 0.0f, 1.0f);
            dstf[x * 3 + 2] = std::clamp(HalfToFloat(row[x * 4 + 2]), 0.0f, 1.0f);
        }
    }
    g_output_readback->Unmap(0, nullptr);
    CopyError(err, err_cap);
    return 1;
}

// Version-2 export used by the Linux/Wine video host.  It mirrors the
// Merserk worker contract: Color/MVec are render-sized, Output is the
// requested high-resolution surface.
__declspec(dllexport) int __cdecl dlss5nr_process_v2(
    const float* rgb_in, const uint16_t* mvec_in, float* rgb_out,
    int input_width, int input_height, int output_width, int output_height,
    int style, int preset, int perf_quality,
    float intensity, float tone, float structure, float skin,
    int automask, int reset, char* err, int err_cap) {
    return ProcessFrame(rgb_in, mvec_in, rgb_out, input_width, input_height, output_width, output_height,
                        style, preset, perf_quality, intensity, tone, structure, skin,
                        automask, reset, err, err_cap);
}

// Keep the original ComfyUI image ABI working in native-size mode.
__declspec(dllexport) int __cdecl dlss5nr_process(
    const float* rgb_in, float* rgb_out, int width, int height,
    int style, int preset, float intensity, float tone, float structure, float skin,
    int automask, int reset, char* err, int err_cap) {
    if (width <= 0 || height <= 0) {
        SetError("Invalid image dimensions"); CopyError(err, err_cap); return 0;
    }
    std::vector<uint16_t> zero_motion(static_cast<size_t>(width) * height * 2u, 0);
    return ProcessFrame(rgb_in, zero_motion.data(), rgb_out, width, height, width, height,
                        style, preset, 5, intensity, tone, structure, skin,
                        automask, reset, err, err_cap);
}

__declspec(dllexport) void __cdecl dlss5nr_shutdown() {
    std::lock_guard<std::mutex> guard(g_mutex);
    ShutdownUnlocked();
}

}
