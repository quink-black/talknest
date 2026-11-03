#pragma once

#include <cstddef>
#include <cstdint>
#include <vector>

#include "esp_afe_sr_models.h"
#include "model_path.h"

class AfeProcessor {
public:
    bool initialize();
    void reset();
    void feed(const int16_t *mmr_pcm, size_t frame_count);
    const std::vector<int16_t> &fetch();

private:
    srmodel_list_t *models_ = nullptr;
    const esp_afe_sr_iface_t *interface_ = nullptr;
    esp_afe_sr_data_t *data_ = nullptr;
    size_t feed_frames_ = 0;
    std::vector<int16_t> input_;
    std::vector<int16_t> output_;
};
