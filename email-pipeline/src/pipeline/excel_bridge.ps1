# Fills an Excel tool with inputs, lets Excel calculate, reads results back, saves a copy.
# Called by pipeline/excel.py:  powershell -File excel_bridge.ps1 <job.json> <result.json> <pid.txt>
# The template is opened read-only and never modified. No macros run (AutomationSecurity = 3).
param([string]$JobPath, [string]$ResultPath, [string]$PidPath)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Web.Extensions
$json = New-Object System.Web.Script.Serialization.JavaScriptSerializer
$json.MaxJsonLength = [int]::MaxValue
$job = $json.DeserializeObject([IO.File]::ReadAllText($JobPath, [Text.Encoding]::UTF8))
$result = @{ ok = $false }
$xl = $null; $wb = $null
$t0 = Get-Date

function Retry([scriptblock]$action) {
  # Excel rejects COM calls while it is busy (opening, calculating): 0x800AC472, 0x80010001
  for ($i = 0; ; $i++) {
    try { return & $action } catch {
      $busy = $_.Exception.Message -match "0x800AC472|0x80010001|0x8001010A"
      if (-not $busy -or $i -ge 600) { throw }
      Start-Sleep -Milliseconds 500
    }
  }
}
function Val($v) {
  # the JSON reader yields Decimal for numbers, which COM cannot pass to Excel
  if ($v -is [decimal]) { return [double]$v }
  return $v
}
function Set-Value2($range, $value) {
  # PowerShell caches a COM property setter per call site with the type of the first value
  # (a text then a number fails with "cannot cast"): set through reflection instead.
  [void][System.__ComObject].InvokeMember("Value2", [Reflection.BindingFlags]::SetProperty, $null, $range, @(,$value))
}
function Get-Prop($obj, $name) {
  # the leading comma stops PowerShell from unrolling a 2D array into a flat list
  return ,[System.__ComObject].InvokeMember($name, [Reflection.BindingFlags]::GetProperty, $null, $obj, $null)
}
function Plain($v) {
  # only JSON-friendly values leave this script
  if ($v -eq $null -or $v -is [string] -or $v -is [bool] -or $v -is [double] -or $v -is [int]) { return $v }
  if ($v -is [decimal] -or $v -is [single] -or $v -is [long]) { return [double]$v }
  if ($v -is [datetime]) { return $v.ToOADate() }
  return [string]$v
}
function Sheet($name) {
  try { return $wb.Worksheets.Item($name) } catch { throw "sheet '$name' not found in the workbook" }
}
function Cell-Out($c) {
  $text = [string](Get-Prop $c "Text")
  $v = Plain (Get-Prop $c "Value2")
  if ($v -is [int] -and $text -like "#*") { $v = $null }  # Excel error values arrive as Int32 codes
  return @{ value = $v; text = $text }
}

try {
  $before = @(Get-Process EXCEL -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
  $xl = New-Object -ComObject Excel.Application
  $new = @(Get-Process EXCEL -ErrorAction SilentlyContinue | Where-Object { $before -notcontains $_.Id } | ForEach-Object { $_.Id })
  $excelPid = if ($new.Count -eq 1) { $new[0] } else { $null }
  if ($excelPid) { [IO.File]::WriteAllText($PidPath, [string]$excelPid) }
  $xl.Visible = $false; $xl.DisplayAlerts = $false; $xl.ScreenUpdating = $false
  $xl.EnableEvents = $false; $xl.AskToUpdateLinks = $false; $xl.AutomationSecurity = 3
  $result.excel_version = [string]$xl.Version
  $wb = Retry { $xl.Workbooks.Open($job["template"], 0, $true) }
  Retry { $xl.Calculation = -4135 }  # manual while writing

  foreach ($w in $job["writes"]) {
    $c = (Sheet $w["sheet"]).Range($w["cell"])
    $value = Val $w["value"]
    try { Retry { Set-Value2 $c $value } } catch { throw "cannot write $($w["sheet"])!$($w["cell"]): $($_.Exception.Message)" }
  }
  foreach ($t in $job["tables"]) {
    $rows = $t["rows"]
    if ($rows.Count -eq 0) { continue }
    $ncol = $rows[0].Count
    $arr = New-Object 'object[,]' $rows.Count, $ncol
    for ($i = 0; $i -lt $rows.Count; $i++) { for ($j = 0; $j -lt $ncol; $j++) { $arr[$i, $j] = Val $rows[$i][$j] } }
    $ws = Sheet $t["sheet"]
    $target = $ws.Range($t["cell"]).Resize($rows.Count, $ncol)
    # always an object[,], so the setter-type issue of Set-Value2 does not arise here
    try { Retry { $target.Value2 = $arr } } catch { throw "cannot write $($t["sheet"])!$($target.Address(0,0)): $($_.Exception.Message)" }
  }

  Retry { $xl.Calculation = -4105 }  # automatic
  if ($job["full_calculation"]) { Retry { $xl.CalculateFull() } } else { Retry { $xl.Calculate() } }
  while ((Retry { $xl.CalculationState }) -ne 0) { Start-Sleep -Milliseconds 500 }  # 0 = xlDone
  $result.calc_seconds = [int]((Get-Date) - $t0).TotalSeconds

  $reads = @{}
  foreach ($r in $job["reads"]) {
    $reads["$($r["sheet"])!$($r["cell"])"] = Cell-Out ((Sheet $r["sheet"]).Range($r["cell"]))
  }
  $result.reads = $reads
  $ranges = @{}
  foreach ($r in $job["read_ranges"]) {
    $rng = (Sheet $r["sheet"]).Range($r["range"])
    $vals = Get-Prop $rng "Value2"
    $out = New-Object System.Collections.ArrayList
    if ($vals -is [array]) {
      for ($i = 1; $i -le $rng.Rows.Count; $i++) {
        $row = New-Object System.Collections.ArrayList
        for ($j = 1; $j -le $rng.Columns.Count; $j++) {
          $v = $vals.GetValue($i, $j)  # $vals[$i, $j] would slice, not index
          if ($v -is [int] -and [string](Get-Prop $rng.Cells.Item($i, $j) "Text") -like "#*") { $v = "#ERROR" }
          [void]$row.Add((Plain $v))
        }
        [void]$out.Add($row)
      }
    } else { [void]$out.Add(@(,(Plain $vals))) }
    $ranges["$($r["sheet"])!$($r["range"])"] = $out
  }
  $result.ranges = $ranges

  if ($job["output"]) {
    if ($job["activate"]) { (Sheet $job["activate"]).Activate() }
    $wb.SaveAs($job["output"], 50)  # 50 = .xlsb
  }
  $result.ok = $true
} catch {
  $result.error = [string]$_.Exception.Message
} finally {
  if ($wb) { try { $wb.Close($false) } catch {} }
  if ($xl) { try { $xl.Quit() } catch {}; [void][Runtime.InteropServices.Marshal]::ReleaseComObject($xl) }
  [GC]::Collect(); [GC]::WaitForPendingFinalizers()
  if ($excelPid) {  # Excel lingers while COM references survive: make sure our instance is gone
    for ($i = 0; $i -lt 20 -and (Get-Process -Id $excelPid -ErrorAction SilentlyContinue); $i++) { Start-Sleep -Milliseconds 250 }
    Stop-Process -Id $excelPid -Force -ErrorAction SilentlyContinue
  }
  $result.seconds = [int]((Get-Date) - $t0).TotalSeconds
  [IO.File]::WriteAllText($ResultPath, ($result | ConvertTo-Json -Depth 10 -Compress), (New-Object Text.UTF8Encoding($false)))
}
