#include "afe_processor.hpp"

#include "esp_log.h"
#include "esp_partition.h"

namespace {
constexpr char TAG[] = "afe";
}

bool AfeProcessor::initialize()
{
    if (esp_partition_find_first(ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_ANY,
                                 "model") == nullptr) {
        ESP_LOGE(TAG, "The model partition is missing");
        return false;
    }
    models_ = esp_srmodel_init("model");
    if (models_ == nullptr) {
        ESP_LOGE(TAG, "Speech models could not be loaded");
        return false;
    }
    afe_config_t *config = afe_config_init("MMR", models_, AFE_TYPE_FD, AFE_MODE_LOW_COST);
    if (config == nullptr) {
        return false;
    }
    config->aec_init = true;
    config->ns_init = false;
    config->vad_init = false;
    config->wakenet_init = false;
    config->agc_init = false;
    config->memory_alloc_mode = AFE_MEMORY_ALLOC_MORE_PSRAM;
    interface_ = esp_afe_handle_from_config(config);
    if (interface_ != nullptr) {
        data_ = interface_->create_from_config(config);
    }
    afe_config_free(config);
    if (interface_ == nullptr || data_ == nullptr) {
        ESP_LOGE(TAG, "AFE creation failed");
        interface_ = nullptr;
        data_ = nullptr;
        return false;
    }
    feed_frames_ = interface_->get_feed_chunksize(data_);
    input_.reserve(feed_frames_ * 6);
    output_.reserve(2048);
    interface_->print_pipeline(data_);
    return feed_frames_ > 0;
}

void AfeProcessor::reset()
{
    input_.clear();
    output_.clear();
    if (interface_ != nullptr && data_ != nullptr) {
        interface_->reset_buffer(data_);
    }
}

void AfeProcessor::feed(const int16_t *mmr_pcm, size_t frame_count)
{
    if (interface_ == nullptr || data_ == nullptr || mmr_pcm == nullptr) {
        return;
    }
    input_.insert(input_.end(), mmr_pcm, mmr_pcm + frame_count * 3);
    const size_t feed_samples = feed_frames_ * 3;
    size_t consumed = 0;
    while (input_.size() - consumed >= feed_samples) {
        interface_->feed(data_, input_.data() + consumed);
        consumed += feed_samples;
    }
    if (consumed > 0) {
        input_.erase(input_.begin(), input_.begin() + consumed);
    }
}

const std::vector<int16_t> &AfeProcessor::fetch()
{
    output_.clear();
    if (interface_ == nullptr || data_ == nullptr) {
        return output_;
    }
    afe_fetch_result_t *result = interface_->fetch_with_delay(data_, pdMS_TO_TICKS(100));
    if (result != nullptr && result->ret_value != ESP_FAIL &&
        result->data != nullptr && result->data_size > 0) {
        const int16_t *samples = result->data;
        output_.insert(output_.end(), samples,
                       samples + result->data_size / sizeof(int16_t));
    }
    return output_;
}
