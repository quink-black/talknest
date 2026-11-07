#include "board_audio.hpp"

#include "bsp/esp32_s3_touch_amoled_2_06.h"
#include "driver/gpio.h"
#include "driver/i2s_tdm.h"
#include "esp_check.h"
#include "esp_codec_dev_defaults.h"
#include "esp_log.h"

namespace {
constexpr char TAG[] = "board_audio";
constexpr uint16_t CONNECTED_MIC_MASK = (1U << 0) | (1U << 1) | (1U << 2);

i2s_chan_handle_t tx_channel;
i2s_chan_handle_t rx_channel;
const audio_codec_data_if_t *data_interface;
esp_codec_dev_handle_t speaker;
esp_codec_dev_handle_t microphone;
bool opened;

esp_err_t initialize_bus()
{
    i2s_std_config_t tx_config = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(BOARD_AUDIO_SAMPLE_RATE),
        .slot_cfg = I2S_STD_PHILIP_SLOT_DEFAULT_CONFIG(
            I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {
            .mclk = BSP_I2S_MCLK,
            .bclk = BSP_I2S_SCLK,
            .ws = BSP_I2S_LCLK,
            .dout = BSP_I2S_DOUT,
            .din = I2S_GPIO_UNUSED,
            .invert_flags = {},
        },
    };
    tx_config.clk_cfg.mclk_multiple = I2S_MCLK_MULTIPLE_256;

    i2s_tdm_config_t rx_config = {
        .clk_cfg = I2S_TDM_CLK_DEFAULT_CONFIG(BOARD_AUDIO_SAMPLE_RATE),
        .slot_cfg = I2S_TDM_PHILIP_SLOT_DEFAULT_CONFIG(
            I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO,
            static_cast<i2s_tdm_slot_mask_t>(I2S_TDM_SLOT0 | I2S_TDM_SLOT1 |
                                             I2S_TDM_SLOT2 | I2S_TDM_SLOT3)),
        .gpio_cfg = {
            .mclk = BSP_I2S_MCLK,
            .bclk = BSP_I2S_SCLK,
            .ws = BSP_I2S_LCLK,
            .dout = I2S_GPIO_UNUSED,
            .din = BSP_I2S_DSIN,
            .invert_flags = {},
        },
    };
    rx_config.clk_cfg.mclk_multiple = I2S_MCLK_MULTIPLE_256;
    rx_config.clk_cfg.bclk_div = 8;
    rx_config.slot_cfg.total_slot = BOARD_AUDIO_CAPTURE_CHANNELS;

    i2s_chan_config_t channel_config = I2S_CHANNEL_DEFAULT_CONFIG(0, I2S_ROLE_MASTER);
    channel_config.auto_clear = true;
    ESP_RETURN_ON_ERROR(i2s_new_channel(&channel_config, &tx_channel, &rx_channel),
                        TAG, "Create I2S channels failed");
    ESP_RETURN_ON_ERROR(i2s_channel_init_std_mode(tx_channel, &tx_config),
                        TAG, "Initialize stereo playback failed");
    ESP_RETURN_ON_ERROR(i2s_channel_init_tdm_mode(rx_channel, &rx_config),
                        TAG, "Initialize TDM capture failed");
    ESP_RETURN_ON_ERROR(i2s_channel_enable(tx_channel), TAG, "Enable playback failed");
    ESP_RETURN_ON_ERROR(i2s_channel_enable(rx_channel), TAG, "Enable capture failed");

    audio_codec_i2s_cfg_t config = {
        .port = 0,
        .rx_handle = rx_channel,
        .tx_handle = tx_channel,
        .clk_src = 0,
    };
    data_interface = audio_codec_new_i2s_data(&config);
    ESP_RETURN_ON_FALSE(data_interface != nullptr, ESP_ERR_NO_MEM, TAG,
                        "Create I2S codec interface failed");
    return ESP_OK;
}

esp_err_t initialize_speaker()
{
    const audio_codec_gpio_if_t *gpio = audio_codec_new_gpio();
    ESP_RETURN_ON_FALSE(gpio != nullptr, ESP_ERR_NO_MEM, TAG, "Create PA interface failed");

    audio_codec_i2c_cfg_t i2c_config = {
        .port = BSP_I2C_NUM,
        .addr = ES8311_CODEC_DEFAULT_ADDR,
        .bus_handle = bsp_i2c_get_handle(),
    };
    const audio_codec_ctrl_if_t *control = audio_codec_new_i2c_ctrl(&i2c_config);
    ESP_RETURN_ON_FALSE(control != nullptr, ESP_ERR_NO_MEM, TAG, "Create ES8311 control failed");

    esp_codec_dev_hw_gain_t gain = {
        .pa_voltage = 5.0F,
        .codec_dac_voltage = 3.3F,
        .pa_gain = 0.0F,
    };
    es8311_codec_cfg_t codec_config = {
        .ctrl_if = control,
        .gpio_if = gpio,
        .codec_mode = ESP_CODEC_DEV_WORK_MODE_DAC,
        .pa_pin = BSP_POWER_AMP_IO,
        .pa_reverted = false,
        .master_mode = false,
        .use_mclk = true,
        .digital_mic = false,
        .invert_mclk = false,
        .invert_sclk = false,
        .hw_gain = gain,
        .no_dac_ref = false,
        .mclk_div = 256,
    };
    const audio_codec_if_t *codec = es8311_codec_new(&codec_config);
    ESP_RETURN_ON_FALSE(codec != nullptr, ESP_ERR_NO_MEM, TAG, "Create ES8311 codec failed");

    esp_codec_dev_cfg_t device_config = {
        .dev_type = ESP_CODEC_DEV_TYPE_OUT,
        .codec_if = codec,
        .data_if = data_interface,
    };
    speaker = esp_codec_dev_new(&device_config);
    ESP_RETURN_ON_FALSE(speaker != nullptr, ESP_ERR_NO_MEM, TAG, "Create speaker failed");
    return ESP_OK;
}

esp_err_t initialize_microphone()
{
    audio_codec_i2c_cfg_t i2c_config = {
        .port = BSP_I2C_NUM,
        .addr = ES7210_CODEC_DEFAULT_ADDR,
        .bus_handle = bsp_i2c_get_handle(),
    };
    const audio_codec_ctrl_if_t *control = audio_codec_new_i2c_ctrl(&i2c_config);
    ESP_RETURN_ON_FALSE(control != nullptr, ESP_ERR_NO_MEM, TAG, "Create ES7210 control failed");

    es7210_codec_cfg_t codec_config = {
        .ctrl_if = control,
        .master_mode = false,
        .mic_selected = CONNECTED_MIC_MASK,
        .mclk_src = ES7210_MCLK_FROM_PAD,
        .mclk_div = 256,
    };
    const audio_codec_if_t *codec = es7210_codec_new(&codec_config);
    ESP_RETURN_ON_FALSE(codec != nullptr, ESP_ERR_NO_MEM, TAG, "Create ES7210 codec failed");

    esp_codec_dev_cfg_t device_config = {
        .dev_type = ESP_CODEC_DEV_TYPE_IN,
        .codec_if = codec,
        .data_if = data_interface,
    };
    microphone = esp_codec_dev_new(&device_config);
    ESP_RETURN_ON_FALSE(microphone != nullptr, ESP_ERR_NO_MEM, TAG, "Create microphone failed");
    return ESP_OK;
}
} // namespace

esp_err_t board_audio_initialize()
{
    ESP_RETURN_ON_ERROR(bsp_i2c_init(), TAG, "Initialize I2C failed");
    ESP_RETURN_ON_ERROR(initialize_bus(), TAG, "Initialize audio bus failed");
    ESP_RETURN_ON_ERROR(initialize_speaker(), TAG, "Initialize speaker failed");
    ESP_RETURN_ON_ERROR(initialize_microphone(), TAG, "Initialize microphone failed");
    return ESP_OK;
}

void board_audio_set_speaker_enabled(bool enabled)
{
    /* Only the external class-D amplifier is gated. The ES8311 and the I2S
     * stream stay in the state board_audio_open() left them, so the capture
     * path (including the ES8311 loopback slot) is untouched. */
    gpio_set_level(BSP_POWER_AMP_IO, enabled ? 1 : 0);
}

esp_err_t board_audio_open(int speaker_volume, float mic_gain_db)
{
    ESP_RETURN_ON_FALSE(speaker != nullptr && microphone != nullptr,
                        ESP_ERR_INVALID_STATE, TAG, "Audio devices are not initialized");
    if (opened) {
        return ESP_OK;
    }

    esp_codec_dev_sample_info_t output = {
        .bits_per_sample = 16,
        .channel = BOARD_AUDIO_PLAYBACK_CHANNELS,
        .channel_mask = 0,
        .sample_rate = BOARD_AUDIO_SAMPLE_RATE,
        .mclk_multiple = 256,
    };
    esp_codec_dev_sample_info_t input = {
        .bits_per_sample = 16,
        .channel = BOARD_AUDIO_CAPTURE_CHANNELS,
        .channel_mask = I2S_TDM_SLOT0 | I2S_TDM_SLOT1 | I2S_TDM_SLOT2 | I2S_TDM_SLOT3,
        .sample_rate = BOARD_AUDIO_SAMPLE_RATE,
        .mclk_multiple = 256,
    };
    ESP_RETURN_ON_ERROR(esp_codec_dev_open(speaker, &output), TAG, "Open speaker failed");
    ESP_RETURN_ON_ERROR(esp_codec_dev_set_out_mute(speaker, false), TAG, "Unmute speaker failed");
    ESP_RETURN_ON_ERROR(esp_codec_dev_set_out_vol(speaker, speaker_volume),
                        TAG, "Set speaker volume failed");
    ESP_RETURN_ON_ERROR(esp_codec_dev_open(microphone, &input), TAG, "Open microphone failed");
    ESP_RETURN_ON_ERROR(esp_codec_dev_set_in_channel_gain(
                            microphone, CONNECTED_MIC_MASK, mic_gain_db),
                        TAG, "Set microphone gain failed");
    opened = true;
    ESP_LOGI(TAG, "Voice audio ready at 24 kHz");
    return ESP_OK;
}

esp_err_t board_audio_close()
{
    if (!opened) {
        return ESP_OK;
    }
    opened = false;
    esp_err_t first_error = esp_codec_dev_close(microphone);
    esp_err_t playback_error = esp_codec_dev_close(speaker);
    return first_error != ESP_OK ? first_error : playback_error;
}

esp_err_t board_audio_read(int16_t *tdm_pcm, size_t frame_count)
{
    ESP_RETURN_ON_FALSE(opened && tdm_pcm != nullptr, ESP_ERR_INVALID_STATE,
                        TAG, "Capture is not open");
    return esp_codec_dev_read(microphone, tdm_pcm,
                              frame_count * BOARD_AUDIO_CAPTURE_CHANNELS * sizeof(int16_t));
}

esp_err_t board_audio_write(const int16_t *stereo_pcm, size_t frame_count)
{
    ESP_RETURN_ON_FALSE(opened && stereo_pcm != nullptr, ESP_ERR_INVALID_STATE,
                        TAG, "Playback is not open");
    return esp_codec_dev_write(speaker, const_cast<int16_t *>(stereo_pcm),
                               frame_count * BOARD_AUDIO_PLAYBACK_CHANNELS * sizeof(int16_t));
}
