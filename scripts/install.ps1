# Installs Unix into a Blender version as a live link to this folder, then enables it.
# Usage:  .\scripts\install.ps1                      (Blender 4.5)
#         .\scripts\install.ps1 -Version 5.2 -Blender "C:\path\to\blender.exe"
#         .\scripts\install.ps1 -Uninstall
param(
    [string]$Version = "4.5",
    [string]$Blender = "C:\Program Files\Blender Foundation\Blender $Version\blender.exe",
    [string]$UserScripts = "$env:APPDATA\Blender Foundation\Blender\$Version\scripts",
    [switch]$Uninstall
)
$ErrorActionPreference = "Stop"
$source = Join-Path (Split-Path $PSScriptRoot -Parent) "unix"
$addons = Join-Path $UserScripts "addons"
$link = Join-Path $addons "unix"

if (-not (Test-Path $Blender)) { throw "Blender not found at $Blender. Pass -Blender <path>." }

if ($Uninstall) {
    & $Blender -b --python-expr "import bpy; bpy.ops.preferences.addon_disable(module='unix'); bpy.ops.wm.save_userpref()" | Out-Null
    if (Test-Path $link) { cmd /c rmdir "$link" | Out-Null }   # removes the link only, never the source
    Write-Host "Unix removed from Blender $Version."
    return
}

New-Item -ItemType Directory -Force $addons | Out-Null
if (Test-Path $link) { cmd /c rmdir "$link" | Out-Null }
cmd /c mklink /J "$link" "$source" | Out-Null
& $Blender -b --python-expr "import bpy; bpy.ops.preferences.addon_enable(module='unix'); bpy.ops.wm.save_userpref(); print('UNIX_ENABLED', 'unix' in bpy.context.preferences.addons)" |
    Select-String "UNIX_ENABLED"
Write-Host "Linked $link -> $source"
Write-Host "Open Blender $Version, press N in the 3D viewport and choose the Unix tab."
