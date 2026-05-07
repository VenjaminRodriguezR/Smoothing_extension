cmake_minimum_required(VERSION 3.20.6...3.22.6 FATAL_ERROR)

project(Smoothing_extension)

#-----------------------------------------------------------------------------
# Extension meta-information
set(EXTENSION_HOMEPAGE "https://www.slicer.org/wiki/Documentation/Nightly/Extensions/Smoothing_extension")
set(EXTENSION_CONTRIBUTORS "Benjamin Rodriguez Romo (Universidad Adolfo Ibañez), Steven Mell (Rush University Medical Center), Catherin Yuh (Rush University Medical Center)")
set(EXTENSION_DESCRIPTION "Develop to compare the impact of different parameters on smoothing algorithms. 
")
set(EXTENSION_ICONURL "https://www.example.com/Slicer/Extensions/Smoothing_extension.png")
set(EXTENSION_SCREENSHOTURLS "https://www.example.com/Slicer/Extensions/Smoothing_extension/Screenshots/1.png")
set(EXTENSION_DEPENDS "NA") # Specified as a list or "NA" if no dependencies

#-----------------------------------------------------------------------------
# Extension dependencies
find_package(Slicer REQUIRED)
include(${Slicer_USE_FILE})

#-----------------------------------------------------------------------------
# Extension modules
add_subdirectory(Smoothing)
## NEXT_MODULE

#-----------------------------------------------------------------------------
include(${Slicer_EXTENSION_GENERATE_CONFIG})
include(${Slicer_EXTENSION_CPACK})
