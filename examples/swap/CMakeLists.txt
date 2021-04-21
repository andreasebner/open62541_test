##########################
# SWAP Information Model #
##########################

set(SWAP_CSV_DIRPREFIX ../../../../Informationsmodell/Output)
set(SWAP_BSD_DIRPREFIX ../../../../Informationsmodell/Output)
set(SWAP_NS_DIRPREFIX ../../../../Informationsmodell/Output)

if(NOT CMAKE_PROJECT_NAME STREQUAL PROJECT_NAME)
    # needed or cmake doesn't recognize dependencies of generated files
    set(PROJECT_BINARY_DIR ${CMAKE_BINARY_DIR})
endif()

message("Path to generated SWAP-Nodesets ${SWAP_NS_DIRPREFIX}")

if(UA_NAMESPACE_ZERO STREQUAL "FULL")
    ua_generate_nodeset_and_datatypes(
            NAME "swap_nodeset"
            FILE_CSV "${SWAP_CSV_DIRPREFIX}/swap_nodeset.csv"
            FILE_BSD "${SWAP_BSD_DIRPREFIX}/swap_nodeset.bsd"
            NAMESPACE_IDX 2
            FILE_NS "${SWAP_NS_DIRPREFIX}/swap_nodeset.xml"
            INTERNAL
    )
    add_example(swap_server swap_nodeset.c
            ${UA_NODESET_SWAP_NODESET_SOURCES}
            ${UA_TYPES_SWAP_NODESET_SOURCES})
    add_dependencies(swap_server open62541-generator-ns-swap_nodeset)
endif()