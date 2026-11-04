#include <algorithm>
#include <array>
#include <cassert>
#include <cstdint>
#include <string>

#include "profile_match.hpp"

int main()
{
    std::array<uint8_t, 33> raw = {};
    const char *ssid = "HomeNet";
    for (size_t i = 0; i < 7; ++i) {
        raw[i] = static_cast<uint8_t>(ssid[i]);
    }
    assert(profile_match::ssid_equals(raw.data(), raw.size(), "HomeNet"));
    assert(!profile_match::ssid_equals(raw.data(), raw.size(), "Home"));
    assert(!profile_match::ssid_equals(raw.data(), raw.size(), "HomeNet2"));
    assert(!profile_match::ssid_equals(raw.data(), raw.size(), "homenet"));

    // UTF-8 SSIDs are compared byte for byte, not as text.
    std::array<uint8_t, 33> utf8 = {};
    const std::string wide = "\xe4\xb8\xad\xe6\x96\x87";
    std::copy(wide.begin(), wide.end(), utf8.begin());
    assert(profile_match::ssid_equals(utf8.data(), utf8.size(), wide));

    // A 32-byte SSID may fill the whole array without a terminator.
    std::array<uint8_t, 33> full;
    full.fill('A');
    assert(profile_match::ssid_equals(full.data(), full.size(),
                                      std::string(33, 'A')));

    std::array<uint8_t, 33> empty = {};
    assert(profile_match::ssid_equals(empty.data(), empty.size(), ""));
    assert(!profile_match::ssid_equals(raw.data(), raw.size(), ""));
    return 0;
}
