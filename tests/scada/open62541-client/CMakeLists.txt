# open62541 scenario client for the L1 SCADA suite (tests/scada/README.md). Test-only.
# Built by build.sh against open62541 installed at a pinned, commit-checked version.
cmake_minimum_required(VERSION 3.16)
project(scada_l1_open62541 C)

find_package(open62541 1.4 REQUIRED)

add_executable(scada_l1_open62541 scada_l1_open62541.c)
target_link_libraries(scada_l1_open62541 PRIVATE open62541::open62541)
set_target_properties(scada_l1_open62541 PROPERTIES C_STANDARD 99)
target_compile_options(scada_l1_open62541 PRIVATE -Wall -Wextra -Wno-unused-parameter)
