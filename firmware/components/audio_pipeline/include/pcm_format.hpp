#pragma once

#include <algorithm>
#include <cstddef>
#include <cstdint>

namespace pcm_format {

inline int16_t scale_sample(int16_t sample, float gain)
{
    int32_t scaled = static_cast<int32_t>(sample * gain);
    return static_cast<int16_t>(std::clamp(scaled, static_cast<int32_t>(-32768),
                                           static_cast<int32_t>(32767)));
}

inline int16_t mix_tdm_microphones(const int16_t *tdm, size_t frame)
{
    constexpr size_t channels = 4;
    const int32_t mic1 = tdm[frame * channels];
    const int32_t mic2 = tdm[frame * channels + 2];
    return static_cast<int16_t>((mic1 + mic2) / 2);
}

inline void mono_to_stereo(const int16_t *mono, int16_t *stereo, size_t frame_count)
{
    for (size_t frame = 0; frame < frame_count; ++frame) {
        stereo[frame * 2] = mono[frame];
        stereo[frame * 2 + 1] = mono[frame];
    }
}

} // namespace pcm_format
