// Version-4 frame transport for the direct NGX bridge under Wine.
#include "dlss5nr_bridge.cpp"
#include <fcntl.h>
#include <io.h>

#pragma pack(push, 1)
struct VideoHeader {
    uint32_t magic, width, height, out_width, out_height, warmup, frames;
    uint32_t quality, model, profile, preset, style, automask, ui;
    float intensity, tone, structure, skin;
};
struct FrameHeader { uint32_t magic, index, reset, reserved; int64_t pts; };
struct ResultHeader { uint32_t magic, index, ok, bytes, result; int64_t pts; };
#pragma pack(pop)
static_assert(sizeof(VideoHeader) == 72);
static_assert(sizeof(FrameHeader) == 24);
static_assert(sizeof(ResultHeader) == 28);

static bool Read(void* data, size_t size) {
    return std::fread(data, 1, size, stdin) == size;
}
static bool Write(const void* data, size_t size) {
    return std::fwrite(data, 1, size, stdout) == size && std::fflush(stdout) == 0;
}

int wmain(int argc, wchar_t** argv) {
    if (argc != 2) { std::fprintf(stderr, "Usage: dlss5-worker.exe RUNTIME_DIRECTORY\n"); return 2; }
    _setmode(_fileno(stdin), _O_BINARY);
    _setmode(_fileno(stdout), _O_BINARY);
    VideoHeader h{};
    if (!Read(&h, sizeof(h)) || h.magic != 0x34563544 || !h.frames || h.warmup > 16 ||
        h.width < 64 || h.height < 64 || h.out_width < 64 || h.out_height < 64 ||
        std::max(h.out_width, h.out_height) > 7680 || std::min(h.out_width, h.out_height) > 4320 ||
        h.width > 7680 || h.height > 7680 || h.profile || h.ui) {
        std::fprintf(stderr, "Invalid version-4 stream header\n"); return 2;
    }
    float factor;
    if (!FixedScalingRatio(h.quality, &factor)) return 2;
    const uint32_t width = std::lround(h.out_width / factor);
    const uint32_t height = std::lround(h.out_height / factor);
    char error[4096]{};
    uint32_t setup[12] = {0x34505553, 0, 0xBAD00001, width, height, h.out_width, h.out_height,
                          64, 64, 7680, 4320, 0};
    SetEnvironmentVariableA("DLSS5NR_DISABLE_OTHER_SINKS", "1");
    if (!dlss5nr_init(0, argv[1], error, sizeof(error))) {
        std::fprintf(stderr, "%s\n", error); Write(setup, sizeof(setup)); return 3;
    }
    std::fprintf(stderr, "[dlss5nr] signed NR initialized\n");
    // Set and read back the NGX hints using the core's MSVC parameter ABI.
    for (const char* mode : {"DLAA", "Quality", "Balanced", "Performance", "UltraPerformance", "UltraQuality"}) {
        const std::string key = std::string("DLSS.Hint.Render.Preset.") + mode;
        SetParamUInt(key.c_str(), h.model);
        using GetInt = NGXResult(__cdecl*)(void*, const char*, int*);
        int actual = -1;
        void** table = *reinterpret_cast<void***>(g_params);
        if (reinterpret_cast<GetInt>(table[11])(g_params, key.c_str(), &actual) != NGX_SUCCESS ||
            actual != static_cast<int>(h.model)) {
            std::fprintf(stderr, "DLSS model preset hint rejected: %s\n", key.c_str());
            Write(setup, sizeof(setup)); dlss5nr_shutdown(); return 3;
        }
    }
    if (!EnsureFeature(width, height, h.out_width, h.out_height, h.style, h.preset, h.quality,
                       h.intensity, h.tone, h.structure, h.skin, h.automask)) {
        std::fprintf(stderr, "%s\n", g_last_error.c_str());
        Write(setup, sizeof(setup)); dlss5nr_shutdown(); return 3;
    }
    setup[1] = setup[2] = 1;
    setup[11] = h.model;
    if (!Write(setup, sizeof(setup))) { dlss5nr_shutdown(); return 4; }
    const size_t pixels = static_cast<size_t>(width) * height;
    const size_t output_pixels = static_cast<size_t>(h.out_width) * h.out_height;
    std::vector<uint8_t> rgba(pixels * 4), output(output_pixels * 4, 255);
    std::vector<uint16_t> motion(pixels * 2);
    std::vector<float> rgb(pixels * 3), enhanced(output_pixels * 3);
    int exit_code = 0;
    for (uint32_t index = 0; index < h.frames; ++index) {
        FrameHeader frame{};
        // Closing stdin after complete frames is also used for video previews.
        const size_t received = std::fread(&frame, 1, sizeof(frame), stdin);
        if (received == 0 && std::feof(stdin)) break;
        if (received != sizeof(frame)) { exit_code = 4; break; }
        if (frame.magic != 0x314D5246 || frame.index != index ||
            !Read(rgba.data(), rgba.size()) || !Read(motion.data(), motion.size() * 2)) {
            std::fprintf(stderr, "Incomplete or invalid frame %u\n", index); exit_code = 4; break;
        }
        for (size_t p = 0; p < pixels; ++p)
            for (size_t c = 0; c < 3; ++c) rgb[p * 3 + c] = rgba[p * 4 + c] / 255.0f;
        int ok = 0;
        const unsigned passes = index == 0 ? h.warmup + 1 : 1;
        for (unsigned pass = 0; pass < passes; ++pass) {
            ok = dlss5nr_process_v2(rgb.data(), motion.data(), enhanced.data(), width, height,
                h.out_width, h.out_height, h.style, h.preset, h.quality, h.intensity, h.tone,
                h.structure, h.skin, h.automask, pass == 0 && frame.reset, error, sizeof(error));
            if (!ok) break;
        }
        ResultHeader result{0x3154554F, index, static_cast<uint32_t>(ok),
                            static_cast<uint32_t>(output.size()), ok ? 1u : 0xBAD00005u, frame.pts};
        if (!ok) std::fprintf(stderr, "Frame %u: %s\n", index, error);
        if (!Write(&result, sizeof(result)) || !ok) { exit_code = 5; break; }
        for (size_t p = 0; p < output_pixels; ++p)
            for (size_t c = 0; c < 3; ++c)
                output[p * 4 + c] = static_cast<uint8_t>(std::lround(std::clamp(enhanced[p * 3 + c], 0.0f, 1.0f) * 255));
        if (!Write(output.data(), output.size())) { exit_code = 4; break; }
    }
    dlss5nr_shutdown();
    return exit_code;
}
