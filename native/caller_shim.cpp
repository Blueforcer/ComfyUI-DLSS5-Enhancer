// SPDX-License-Identifier: MIT
// Copyright (c) 2026 ComfyUI-DLSS5-NR contributors

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <d3d12.h>

using NGXResult = int;

struct NGXHandle { unsigned int Id; };
struct NGXParameter;

// IMPORTANT: nvngx_dlssnr.dll is a snippet build. Its Init_Ext ABI differs
// from the public/core NGX Init_Ext ABI in the order of the last two args:
//   snippet: (app, path, device, FeatureCommonInfo*, version)
// The exported shim keeps the bridge-facing order (version, common_info) and
// deliberately reorders those arguments for the real snippet call.
using SnippetInitFn = NGXResult(__cdecl*)(unsigned long long, const wchar_t*, ID3D12Device*, const void*, int);
using CreateFn = NGXResult(__cdecl*)(ID3D12GraphicsCommandList*, int, NGXParameter*, NGXHandle**);
using EvalFn = NGXResult(__cdecl*)(ID3D12GraphicsCommandList*, const NGXHandle*, const NGXParameter*, void*);
using ReleaseFn = NGXResult(__cdecl*)(NGXHandle*);
using ShutdownFn = NGXResult(__cdecl*)();

// The NR runtime validates the module that owns its RETURN ADDRESS. A trivial
// wrapper built with /O2 can be tail-call-optimized into a JMP, which would
// leave the return address in dlss5nr_bridge.dll and trigger 0xBAD00002.
// This observable post-call store forces a real CALL/RET through this DLL.
static volatile LONG g_post_call_sink = 0;
static __forceinline NGXResult FinishCall(NGXResult r) {
    g_post_call_sink = static_cast<LONG>(r);
    return r;
}

extern "C" {

__declspec(dllexport) __declspec(noinline) NGXResult __cdecl DLSSNR_CallInit(
    void* real_fn, unsigned long long app_id, const wchar_t* path,
    ID3D12Device* device, int version, const void* common_info) {
    NGXResult r = reinterpret_cast<SnippetInitFn>(real_fn)(app_id, path, device, common_info, version);
    return FinishCall(r);
}

__declspec(dllexport) __declspec(noinline) NGXResult __cdecl DLSSNR_CallCreate(
    void* real_fn, ID3D12GraphicsCommandList* list, int feature_id,
    NGXParameter* params, NGXHandle** handle) {
    NGXResult r = reinterpret_cast<CreateFn>(real_fn)(list, feature_id, params, handle);
    return FinishCall(r);
}

__declspec(dllexport) __declspec(noinline) NGXResult __cdecl DLSSNR_CallEvaluate(
    void* real_fn, ID3D12GraphicsCommandList* list, const NGXHandle* handle,
    const NGXParameter* params, void* callback) {
    NGXResult r = reinterpret_cast<EvalFn>(real_fn)(list, handle, params, callback);
    return FinishCall(r);
}

__declspec(dllexport) __declspec(noinline) NGXResult __cdecl DLSSNR_CallRelease(void* real_fn, NGXHandle* handle) {
    NGXResult r = reinterpret_cast<ReleaseFn>(real_fn)(handle);
    return FinishCall(r);
}

__declspec(dllexport) __declspec(noinline) NGXResult __cdecl DLSSNR_CallShutdown(void* real_fn) {
    NGXResult r = reinterpret_cast<ShutdownFn>(real_fn)();
    return FinishCall(r);
}

}
