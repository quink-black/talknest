#include <array>
#include <cassert>
#include <cstdint>

#include "pcm_format.hpp"

int main()
{
    std::array<int16_t, 8> tdm = {
        1000, 30000, 2000, 32767,
        32767, -100, 32767, 0,
    };
    assert(pcm_format::mix_tdm_microphones(tdm.data(), 0) == 1500);
    assert(pcm_format::mix_tdm_microphones(tdm.data(), 1) == 32767);
    assert(pcm_format::scale_sample(1000, 8.0F) == 8000);
    assert(pcm_format::scale_sample(30000, 8.0F) == 32767);
    assert(pcm_format::scale_sample(-30000, 8.0F) == -32768);

    std::array<int16_t, 3> mono = {-32768, 0, 32767};
    std::array<int16_t, 6> stereo = {};
    pcm_format::mono_to_stereo(mono.data(), stereo.data(), mono.size());
    for (size_t index = 0; index < mono.size(); ++index) {
        assert(stereo[index * 2] == mono[index]);
        assert(stereo[index * 2 + 1] == mono[index]);
    }

    std::array<int16_t, 8> upstream = {};
    pcm_format::tdm_to_upstream_stereo(tdm.data(), upstream.data(), 2, 2.0F);
    assert(upstream[0] == 2000);    // mic1 of frame 0, scaled
    assert(upstream[1] == 30000);   // slot 1 reference of frame 0, raw
    assert(upstream[2] == 32767);   // mic1 of frame 1 saturates, not wraps
    assert(upstream[3] == -100);    // slot 1 reference of frame 1, raw
    return 0;
}
