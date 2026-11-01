# Draws the Orbit app icon: a white planet circling a gold sun on a tilted orbit, on the app's teal.
# Writes orbit.ico (16-256 px, for the desktop and taskbar shortcut) and orbit-1024.png next to this file.
# Run: powershell -ExecutionPolicy Bypass -File brand\make-icon.ps1
Add-Type -AssemblyName System.Drawing
$ErrorActionPreference = 'Stop'

function C([string]$hex) {
  $h = $hex.TrimStart('#')
  [System.Drawing.Color]::FromArgb([Convert]::ToInt32($h.Substring(0, 2), 16), [Convert]::ToInt32($h.Substring(2, 2), 16), [Convert]::ToInt32($h.Substring(4, 2), 16))
}
function Mix($a, $b, [double]$t) {
  [System.Drawing.Color]::FromArgb([int]($a.R + ($b.R - $a.R) * $t), [int]($a.G + ($b.G - $a.G) * $t), [int]($a.B + ($b.B - $a.B) * $t))
}
function RoundRect([float]$x, [float]$y, [float]$w, [float]$h, [float]$r) {
  $p = New-Object System.Drawing.Drawing2D.GraphicsPath
  $d = 2 * $r
  $p.AddArc($x, $y, $d, $d, 180, 90); $p.AddArc($x + $w - $d, $y, $d, $d, 270, 90)
  $p.AddArc($x + $w - $d, $y + $h - $d, $d, $d, 0, 90); $p.AddArc($x, $y + $h - $d, $d, $d, 90, 90)
  $p.CloseFigure(); $p
}
function Ball($g, [float]$cx, [float]$cy, [float]$r, $c1, $c2) {
  $rect = New-Object System.Drawing.RectangleF(($cx - $r), ($cy - $r), (2 * $r), (2 * $r))
  $g.FillEllipse((New-Object System.Drawing.Drawing2D.LinearGradientBrush($rect, $c1, $c2, 55)), $rect)
}

# A point on the tilted orbit. Degrees run clockwise from the upper-right tip, where the planet sits.
function OrbitPoint([double]$deg, [double]$rx, [double]$ry) {
  $t = $deg * [Math]::PI / 180; $tilt = -35 * [Math]::PI / 180
  $x = $rx * [Math]::Cos($t); $y = $ry * [Math]::Sin($t)
  , @((128 + $x * [Math]::Cos($tilt) - $y * [Math]::Sin($tilt)), (128 + $x * [Math]::Sin($tilt) + $y * [Math]::Cos($tilt)))
}

# Everything is drawn on a 256 x 256 grid. At 32 px and below, the fading far side is dropped and shapes get bolder.
function Draw-Orbit($g, [int]$size) {
  $small = $size -le 32
  $tile = New-Object System.Drawing.RectangleF(8, 8, 240, 240)
  $g.FillPath((New-Object System.Drawing.Drawing2D.LinearGradientBrush($tile, (C '#12838F'), (C '#062A35'), 90)), (RoundRect 8 8 240 240 54))

  $rx = 100
  if ($small) { $ry = 64; $width = 25; $planet = 28; $clear = 17 } else { $ry = 60; $width = 20; $planet = 23; $clear = 9 }
  $head = OrbitPoint 0 $rx $ry

  # The orbit stops short on both sides of the planet, leaving it clear space
  $gap = 1
  while ($true) {
    $q = OrbitPoint $gap $rx $ry
    $dist = [Math]::Sqrt([Math]::Pow($q[0] - $head[0], 2) + [Math]::Pow($q[1] - $head[1], 2))
    if ($dist -ge $planet + $width / 2 + $clear) { break }
    $gap++
  }

  # Far side first and dimmer, near side last and bright, so the ring looks tilted in space
  $white = C '#FFFFFF'; $far = if ($small) { $white } else { C '#7CCFD8' }
  foreach ($side in @((360 - $gap)..180), @($gap..180)) {
    for ($i = 1; $i -lt $side.Count; $i++) {
      $a = OrbitPoint $side[$i - 1] $rx $ry; $b = OrbitPoint $side[$i] $rx $ry
      $near = ([Math]::Sin($side[$i] * [Math]::PI / 180) + 1) / 2
      $pen = New-Object System.Drawing.Pen((Mix $far $white $near), $width)
      $pen.StartCap = 'Round'; $pen.EndCap = 'Round'
      $g.DrawLine($pen, [float]$a[0], [float]$a[1], [float]$b[0], [float]$b[1])
      $pen.Dispose()
    }
  }

  Ball $g 128 128 32 (C '#FFDC78') (C '#EE9D1E')
  Ball $g $head[0] $head[1] $planet $white (C '#CFEFF2')
}

function Render([int]$size) {
  $bmp = New-Object System.Drawing.Bitmap($size, $size, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.SmoothingMode = 'AntiAlias'; $g.PixelOffsetMode = 'HighQuality'; $g.CompositingQuality = 'HighQuality'
  $g.Clear([System.Drawing.Color]::Transparent)
  $g.ScaleTransform($size / 256.0, $size / 256.0)
  Draw-Orbit $g $size
  $g.Dispose()
  $ms = New-Object System.IO.MemoryStream
  $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
  $bmp.Dispose()
  , $ms.ToArray()
}

# An .ico file is a small header, one directory entry per size, then each size as a PNG.
function Write-Ico([string]$path, [int[]]$sizes) {
  $pngs = @{}; foreach ($s in $sizes) { $pngs[$s] = Render $s }
  $w = New-Object System.IO.BinaryWriter([System.IO.File]::Create($path))
  $w.Write([uint16]0); $w.Write([uint16]1); $w.Write([uint16]$sizes.Count)
  $offset = 6 + 16 * $sizes.Count
  foreach ($s in $sizes) {
    $b = [byte]$(if ($s -ge 256) { 0 } else { $s })
    $w.Write($b); $w.Write($b); $w.Write([byte]0); $w.Write([byte]0)
    $w.Write([uint16]1); $w.Write([uint16]32)
    $w.Write([uint32]$pngs[$s].Length); $w.Write([uint32]$offset)
    $offset += $pngs[$s].Length
  }
  foreach ($s in $sizes) { $w.Write($pngs[$s]) }
  $w.Dispose()
}

Write-Ico (Join-Path $PSScriptRoot 'orbit.ico') @(16, 20, 24, 32, 40, 48, 64, 96, 128, 256)
[System.IO.File]::WriteAllBytes((Join-Path $PSScriptRoot 'orbit-1024.png'), (Render 1024))
Write-Output "Wrote orbit.ico and orbit-1024.png in $PSScriptRoot"
