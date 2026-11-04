set(CONFIG_DIR "${CMAKE_CURRENT_LIST_DIR}/../../config")
set(CA_FILE "${CONFIG_DIR}/ca.pem")

# Every talknest*.json in config/ is one environment profile. The plain
# talknest.json is the default environment and is kept first, so a failed
# scan reliably falls back to it; the rest follow in file-name order.
# A plain GLOB: IDF evaluates component CMakeLists in script mode as well,
# where CONFIGURE_DEPENDS is invalid. Adding or removing a profile file
# therefore needs an explicit "idf.py reconfigure" (documented in README).
file(GLOB PROFILE_FILES "${CONFIG_DIR}/talknest*.json")
list(FILTER PROFILE_FILES EXCLUDE REGEX "/talknest\\.example\\.json$")
list(SORT PROFILE_FILES)
list(FIND PROFILE_FILES "${CONFIG_DIR}/talknest.json" DEFAULT_INDEX)
if(DEFAULT_INDEX GREATER -1)
    list(REMOVE_AT PROFILE_FILES ${DEFAULT_INDEX})
    list(PREPEND PROFILE_FILES "${CONFIG_DIR}/talknest.json")
endif()
list(LENGTH PROFILE_FILES PROFILE_COUNT)

set(MAX_PROFILES 8)
if(PROFILE_COUNT EQUAL 0)
    message(FATAL_ERROR "Create at least one config/talknest*.json from the documented example before building")
endif()
if(PROFILE_COUNT GREATER MAX_PROFILES)
    message(FATAL_ERROR "config/ has ${PROFILE_COUNT} profiles; the limit is ${MAX_PROFILES}. Remove unused profiles; raising the limit requires extending the symbol table in app_config.cpp")
endif()
if(NOT EXISTS "${CA_FILE}")
    message(FATAL_ERROR "Create config/ca.pem from the documented example before building")
endif()

# Stage every profile under a fixed name so the embedded symbols never
# depend on the user's file names. Slots without a profile hold an empty
# JSON object; only PROFILE_COUNT of them are parsed at runtime.
# A fixed location under CMAKE_BINARY_DIR: IDF evaluates component
# CMakeLists twice during configuration, and CMAKE_CURRENT_BINARY_DIR is
# not the same in both passes.
set(PROFILE_DIR "${CMAKE_BINARY_DIR}/app_config_profiles")
file(MAKE_DIRECTORY "${PROFILE_DIR}")

set(EMBED_LIST "${CA_FILE}")
set(PROFILE_NAMES "")
math(EXPR LAST_SLOT "${MAX_PROFILES} - 1")
foreach(i RANGE 0 ${LAST_SLOT})
    set(STAGED "${PROFILE_DIR}/profile_${i}.json")
    if(i LESS PROFILE_COUNT)
        list(GET PROFILE_FILES ${i} SOURCE)
        configure_file("${SOURCE}" "${STAGED}" COPYONLY)
        get_filename_component(FNAME "${SOURCE}" NAME)
        string(REGEX REPLACE "\\.json$" "" STEM "${FNAME}")
        if(STEM STREQUAL "talknest")
            set(PNAME "default")
        else()
            string(REGEX REPLACE "^talknest\\." "" PNAME "${STEM}")
        endif()
        if(NOT PNAME MATCHES "^[A-Za-z0-9_-]+$")
            message(FATAL_ERROR "Profile name '${PNAME}' from ${SOURCE} may only contain letters, digits, dash and underscore")
        endif()
        list(APPEND PROFILE_NAMES "${PNAME}")
    else()
        file(WRITE "${STAGED}" "{}")
    endif()
    list(APPEND EMBED_LIST "${STAGED}")
endforeach()

set(MANIFEST "${PROFILE_DIR}/profiles_manifest.h")
set(MANIFEST_TEXT "#pragma once\n#define TALKNEST_MAX_PROFILES ${MAX_PROFILES}\n#define TALKNEST_PROFILE_COUNT ${PROFILE_COUNT}\n")
string(APPEND MANIFEST_TEXT "static const char *const talknest_profile_names[TALKNEST_PROFILE_COUNT] = {")
foreach(PNAME IN LISTS PROFILE_NAMES)
    string(APPEND MANIFEST_TEXT "\"${PNAME}\", ")
endforeach()
string(APPEND MANIFEST_TEXT "};\n")
file(WRITE "${MANIFEST}" "${MANIFEST_TEXT}")

idf_component_register(
    SRCS "app_config.cpp"
    INCLUDE_DIRS "include" "${PROFILE_DIR}"
    REQUIRES espressif__cjson waveshare__esp32_s3_touch_amoled_2_06
    EMBED_FILES ${EMBED_LIST}
)
