param(
    [switch]$DryRun,
    [switch]$SkipHotkeyRestart
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root ".venv\Scripts\python.exe"
$wscript = Join-Path $env:SystemRoot "System32\wscript.exe"
$icon = Join-Path $root "assets\muesli-icon.ico"
$appId = "Muesli.App"
$desktop = Join-Path ([Environment]::GetFolderPath("Desktop")) "Muesli.lnk"
$programs = [Environment]::GetFolderPath("Programs")
$startup = [Environment]::GetFolderPath("Startup")
$recordShortcut = Join-Path $programs "Muesli Record.lnk"
$hotkeyShortcut = Join-Path $startup "Muesli Hotkey.lnk"
$taskbarDir = Join-Path $env:APPDATA "Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar"
$taskbarShortcut = Join-Path $taskbarDir "Muesli.lnk"
$guiLauncher = Join-Path $root "muesli_gui_launcher.vbs"
$hotkeyScript = Join-Path $root "muesli_hotkey.py"
$hotkeyLauncher = Join-Path $root "muesli_hotkey_launcher.vbs"
$guiScript = Join-Path $root "muesli_gui.py"

if (-not ("Muesli.ShortcutProperties" -as [type])) {
Add-Type -Language CSharp @"
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;

namespace Muesli {
    [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
    class CShellLink {}

    [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid("000214F9-0000-0000-C000-000000000046")]
    interface IShellLinkW {}

    [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid("0000010B-0000-0000-C000-000000000046")]
    interface IPersistFile {
        void GetClassID(out Guid pClassID);
        void IsDirty();
        void Load([MarshalAs(UnmanagedType.LPWStr)] string pszFileName, uint dwMode);
        void Save([MarshalAs(UnmanagedType.LPWStr)] string pszFileName, bool fRemember);
        void SaveCompleted([MarshalAs(UnmanagedType.LPWStr)] string pszFileName);
        void GetCurFile([MarshalAs(UnmanagedType.LPWStr)] out string ppszFileName);
    }

    [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")]
    interface IPropertyStore {
        uint GetCount(out uint cProps);
        uint GetAt(uint iProp, out PROPERTYKEY pkey);
        uint GetValue(ref PROPERTYKEY key, out PROPVARIANT pv);
        uint SetValue(ref PROPERTYKEY key, ref PROPVARIANT pv);
        uint Commit();
    }

    [StructLayout(LayoutKind.Sequential, Pack = 4)]
    struct PROPERTYKEY {
        public Guid fmtid;
        public uint pid;
        public PROPERTYKEY(Guid formatId, uint propertyId) {
            fmtid = formatId;
            pid = propertyId;
        }
    }

    [StructLayout(LayoutKind.Sequential)]
    struct PROPVARIANT : IDisposable {
        public ushort vt;
        public ushort wReserved1;
        public ushort wReserved2;
        public ushort wReserved3;
        public IntPtr p;
        public int p2;

        public PROPVARIANT(string value) : this() {
            vt = 31;
            p = Marshal.StringToCoTaskMemUni(value);
        }

        public void Dispose() {
            NativeMethods.PropVariantClear(ref this);
        }
    }

    static class NativeMethods {
        [DllImport("ole32.dll")]
        internal static extern int PropVariantClear(ref PROPVARIANT pvar);
    }

    public static class ShortcutProperties {
        static readonly Guid AppUserModelGuid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3");

        public static void SetAppDetails(string shortcutPath, string appId, string relaunchIcon) {
            var link = (CShellLink)new CShellLink();
            var persist = (IPersistFile)link;
            persist.Load(shortcutPath, 0);
            var store = (IPropertyStore)link;

            var appIdKey = new PROPERTYKEY(AppUserModelGuid, 5);
            var appIdValue = new PROPVARIANT(appId);
            try {
                store.SetValue(ref appIdKey, ref appIdValue);
            } finally {
                appIdValue.Dispose();
            }

            if (!string.IsNullOrWhiteSpace(relaunchIcon)) {
                var iconKey = new PROPERTYKEY(AppUserModelGuid, 3);
                var iconValue = new PROPVARIANT(relaunchIcon);
                try {
                    store.SetValue(ref iconKey, ref iconValue);
                } finally {
                    iconValue.Dispose();
                }
            }

            store.Commit();
            persist.Save(shortcutPath, true);
        }
    }
}
"@
}

function Write-Shortcut {
    param(
        [string]$Path,
        [string]$TargetPath,
        [string]$Arguments,
        [string]$Description,
        [string]$Hotkey = ""
    )

    if ($DryRun) {
        [pscustomobject]@{
            Action = "WriteShortcut"
            Path = $Path
            TargetPath = $TargetPath
            Arguments = $Arguments
            WorkingDirectory = $root
            IconLocation = $(if (Test-Path $icon) { $icon } else { "shell32.dll,168" })
            Description = $Description
            Hotkey = $Hotkey
        }
        return
    }

    $ws = New-Object -ComObject WScript.Shell
    $s = $ws.CreateShortcut($Path)
    $s.TargetPath = $TargetPath
    $s.Arguments = $Arguments
    $s.WorkingDirectory = $root
    $s.Description = $Description
    if (Test-Path $icon) {
        $s.IconLocation = $icon
    } else {
        $s.IconLocation = "shell32.dll,168"
    }
    $s.Hotkey = $Hotkey
    $s.Save()
    [System.Runtime.InteropServices.Marshal]::ReleaseComObject($s) | Out-Null
    [System.Runtime.InteropServices.Marshal]::ReleaseComObject($ws) | Out-Null
    $s = $null
    $ws = $null
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
    $relaunchIcon = if (Test-Path $icon) { "$icon,0" } else { "" }
    try {
        [Muesli.ShortcutProperties]::SetAppDetails($Path, $appId, $relaunchIcon)
    } catch {
        Write-Verbose ("Skipping AppID metadata update for {0}: {1}" -f $Path, $_.Exception.Message)
    }
}

if (-not (Test-Path $python)) {
    throw "Missing virtualenv launcher: $python"
}

Write-Shortcut -Path $desktop -TargetPath $wscript -Arguments ('"' + $guiLauncher + '"') -Description "Muesli"
Write-Shortcut -Path $recordShortcut -TargetPath $wscript -Arguments ('"' + $guiLauncher + '" "--record"') -Description "Muesli Record"
Write-Shortcut -Path $hotkeyShortcut -TargetPath $wscript -Arguments ('"' + $hotkeyLauncher + '"') -Description "Muesli Hotkey"

if ($DryRun) {
    [pscustomobject]@{
        Action = "MirrorDesktopShortcutToTaskbar"
        Source = $desktop
        Destination = $taskbarShortcut
        DestinationExists = (Test-Path $taskbarDir)
    }
} else {
    if (Test-Path $taskbarDir) {
        Copy-Item -LiteralPath $desktop -Destination $taskbarShortcut -Force
    }
}

if (-not $SkipHotkeyRestart) {
    if ($DryRun) {
        [pscustomobject]@{
            Action = "RestartHotkeyAgent"
            Script = $hotkeyScript
            WorkingDirectory = $root
        }
    } else {
        Get-CimInstance Win32_Process |
            Where-Object { ($_.Name -eq "pythonw.exe" -or $_.Name -eq "python.exe") -and $_.CommandLine -match "muesli_hotkey.py" } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Milliseconds 300
        Start-Process -FilePath $python -ArgumentList ('"' + $hotkeyScript + '"') -WorkingDirectory $root -WindowStyle Hidden
    }
}
