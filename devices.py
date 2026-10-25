"""What Friday knows about his two devices, how she changes their settings, and the evidence she gathers to
diagnose them.

Laptop: PowerShell probes. They run in Windows PowerShell 5.1 (not pwsh), because the WinRT radio and hotspot
APIs only project there. A compact status line is refreshed in the background every couple of minutes for her
context; full reports are made on demand (`laptop_report`). Settings go through a catalogue (`set_laptop`) so the
brain never has to invent a command for the common things, and any Settings page opens by name (`open_laptop_page`).

Phone: the phone app sends a compact status with every /jobs poll (Device.kt on the phone), fuller reports and
settings run as a phone job of kind "action" ({"a":"device_report"} / {"a":"setting"} / {"a":"open_settings"}),
and adb is used on top whenever the phone is plugged in with USB debugging on (`adb_report`)."""

import hashlib
import json
import logging
import os
import re
import socket
import subprocess
import threading
import time

log = logging.getLogger("friday")
NO_WINDOW = 0x08000000
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")


# ---------------------------------------------------------------- running PowerShell

def ps(cmd, timeout=45):
    """One command in Windows PowerShell 5.1; stdout and stderr, stripped."""
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           creationflags=NO_WINDOW, cwd=os.path.expanduser("~"))
        return (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return "(that check took too long and was stopped)"
    except OSError as e:
        return f"(couldn't run PowerShell: {e})"


def ps_file(script, timeout=45):
    """A script with backticks and WinRT types, run as a file (quoting on a command line mangles it)."""
    folder = os.path.join(DATA, "ps")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, hashlib.sha1(script.encode("utf-8")).hexdigest()[:12] + ".ps1")
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8-sig") as f:
            f.write(script)
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           creationflags=NO_WINDOW, cwd=os.path.expanduser("~"))
        return (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return "(that check took too long and was stopped)"
    except OSError as e:
        return f"(couldn't run PowerShell: {e})"


def ps_json(cmd, timeout=45):
    out = ps(cmd.strip() + " | ConvertTo-Json -Compress -Depth 4", timeout)
    try:
        return json.loads(out[out.index("{"):] if "{" in out else out)
    except (ValueError, TypeError):
        log.warning("devices: no JSON from PowerShell: %s", out[:300])
        return {}


def internet_ok(timeout=1.5):
    """A TCP connect to Cloudflare's resolver: true when the laptop is really online."""
    try:
        with socket.create_connection(("1.1.1.1", 53), timeout=timeout):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------- the WinRT radios (Wi-Fi, Bluetooth), hotspot

_RADIO_HEAD = r"""
[Windows.Devices.Radios.Radio,Windows.System.Devices,ContentType=WindowsRuntime] | Out-Null
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | ? { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
Function Await($t, $rt) { $asTask = $asTaskGeneric.MakeGenericMethod($rt); $nt = $asTask.Invoke($null, @($t)); $nt.Wait(-1) | Out-Null; $nt.Result }
$null = Await ([Windows.Devices.Radios.Radio]::RequestAccessAsync()) ([Windows.Devices.Radios.RadioAccessStatus])
$radios = Await ([Windows.Devices.Radios.Radio]::GetRadiosAsync()) ([System.Collections.Generic.IReadOnlyList[Windows.Devices.Radios.Radio]])
"""


def radios():
    """{'WiFi': 'On', 'Bluetooth': 'Off', ...}"""
    out = ps_file(_RADIO_HEAD + "$radios | % { \"$($_.Kind)=$($_.State)\" }", timeout=30)
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line and " " not in line.split("=")[0])


def set_radio(kind, on):
    """kind 'WiFi' | 'Bluetooth' | 'all' (airplane mode = all off)."""
    pick = "$radios" if kind == "all" else f"$radios | ? {{ $_.Kind -eq '{kind}' }}"
    out = ps_file(_RADIO_HEAD + f"foreach ($r in ({pick})) {{ $s = Await ($r.SetStateAsync('{'On' if on else 'Off'}')) "
                  "([Windows.Devices.Radios.RadioAccessStatus]); \"$($r.Kind) -> $s\" }", timeout=30)
    return out or "no such radio"


_HOTSPOT = r"""
[Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime] | Out-Null
[Windows.Networking.Connectivity.NetworkInformation,Windows.Networking.Connectivity,ContentType=WindowsRuntime] | Out-Null
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | ? { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
Function Await($t, $rt) { $asTask = $asTaskGeneric.MakeGenericMethod($rt); $nt = $asTask.Invoke($null, @($t)); $nt.Wait(-1) | Out-Null; $nt.Result }
$profile = [Windows.Networking.Connectivity.NetworkInformation]::GetInternetConnectionProfile()
if ($profile -eq $null) { "no internet connection to share"; exit }
$m = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($profile)
"""


def hotspot(on=None):
    """None = report; True/False = switch the mobile hotspot."""
    if on is None:
        body = '"state=" + $m.TetheringOperationalState + " clients=" + $m.ClientCount + " ssid=" + $m.GetCurrentAccessPointConfiguration().Ssid'
    elif on:
        body = '$r = Await ($m.StartTetheringAsync()) ([Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult]); "hotspot start: " + $r.Status'
    else:
        body = '$r = Await ($m.StopTetheringAsync()) ([Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult]); "hotspot stop: " + $r.Status'
    return ps_file(_HOTSPOT + body, timeout=40)


# ---------------------------------------------------------------- sound (pycaw)

def volume_get():
    """(percent, muted) of the default speakers, or (None, None)."""
    try:
        from pycaw.pycaw import AudioUtilities
        vol = AudioUtilities.GetSpeakers().EndpointVolume
        return round(vol.GetMasterVolumeLevelScalar() * 100), bool(vol.GetMute())
    except Exception as e:
        log.warning("volume read failed: %s", e)
        return None, None


def volume_set(percent=None, mute=None):
    try:
        from pycaw.pycaw import AudioUtilities
        vol = AudioUtilities.GetSpeakers().EndpointVolume
        if percent is not None:
            vol.SetMasterVolumeLevelScalar(max(0, min(100, int(percent))) / 100.0, None)
        if mute is not None:
            vol.SetMute(1 if mute else 0, None)
        return True
    except Exception as e:
        log.warning("volume set failed: %s", e)
        return False


def audio_devices():
    """Default speakers and mic by name, plus every active endpoint."""
    try:
        from pycaw.pycaw import AudioUtilities
        out = AudioUtilities.GetSpeakers()
        mic = AudioUtilities.CreateDevice(AudioUtilities.GetMicrophone()) if AudioUtilities.GetMicrophone() else None
        active = sorted({d.FriendlyName for d in AudioUtilities.GetAllDevices() if getattr(d.state, "name", "") == "Active"})
        return {"speakers": out.FriendlyName, "microphone": mic.FriendlyName if mic else "none", "all": active}
    except Exception as e:
        return {"error": str(e)[:100]}


# ---------------------------------------------------------------- the laptop's live status

_STATUS_PS = r"""
$b = Get-CimInstance Win32_Battery | Select -First 1
$o = Get-CimInstance Win32_OperatingSystem
$c = Get-CimInstance Win32_Processor | Select -First 1
$w = (netsh wlan show interfaces) | Out-String
$ssid = if ($w -match '(?m)^\s+SSID\s*:\s*(.+?)\s*$') { $Matches[1] } else { '' }
$sig = if ($w -match '(?m)^\s+Signal\s*:\s*(\d+)%') { [int]$Matches[1] } else { 0 }
$eth = (Get-NetAdapter -Physical -EA SilentlyContinue | ? { $_.Status -eq 'Up' -and $_.PhysicalMediaType -ne 'Native 802.11' -and $_.Name -notmatch 'Bluetooth|Wi-Fi' } | Select -First 1).Name
$vpn = (Get-NetAdapter -EA SilentlyContinue | ? { $_.Status -eq 'Up' -and ($_.InterfaceDescription -match 'VPN|Proton|WireGuard|TAP|Tunnel' -or $_.Name -match 'VPN|Proton') } | Select -First 1).Name
$disks = @(Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" | % { @{ d=$_.DeviceID; free=[math]::Round($_.FreeSpace/1GB); size=[math]::Round($_.Size/1GB) } })
$br = (Get-CimInstance -Namespace root/WMI -Class WmiMonitorBrightness -EA SilentlyContinue | Select -First 1).CurrentBrightness
$errs = @(Get-WinEvent -FilterHashtable @{LogName='Application','System'; Level=1,2; StartTime=(Get-Date).AddHours(-24)} -MaxEvents 200 -EA SilentlyContinue)
$dark = 1 - [int](Get-ItemPropertyValue 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize' AppsUseLightTheme -EA SilentlyContinue)
$pend = (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired') -or (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending')
$upd = @(Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='Microsoft-Windows-WindowsUpdateClient'; Id=20; StartTime=(Get-Date).AddHours(-24)} -MaxEvents 20 -EA SilentlyContinue).Count
$crash = @(Get-WinEvent -FilterHashtable @{LogName='Application'; ProviderName='Application Error','Application Hang'; StartTime=(Get-Date).AddHours(-24)} -MaxEvents 20 -EA SilentlyContinue).Count
@{ battery=$b.EstimatedChargeRemaining; status=$b.BatteryStatus; ram_used=[math]::Round(($o.TotalVisibleMemorySize-$o.FreePhysicalMemory)/1MB,1); ram_total=[math]::Round($o.TotalVisibleMemorySize/1MB,1);
   cpu=$c.LoadPercentage; ssid=$ssid; signal=$sig; eth=$eth; vpn=$vpn; disks=$disks; brightness=$br; boot=$o.LastBootUpTime.ToString('s');
   errors=$errs.Count; last_error=$(if ($errs.Count) { ($errs[0].TimeCreated.ToString('HH:mm') + ' ' + $errs[0].ProviderName + ': ' + (($errs[0].Message -replace '\s+',' ')).Substring(0, [math]::Min(140, ($errs[0].Message -replace '\s+',' ').Length))) } else { '' });
   dark=$dark; pending_reboot=$pend; update_failures=$upd; app_crashes=$crash }
"""

_laptop = {"t": 0.0, "data": {}, "lock": threading.Lock(), "busy": False}
_phone = {"t": 0.0, "line": "", "seen": 0.0}


def laptop_status(max_age=150, wait=False):
    """The laptop's vital signs, refreshed in the background when older than max_age (wait=True refreshes now)."""
    def refresh():
        try:
            d = ps_json(_STATUS_PS, timeout=60) or {}
            d["internet"] = internet_ok()
            d["volume"], d["muted"] = volume_get()
            d["t"] = time.time()
            with _laptop["lock"]:
                _laptop["data"] = d
                _laptop["t"] = d["t"]
        except Exception:
            log.exception("laptop status failed")
        finally:
            _laptop["busy"] = False

    if time.time() - _laptop["t"] > max_age and not _laptop["busy"]:
        _laptop["busy"] = True
        if wait:
            refresh()
        else:
            threading.Thread(target=refresh, daemon=True, name="laptop-status").start()
    return dict(_laptop["data"])


def _battery_words(d):
    pct, st = d.get("battery"), d.get("status")
    if pct is None:
        return "no battery reading"
    state = {1: "on battery", 2: "plugged in", 3: "full, plugged in", 4: "low", 5: "critical"}.get(st, "charging" if st and st >= 6 else "")
    return f"battery {pct}%" + (f" {state}" if state else "")


def laptop_line():
    """One line for her context: 'laptop: battery 100% plugged in, Wi-Fi Home (strong, internet ok), ...'"""
    d = laptop_status()
    if not d:
        return "laptop: status not read yet"
    bits = [_battery_words(d)]
    if d.get("ssid"):
        sig = d.get("signal") or 0
        bits.append(f"Wi-Fi {d['ssid']} ({'strong' if sig >= 70 else 'weak' if sig < 40 else 'ok'}{', internet ok' if d.get('internet') else ', NO INTERNET'})")
    elif d.get("eth"):
        bits.append(f"wired network{', internet ok' if d.get('internet') else ', NO INTERNET'}")
    else:
        bits.append("no network")
    if d.get("vpn"):
        bits.append(f"VPN on ({d['vpn']})")
    if d.get("cpu") is not None:
        bits.append(f"CPU {d['cpu']}%")
    if d.get("ram_total"):
        bits.append(f"RAM {d['ram_used']} of {d['ram_total']} GB")
    for disk in d.get("disks") or []:
        if disk.get("d") in ("C:", "D:"):
            bits.append(f"{disk['d']} {disk['free']} GB free" + (" (LOW)" if disk["free"] < 10 else ""))
    if d.get("brightness") is not None:
        bits.append(f"brightness {d['brightness']}%")
    if d.get("volume") is not None:
        bits.append(f"volume {d['volume']}%" + (" muted" if d.get("muted") else ""))
    if d.get("boot"):
        try:
            import datetime
            up = (datetime.datetime.now() - datetime.datetime.fromisoformat(d["boot"])).total_seconds() / 3600
            bits.append(f"up {up / 24:.0f} days" if up >= 48 else f"up {up:.0f} h")
        except ValueError:
            pass
    if d.get("pending_reboot"):
        bits.append("a restart is pending for updates")
    if d.get("errors"):
        bits.append(f"{d['errors']} Windows errors in 24 h")
    return "laptop: " + ", ".join(bits)


def phone_seen(status, seen=None):
    """The phone's compact status line, as sent with its /jobs poll."""
    _phone["line"] = status or _phone["line"]
    _phone["seen"] = seen or time.time()
    _phone["t"] = time.time()


def phone_line():
    if not _phone["seen"]:
        return "phone: not linked"
    ago = time.time() - _phone["seen"]
    if ago > 180:
        return f"phone: last seen {ago / 60:.0f} min ago" + (f" (then: {_phone['line']})" if _phone["line"] else "")
    return "phone: " + (_phone["line"] or "linked, no status sent")


def devices_line():
    return f"DEVICES (live): {laptop_line()} | {phone_line()}"


# ---------------------------------------------------------------- laptop reports

_REPORTS = {
    "battery": r"""
$b = Get-CimInstance Win32_Battery | Select -First 1
$full = (Get-CimInstance -Namespace root/WMI -Class BatteryFullChargedCapacity -EA SilentlyContinue | Select -First 1).FullChargedCapacity
$design = (Get-CimInstance -Namespace root/WMI -Class BatteryStaticData -EA SilentlyContinue | Select -First 1).DesignedCapacity
$cycles = (Get-CimInstance -Namespace root/WMI -Class BatteryCycleCount -EA SilentlyContinue | Select -First 1).CycleCount
$st = @{1='discharging';2='plugged in (AC)';3='fully charged';4='low';5='critical';6='charging';7='charging and high';8='charging and low';9='charging and critical'}[[int]$b.BatteryStatus]
"Charge: $($b.EstimatedChargeRemaining)%  State: $st"
if ($b.EstimatedRunTime -and $b.EstimatedRunTime -lt 10000) { "Estimated time left: $([math]::Round($b.EstimatedRunTime/60,1)) h" }
if ($full -and $design) { "Health: $([math]::Round(100*$full/$design))% of design capacity ($full / $design mWh)" }
if ($cycles) { "Cycle count: $cycles" }
"Power plan: " + ((powercfg /getactivescheme) -replace '.*\((.*)\).*','$1')
$ov = (powercfg /overlaygetactive 2>$null | Out-String).Trim(); if ($ov) { "Power mode: $ov" }
"Screen off after (plugged / battery): " + (((powercfg /q SCHEME_CURRENT SUB_VIDEO VIDEOIDLE | Select-String 'Current (AC|DC) Power Setting Index') | % { [convert]::ToInt32(($_ -split ':')[1].Trim(), 16)/60 }) -join ' / ') + ' min'
"Sleep after (plugged / battery): " + (((powercfg /q SCHEME_CURRENT SUB_SLEEP STANDBYIDLE | Select-String 'Current (AC|DC) Power Setting Index') | % { [convert]::ToInt32(($_ -split ':')[1].Trim(), 16)/60 }) -join ' / ') + ' min'
"Top CPU users right now:"; try { Get-Counter '\Process(*)\% Processor Time' -EA Stop | % { $_.CounterSamples } | ? { $_.InstanceName -notin 'idle','_total' } | Sort CookedValue -Desc | Select -First 5 | % { "  " + $_.InstanceName + " " + [math]::Round($_.CookedValue) + "% CPU" } } catch {}
""",
    "storage": r"""
Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" | % { "$($_.DeviceID) $($_.VolumeName): $([math]::Round($_.FreeSpace/1GB)) GB free of $([math]::Round($_.Size/1GB)) GB" }
Get-PhysicalDisk -EA SilentlyContinue | % { "Disk: $($_.FriendlyName) $($_.MediaType) health $($_.HealthStatus)" }
$rb = (New-Object -ComObject Shell.Application).NameSpace(10).Items() | Measure-Object -Property Size -Sum
"Recycle Bin: $([math]::Round($rb.Sum/1MB)) MB in $($rb.Count) items"
foreach ($f in @("$env:USERPROFILE\Downloads", "$env:TEMP", "$env:USERPROFILE\Desktop", "$env:USERPROFILE\Documents", "$env:USERPROFILE\Videos", "$env:LOCALAPPDATA\Temp")) {
  if (Test-Path $f) { $s = (Get-ChildItem $f -Recurse -File -Force -EA SilentlyContinue | Measure-Object Length -Sum).Sum; "$f : $([math]::Round($s/1GB,1)) GB" } }
"Biggest files in Downloads:"; Get-ChildItem "$env:USERPROFILE\Downloads" -Recurse -File -EA SilentlyContinue | Sort Length -Desc | Select -First 6 | % { "  $($_.Name) $([math]::Round($_.Length/1MB)) MB" }
""",
    "performance": r"""
$o = Get-CimInstance Win32_OperatingSystem
"RAM: $([math]::Round(($o.TotalVisibleMemorySize-$o.FreePhysicalMemory)/1MB,1)) of $([math]::Round($o.TotalVisibleMemorySize/1MB,1)) GB used; CPU $((Get-CimInstance Win32_Processor).LoadPercentage)%; up since $($o.LastBootUpTime)"
"Top CPU right now:"; try { Get-Counter '\Process(*)\% Processor Time' -EA Stop | % { $_.CounterSamples } | ? { $_.InstanceName -notin 'idle','_total' } | Sort CookedValue -Desc | Select -First 8 | % { "  " + $_.InstanceName + " " + [math]::Round($_.CookedValue/$env:NUMBER_OF_PROCESSORS) + "%" } } catch { "  (counter unavailable)" }
"Top memory:"; Get-Process | Sort WorkingSet64 -Desc | Select -First 8 | % { "  $($_.ProcessName) $([math]::Round($_.WorkingSet64/1MB)) MB" }
"Apps with windows open: " + ((Get-Process | ? { $_.MainWindowTitle } | % { $_.ProcessName } | Sort -Unique) -join ', ')
"Processes: $((Get-Process).Count); not responding: " + ((Get-Process | ? { -not $_.Responding -and $_.MainWindowTitle } | % { $_.ProcessName }) -join ', ')
$pf = Get-CimInstance Win32_PageFileUsage | Select -First 1; if ($pf) { "Page file: $($pf.CurrentUsage) of $($pf.AllocatedBaseSize) MB" }
""",
    "network": r"""
(netsh wlan show interfaces) -match '^\s+(Name|State|SSID|BSSID|Radio type|Band|Channel|Receive rate|Transmit rate|Signal|Profile)\s*:' | % { $_.Trim() }
Get-NetAdapter -EA SilentlyContinue | ? Status -eq Up | % { "Adapter up: $($_.Name) ($($_.InterfaceDescription)) $($_.LinkSpeed)" }
Get-NetIPConfiguration -EA SilentlyContinue | ? { $_.IPv4DefaultGateway } | % { "IP $($_.IPv4Address.IPAddress) gateway $($_.IPv4DefaultGateway.NextHop) DNS $($_.DNSServer.ServerAddresses -join ', ') on $($_.InterfaceAlias)" }
"Ping 1.1.1.1: " + ((Test-Connection 1.1.1.1 -Count 2 -EA SilentlyContinue | Measure-Object ResponseTime -Average).Average) + " ms"
$dns = try { (Resolve-DnsName google.com -Type A -EA Stop | Select -First 1).IPAddress } catch { 'FAILED ' + $_.Exception.Message }; "DNS google.com: $dns"
$web = try { $t=[Diagnostics.Stopwatch]::StartNew(); $null = Invoke-WebRequest https://www.google.com/generate_204 -UseBasicParsing -TimeoutSec 8; "ok in $($t.ElapsedMilliseconds) ms" } catch { 'FAILED ' + $_.Exception.Message }; "HTTPS: $web"
$p = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -EA SilentlyContinue; "Proxy: " + $(if ($p.ProxyEnable) { $p.ProxyServer } else { 'none' })
Get-VpnConnection -EA SilentlyContinue | % { "VPN profile: $($_.Name) $($_.ConnectionStatus)" }
Get-NetAdapter -EA SilentlyContinue | ? { $_.InterfaceDescription -match 'VPN|Proton|WireGuard|TAP|Tunnel' } | % { "VPN adapter: $($_.Name) $($_.Status)" }
"Known Wi-Fi: " + (((netsh wlan show profiles) -match 'All User Profile' | % { ($_ -split ':')[1].Trim() }) -join ', ')
""",
    "errors": r"""
"Errors and criticals, last 24 h (Application + System):"
Get-WinEvent -FilterHashtable @{LogName='Application','System'; Level=1,2; StartTime=(Get-Date).AddHours(-24)} -MaxEvents 25 -EA SilentlyContinue | % { "  $($_.TimeCreated.ToString('ddd HH:mm')) [$($_.Id)] $($_.ProviderName): " + (($_.Message -replace '\s+',' ')).Substring(0, [math]::Min(220, ($_.Message -replace '\s+',' ').Length)) }
"App crashes and hangs, last 7 days:"
Get-WinEvent -FilterHashtable @{LogName='Application'; ProviderName='Application Error','Application Hang','Windows Error Reporting'; StartTime=(Get-Date).AddDays(-7)} -MaxEvents 12 -EA SilentlyContinue | % { "  $($_.TimeCreated.ToString('ddd HH:mm')) $($_.ProviderName): " + (($_.Message -replace '\s+',' ')).Substring(0, [math]::Min(160, ($_.Message -replace '\s+',' ').Length)) }
"Unexpected shutdowns / blue screens, last 30 days:"
Get-WinEvent -FilterHashtable @{LogName='System'; Id=41,1001,6008; StartTime=(Get-Date).AddDays(-30)} -MaxEvents 6 -EA SilentlyContinue | ? { $_.ProviderName -match 'Kernel-Power|BugCheck|EventLog' } | % { "  $($_.TimeCreated) [$($_.Id)] $($_.ProviderName)" }
"Disk and driver trouble, last 7 days:"
Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='disk','Ntfs','Microsoft-Windows-Ntfs','storahci','stornvme','Microsoft-Windows-Kernel-PnP','Service Control Manager'; Level=1,2,3; StartTime=(Get-Date).AddDays(-7)} -MaxEvents 10 -EA SilentlyContinue | % { "  $($_.TimeCreated.ToString('ddd HH:mm')) $($_.ProviderName): " + (($_.Message -replace '\s+',' ')).Substring(0, [math]::Min(160, ($_.Message -replace '\s+',' ').Length)) }
"Devices with problems:"; Get-PnpDevice -PresentOnly -EA SilentlyContinue | ? { $_.Status -ne 'OK' } | % { "  $($_.Class): $($_.FriendlyName) ($($_.Status), problem $($_.ProblemDescription))" }
"Smart App Control blocks, last 7 days:"; Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-CodeIntegrity/Operational'; Id=3077,3076; StartTime=(Get-Date).AddDays(-7)} -MaxEvents 6 -EA SilentlyContinue | % { "  $($_.TimeCreated.ToString('ddd HH:mm')) " + (($_.Message -replace '\s+',' ')).Substring(0, [math]::Min(160, ($_.Message -replace '\s+',' ').Length)) }
""",
    "updates": r"""
$v = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'; "Windows $($v.ProductName) $($v.DisplayVersion) build $($v.CurrentBuildNumber).$($v.UBR)"
"Restart pending: " + ((Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired') -or (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending'))
$r = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\Results\Install' -EA SilentlyContinue; if ($r) { "Last update install: $($r.LastSuccessTime)" }
$d = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\Results\Detect' -EA SilentlyContinue; if ($d) { "Last check: $($d.LastSuccessTime)" }
"Recent updates:"; Get-HotFix -EA SilentlyContinue | Sort InstalledOn -Desc | Select -First 6 | % { "  $($_.HotFixID) $($_.Description) $($_.InstalledOn.ToString('yyyy-MM-dd'))" }
"Update failures, last 14 days:"; Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='Microsoft-Windows-WindowsUpdateClient'; Id=20; StartTime=(Get-Date).AddDays(-14)} -MaxEvents 5 -EA SilentlyContinue | % { "  $($_.TimeCreated.ToString('ddd HH:mm')) " + (($_.Message -replace '\s+',' ')).Substring(0, [math]::Min(160, ($_.Message -replace '\s+',' ').Length)) }
"Apps with updates (winget):"; try { (winget upgrade --accept-source-agreements 2>$null | Out-String).Split("`n") | Select -Skip 2 | Select -First 12 | % { "  " + $_.Trim() } } catch { "  (winget unavailable)" }
""",
    "devices": r"""
$bt = (Get-PnpDevice -Class Bluetooth -PresentOnly -EA SilentlyContinue | ? { $_.FriendlyName -notmatch 'Enumerator|Transport|Radio|Service|Protocol|Microsoft|Generic|LE Generic|Device Information|RFCOMM|AVCTP|L2CAP|Attribute' })
"Bluetooth devices:"; foreach ($d in $bt) { $c = (Get-PnpDeviceProperty -InstanceId $d.InstanceId -KeyName '{83DA6326-97A6-4088-9453-A1923F573B29} 15' -EA SilentlyContinue).Data; "  $($d.FriendlyName): " + $(if ($c -eq $true) { 'connected' } else { 'paired, not connected' }) }
"USB:"; Get-PnpDevice -PresentOnly -EA SilentlyContinue | ? { $_.InstanceId -like 'USB\*' -and $_.Class -notin 'USB','HIDClass' } | % { "  $($_.Class): $($_.FriendlyName) $($_.Status)" }
"Monitors:"; Get-CimInstance Win32_VideoController | % { "  $($_.Name) $($_.CurrentHorizontalResolution)x$($_.CurrentVerticalResolution) @$($_.CurrentRefreshRate) Hz, driver $($_.DriverVersion)" }
Get-CimInstance -Namespace root/wmi -Class WmiMonitorID -EA SilentlyContinue | % { "  screen: " + (-join [char[]]($_.UserFriendlyName | ? { $_ -ne 0 })) }
"Printers:"; Get-Printer -EA SilentlyContinue | % { "  $($_.Name) ($($_.PrinterStatus))" + $(if ((Get-CimInstance Win32_Printer -Filter "Name='$($_.Name.Replace('\','\\'))'" -EA SilentlyContinue).Default) { ' DEFAULT' }) }
"Cameras: " + ((Get-PnpDevice -Class Camera,Image -PresentOnly -EA SilentlyContinue | % { "$($_.FriendlyName) $($_.Status)" }) -join '; ')
"Problem devices: " + ((Get-PnpDevice -PresentOnly -EA SilentlyContinue | ? { $_.Status -ne 'OK' } | % { "$($_.FriendlyName) ($($_.Status))" }) -join '; ')
""",
    "apps": r"""
$apps = @(Get-ItemProperty 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*','HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*','HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*' -EA SilentlyContinue | ? { $_.DisplayName -and -not $_.SystemComponent })
"Installed programs: $($apps.Count)"; "Recently installed:"; $apps | ? InstallDate | Sort InstallDate -Desc | Select -First 10 | % { "  $($_.DisplayName) $($_.DisplayVersion) ($($_.InstallDate))" }
"Startup apps:"; $ap = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run','HKLM:\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run' -EA SilentlyContinue
Get-CimInstance Win32_StartupCommand | % { $n = $_.Name; $st = 'on'; foreach ($k in $ap) { $v = $k.$n; if ($v -and $v[0] -ne 2) { $st = 'off' } }; "  $n ($st): $($_.Command.Substring(0, [math]::Min(70, $_.Command.Length)))" }
"Startup folder:"; Get-ChildItem "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup" -EA SilentlyContinue | % { "  $($_.Name)" }
"Running with windows:"; Get-Process | ? { $_.MainWindowTitle } | % { "  $($_.ProcessName): $($_.MainWindowTitle.Substring(0, [math]::Min(60, $_.MainWindowTitle.Length)))" }
""",
    "security": r"""
$m = Get-MpComputerStatus -EA SilentlyContinue
if ($m) { "Defender: antivirus $(if ($m.AntivirusEnabled) {'on'} else {'OFF'}), real-time $(if ($m.RealTimeProtectionEnabled) {'on'} else {'OFF'}), signatures $($m.AntivirusSignatureAge) days old, last quick scan $($m.QuickScanEndTime), last full scan $($m.FullScanEndTime)" }
"Threats found recently:"; Get-MpThreatDetection -EA SilentlyContinue | Sort InitialDetectionTime -Desc | Select -First 5 | % { "  $($_.InitialDetectionTime) threat $($_.ThreatID) action $($_.ActionSuccess): " + (($_.Resources -join ', ')).Substring(0, [math]::Min(110, ($_.Resources -join ', ').Length)) }
"Firewall: " + ((Get-NetFirewallProfile -EA SilentlyContinue | % { "$($_.Name) $(if ($_.Enabled) {'on'} else {'OFF'})" }) -join ', ')
$sac = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy' -EA SilentlyContinue).VerifiedAndReputablePolicyState; "Smart App Control: " + @{0='off';1='ON (enforced)';2='evaluation'}[[int]$sac]
$u = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System'; "UAC: " + $(if ($u.EnableLUA) { "on, level $($u.ConsentPromptBehaviorAdmin)" } else { 'OFF' })
try { Get-BitLockerVolume -EA Stop | % { "BitLocker $($_.MountPoint): $($_.ProtectionStatus) $($_.VolumeStatus)" } } catch { "BitLocker: needs admin to read" }
"Admin account: " + ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
"Privacy: location " + (Get-ItemPropertyValue 'HKCU:\Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\location' Value -EA SilentlyContinue) + ", camera " + (Get-ItemPropertyValue 'HKCU:\Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\webcam' Value -EA SilentlyContinue) + ", microphone " + (Get-ItemPropertyValue 'HKCU:\Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone' Value -EA SilentlyContinue)
""",
    "display": r"""
Get-CimInstance Win32_VideoController | % { "Display: $($_.Name) $($_.CurrentHorizontalResolution)x$($_.CurrentVerticalResolution) @$($_.CurrentRefreshRate) Hz" }
"Brightness: " + (Get-CimInstance -Namespace root/WMI -Class WmiMonitorBrightness -EA SilentlyContinue | Select -First 1).CurrentBrightness + "%"
Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.Screen]::AllScreens | % { "Screen $($_.DeviceName): $($_.Bounds.Width)x$($_.Bounds.Height)" + $(if ($_.Primary) {' primary'}) }
"Scaling: " + [math]::Round(100 * (Get-ItemPropertyValue 'HKCU:\Control Panel\Desktop\WindowMetrics' AppliedDPI -EA SilentlyContinue) / 96) + "%"
$p = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize' -EA SilentlyContinue; "Theme: apps $(if ($p.AppsUseLightTheme) {'light'} else {'dark'}), system $(if ($p.SystemUsesLightTheme) {'light'} else {'dark'})"
$nl = (Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\CloudStore\Store\DefaultAccount\Current\default$windows.data.bluelightreduction.bluelightreductionstate\windows.data.bluelightreduction.bluelightreductionstate' -EA SilentlyContinue).Data; if ($nl) { "Night light: " + $(if ($nl[18] -eq 0x15) {'on'} else {'off'}) }
"Screen off after (plugged / battery): " + (((powercfg /q SCHEME_CURRENT SUB_VIDEO VIDEOIDLE | Select-String 'Current (AC|DC) Power Setting Index') | % { [convert]::ToInt32(($_ -split ':')[1].Trim(), 16)/60 }) -join ' / ') + ' min'
"Wallpaper: " + (Get-ItemPropertyValue 'HKCU:\Control Panel\Desktop' WallPaper -EA SilentlyContinue)
""",
    "wifi": r"""
"Networks around:"; (netsh wlan show networks mode=bssid) -match '^\s*(SSID \d+|Signal|Band|Channel)\s*:' | % { $_.Trim() } | Select -First 40
""",
}
_REPORTS["status"] = _REPORTS["overview"] = ""
_REPORTS["memory"] = _REPORTS["cpu"] = _REPORTS["processes"] = _REPORTS["performance"]
_REPORTS["drivers"] = _REPORTS["bluetooth"] = _REPORTS["usb"] = _REPORTS["printers"] = _REPORTS["devices"]
_REPORTS["crashes"] = _REPORTS["logs"] = _REPORTS["errors"]
_REPORTS["internet"] = _REPORTS["network"]
_REPORTS["startup"] = _REPORTS["programs"] = _REPORTS["apps"]
_REPORTS["defender"] = _REPORTS["antivirus"] = _REPORTS["privacy"] = _REPORTS["security"]
_REPORTS["screen"] = _REPORTS["display"]
_REPORTS["disk"] = _REPORTS["space"] = _REPORTS["storage"]
_REPORTS["power"] = _REPORTS["battery"]
LAPTOP_TOPICS = ("status", "battery", "storage", "performance", "network", "wifi", "errors", "updates", "devices", "apps",
                 "security", "display", "sound")


def laptop_report(topic="status"):
    """A plain-text report for the brain (never read aloud as is)."""
    t = (topic or "status").lower().strip()
    if t in ("sound", "audio", "speakers", "microphone", "mic"):
        a = audio_devices()
        v, m = volume_get()
        return (f"Volume {v}%{' (muted)' if m else ''}. Speakers: {a.get('speakers')}. Microphone: {a.get('microphone')}. "
                f"All audio devices: {', '.join(a.get('all', []))}")
    script = _REPORTS.get(t)
    if script is None:
        return f"I don't have a '{topic}' report. Topics: {', '.join(LAPTOP_TOPICS)}."
    head = laptop_line()
    if not script:
        d = laptop_status(wait=True)
        extra = []
        if d.get("last_error"):
            extra.append("Latest Windows error: " + d["last_error"])
        extra.append("Theme: " + ("dark" if d.get("dark") else "light"))
        return head + "\n" + "\n".join(extra)
    return head + "\n\n" + ps(script, timeout=90)


# ---------------------------------------------------------------- laptop settings

SETTINGS_PAGES = {
    "home": "", "system": "system", "display": "display", "night light": "nightlight", "scale": "display", "graphics": "display-advancedgraphics",
    "sound": "sound", "volume mixer": "apps-volume", "notifications": "notifications", "focus": "quiethours", "do not disturb": "quiethours",
    "power": "powersleep", "battery": "batterysaver", "storage": "storagesense", "storage sense": "storagepolicies", "nearby sharing": "crossdevice",
    "multitasking": "multitasking", "clipboard": "clipboard", "about": "about", "bluetooth": "bluetooth", "devices": "connecteddevices",
    "printers": "printers", "mouse": "mousetouchpad", "touchpad": "devices-touchpad", "pen": "pen", "autoplay": "autoplay", "usb": "usb",
    "camera": "camera", "network": "network", "wifi": "network-wifi", "ethernet": "network-ethernet", "vpn": "network-vpn", "hotspot": "network-mobilehotspot",
    "airplane": "network-airplanemode", "proxy": "network-proxy", "data usage": "datausage", "personalization": "personalization",
    "background": "personalization-background", "colors": "personalization-colors", "themes": "themes", "lock screen": "lockscreen",
    "start": "personalization-start", "taskbar": "taskbar", "fonts": "fonts", "apps": "appsfeatures", "installed apps": "appsfeatures",
    "default apps": "defaultapps", "startup": "startupapps", "optional features": "optionalfeatures", "accounts": "yourinfo", "email": "emailandaccounts",
    "sign-in": "signinoptions", "time": "dateandtime", "date": "dateandtime", "language": "regionlanguage", "region": "regionformatting",
    "typing": "typing", "keyboard": "keyboard", "gaming": "gaming-gamebar", "accessibility": "easeofaccess", "text size": "easeofaccess-display",
    "privacy": "privacy", "location": "privacy-location", "microphone": "privacy-microphone", "camera privacy": "privacy-webcam",
    "windows update": "windowsupdate", "update": "windowsupdate", "update history": "windowsupdate-history", "recovery": "recovery",
    "troubleshoot": "troubleshoot", "backup": "backup", "activation": "activation", "developers": "developers", "windows security": "windowsdefender",
    "security": "windowsdefender", "family": "family-group", "find my device": "findmydevice", "remote desktop": "remotedesktop", "projecting": "project",
    "phone link": "mobile-devices", "phone": "mobile-devices", "energy": "powersleep",
}


def open_laptop_page(name):
    """Opens a Windows Settings page by its plain name ("night light", "default apps", "windows update")."""
    n = (name or "").lower().strip()
    key = n if n in SETTINGS_PAGES else next((k for k in SETTINGS_PAGES if k in n or n in k), None)
    uri = "ms-settings:" + (SETTINGS_PAGES.get(key) or "")
    os.startfile(uri)
    return f"Opened Settings{' > ' + key if key else ''}."


def _minutes(v):
    v = str(v).lower().strip()
    m = re.match(r"([\d.]+)\s*(h|hour|hr|m|min|minute|s|sec|second)?", v)
    if not m:
        return 0
    n = float(m.group(1))
    u = (m.group(2) or "m")[0]
    return int(n * 60 if u == "h" else n / 60 if u == "s" else n)


def _on(v):
    return str(v).lower().strip() in ("1", "true", "on", "yes", "enable", "enabled")


def _wallpaper(path):
    path = os.path.expandvars(os.path.expanduser(str(path)))
    if not os.path.isfile(path):
        return False, f"I can't find {os.path.basename(path)}."
    out = ps(f"Add-Type -TypeDefinition 'using System.Runtime.InteropServices; public class W {{ [DllImport(\"user32.dll\", SetLastError=true)] public static extern bool SystemParametersInfo(int a, int b, string c, int d); }}'; [W]::SystemParametersInfo(20, 0, '{path}', 3)")
    return "True" in out, "Wallpaper changed." if "True" in out else "Windows wouldn't take that picture."


def _startup_app(name, on):
    out = ps(f"$n = '{name}'; $hit = Get-CimInstance Win32_StartupCommand | ? {{ $_.Name -like \"*$n*\" }} | Select -First 1; if (-not $hit) {{ 'none' }} else {{ "
             f"$k = if ($hit.Location -match 'HKLM') {{ 'HKLM:' }} else {{ 'HKCU:' }}; $k += '\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\StartupApproved\\Run'; "
             f"$bytes = [byte[]]({'2' if on else '3'},0,0,0,0,0,0,0,0,0,0,0); New-Item $k -Force | Out-Null; Set-ItemProperty $k -Name $hit.Name -Value $bytes -Type Binary; $hit.Name }}")
    if out.strip() == "none":
        return False, f"There's no startup app like {name}."
    return True, f"{out.strip()} will {'start' if on else 'no longer start'} with Windows."


def set_laptop(what, value=None):
    """Changes one laptop setting from the catalogue. Returns (ok, short result for the brain)."""
    w = (what or "").lower().strip().replace("_", " ")
    v = value
    try:
        if w in ("brightness", "screen brightness"):
            n = max(0, min(100, int(float(str(v).rstrip("%")))))
            out = ps(f"(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods | Select -First 1).WmiSetBrightness(1, {n})")
            return "Exception" not in out, f"Brightness set to {n}%." if "Exception" not in out else "This screen doesn't let me set brightness."
        if w in ("volume", "sound volume", "speaker volume"):
            n = max(0, min(100, int(float(str(v).rstrip("%")))))
            ok = volume_set(percent=n, mute=False)
            return ok, f"Volume set to {n}%." if ok else "Couldn't change the volume."
        if w in ("mute", "muted"):
            ok = volume_set(mute=_on(v) if v is not None else True)
            return ok, ("Muted." if (v is None or _on(v)) else "Unmuted.") if ok else "Couldn't change mute."
        if w in ("dark mode", "theme", "light mode"):
            dark = _on(v) if w != "light mode" else not _on(v)
            ps(f"$k='HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize'; Set-ItemProperty $k AppsUseLightTheme {0 if dark else 1}; Set-ItemProperty $k SystemUsesLightTheme {0 if dark else 1}")
            return True, f"{'Dark' if dark else 'Light'} mode on."
        if w in ("wifi", "wi-fi", "wireless"):
            return "->" in (out := set_radio("WiFi", _on(v))), f"Wi-Fi {'on' if _on(v) else 'off'} ({out})."
        if w == "bluetooth":
            return "->" in (out := set_radio("Bluetooth", _on(v))), f"Bluetooth {'on' if _on(v) else 'off'} ({out})."
        if w in ("airplane", "airplane mode", "flight mode"):
            out = set_radio("all", not _on(v))
            return "->" in out, f"Airplane mode {'on: radios off' if _on(v) else 'off: radios back on'} ({out})."
        if w in ("hotspot", "mobile hotspot"):
            out = hotspot(_on(v))
            return "Success" in out, f"Hotspot: {out}"
        if w in ("screen timeout", "screen off", "display timeout", "turn off screen after"):
            m = _minutes(v)
            ps(f"powercfg /change monitor-timeout-ac {m}; powercfg /change monitor-timeout-dc {m}")
            return True, f"The screen now turns off after {m} minutes." if m else "The screen now stays on."
        if w in ("sleep timeout", "sleep after", "sleep"):
            m = _minutes(v)
            ps(f"powercfg /change standby-timeout-ac {m}; powercfg /change standby-timeout-dc {m}")
            return True, f"The laptop now sleeps after {m} minutes." if m else "The laptop now never sleeps on its own."
        if w in ("power mode", "performance mode", "power plan"):
            s = str(v).lower()
            key = "OVERLAY_SCHEME_MAX" if re.search(r"best perf|high|max|performance", s) else "OVERLAY_SCHEME_MIN" if re.search(r"effic|saver|low|battery|eco", s) else "OVERLAY_SCHEME_BALANCED"
            out = ps(f"powercfg /overlaysetactive {key}; powercfg /overlaygetactive")
            return "Invalid" not in out, f"Power mode: {key.split('_')[-1].title()}."
        if w in ("battery saver", "power saver", "energy saver"):
            thr = 100 if _on(v) else 20
            ps(f"powercfg /setdcvalueindex SCHEME_CURRENT SUB_ENERGYSAVER ESBATTTHRESHOLD {thr}; powercfg /setactive SCHEME_CURRENT")
            return True, f"Battery saver {'on whenever it is on battery' if _on(v) else 'back to kicking in at 20%'}."
        if w in ("notifications", "toasts"):
            ps(f"Set-ItemProperty 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\PushNotifications' ToastEnabled {1 if _on(v) else 0}")
            return True, f"Notifications {'on' if _on(v) else 'off'}."
        if w in ("clipboard history", "clipboard"):
            ps(f"New-Item 'HKCU:\\Software\\Microsoft\\Clipboard' -Force | Out-Null; Set-ItemProperty 'HKCU:\\Software\\Microsoft\\Clipboard' EnableClipboardHistory {1 if _on(v) else 0}")
            return True, f"Clipboard history {'on' if _on(v) else 'off'}."
        if w in ("location", "location access"):
            ps(f"Set-ItemProperty 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\ConsentStore\\location' Value {'Allow' if _on(v) else 'Deny'}")
            return True, f"Location access {'allowed' if _on(v) else 'blocked'} for apps."
        if w in ("camera access", "camera privacy", "webcam"):
            ps(f"Set-ItemProperty 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\ConsentStore\\webcam' Value {'Allow' if _on(v) else 'Deny'}")
            return True, f"Camera access {'allowed' if _on(v) else 'blocked'} for apps."
        if w in ("microphone access", "mic access", "microphone privacy"):
            ps(f"Set-ItemProperty 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\ConsentStore\\microphone' Value {'Allow' if _on(v) else 'Deny'}")
            return True, f"Microphone access {'allowed' if _on(v) else 'blocked'} for apps."
        if w in ("mouse speed", "pointer speed"):
            n = max(1, min(20, int(v)))
            ps(f"Add-Type -TypeDefinition 'using System.Runtime.InteropServices; public class M {{ [DllImport(\"user32.dll\")] public static extern bool SystemParametersInfo(uint a, uint b, uint c, uint d); }}'; [M]::SystemParametersInfo(113, 0, {n}, 2); Set-ItemProperty 'HKCU:\\Control Panel\\Mouse' MouseSensitivity {n}")
            return True, f"Mouse speed set to {n} of 20."
        if w in ("default printer", "printer"):
            out = ps(f"$p = Get-Printer | ? {{ $_.Name -like '*{v}*' }} | Select -First 1; if ($p) {{ (New-Object -ComObject WScript.Network).SetDefaultPrinter($p.Name); $p.Name }} else {{ 'none' }}")
            return out != "none", f"Default printer: {out}." if out != "none" else f"No printer like {v}."
        if w in ("startup app", "startup"):
            name, on = (v.get("name"), v.get("on", True)) if isinstance(v, dict) else (str(v), True)
            return _startup_app(name, _on(on))
        if w == "wallpaper":
            return _wallpaper(v)
        if w in ("lock", "lock screen"):
            subprocess.Popen(["rundll32.exe", "user32.dll,LockWorkStation"], creationflags=NO_WINDOW)
            return True, "Locked."
        if w in ("display off", "screen off now", "turn off the screen"):
            ps("Add-Type -TypeDefinition 'using System.Runtime.InteropServices; public class S { [DllImport(\"user32.dll\")] public static extern int SendMessage(int h, int m, int w, int l); }'; [S]::SendMessage(-1, 0x0112, 0xF170, 2)")
            return True, "Screen off."
        if w in ("go to sleep", "sleep now", "suspend"):
            ps("Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.Application]::SetSuspendState('Suspend', $false, $false)")
            return True, "Going to sleep."
        if w == "hibernate":
            subprocess.Popen(["shutdown", "/h"], creationflags=NO_WINDOW)
            return True, "Hibernating."
        if w in ("restart", "reboot"):
            subprocess.Popen(["shutdown", "/r", "/t", "5"], creationflags=NO_WINDOW)
            return True, "Restarting in five seconds."
        if w in ("shutdown", "shut down", "power off"):
            subprocess.Popen(["shutdown", "/s", "/t", "5"], creationflags=NO_WINDOW)
            return True, "Shutting down in five seconds."
        if w in ("empty recycle bin", "recycle bin"):
            ps("Clear-RecycleBin -Force -EA SilentlyContinue")
            return True, "Recycle Bin emptied."
        if w in ("clear temp", "temp files", "clean temp"):
            out = ps("$b=0; foreach ($d in @($env:TEMP, \"$env:LOCALAPPDATA\\Temp\", \"$env:WINDIR\\Temp\")) { Get-ChildItem $d -Recurse -Force -EA SilentlyContinue | ? { -not $_.PSIsContainer -and $_.LastWriteTime -lt (Get-Date).AddDays(-1) } | % { $b += $_.Length; Remove-Item $_.FullName -Force -EA SilentlyContinue } }; [math]::Round($b/1MB)")
            return True, f"Cleared about {out.strip() or 0} MB of old temp files."
        if w in ("check updates", "windows update", "update"):
            os.startfile("ms-settings:windowsupdate")
            subprocess.Popen(["UsoClient", "StartInteractiveScan"], creationflags=NO_WINDOW)
            return True, "Checking for Windows updates; the page is open."
        if w in ("defender scan", "virus scan", "quick scan"):
            threading.Thread(target=ps, args=("Start-MpScan -ScanType QuickScan", 900), daemon=True).start()
            return True, "A quick virus scan has started in the background."
        if w in ("flush dns", "dns"):
            ps("Clear-DnsClientCache; ipconfig /flushdns")
            return True, "DNS cache flushed."
        if w in ("reconnect wifi", "wifi reconnect"):
            out = ps("$i = (netsh wlan show interfaces | Select-String '^\\s+Profile\\s*:').ToString().Split(':')[1].Trim(); netsh wlan disconnect | Out-Null; Start-Sleep 2; netsh wlan connect name=\"$i\"")
            return "success" in out.lower(), "Reconnected to Wi-Fi." if "success" in out.lower() else f"Wi-Fi: {out[:120]}"
        if w in ("connect wifi", "join wifi", "wifi network"):
            out = ps(f"netsh wlan connect name=\"{v}\"")
            return "success" in out.lower(), out[:140]
        if w in ("restart explorer", "explorer"):
            ps("Stop-Process -Name explorer -Force; Start-Sleep 1; if (-not (Get-Process explorer -EA SilentlyContinue)) { Start-Process explorer }")
            return True, "Explorer restarted (taskbar and desktop reloaded)."
        if w in ("night light", "nightlight", "focus assist", "do not disturb", "scaling", "scale", "resolution", "refresh rate", "audio output", "output device", "default browser", "default apps", "timezone", "time zone"):
            page = {"night light": "night light", "nightlight": "night light", "focus assist": "focus", "do not disturb": "focus", "scaling": "display", "scale": "display",
                    "resolution": "display", "refresh rate": "display", "audio output": "sound", "output device": "sound", "default browser": "default apps",
                    "default apps": "default apps", "timezone": "time", "time zone": "time"}[w]
            open_laptop_page(page)
            return False, (f"'{what}' has no direct switch, so I opened Settings > {page}: finish it there with the click action "
                           f"(target = the control's label) or a task, and set it to {value}.")
    except (ValueError, TypeError) as e:
        return False, f"Bad value for {what}: {e}"
    return False, (f"'{what}' isn't in my laptop settings list. Try the powershell action, or open the page with "
                   f"{{\"a\":\"settings\",\"device\":\"laptop\",\"open\":\"<page>\"}} and click there.")


LAPTOP_SETTINGS_DOC = ("brightness 0-100, volume 0-100, mute on/off, dark mode on/off, wifi on/off, bluetooth on/off, airplane on/off, "
                       "hotspot on/off, screen timeout <minutes>, sleep timeout <minutes>, power mode best performance|balanced|best efficiency, "
                       "battery saver on/off, notifications on/off, clipboard history on/off, location on/off, camera access on/off, "
                       "microphone access on/off, mouse speed 1-20, default printer <name>, startup app {\"name\":..,\"on\":bool}, "
                       "wallpaper <path>, connect wifi <network>, reconnect wifi, flush dns, lock, display off, go to sleep, hibernate, "
                       "restart, shutdown, empty recycle bin, clear temp, check updates, defender scan, restart explorer")
LAPTOP_RISKY = {"restart", "reboot", "shutdown", "shut down", "power off", "empty recycle bin", "recycle bin", "clear temp", "temp files", "clean temp", "hibernate", "wifi", "wi-fi", "airplane", "airplane mode", "flight mode"}


# ---------------------------------------------------------------- the phone through adb (only when plugged in)

ADB = os.environ.get("FRIDAY_ADB") or next((p for p in (r"D:\dev\Android\Sdk\platform-tools\adb.exe",
                                                       os.path.expandvars(r"%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe")) if os.path.isfile(p)), "adb")


def adb(*args, timeout=20):
    try:
        r = subprocess.run([ADB, *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           creationflags=NO_WINDOW)
        return (r.stdout + r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"(adb: {e})"


def adb_present():
    out = adb("devices")
    return any(line.endswith("\tdevice") for line in out.splitlines())


def adb_report():
    """Deep phone evidence through adb: battery, memory, heat, crashes, recent app deaths. Only when it's plugged in."""
    if not adb_present():
        return ""
    parts = ["(phone plugged in: adb evidence)"]
    parts.append("Battery: " + " ".join(l.strip() for l in adb("shell", "dumpsys", "battery").splitlines() if re.search(r"level|status|health|temperature|plugged", l)))
    parts.append("Memory: " + " ".join(l.strip() for l in adb("shell", "dumpsys", "meminfo").splitlines() if re.search(r"Total RAM|Free RAM|Used RAM|Lost RAM|ZRAM", l)))
    parts.append("Storage: " + adb("shell", "df", "-h", "/data").splitlines()[-1] if adb("shell", "df", "-h", "/data").splitlines() else "")
    parts.append("Thermal: " + " ".join(l.strip() for l in adb("shell", "dumpsys", "thermalservice").splitlines() if "Thermal Status" in l or "mStatus" in l)[:200])
    parts.append("Uptime: " + adb("shell", "uptime"))
    parts.append("Android: " + adb("shell", "getprop", "ro.build.version.release") + ", One UI " + adb("shell", "getprop", "ro.build.version.oneui") + ", patch " + adb("shell", "getprop", "ro.build.version.security_patch"))
    crash = adb("shell", "logcat", "-b", "crash", "-d", "-t", "80")
    parts.append("Crash log (recent):\n" + (crash[-3000:] if crash else "(empty)"))
    drop = adb("shell", "dumpsys", "dropbox")
    recent = [l for l in drop.splitlines() if re.search(r"crash|anr|wtf|lowmem|watchdog", l, re.I)][-12:]
    parts.append("Recent app deaths (dropbox): " + ("; ".join(l.strip() for l in recent) if recent else "none listed"))
    parts.append("Friday app: " + adb("shell", "dumpsys", "package", "com.example.friday").split("User 0:")[-1][:400].replace("\n", " "))
    return "\n".join(parts)


# ---------------------------------------------------------------- the knowledge the diagnose agent gets

def diagnose_evidence(device, problem, phone_report=""):
    """Everything she can gather herself before the agent starts: status + the reports the problem's words call for."""
    p = (problem or "").lower()
    out = [f"PROBLEM: {problem}", f"WHEN: {time.strftime('%A %d %B %Y, %H:%M')}", devices_line()]
    if device in ("laptop", "both"):
        topics = ["errors"]
        picks = [("battery", r"batter|charg|power|drain"), ("network", r"wifi|wi-fi|internet|network|connect|slow net|dns|vpn|hotspot|online|offline"),
                 ("performance", r"slow|lag|hang|freez|frozen|stuck|not respond|cpu|ram|memory|fan|hot|heat"), ("storage", r"storage|space|disk|full"),
                 ("devices", r"bluetooth|usb|mouse|keyboard|printer|camera|webcam|monitor|screen|display|driver|device|headphone|speaker|mic"),
                 ("sound", r"sound|audio|speaker|mic|volume|headphone"), ("updates", r"update|install|upgrade|restart pending"),
                 ("security", r"virus|malware|defender|security|blocked|smart app|firewall"), ("display", r"screen|display|bright|dark|night light|resolution|scale"),
                 ("apps", r"app|program|startup|boot|login|start-up|crash|open|launch")]
        topics += [t for t, rx in picks if re.search(rx, p) and t not in topics]
        if len(topics) == 1:
            topics += ["performance", "network"]
        for t in topics[:5]:
            out.append(f"\n=== LAPTOP {t.upper()} ===\n{laptop_report(t)}")
    if device in ("phone", "both"):
        out.append("\n=== PHONE (its own report) ===\n" + (phone_report or "(the phone didn't answer; it may be off the link)"))
        a = adb_report()
        if a:
            out.append("\n=== PHONE (adb) ===\n" + a)
    return "\n".join(out)


DIAGNOSE_PROMPT = """You are FRIDAY's diagnostic mind, started by voice from Sam's Windows 11 laptop (Dell 15 D15260, Intel Core
Ultra 5 225H, 16 GB, 512 GB NVMe, Windows 11 Home 26300) and his Samsung Galaxy S26 Ultra. He has a problem; the evidence
Friday gathered is in the task. Your job: find the REAL cause fast, fix it when a fix is quick and safe, and say
plainly what's left for him.
HOW TO WORK
- Read the evidence first; most answers are already in it (and in PAST FIXES: the same problem may be solved there).
  Form one clear hypothesis, check it with a command, then fix. Search the web only for an exact error text you don't
  understand.
- TIME BOX: he's waiting by voice. Reach a verdict within 6 commands. If a check doesn't work the first way, try ONE
  other way, then move on with what you have. Never write, compile or install tools (no C#, no new Python packages, no
  scripts saved to disk) to run a check: if a check needs that, skip it and say what you couldn't verify.
- A fix only if it's one or two commands and reversible. Anything bigger (driver reinstall, a restart, a reset) is his
  decision: name it as the one step for him.
- ONLY the device the task names. Touch the other device only if the task says "both".
- Laptop: you have his shell. Windows PowerShell 5.1 (`powershell -NoProfile -Command ...`) for WinRT APIs, pwsh for the
  rest, winget, registry (HKCU first), services, drivers (pnputil), netsh, powercfg, sfc/DISM when system files are
  suspect. `python D:\\dev\\Friday\\devices.py report <topic>` prints Friday's own reports (status, battery, storage,
  performance, network, wifi, errors, updates, devices, apps, security, display, sound), and
  `python D:\\dev\\Friday\\devices.py set "<setting>" "<value>"` changes a setting from her catalogue.
  Admin rights: you run as him, not admin. If a fix truly needs admin, run it ONCE via
  `Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile -Command ...'` and wait up to 60 s. A Windows
  "Do you want to allow this app to make changes" box then sits on his laptop screen; if it wasn't approved, say exactly
  that in plain words ("a Windows permission box popped up on the laptop and is still waiting; tap Yes on it, or ...").
  Never say "approval" or "prompt" without saying where it is.
- Phone: if `adb devices` lists it, use adb (shell, dumpsys, settings, logcat, pm; never factory reset, never uninstall
  his apps, never `settings put` without reading the old value first and noting it). Otherwise, or in addition, run phone
  actions through Friday's app with `python D:\\dev\\Friday\\phone_do.py '<json action>'` (prints the phone's result):
  {"a":"device_report","topic":"status|battery|storage|memory|network|display|sound|apps|bluetooth|device|errors|permissions|thermal"},
  {"a":"setting","what":"...","value":...}, {"a":"open_settings","page":"..."}, {"a":"toggle","what":"...","on":true},
  {"a":"read_screen"}, {"a":"tap","target":"..."}, {"a":"look","question":"..."}, {"a":"task","goal":"..."} (step by
  step in the phone's Settings; the phone must be unlocked and he sees it happen). Screen tasks: one at a time, wait for
  the result.
- Prefer reversible fixes: restart a service, reconnect, reinstall a driver, clear a cache, change a setting back.
  Note every change you make. NEVER delete his files, documents, photos or chats; never reset Windows, BitLocker or the
  phone; never remove apps unless the task says so; never touch D:\\dev\\Friday's code (that's Friday herself; her own
  improve_self job does that).
- Verify: after a fix, re-run the check that showed the problem once. If it still fails, stop and report; don't chase
  the next cause past the time box.
- Record what you learnt in D:\\dev\\Friday\\data\\fixes.md (append: date, device, problem, cause, fix, verified yes/no) so
  the next time is faster; read it first, the same problem may be there already.
FINAL MESSAGE, in exactly this shape:
SAY: <EXACTLY two short spoken sentences, 30 words in all at most, warm and casual, American English, no markdown, no
paths, nothing technical: 1) what's wrong in plain words, 2) the one thing he should do now (or that it's fixed).
Details, times, event names, second-best advice: all below the line, never in SAY>
---
<the write-up for her window, 4-8 plain lines: cause, what you checked, what you tried, what to do if it comes back>
Friday speaks the SAY part and shows the rest as a card. Everything after "---" is for reading, not listening."""


# ---------------------------------------------------------------- command line (for the diagnose agent and tests)

if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "report":
        print(laptop_report(sys.argv[2] if len(sys.argv) > 2 else "status"))
    elif cmd == "set":
        ok, text = set_laptop(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
        print(("ok: " if ok else "no: ") + text)
    elif cmd == "open":
        print(open_laptop_page(sys.argv[2]))
    elif cmd == "adb":
        print(adb_report() or "phone not plugged in")
    elif cmd == "radios":
        print(radios())
    else:
        print(laptop_line())
