cmake_minimum_required(VERSION 3.10)
project(unityport_hlslcc CXX)

# Unity's HLSLcc fork, checked out as a git submodule
set(HLSLCC_DIR "${CMAKE_CURRENT_SOURCE_DIR}/HLSLcc" CACHE PATH "HLSLcc source directory")
if(NOT EXISTS "${HLSLCC_DIR}/CMakeLists.txt")
    message(FATAL_ERROR "HLSLcc not found in ${HLSLCC_DIR}; run: git submodule update --init")
endif()
set(HLSLCC_LIBRARY_SHARED OFF CACHE BOOL "" FORCE)
add_subdirectory(${HLSLCC_DIR} hlslcc EXCLUDE_FROM_ALL)

add_executable(hlslcc_tool hlslcc_tool.cpp)
target_link_libraries(hlslcc_tool hlslcc)
set_target_properties(hlslcc_tool PROPERTIES CXX_STANDARD 11)
