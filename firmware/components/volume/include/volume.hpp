#pragma once

#include "esp_err.h"

/* Device-side speaker volume, persisted in NVS. The config file value is
 * the factory default; a swipe-adjusted value overrides it at runtime and
 * survives reboot. */
namespace volume {

/* Load the saved value (or config_default) into memory. Call once at boot
 * after nvs_flash_init(). Does not touch the codec. */
esp_err_t init(int config_default);

/* Current value, 0-100. */
int get();

/* Push the current value to the codec. Call after board_audio_open() so a
 * fresh or re-opened codec (including wake from light sleep) uses the saved
 * volume rather than the config default. */
esp_err_t apply();

/* Change by signed swipe notches, apply to the codec, mark NVS dirty.
 * Returns the resulting value. */
int change(int steps);

/* Flush a pending change to NVS. Safe to call when nothing changed. Call
 * at the end of a swipe gesture, not once per notch. */
esp_err_t commit();

} // namespace volume
