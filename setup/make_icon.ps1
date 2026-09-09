# Baut rankyoinker.ico aus dem Website-Logo nach (siehe .brand-mark in index.html):
# abgerundetes Quadrat mit Gold-Verlauf (--red -> --red-deep) und dunklem "Y".
# Nur zum einmaligen Erzeugen der Icon-Datei - kein Teil der App selbst.
Add-Type -AssemblyName System.Drawing

function New-LogoBitmap([int]$size) {
    $bmp = New-Object System.Drawing.Bitmap($size, $size)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
    $g.Clear([System.Drawing.Color]::Transparent)

    $radius = [Math]::Max(2, [int]($size * 0.30))
    $rect = New-Object System.Drawing.Rectangle(0, 0, ($size - 1), ($size - 1))

    $path = New-Object System.Drawing.Drawing2D.GraphicsPath
    $d = $radius * 2
    $path.AddArc($rect.X, $rect.Y, $d, $d, 180, 90)
    $path.AddArc($rect.Right - $d, $rect.Y, $d, $d, 270, 90)
    $path.AddArc($rect.Right - $d, $rect.Bottom - $d, $d, $d, 0, 90)
    $path.AddArc($rect.X, $rect.Bottom - $d, $d, $d, 90, 90)
    $path.CloseFigure()

    $colTL = [System.Drawing.ColorTranslator]::FromHtml("#c9a869")
    $colBR = [System.Drawing.ColorTranslator]::FromHtml("#8a7248")
    $brush = New-Object System.Drawing.Drawing2D.LinearGradientBrush(
        (New-Object System.Drawing.Point(0, 0)),
        (New-Object System.Drawing.Point($size, $size)),
        $colTL, $colBR)
    $g.FillPath($brush, $path)

    $textColor = [System.Drawing.ColorTranslator]::FromHtml("#0a0a0b")
    $fontSize = [single]($size * 0.56)
    $font = New-Object System.Drawing.Font("Segoe UI", $fontSize, [System.Drawing.FontStyle]::Bold, [System.Drawing.GraphicsUnit]::Pixel)
    $sf = New-Object System.Drawing.StringFormat
    $sf.Alignment = [System.Drawing.StringAlignment]::Center
    $sf.LineAlignment = [System.Drawing.StringAlignment]::Center
    $textBrush = New-Object System.Drawing.SolidBrush($textColor)
    $g.DrawString("Y", $font, $textBrush, (New-Object System.Drawing.RectangleF(0, [single](-$size*0.03), $size, $size)), $sf)

    $g.Dispose()
    return $bmp
}

function ConvertTo-PngBytes([System.Drawing.Bitmap]$bmp) {
    $ms = New-Object System.IO.MemoryStream
    $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
    return $ms.ToArray()
}

$sizes = @(16, 32, 48, 256)
$images = @{}
foreach ($s in $sizes) {
    $bmp = New-LogoBitmap $s
    $images[$s] = ConvertTo-PngBytes $bmp
    $bmp.Dispose()
    Write-Output ("size $s -> " + $images[$s].Length + " bytes")
}

$ErrorActionPreference = 'Stop'
$outPath = Join-Path $PSScriptRoot "rankyoinker.ico"
$ms2 = New-Object System.IO.MemoryStream
$bw = New-Object System.IO.BinaryWriter($ms2)

# ICONDIR
$bw.Write([UInt16]0)      # reserved
$bw.Write([UInt16]1)      # type = icon
$bw.Write([UInt16]$sizes.Count)

$headerSize = 6 + (16 * $sizes.Count)
$offset = $headerSize
foreach ($s in $sizes) {
    $data = $images[$s]
    $wh = if ($s -ge 256) { 0 } else { $s }
    $bw.Write([byte]$wh)        # width
    $bw.Write([byte]$wh)        # height
    $bw.Write([byte]0)          # color palette
    $bw.Write([byte]0)          # reserved
    $bw.Write([UInt16]1)        # color planes
    $bw.Write([UInt16]32)       # bits per pixel
    $bw.Write([Int32]$data.Length)
    $bw.Write([Int32]$offset)
    $offset += $data.Length
}
foreach ($s in $sizes) {
    $bw.Write([byte[]]$images[$s])
}
$bw.Flush()
$allBytes = $ms2.ToArray()
Write-Output ("Gesamtgroesse im Speicher: " + $allBytes.Length)
[System.IO.File]::WriteAllBytes($outPath, $allBytes)
$bw.Close()
$ms2.Close()

Write-Output "Icon geschrieben: $outPath"
