# Runs every test that needs no model call and no internet. Add -Agent to also run one real turn.
param(
    [string]$Blender = "C:\Program Files\Blender Foundation\Blender 4.5\blender.exe",
    [switch]$Agent
)
$root = Split-Path $PSScriptRoot -Parent
python (Join-Path $root "tests\test_bridge.py") | Select-String "^(FAIL|RESULT)"
foreach ($test in "test_tools.py", "test_tools_extended.py", "test_engine.py", "test_engines.py", "test_unreal_link.py", "test_roblox_rig.py", "test_roblox_surface.py", "test_projection.py", "test_export_alone.py", "test_rigging.py", "test_motion.py", "test_team.py", "test_goal.py", "test_quality.py", "test_vfx.py", "test_skills.py", "test_responsiveness.py", "test_stability.py", "test_watchdog.py") {
    Write-Host "== $test"
    & $Blender -b --factory-startup --python (Join-Path $root "tests\$test") | Select-String "^(FAIL|PROBLEM|SKILLS|RESULT)"
}
if ($Agent) {
    & $Blender -b --factory-startup --python (Join-Path $root "tests\test_agent_e2e.py") | Select-String "^(SEND|  |TEXT|ERROR|META|OBJECTS)"
}
# The island's look: tests\island_render.py draws every state to PNG files (needs a window session, not -b).
# The crash under load: tests\stress_threads.py hammers a windowed Blender with concurrent tool calls (see its header).

