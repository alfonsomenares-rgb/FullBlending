# FullBlending
A Python tool for converting virtual multi-color G-code into printable filament patterns using a limited number of real filaments. It supports Bambu Studio, OrcaSlicer, and Snapmaker workflows, with CMYK/CMYW/CMYKW pattern generation, translucency simulation, duplicate color filtering, and a 3D G-code preview.

Unlike Full Spectrum-style workflows, this method is designed to include the internal walls as part of the final perceived color. Instead of relying only on the outer visible wall, it uses wall-layer patterns and filament translucency to let inner colors influence the result.

AI Assistance Disclosure

This project was developed with the assistance of AI tools. I am a beginner in Python, so AI was used to help write, organize, debug, and improve the code.

However, the core concept, workflow, color-mixing logic, pattern strategy, feature decisions, testing direction, and overall project idea were created and directed by me. The tool is based on my own experimentation with multi-color 3D printing, virtual filament markers, CMYK/CMYW/CMYKW pattern generation, translucency behavior, and G-code conversion workflows.

In short: the code was AI-assisted, but the logic, design decisions, and project direction are mine.
