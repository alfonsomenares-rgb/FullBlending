# FullBlending
A Python tool for converting virtual multi-color G-code into printable filament patterns using a limited number of real filaments. It supports Bambu Studio, OrcaSlicer, and Snapmaker workflows, with CMYK/CMYW/CMYKW pattern generation, translucency simulation, duplicate color filtering, and a 3D G-code preview.

Unlike Full Spectrum-style workflows, this method is designed to include the internal walls as part of the final perceived color. Instead of relying only on the outer visible wall, it uses wall-layer patterns and filament translucency to let inner colors influence the result.

AI Assistance Disclosure

This project was developed with the assistance of AI tools. I am a beginner in Python, so AI was used to help write, organize, debug, and improve the code.

However, the core concept, workflow, color-mixing logic, pattern strategy, feature decisions, testing direction, and overall project idea were created and directed by me. The tool is based on my own experimentation with multi-color 3D printing, virtual filament markers, CMYK/CMYW/CMYKW pattern generation, translucency behavior, and G-code conversion workflows.

In short: the code was AI-assisted, but the logic, design decisions, and project direction are mine.

# User Guide

This tool converts virtual multi-color G-code into printable filament patterns using a limited number of real filaments. It can be used with Bambu Lab / OrcaSlicer workflows and with Snapmaker U1 workflows.

The basic idea is:

* Real base filaments are the physical colors loaded in the printer.
* Virtual marker filaments are temporary colors used only to paint the model in the slicer.
* The tool replaces those virtual markers with printable wall patterns using the real base filaments.
* The perceived color is estimated using a subtractive/translucency-aware model instead of simple RGB averaging.

---

# 1. Important Concepts

## Base Filaments

Base filaments are the real filaments physically loaded in the printer.

Examples:

```text
CMYK  = Cyan, Magenta, Yellow, Black
CMYW  = Cyan, Magenta, Yellow, White
CMYKW = Cyan, Magenta, Yellow, Black, White
```

For Snapmaker U1, you normally use only 4 real filaments:

```text
T0, T1, T2, T3
```

For Bambu Lab printers with more available materials, you can use more base filaments if needed.

---

## Virtual Marker Filaments

Virtual markers are colors used in the slicer only to paint the model. They are not meant to be physically loaded in the printer.

Example:

```text
T0 = Cyan base filament
T1 = Magenta base filament
T2 = Yellow base filament
T3 = Black base filament
T4+ = virtual color markers
```

After conversion, the virtual markers should be replaced with real printable patterns.

---

## Outer Walls and Inner Walls

Unlike Full Spectrum-style workflows that mainly focus on the visible outer surface, this tool also uses the internal walls as part of the final perceived color.

The outer wall is the most visible layer, but the inner walls can still affect the final color through:

* filament translucency
* light absorption
* darkening
* light diffusion
* subtractive color interaction
* layered wall patterns

This allows the tool to generate colors using both the exterior and the interior structure of the print the tool to generate colors using both the exterior and the.

---

# 2. Recommended Workflow

## Step 1 — Prepare the Model in the Slicer

Paint your model using multiple virtual colors.

For example, in Bambu Studio or OrcaSlicer:

```text
T0-T3 = real base filaments
T4+   = virtual marker colors
```

The virtual colors represent the target colors you want to approximate.

---

## Step 2 — Export the G-code

Export the sliced file as:

```text
.gcode
```

or, for Bambu/Orca workflows:

```text
.gcode.3mf
```

For Snapmaker, plain `.gcode` is recommended.

---

## Step 3 — Open the Tool

Run the Python file or use the Windows `.bat` launcher.

```text
python bambu_palette_pattern_bridge.py
```

---

## Step 4 — Load the G-code

Use the input file selector to load your sliced G-code or G-code 3MF file.

---

## Step 5 — Configure the Base Filaments

Set how many real base filaments you are using.

Examples:

```text
3 base filaments = CMY
4 base filaments = CMYK or CMYW
5 base filaments = CMYKW
```

The number of base filaments determines where virtual marker filaments begin.

Example:

```text
4 base filaments → virtual markers start at T4
5 base filaments → virtual markers start at T5
```

---

# 3. Bambu Lab / OrcaSlicer Instructions

Use this mode when your output is meant for Bambu Studio, OrcaSlicer, or Bambu Lab printers.

## Recommended Settings for Bambu

```text
Output machine: Bambu / Orca
Input: .gcode or .gcode.3mf
Output: .gcode or .gcode.3mf
Base filaments: 4 or 5, depending on your AMS setup
```

## Bambu with CMYK

Use this if you want stronger dark colors and better shadows.

```text
T0 = Cyan
T1 = Magenta
T2 = Yellow
T3 = Black
Virtual markers start at T4
```

Pros:

* Better dark colors
* Better browns
* Better shadows
* Better black influence

Cons:

* Pastel colors are harder
* Light colors may look darker because there is no white filament

---

## Bambu with CMYW

Use this if you want brighter colors and pastel tones.

```text
T0 = Cyan
T1 = Magenta
T2 = Yellow
T3 = White
Virtual markers start at T4
```

Pros:

* Better light colors
* Better pastel colors
* Better soft tones

Cons:

* Deep blacks are not possible
* Dark colors must be simulated using CMY mixing

---

## Bambu with CMYKW

Use this if your setup supports 5 real base filaments.

```text
T0 = Cyan
T1 = Magenta
T2 = Yellow
T3 = Black
T4 = White
Virtual markers start at T5
```

Pros:

* Best general color range
* Better darks and lights
* Better control over contrast
* More useful for translucency-based patterns

Cons:

* Requires 5 real filament slots
* Not suitable for Snapmaker U1 if only 4 filaments are available

---

## Bambu Output Notes

For Bambu / Orca `.gcode.3mf` output, the tool attempts to update internal color metadata such as:

```text
filament_colour
project_settings.config
plate_1.json
plate_1.gcode
```

This helps Bambu Studio / OrcaSlicer display the correct base filament colors after conversion.

---

# 4. Snapmaker U1 Instructions

Use this mode when your output is meant for Snapmaker U1.

Snapmaker U1 should only receive the real base filaments, usually:

```text
T0, T1, T2, T3
```

Virtual markers such as `T4+` should not remain in the final output.

---

## Recommended Settings for Snapmaker

```text
Output machine: Snapmaker
Input: original painted G-code
Output: plain .gcode
Base filaments: 4
```

Do not use `.gcode.3mf` as the final Snapmaker output unless you specifically know what you are doing.

The safest output is:

```text
.gcode
```

---

## Snapmaker with CMYK

Recommended when you need dark colors.

```text
T0 = Cyan
T1 = Magenta
T2 = Yellow
T3 = Black
Virtual markers start at T4
```

Use this for:

* darker models
* browns
* shadows
* high-contrast prints

Limitation:

* light pastel colors will be harder to reproduce without white

---

## Snapmaker with CMYW

Recommended when you need light colors.

```text
T0 = Cyan
T1 = Magenta
T2 = Yellow
T3 = White
Virtual markers start at T4
```

Use this for:

* bright models
* pastel colors
* soft colors
* light skin tones
* light green, pink, blue, cream, etc.

Limitation:

* black and very dark colors will not be accurate

---

## Snapmaker Output Rules

When Snapmaker output is selected, the tool should:

```text
- remove virtual marker filaments from metadata
- keep only T0-T3
- remap T4+ commands into T0-T3 patterns
- clean leftover filament arrays
- output a plain .gcode file
```

If the slicer still shows many extra filaments, the file may still contain old virtual marker metadata.

In that case:

```text
Use the original input G-code again.
Do not reconvert an already converted output file.
Export as plain .gcode.
Select Snapmaker output mode.
```

---

# 5. Main Options

## Output Machine

Selects the target workflow.

```text
Bambu / Orca
Snapmaker
```

Use `Bambu / Orca` when the output will be opened again in Bambu Studio or OrcaSlicer.

Use `Snapmaker` when the output will be used in Snapmaker Orca or Snapmaker U1.

---

## Number of Base Filaments

Controls how many real filaments are physically available.

Examples:

```text
3 = CMY
4 = CMYK or CMYW
5 = CMYKW
```

This also controls where virtual markers begin.

---

## Active Symbols

Controls which color symbols are used by the pattern generator.

Examples:

```text
CMY
CMYK
CMYW
CMYKW
```

Use only the symbols that match your real filament setup.

---

## Base Color / Filament Mapping

This lets you assign each symbol to a real filament slot.

Example:

```text
C → T0
M → T1
Y → T2
K → T3
W → T4
```

For Snapmaker, avoid assigning anything beyond T3.

---

## Base Color HEX

Each base filament has a color value.

Example:

```text
C = #00FFFF
M = #FF00FF
Y = #FFFF00
K = #000000
W = #FFFFFF
```

These values are used for:

* palette generation
* perceived color simulation
* recipe matching
* visual preview
* output metadata

For better results, set these colors close to your real filament colors.

---

## Filament Translucency

Each filament can have a translucency value.

```text
0 = very opaque
1 = very translucent
```

This controls how much inner walls affect the final perceived color.

Suggested starting values:

```text
C = 0.30–0.45
M = 0.30–0.45
Y = 0.30–0.45
K = 0.05–0.20
W = 0.15–0.35
```

If black inside the model affects the color too much, reduce the translucency of the outer filament or reduce the black filament translucency.

If inner walls are not visible enough, increase the translucency of the outer filament.

---

## Duplicate Color Filter

Removes repeated or visually similar colors from the generated palette.

The tool can filter colors using perceptual distance.

Recommended value:

```text
DeltaE 1.8
```

Use higher values if you see too many similar colors.

Use lower values if the tool removes too many useful variations.

Example:

```text
0.8 = softer filtering
1.8 = normal filtering
2.5–3.0 = stronger filtering
```

---

## 2-Layer and 3-Layer Recipes

The tool can generate recipes using both 2-layer and 3-layer patterns.

## 3-Layer Recipes

A 3-layer recipe uses three pattern steps.

Example:

```text
Outer: C Y C
Inner: K W K
```

This means the visible result is built from a repeating 3-layer cycle.

---

## 2-Layer Recipes

A 2-layer recipe uses two pattern steps.

Example:

```text
Outer: W K
Inner: K W
```

This creates a 50/50 pattern between two combinations.

2-layer recipes are useful when a color is better represented by a simple half-and-half alternation.

---

## Manual Recipe Selection

You can manually assign a recipe to a virtual marker color.

Use this when the automatic match is not visually correct.

A recipe generally defines:

```text
outer wall pattern
inner wall pattern
layer cycle length
```

Example:

```text
CYC:KWK
```

means:

```text
Outer wall cycle = C Y C
Inner wall cycle = K W K
```

---

## Automatic Recipe Assignment

The tool can automatically assign the closest physical pattern to each virtual marker color.

It compares the target marker color against the generated palette and chooses the closest match.

The match uses the tool’s physical color simulation, not simple RGB averaging.

---

# 6. 3D G-code Viewer

The 3D viewer is used to preview the converted G-code before printing.

## Viewer Source

The viewer can use different sources.

```text
Final temporary
Output file
Original
```

Recommended:

```text
Final temporary
```

This shows the converted result using the current settings before you save the final file.

Use `Original` only if you want to inspect the unconverted input.

---

## Viewer Color Mode

The viewer can show colors in different ways.

```text
Real filament color
Perceived PLA color
```

Use `Real filament color` to debug tool changes and check which physical filament is being used.

Use `Perceived PLA color` to estimate the final visual result after translucency and wall interaction.

---

## Viewer Layer Controls

The viewer can display:

```text
one layer only
all layers up to selected height
the full model
```

Use layer preview to check whether patterns are alternating correctly.

---

## Viewer Feature Filter

The viewer can filter by G-code feature.

Examples:

```text
All
Outer wall
Inner wall
Top / Bottom
Infill
```

Use `Outer wall` to check if the visible pattern is being applied correctly.

Use `Inner wall` to check the support colors that influence translucency.

---

## Camera Controls

```text
Left mouse drag  = rotate camera
Right mouse drag = pan camera
Middle mouse drag = pan camera
Mouse wheel      = zoom
Yaw slider       = rotate horizontally
Pitch slider     = rotate vertically
Reset camera     = restore default view
```

---

## Visual Volume

The viewer can show toolpaths as thicker visual cords instead of thin lines.

Use this to better understand how the extruded material may look.

This is still a visual approximation, not a full physical simulation.

---

# 7. Common Problems

## The output still shows too many filaments

This usually means old virtual marker metadata is still present.

For Snapmaker:

```text
Use Snapmaker output mode.
Export as plain .gcode.
Do not use .gcode.3mf.
Do not reconvert an already converted output.
```

---

## The exterior has no visible pattern

Possible causes:

```text
The file was already converted before.
Layer changes were not detected.
The viewer is showing the original file instead of the final temporary file.
The selected feature filter is hiding outer walls.
```

Try:

```text
Use the original input G-code.
Set viewer source to Final temporary.
Filter by Outer wall.
Check layer slider.
Export again with the latest version.
```

---

## Colors are too dark

Possible causes:

```text
Black filament has too much influence.
The outer filament is too translucent.
The selected setup is CMYK without white.
```

Try:

```text
Lower black translucency.
Lower outer filament translucency.
Use CMYW if the model needs lighter colors.
Use CMYKW if your printer supports 5 base filaments.
```

---

## Colors are too light

Possible causes:

```text
White has too much influence.
Black is missing from the base set.
CMYW is being used instead of CMYK.
```

Try:

```text
Use CMYK for darker models.
Reduce white influence.
Increase CMY pigment strength by using more saturated base colors.
```

---

## Pastel colors look wrong

Pastel colors usually need white.

Use:

```text
CMYW
```

or:

```text
CMYKW
```

CMYK alone is not ideal for pastel colors.

---

## Dark colors look wrong

Dark colors usually need black.

Use:

```text
CMYK
```

or:

```text
CMYKW
```

CMYW alone is not ideal for deep dark colors.

---

# 8. Best Practice Recommendations

## For Snapmaker U1

Use one of these setups:

```text
CMYK = better dark colors
CMYW = better light colors
```

Recommended output:

```text
plain .gcode
```

Avoid:

```text
.gcode.3mf as final Snapmaker output
```

---

## For Bambu Lab / Orca

Use:

```text
CMYK  = good general 4-color setup
CMYW  = better light colors
CMYKW = best range if 5 filaments are available
```

Bambu / Orca can use `.gcode.3mf`, but plain `.gcode` is also useful for debugging.

---

# 9. Limitations

This tool estimates color appearance, but it cannot perfectly predict real printed colors.

The final result depends on:

```text
filament brand
pigment strength
real translucency
wall thickness
number of walls
layer height
line width
temperature
lighting conditions
printer calibration
slicer behavior
```

The color model is designed to be physically more realistic than RGB averaging, but it is still an approximation.

For best results, print small calibration samples and adjust the base filament colors and translucency values.
