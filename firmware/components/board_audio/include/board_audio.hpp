#pragma once

#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

constexpr uint32_t BOARD_AUDIO_SAMPLE_RATE = 24000;
constexpr size_t BOARD_AUDIO_CAPTURE_CHANNELS = 4;
constexpr size_t BOARD_AUDIO_PLAYBACK_CHANNELS = 2;

esp_err_t board_audio_initialize();
esp_err_t board_audio_open(int speaker_volume, float mic_gain_db);
esp_err_t board_audio_close();
// Set the ES8311 playback volume (0-100). Safe to call repeatedly while the
// codec is open; it only writes the DAC volume register.
esp_err_t board_audio_set_volume(int volume);
esp_err_t board_audio_read(int16_t *tdm_pcm, size_t frame_count);
esp_err_t board_audio_write(const int16_t *stereo_pcm, size_t frame_count);
