cmake_minimum_required(VERSION 3.14)
project(sample LANGUAGES CXX)

enable_testing()

add_library(sample src/sample.cpp)
target_include_directories(sample PUBLIC include)

add_executable(sample_tests tests/sample_tests.cpp)
target_link_libraries(sample_tests PRIVATE sample)
add_test(NAME sample_tests COMMAND sample_tests)

add_executable(other_tests tests/other_tests.cpp)
target_link_libraries(other_tests PRIVATE sample)
add_test(NAME other_tests COMMAND other_tests)
