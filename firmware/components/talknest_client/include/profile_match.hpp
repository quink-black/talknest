#pragma once

#include <cstdint>
#include <cstring>
#include <string>

namespace profile_match {

/* Compare a scanned AP SSID (a NUL-terminated byte array, which may hold
 * UTF-8) against a configured SSID byte for byte. Exact matching keeps a
 * prefix or a similarly named network from selecting the wrong profile. */
inline bool ssid_equals(const uint8_t *raw, size_t max_length,
                        const std::string &ssid)
{
    size_t length = 0;
    while (length < max_length && raw[length] != 0) {
        ++length;
    }
    return length == ssid.size() &&
           std::memcmp(raw, ssid.data(), length) == 0;
}

} // namespace profile_match
