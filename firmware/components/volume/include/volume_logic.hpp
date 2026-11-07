#pragma once

#include <cstdint>

/* Pure volume arithmetic, kept free of ESP-IDF headers so the host tests
 * can exercise the same functions the firmware uses. */
namespace volume_logic {

constexpr int kMin = 0;
constexpr int kMax = 100;
constexpr int kStep = 5;       // percent added per swipe notch
constexpr int kDefault = 80;   // used until a config value or NVS value is known

constexpr int clamp(int value)
{
    return value < kMin ? kMin : (value > kMax ? kMax : value);
}

/* Apply a signed number of notches and return the clamped result. A notch
 * count of zero is a valid query, used when a gesture ends without moving. */
constexpr int apply_steps(int current, int steps)
{
    return clamp(current + steps * kStep);
}

} // namespace volume_logic
