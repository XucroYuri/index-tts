[CmdletBinding()]
param(
    [ValidateSet("Bootstrap", "Full")][string]$Profile = "Bootstrap",
    [ValidateSet("Auto", "CU128", "CU126", "CPU")][string]$Device = "Auto",
    [string]$Version = "0.2.0",
    [string]$OutputRoot = "",
    [string]$WorkRoot = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if ($Profile -eq "Full" -and $env:GITHUB_ACTIONS -eq "true") { throw "profile=full is local-only and cannot be built by a GitHub upload workflow" }
if ($Version -notmatch "^[0-9A-Za-z][0-9A-Za-z._-]{0,127}$") { throw "package Version must contain only ASCII letters, digits, dot, underscore, or hyphen (maximum 128 characters)" }

if (-not ("TtsMorePortableDirectoryHandle" -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

public static class TtsMorePortableDirectoryHandle
{
    [StructLayout(LayoutKind.Sequential)]
    private struct ByHandleFileInformation
    {
        public uint FileAttributes;
        public System.Runtime.InteropServices.ComTypes.FILETIME CreationTime;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastAccessTime;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastWriteTime;
        public uint VolumeSerialNumber;
        public uint FileSizeHigh;
        public uint FileSizeLow;
        public uint NumberOfLinks;
        public uint FileIndexHigh;
        public uint FileIndexLow;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct UnicodeString
    {
        public ushort Length;
        public ushort MaximumLength;
        public IntPtr Buffer;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct ObjectAttributes
    {
        public int Length;
        public IntPtr RootDirectory;
        public IntPtr ObjectName;
        public uint Attributes;
        public IntPtr SecurityDescriptor;
        public IntPtr SecurityQualityOfService;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct IoStatusBlock
    {
        public IntPtr Status;
        public IntPtr Information;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct FileDispositionInformation
    {
        [MarshalAs(UnmanagedType.Bool)]
        public bool DeleteFile;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern SafeFileHandle CreateFile(
        string fileName,
        uint desiredAccess,
        uint shareMode,
        IntPtr securityAttributes,
        uint creationDisposition,
        uint flagsAndAttributes,
        IntPtr templateFile);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetFileInformationByHandle(
        SafeFileHandle file,
        out ByHandleFileInformation information);

    [DllImport("ntdll.dll")]
    private static extern int NtCreateFile(
        out IntPtr fileHandle,
        uint desiredAccess,
        ref ObjectAttributes objectAttributes,
        out IoStatusBlock ioStatusBlock,
        IntPtr allocationSize,
        uint fileAttributes,
        uint shareAccess,
        uint createDisposition,
        uint createOptions,
        IntPtr eaBuffer,
        uint eaLength);

    [DllImport("ntdll.dll")]
    private static extern uint RtlNtStatusToDosError(int status);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetFileInformationByHandle(
        SafeFileHandle file,
        int fileInformationClass,
        ref FileDispositionInformation information,
        int bufferSize);

    public static SafeFileHandle Open(string path, bool shareDelete)
    {
        return Open(path, shareDelete, false);
    }

    public static SafeFileHandle Open(string path, bool shareDelete, bool childAccess)
    {
        const uint FileReadAttributes = 0x00000080;
        const uint FileListDirectory = 0x00000001;
        const uint FileAddSubdirectory = 0x00000004;
        const uint FileShareRead = 0x00000001;
        const uint FileShareWrite = 0x00000002;
        const uint FileShareDelete = 0x00000004;
        const uint OpenExisting = 3;
        const uint FileFlagBackupSemantics = 0x02000000;
        const uint FileFlagOpenReparsePoint = 0x00200000;
        uint share = FileShareRead | FileShareWrite | (shareDelete ? FileShareDelete : 0);
        uint access = FileReadAttributes | FileListDirectory | (childAccess ? FileAddSubdirectory : 0);
        SafeFileHandle handle = CreateFile(
            path,
            access,
            share,
            IntPtr.Zero,
            OpenExisting,
            FileFlagBackupSemantics | FileFlagOpenReparsePoint,
            IntPtr.Zero);
        if (handle.IsInvalid)
        {
            int error = Marshal.GetLastWin32Error();
            handle.Dispose();
            throw new Win32Exception(error, "Cannot open worker staging directory by handle: " + path);
        }
        return handle;
    }

    public static SafeFileHandle CreateDirectoryRelative(
        SafeFileHandle parent,
        string name,
        bool shareDelete)
    {
        if (String.IsNullOrEmpty(name) || name == "." || name == ".." || name.Contains("\\") || name.Contains("/"))
        {
            throw new ArgumentException("Unsafe worker staging directory name", "name");
        }
        IntPtr nameBuffer = IntPtr.Zero;
        IntPtr unicodePointer = IntPtr.Zero;
        try
        {
            nameBuffer = Marshal.StringToHGlobalUni(name);
            UnicodeString unicode = new UnicodeString();
            unicode.Length = checked((ushort)(name.Length * 2));
            unicode.MaximumLength = checked((ushort)((name.Length + 1) * 2));
            unicode.Buffer = nameBuffer;
            unicodePointer = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(UnicodeString)));
            Marshal.StructureToPtr(unicode, unicodePointer, false);
            ObjectAttributes attributes = new ObjectAttributes();
            attributes.Length = Marshal.SizeOf(typeof(ObjectAttributes));
            attributes.RootDirectory = parent.DangerousGetHandle();
            attributes.ObjectName = unicodePointer;
            attributes.Attributes = 0x00000040;
            IoStatusBlock ioStatus;
            IntPtr rawHandle;
            const uint DesiredAccess = 0x00100000 | 0x00010000 | 0x00000080 | 0x00000007;
            const uint ShareRead = 0x00000001;
            const uint ShareWrite = 0x00000002;
            const uint ShareDelete = 0x00000004;
            const uint FileCreate = 2;
            const uint FileDirectoryFile = 0x00000001;
            const uint FileSynchronousIoNonalert = 0x00000020;
            const uint FileOpenReparsePoint = 0x00200000;
            uint share = ShareRead | ShareWrite | (shareDelete ? ShareDelete : 0);
            int status = NtCreateFile(
                out rawHandle,
                DesiredAccess,
                ref attributes,
                out ioStatus,
                IntPtr.Zero,
                0x00000010,
                share,
                FileCreate,
                FileDirectoryFile | FileSynchronousIoNonalert | FileOpenReparsePoint,
                IntPtr.Zero,
                0);
            if (status < 0)
            {
                throw new Win32Exception(
                    unchecked((int)RtlNtStatusToDosError(status)),
                    "Cannot atomically create worker staging directory: " + name);
            }
            return new SafeFileHandle(rawHandle, true);
        }
        finally
        {
            if (unicodePointer != IntPtr.Zero) { Marshal.FreeHGlobal(unicodePointer); }
            if (nameBuffer != IntPtr.Zero) { Marshal.FreeHGlobal(nameBuffer); }
        }
    }

    public static void MarkDirectoryForDeletion(SafeFileHandle handle)
    {
        FileDispositionInformation information = new FileDispositionInformation();
        information.DeleteFile = true;
        if (!SetFileInformationByHandle(
            handle,
            4,
            ref information,
            Marshal.SizeOf(typeof(FileDispositionInformation))))
        {
            throw new Win32Exception(
                Marshal.GetLastWin32Error(),
                "Cannot mark verified worker staging directory for deletion");
        }
    }

    public static string Identity(SafeFileHandle handle)
    {
        ByHandleFileInformation information;
        if (!GetFileInformationByHandle(handle, out information))
        {
            throw new Win32Exception(Marshal.GetLastWin32Error(), "Cannot inspect worker staging directory handle");
        }
        const uint FileAttributeReparsePoint = 0x00000400;
        if ((information.FileAttributes & FileAttributeReparsePoint) != 0)
        {
            throw new InvalidOperationException("Worker staging directory handle resolves to a reparse point");
        }
        ulong index = ((ulong)information.FileIndexHigh << 32) | information.FileIndexLow;
        return information.VolumeSerialNumber.ToString("X8") + ":" + index.ToString("X16");
    }

    public static uint NumberOfLinks(string path)
    {
        const uint FileReadAttributes = 0x00000080;
        const uint FileShareRead = 0x00000001;
        const uint FileShareWrite = 0x00000002;
        const uint FileShareDelete = 0x00000004;
        const uint OpenExisting = 3;
        const uint FileFlagOpenReparsePoint = 0x00200000;
        using (SafeFileHandle handle = CreateFile(path, FileReadAttributes,
            FileShareRead | FileShareWrite | FileShareDelete, IntPtr.Zero,
            OpenExisting, FileFlagOpenReparsePoint, IntPtr.Zero))
        {
            if (handle.IsInvalid) { throw new Win32Exception(Marshal.GetLastWin32Error(), "Cannot inspect staged file link count: " + path); }
            ByHandleFileInformation information;
            if (!GetFileInformationByHandle(handle, out information)) { throw new Win32Exception(Marshal.GetLastWin32Error(), "Cannot inspect staged file link count: " + path); }
            return information.NumberOfLinks;
        }
    }

    public static bool ContainsAnyPrefix(string path, string[] prefixes)
    {
        var patterns = new System.Collections.Generic.List<byte[]>();
        foreach (string prefix in prefixes)
        {
            if (String.IsNullOrEmpty(prefix)) { continue; }
            patterns.Add(System.Text.Encoding.UTF8.GetBytes(prefix));
            patterns.Add(System.Text.Encoding.Unicode.GetBytes(prefix));
        }
        if (patterns.Count == 0) { return false; }
        int maximum = 1;
        foreach (byte[] pattern in patterns) { maximum = Math.Max(maximum, pattern.Length); }
        byte[] buffer = new byte[1048576 + maximum - 1];
        using (var stream = new System.IO.FileStream(path, System.IO.FileMode.Open,
            System.IO.FileAccess.Read, System.IO.FileShare.ReadWrite | System.IO.FileShare.Delete))
        {
            int carry = 0;
            int read;
            while ((read = stream.Read(buffer, carry, 1048576)) > 0)
            {
                int length = carry + read;
                foreach (byte[] pattern in patterns)
                {
                    for (int offset = 0; offset <= length - pattern.Length; offset++)
                    {
                        int index = 0;
                        while (index < pattern.Length && buffer[offset + index] == pattern[index]) { index++; }
                        if (index == pattern.Length) { return true; }
                    }
                }
                carry = Math.Min(maximum - 1, length);
                if (carry > 0) { Buffer.BlockCopy(buffer, length - carry, buffer, 0, carry); }
            }
        }
        return false;
    }
}
'@
}

function Get-PortableFileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    $stream = [IO.File]::OpenRead($Path)
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha256.ComputeHash($stream))).Replace("-", "").ToLowerInvariant() }
    finally {
        $stream.Dispose()
        $sha256.Dispose()
    }
}

function Assert-PortableWorkPath {
    param([Parameter(Mandatory = $true)][string]$CandidatePath)
    try {
        $fullPath = [IO.Path]::GetFullPath($CandidatePath)
        $volumeRoot = [IO.Path]::GetPathRoot($fullPath)
    }
    catch {
        throw "WorkRoot path validation failed closed. Choose a different -WorkRoot path. Error: $($_.Exception.Message)"
    }
    if ([string]::IsNullOrWhiteSpace($volumeRoot)) {
        throw "WorkRoot path validation failed closed. Choose an absolute -WorkRoot path."
    }
    $pathsToCheck = @($volumeRoot)
    $currentPath = $volumeRoot
    foreach ($segment in @($fullPath.Substring($volumeRoot.Length) -split '[\\/]' | Where-Object { $_ })) {
        $currentPath = Join-Path $currentPath $segment
        $pathsToCheck += $currentPath
    }
    foreach ($currentPath in $pathsToCheck) {
        try {
            $item = Get-Item -LiteralPath $currentPath -Force -ErrorAction Stop
        }
        catch [System.Management.Automation.ItemNotFoundException] {
            return $false
        }
        catch {
            throw "WorkRoot path validation failed closed. Choose a different -WorkRoot path. Path: $currentPath. Error: $($_.Exception.Message)"
        }
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "WorkRoot path must not traverse a reparse point. Choose -WorkRoot outside junctions and symbolic links. Path: $currentPath"
        }
        if (!$item.PSIsContainer) {
            throw "WorkRoot path contains an existing non-directory segment. Choose a different -WorkRoot path. Path: $currentPath"
        }
    }
    return $true
}

$Bundle = [System.IO.Path]::GetFullPath($PSScriptRoot)
$Root = [System.IO.Path]::GetFullPath((Split-Path -Parent $Bundle))
$config = Get-Content -LiteralPath (Join-Path $Bundle "component.json") -Raw | ConvertFrom-Json
$modelLock = Get-Content -LiteralPath (Join-Path $Bundle "locks\models.lock.json") -Raw | ConvertFrom-Json
if (!$modelLock.complete) { throw "release package blocked by incomplete model lock: $($modelLock.missing_required_paths -join ', ')" }
$profileName = $Profile.ToLowerInvariant()
if (!$OutputRoot) { $OutputRoot = Join-Path $Root "artifacts\portable\$profileName" }
$OutputRoot = [System.IO.Path]::GetFullPath($OutputRoot)
$packageName = if ($Profile -eq "Full") { "$($config.component)-$Version-windows-x64-full-staging" } else { "$($config.component)-$Version-windows-x64-$profileName" }
$workBase = if ($WorkRoot) { [IO.Path]::GetFullPath($WorkRoot) } else { [IO.Path]::GetFullPath([IO.Path]::GetTempPath()) }
$normalizedSourceRoot = $Root.TrimEnd("\", "/")
$normalizedWorkBase = $workBase.TrimEnd("\", "/")
$sourceRootBoundary = $normalizedSourceRoot + [IO.Path]::DirectorySeparatorChar
if (
    [string]::Equals($normalizedWorkBase, $normalizedSourceRoot, [StringComparison]::OrdinalIgnoreCase) -or
    $normalizedWorkBase.StartsWith($sourceRootBoundary, [StringComparison]::OrdinalIgnoreCase)
) {
    throw "WorkRoot must be outside source checkout. Set -WorkRoot to a directory outside '$Root' (for example C:\tm)."
}
[void](Assert-PortableWorkPath -CandidatePath $workBase)
$workIdentity = "tts-more-worker-$PID-$([Guid]::NewGuid().ToString('N').Substring(0, 12))"
$work = [IO.Path]::GetFullPath((Join-Path $workBase $workIdentity))
$stage = Join-Path $work $packageName
$revision = (& git -C $Root rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $revision -notmatch "^[0-9a-f]{40}$") { throw "source revision is not available" }

if ($null -ne $config.PSObject.Properties['submodules']) {
    foreach ($submodule in $config.submodules.PSObject.Properties) {
        $path = Join-Path $Root $submodule.Name
        if (!(Test-Path -LiteralPath $path) -or !(Get-ChildItem -LiteralPath $path -Force | Select-Object -First 1)) { throw "locked submodule is not initialized: $($submodule.Name)" }
        $actual = (& git -C $path rev-parse HEAD).Trim()
        if ($actual -ne [string]$submodule.Value) { throw "submodule drift: $($submodule.Name) expected $($submodule.Value), found $actual" }
    }
}

$excluded = @(".git", ".venv", "runtime", "data", "artifacts", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache")
$recursiveExcluded = @(".git", ".venv", "artifacts", "cache", ".cache", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache")
$excludedFiles = @(".env", ".env.local")
$GuideFileName = [string]::Concat([char]0x4F7F, [char]0x7528, [char]0x8BF4, [char]0x660E, "-", [char]0x5148, [char]0x770B, [char]0x8FD9, [char]0x91CC, ".txt")
$rootEntries = @("Initialize.cmd", "Start.cmd", "Stop.cmd", "Repair.cmd", "Build-Package.ps1", "Start-WebUI.cmd", $GuideFileName)
$stageApp = Join-Path $stage "app"
$safeWindowsPathBudget = 240

function Update-PortablePathBudget {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectedPath,
        [Parameter(Mandatory = $true)][ref]$MaximumLength,
        [Parameter(Mandatory = $true)][ref]$MaximumPath
    )
    $length = $ProjectedPath.Length
    if ($length -gt $MaximumLength.Value) {
        $MaximumLength.Value = $length
        $MaximumPath.Value = $ProjectedPath
    }
}

function Measure-PortableTreePathBudget {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination,
        [string[]]$ExcludedNames = @(),
        [string[]]$ExcludedFiles = @(),
        [Parameter(Mandatory = $true)][ref]$MaximumLength,
        [Parameter(Mandatory = $true)][ref]$MaximumPath
    )
    Update-PortablePathBudget -ProjectedPath $Destination -MaximumLength $MaximumLength -MaximumPath $MaximumPath
    foreach ($entry in Get-ChildItem -LiteralPath $Source -Force) {
        if ($entry.Name -in $ExcludedNames -or $entry.Name -in $ExcludedFiles -or $entry.Name -match '^\.env(?:\..+)?$') { continue }
        if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { continue }
        $target = Join-Path $Destination $entry.Name
        Update-PortablePathBudget -ProjectedPath $target -MaximumLength $MaximumLength -MaximumPath $MaximumPath
        if ($entry.PSIsContainer) {
            Measure-PortableTreePathBudget -Source $entry.FullName -Destination $target -ExcludedNames $ExcludedNames -ExcludedFiles $ExcludedFiles -MaximumLength $MaximumLength -MaximumPath $MaximumPath
        }
    }
}

function Assert-PortableTreePathBudget {
    $maximumLength = 0
    $maximumPath = ""
    $generatedPaths = @(
        $stage,
        $stageApp,
        (Join-Path $stage "package\tts-more-package.json"),
        (Join-Path $stage "licenses\UPSTREAM-LICENSE"),
        (Join-Path $stage "licenses\INTEGRATION-LICENSE"),
        (Join-Path $stage "licenses\INTEGRATION-NOTICE"),
        (Join-Path $stage "licenses\THIRD_PARTY_NOTICES.json"),
        (Join-Path $stage "SHA256SUMS.txt")
    ) + @($rootEntries | ForEach-Object { Join-Path $stage $_ })
    foreach ($projectedPath in $generatedPaths) {
        Update-PortablePathBudget -ProjectedPath $projectedPath -MaximumLength ([ref]$maximumLength) -MaximumPath ([ref]$maximumPath)
    }
    foreach ($entry in Get-ChildItem -LiteralPath $Root -Force | Where-Object { $_.Name -notin $excluded -and $_.Name -notin $excludedFiles -and $_.Name -notmatch '^\.env(?:\..+)?$' -and $_.Name -notin $rootEntries }) {
        if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { continue }
        $destination = Join-Path $stageApp $entry.Name
        if ($entry.PSIsContainer) {
            Measure-PortableTreePathBudget -Source $entry.FullName -Destination $destination -ExcludedNames $recursiveExcluded -ExcludedFiles $excludedFiles -MaximumLength ([ref]$maximumLength) -MaximumPath ([ref]$maximumPath)
        } else {
            Update-PortablePathBudget -ProjectedPath $destination -MaximumLength ([ref]$maximumLength) -MaximumPath ([ref]$maximumPath)
        }
    }
    if ($maximumLength -gt $safeWindowsPathBudget) {
        throw "worker package staging path budget exceeded before copy: projected path length $maximumLength exceeds the safe Windows limit $safeWindowsPathBudget. Use -WorkRoot with a shorter external directory (for example C:\tm). Projected path: $maximumPath"
    }
}

function Copy-PortableTree {
    param(
        [string]$Source,
        [string]$Destination,
        [string[]]$ExcludedNames = @(),
        [string[]]$ExcludedFiles = @()
    )
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    foreach ($entry in Get-ChildItem -LiteralPath $Source -Force) {
        if ($entry.Name -in $ExcludedNames -or $entry.Name -in $ExcludedFiles -or $entry.Name -match '^\.env(?:\..+)?$') { continue }
        if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { continue }
        $target = Join-Path $Destination $entry.Name
        if ($entry.PSIsContainer) {
            Copy-PortableTree -Source $entry.FullName -Destination $target -ExcludedNames $ExcludedNames -ExcludedFiles $ExcludedFiles
        } else {
            Copy-Item -LiteralPath $entry.FullName -Destination $target -Force
        }
    }
}

function Add-AppPrefix {
    param([Parameter(Mandatory = $true)][string]$RelativePath)
    $normalized = $RelativePath.Replace("\", "/").TrimStart("/")
    if ([string]::IsNullOrWhiteSpace($normalized) -or $normalized -eq "." -or $normalized -split "/" -contains "..") {
        throw "model lock contains an unsafe package-relative path"
    }
    if ($normalized -eq "app" -or $normalized.StartsWith("app/", [StringComparison]::OrdinalIgnoreCase)) {
        return $normalized
    }
    return "app/$normalized"
}

function Get-CanonicalTextSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    $text = [Text.Encoding]::UTF8.GetString([IO.File]::ReadAllBytes($Path)).Replace("`r`n", "`n")
    $bytes = (New-Object Text.UTF8Encoding($false)).GetBytes($text)
    $hasher = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($hasher.ComputeHash($bytes))).Replace("-", "").ToLowerInvariant() }
    finally { $hasher.Dispose() }
}

function Remove-WorkerFullRuntimeBytecode {
    param([Parameter(Mandatory = $true)][string]$PackageRoot)
    $runtimeRoot = [IO.Path]::GetFullPath((Join-Path $PackageRoot "runtime\live"))
    foreach ($directory in @(Get-ChildItem -LiteralPath $runtimeRoot -Directory -Recurse -Force | Where-Object { $_.Name -eq "__pycache__" } | Sort-Object FullName -Descending)) {
        if (($directory.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Full runtime bytecode cleanup refused a reparse point" }
        Remove-Item -LiteralPath $directory.FullName -Recurse -Force
    }
    foreach ($file in @(Get-ChildItem -LiteralPath $runtimeRoot -File -Recurse -Force -Filter "*.pyc")) {
        if (($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Full runtime bytecode cleanup refused a reparse point" }
        Remove-Item -LiteralPath $file.FullName -Force
    }
}

function Assert-WorkerFullRuntimeBoundary {
    param([Parameter(Mandatory = $true)][string]$PackageRoot)
    foreach ($entry in @(Get-ChildItem -LiteralPath $PackageRoot -Recurse -Force)) {
        if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Full package staging contains a reparse point: $($entry.FullName)" }
        foreach ($segment in @($entry.FullName.Substring($PackageRoot.Length).TrimStart("\", "/") -split '[\\/]')) {
            if ($segment -eq "__pycache__" -or $segment.EndsWith(".pyc", [StringComparison]::OrdinalIgnoreCase)) { throw "Full package staging contains Python bytecode: $($entry.FullName)" }
            if ($segment -in @("pyvenv.cfg", "conda-meta", "condabin", "Miniforge") -or $segment -like "Miniforge*") { throw "Full package staging contains forbidden portable-runtime content: $($entry.FullName)" }
        }
    }
    foreach ($file in @(Get-ChildItem -LiteralPath $PackageRoot -Recurse -File -Force)) {
        if ([TtsMorePortableDirectoryHandle]::NumberOfLinks($file.FullName) -gt 1) { throw "Full package staging contains a multiply-linked file: $($file.FullName)" }
    }
}

function Test-WorkerFullRuntimeOnOtherVolume {
    param([string]$PackageRoot, [string]$ExpectedPython, [string]$ImportProbe)
    $sourceVolume = [IO.Path]::GetPathRoot([IO.Path]::GetFullPath($PackageRoot))
    $probeRoot = $null
    foreach ($drive in @(Get-PSDrive -PSProvider FileSystem | Where-Object { $_.Root -and ![string]::Equals([IO.Path]::GetPathRoot($_.Root), $sourceVolume, [StringComparison]::OrdinalIgnoreCase) })) {
        try {
            $candidate = Join-Path $drive.Root ("tts-more-worker-runtime-probe-" + [Guid]::NewGuid().ToString("N"))
            New-Item -ItemType Directory -Path $candidate -ErrorAction Stop | Out-Null
            $probeRoot = $candidate
            break
        }
        catch { continue }
    }
    if ([string]::IsNullOrWhiteSpace($probeRoot)) { return "not_available" }
    try {
        $runtimeCopy = Join-Path $probeRoot "runtime-live"
        Copy-Item -LiteralPath (Join-Path $PackageRoot "runtime\live") -Destination $runtimeCopy -Recurse
        $python = Join-Path $runtimeCopy "python.exe"
        & $python -c "import platform,sys; raise SystemExit(0 if platform.python_version()==sys.argv[1] else 1)" $ExpectedPython
        if ($LASTEXITCODE -ne 0) { throw "cross-volume embedded Python version probe failed" }
        & $python -c $ImportProbe
        if ($LASTEXITCODE -ne 0) { throw "cross-volume embedded Python import probe failed" }
        return "passed"
    }
    finally { if (Test-Path -LiteralPath $probeRoot) { Remove-Item -LiteralPath $probeRoot -Recurse -Force } }
}

function Test-WorkerBinaryContainsMachinePrefix {
    param([string]$Path, [string[]]$Prefixes)
    return [TtsMorePortableDirectoryHandle]::ContainsAnyPrefix($Path, $Prefixes)
}

function Assert-WorkerFullArchiveBoundary {
    param([Parameter(Mandatory = $true)][string]$ArchivePath)
    Add-Type -AssemblyName System.IO.Compression
    $stream = [IO.File]::OpenRead($ArchivePath)
    try {
        $archive = New-Object IO.Compression.ZipArchive($stream, [IO.Compression.ZipArchiveMode]::Read, $false)
        try {
            foreach ($entry in $archive.Entries) {
                foreach ($segment in @($entry.FullName -split '[/\\]')) {
                    if ($segment -eq "__pycache__" -or $segment.EndsWith(".pyc", [StringComparison]::OrdinalIgnoreCase)) { throw "Full package archive contains Python bytecode: $($entry.FullName)" }
                    if ($segment -in @("pyvenv.cfg", "conda-meta", "condabin", "Miniforge") -or $segment -like "Miniforge*") { throw "Full package archive contains forbidden portable-runtime content: $($entry.FullName)" }
                }
            }
        }
        finally { $archive.Dispose() }
    }
    finally { $stream.Dispose() }
}

$buildPythonOutput = @(& (Join-Path $Bundle "Resolve-PortableBuildPython.ps1") `
    -PackageRoot $Root `
    -BuildToolsRoot (Join-Path $Bundle "build-tools") `
    -BootstrapCondaPath (Join-Path $Bundle "bootstrap-conda.ps1") `
    -ToolchainLockPath (Join-Path $Bundle "locks\toolchain.lock.json") `
    -PortableInstallPath (Join-Path $Bundle "portable_install.py"))
if ($LASTEXITCODE -ne 0 -or $buildPythonOutput.Count -eq 0) { throw "portable build-tools bootstrap failed" }
$buildPython = [IO.Path]::GetFullPath([string]$buildPythonOutput[-1])
& $buildPython (Join-Path $Bundle "portable_packages.py") audit-builder-source --root $Root --component ([string]$config.component) --profile $profileName
if ($LASTEXITCODE -ne 0) { throw "$($config.component) source dirty: copied source audit failed" }

Assert-PortableTreePathBudget
$createdWorkHandle = $null
$createdWorkIdentity = $null
$workCreated = $false
$workBaseHandle = $null
try {
New-Item -ItemType Directory -Force -Path $workBase | Out-Null
[void](Assert-PortableWorkPath -CandidatePath $workBase)
$workBaseHandle = [TtsMorePortableDirectoryHandle]::Open($workBase, $false, $true)
$createdWorkHandle = [TtsMorePortableDirectoryHandle]::CreateDirectoryRelative($workBaseHandle, $workIdentity, $false)
$workCreated = $true
$createdWorkIdentity = [TtsMorePortableDirectoryHandle]::Identity($createdWorkHandle)
New-Item -ItemType Directory -Force -Path $stage, $stageApp, (Join-Path $stage "package"), (Join-Path $stage "licenses") | Out-Null
[void](Assert-PortableWorkPath -CandidatePath $stage)
foreach ($entry in Get-ChildItem -LiteralPath $Root -Force | Where-Object { $_.Name -notin $excluded -and $_.Name -notin $excludedFiles -and $_.Name -notmatch '^\.env(?:\..+)?$' -and $_.Name -notin $rootEntries }) {
    if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { continue }
    $destination = Join-Path $stageApp $entry.Name
    if ($entry.PSIsContainer) {
        Copy-PortableTree -Source $entry.FullName -Destination $destination -ExcludedNames $recursiveExcluded -ExcludedFiles $excludedFiles
    } else {
        Copy-Item -LiteralPath $entry.FullName -Destination $destination -Force
    }
}
foreach ($name in @("Initialize.cmd", "Start.cmd", "Stop.cmd", "Repair.cmd", "Start-WebUI.cmd")) {
    $payload = (Get-Content -LiteralPath (Join-Path $Root $name) -Raw).Replace("%~dp0tts_more\", "%~dp0app\tts_more\")
    Set-Content -LiteralPath (Join-Path $stage $name) -Value $payload -Encoding ASCII
}
Copy-Item -LiteralPath (Join-Path $Root $GuideFileName) -Destination (Join-Path $stage $GuideFileName) -Force
@'
throw "This delivered portable package cannot rebuild itself. Use the corresponding source checkout and its Build-Package.ps1."
'@ | Set-Content -LiteralPath (Join-Path $stage "Build-Package.ps1") -Encoding ASCII

$stagedBundle = Join-Path $stageApp "tts_more"
$stagedPortablePython = Join-Path $stagedBundle "portable-python.ps1"
if (!(Test-Path -LiteralPath $stagedPortablePython -PathType Leaf)) { throw "portable-python.ps1 is missing from worker package staging" }
$stagedConfigPath = Join-Path $stagedBundle "component.json"
$stagedModelLockPath = Join-Path $stagedBundle "locks\models.lock.json"
$stagedConfig = Get-Content -LiteralPath $stagedConfigPath -Raw | ConvertFrom-Json
$stagedConfig | Add-Member -NotePropertyName source_root -NotePropertyValue "app" -Force
$stagedConfig | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $stagedConfigPath -Encoding UTF8
$stagedModelLock = Get-Content -LiteralPath $stagedModelLockPath -Raw | ConvertFrom-Json
$stagedModelLock.required_paths = @($stagedModelLock.required_paths | ForEach-Object { Add-AppPrefix -RelativePath ([string]$_) })
foreach ($asset in @($stagedModelLock.assets)) { $asset.target = Add-AppPrefix -RelativePath ([string]$asset.target) }
$stagedModelLock | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $stagedModelLockPath -Encoding UTF8

$stagedIntegrationPath = Join-Path $stagedBundle "integration.manifest.json"
$stagedIntegration = Get-Content -LiteralPath $stagedIntegrationPath -Raw | ConvertFrom-Json
$helperManifestEntry = $stagedIntegration.files.PSObject.Properties["tts_more/portable-python.ps1"]
if (!$helperManifestEntry -or ![string]::Equals([string]$helperManifestEntry.Value, (Get-CanonicalTextSha256 -Path $stagedPortablePython), [StringComparison]::OrdinalIgnoreCase)) {
    throw "portable-python.ps1 is missing from exact integration manifest coverage"
}
$stagedFiles = [ordered]@{}
foreach ($entry in $stagedIntegration.files.PSObject.Properties) {
    $relative = [string]$entry.Name
    if (!$relative.StartsWith("tts_more/", [StringComparison]::Ordinal)) { continue }
    $digest = [string]$entry.Value
    if ($relative -in @("tts_more/component.json", "tts_more/locks/models.lock.json")) {
        $digest = Get-CanonicalTextSha256 -Path (Join-Path $stageApp $relative.Replace("/", "\"))
    }
    $stagedFiles[$relative] = $digest
}
$stagedIntegration.files = [pscustomobject]$stagedFiles
$stagedIntegrationJson = ($stagedIntegration | ConvertTo-Json -Depth 12) + "`n"
[IO.File]::WriteAllText($stagedIntegrationPath, $stagedIntegrationJson, (New-Object Text.UTF8Encoding($false)))

if (Test-Path -LiteralPath (Join-Path $stageApp "LICENSE") -PathType Leaf) {
    Copy-Item -LiteralPath (Join-Path $stageApp "LICENSE") -Destination (Join-Path $stage "licenses\UPSTREAM-LICENSE") -Force
}
Copy-Item -LiteralPath (Join-Path $stagedBundle "LICENSE.integration") -Destination (Join-Path $stage "licenses\INTEGRATION-LICENSE") -Force
Copy-Item -LiteralPath (Join-Path $stagedBundle "NOTICE.integration") -Destination (Join-Path $stage "licenses\INTEGRATION-NOTICE") -Force
$modelDataDirectories = @("pretrained_models", "checkpoints", "SoVITS_weights", "GPT_weights")
$alwaysLocalModelDirectories = @("SoVITS_weights", "GPT_weights")
@(Get-ChildItem -LiteralPath $stageApp -Directory -Recurse -Force | Where-Object { $_.Name -in $alwaysLocalModelDirectories } | Sort-Object FullName -Descending) | ForEach-Object {
    $resolved = [System.IO.Path]::GetFullPath($_.FullName)
    if (!$resolved.StartsWith($stage, [StringComparison]::OrdinalIgnoreCase)) { throw "refusing to remove user model data outside package stage: $resolved" }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
if ($Profile -eq "Bootstrap") {
    $defaultModelDirectories = @("pretrained_models", "checkpoints")
    @(Get-ChildItem -LiteralPath $stageApp -Directory -Recurse -Force | Where-Object { $_.Name -in $defaultModelDirectories } | Sort-Object FullName -Descending) | ForEach-Object {
        $resolved = [System.IO.Path]::GetFullPath($_.FullName)
        if (!$resolved.StartsWith($stage, [StringComparison]::OrdinalIgnoreCase)) { throw "refusing to remove model data outside package stage: $resolved" }
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
    $embeddedModelFiles = @(Get-ChildItem -LiteralPath $stageApp -File -Recurse -Force | Where-Object {
        $_.Name -match "\.(safetensors|ckpt|pth|pt|t7|onnx|bin)$"
    })
    foreach ($embeddedModelFile in $embeddedModelFiles) {
        $resolved = [System.IO.Path]::GetFullPath($embeddedModelFile.FullName)
        if (!$resolved.StartsWith($stage, [StringComparison]::OrdinalIgnoreCase)) { throw "refusing to remove embedded model data outside package stage: $resolved" }
        Remove-Item -LiteralPath $resolved -Force
    }
}
@(Get-ChildItem -LiteralPath $stage -Directory -Recurse -Force | Where-Object { $_.Name -in @("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache") } | Sort-Object FullName -Descending) | ForEach-Object {
    $resolved = [System.IO.Path]::GetFullPath($_.FullName)
    if (!$resolved.StartsWith($stage, [StringComparison]::OrdinalIgnoreCase)) { throw "refusing to clean outside package stage: $resolved" }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}

$integrationManifest = Get-Content -LiteralPath (Join-Path $stagedBundle "integration.manifest.json") -Raw | ConvertFrom-Json
$integrationSha = Get-PortableFileSha256 -Path (Join-Path $stagedBundle "integration.manifest.json")
$deviceProfiles = if ($Device -eq "Auto") { @("auto", "cu128", "cu126", "cpu") } else { @($Device.ToLowerInvariant()) }
$capabilities = switch ([string]$config.component) {
    "gpt-sovits" { @("tts", "trained_weights_voice", "reference_audio_voice", "artifact-transfer") }
    "indextts" { @("tts", "reference_audio_voice", "emotion-text", "artifact-transfer") }
    "cosyvoice" { @("tts", "sft", "zero-shot", "cross-lingual", "instruct", "artifact-transfer") }
}
$manifest = [ordered]@{
    schema_version = 2; component = [string]$config.component
    package_id = [string]$config.component; release_version = $Version; version = $Version
    build_id = "$($config.component)-$Version-$($revision.Substring(0, 12))"; package_profile = $profileName
    platform = "windows-x64"; api_contract = "tts-more-v1"
    protocol = @{ name = "tts-more-v1"; version = "1.0"; controller_range = ">=0.2.0,<0.3.0" }
    source = @{ repository = [string]$config.upstream_repository; revision = $revision }
    integration = @{ version = [string]$integrationManifest.integration_version; source_revision = [string]$integrationManifest.source_revision; bundle_sha256 = $integrationSha }
    runtime = @{ python_version = [string]$config.python; device_profiles = @($deviceProfiles); lock = "app/tts_more/locks/runtime.lock.json"; state_path = "data/local/install-state.json" }
    models = @{ lock = "app/tts_more/locks/models.lock.json"; required = $true }
    data_root = "data/local"
    data = @{ user = "data/user"; local = "data/local"; cache = "data/cache"; operations = "data/local/operations" }
    launchers = @{ initialize = "Initialize.cmd"; start = "Start.cmd"; stop = "Stop.cmd"; repair = "Repair.cmd"; build = "Build-Package.ps1" }
    endpoint = @{ default_url = "http://127.0.0.1:$($config.port)"; port = [int]$config.port; health_path = "/health"; capabilities_path = "/capabilities"; bind_policy = "loopback" }
    capabilities = $capabilities; sha256_manifest = "SHA256SUMS.txt"; licenses = "licenses/THIRD_PARTY_NOTICES.json"
}
$manifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $stage "package\tts-more-package.json") -Encoding UTF8
@{ schema_version = 1; component = $config.component; integration_license = "Apache-2.0"; upstream_license = "app/LICENSE"; model_license = $modelLock.license; model_repository = $modelLock.upstream_repository; model_snapshot_revision = $modelLock.snapshot_revision } | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $stage "licenses\THIRD_PARTY_NOTICES.json") -Encoding UTF8

$resolvedProfile = ""
$runtimeCrossVolumeProbe = "not_run"
$isolatedUvCache = ""
if ($Profile -eq "Full") {
    $previousUvCache = $env:UV_CACHE_DIR
    $isolatedUvCache = Join-Path $work "uv-cache"
    try {
        $env:UV_CACHE_DIR = $isolatedUvCache
        & (Join-Path $stagedBundle "Initialize.ps1") -Device $Device -PackageRoot $stage
        if ($LASTEXITCODE -ne 0) { throw "full package initialization failed" }
    }
    finally {
        $env:UV_CACHE_DIR = $previousUvCache
        if (Test-Path -LiteralPath $isolatedUvCache) { Remove-Item -LiteralPath $isolatedUvCache -Recurse -Force }
    }
    Remove-WorkerFullRuntimeBytecode -PackageRoot $stage
    Assert-WorkerFullRuntimeBoundary -PackageRoot $stage
    $runtimeLockForProbe = Get-Content -LiteralPath (Join-Path $stagedBundle "locks\runtime.lock.json") -Raw | ConvertFrom-Json
    $runtimeCrossVolumeProbe = Test-WorkerFullRuntimeOnOtherVolume -PackageRoot $stage -ExpectedPython ([string]$runtimeLockForProbe.python_version) -ImportProbe ([string]$runtimeLockForProbe.import_probe)
    $statePath = Join-Path $stage "data\local\install-state.json"
    $profileOutput = @(& $buildPython (Join-Path $Bundle "portable_packages.py") resolve-full-profile --state $statePath --component ([string]$config.component) --build-id ([string]$manifest.build_id) --requested-profile $Device.ToLowerInvariant() 2>&1)
    $profileExit = $LASTEXITCODE
    if ($profileExit -ne 0 -or $profileOutput.Count -ne 1 -or [string]::IsNullOrWhiteSpace([string]$profileOutput[0])) {
        throw "requested device profile does not match resolved profile or install state is invalid"
    }
    $resolvedProfile = ([string]$profileOutput[0]).Trim().ToLowerInvariant()
    $manifest.runtime.device_profiles = @($resolvedProfile)
    $manifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $stage "package\tts-more-package.json") -Encoding UTF8
    $nameOutput = @(& $buildPython (Join-Path $Bundle "portable_packages.py") full-package-name --component ([string]$config.component) --version $Version --resolved-profile $resolvedProfile 2>&1)
    $nameExit = $LASTEXITCODE
    if ($nameExit -ne 0 -or $nameOutput.Count -ne 1 -or [string]::IsNullOrWhiteSpace([string]$nameOutput[0])) {
        throw "shared Full package naming rule failed for worker"
    }
    $packageName = ([string]$nameOutput[0]).Trim()
    if ($packageName.EndsWith(".zip", [StringComparison]::OrdinalIgnoreCase)) { $packageName = $packageName.Substring(0, $packageName.Length - 4) }
}
if ($Profile -eq "Bootstrap") {
    $forbidden = @(Get-ChildItem -LiteralPath $stage -Recurse -Force | Where-Object {
        $_.Name -eq ".git" -or ($_.PSIsContainer -and $_.Name -in $modelDataDirectories) -or $_.FullName -match "[\\/](\.venv|runtime[\\/]live|data[\\/](cache|local|models))([\\/]|$)" -or $_.Name -match "\.(safetensors|ckpt|pth|pt|t7|onnx|bin)$"
    })
    if ($forbidden.Count -gt 0) { throw "bootstrap audit found forbidden runtime/model asset: $($forbidden.FullName -join ', ')" }
    $lockedModelPaths = @($stagedModelLock.required_paths) + @($stagedModelLock.assets | ForEach-Object { $_.target })
    $forbiddenLockedAssets = @($lockedModelPaths | ForEach-Object {
        $candidate = Join-Path $stage ([string]$_).Replace("/", "\")
        if (Test-Path -LiteralPath $candidate) { Get-Item -LiteralPath $candidate -Force }
    })
    if ($forbiddenLockedAssets.Count -gt 0) { throw "bootstrap audit found locked model asset: $($forbiddenLockedAssets.FullName -join ', ')" }
}
$machinePrefixes = @($Root, $work, $stage, $workBase, $isolatedUvCache, [IO.Path]::GetTempPath(), $env:TEMP, $env:TMP, $env:USERPROFILE, "$($env:HOMEDRIVE)$($env:HOMEPATH)") | Where-Object { ![string]::IsNullOrWhiteSpace([string]$_) -and ([string]$_).Length -ge 3 } | Select-Object -Unique
$runtimeLiveRoot = [IO.Path]::GetFullPath((Join-Path $stage "runtime\live"))
$machinePathLeak = @(Get-ChildItem -LiteralPath $stage -Recurse -File | Where-Object { !$_.FullName.StartsWith($runtimeLiveRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -and $_.Length -lt 5MB } | Select-String -SimpleMatch -Pattern $machinePrefixes -ErrorAction SilentlyContinue)
$runtimePrefixLeak = @()
if ($Profile -eq "Full") { $runtimePrefixLeak = @(Get-ChildItem -LiteralPath $runtimeLiveRoot -Recurse -File -Force | Where-Object { Test-WorkerBinaryContainsMachinePrefix -Path $_.FullName -Prefixes $machinePrefixes }) }
if ($machinePathLeak.Count -gt 0) { throw "package contains a build-machine absolute path: $($machinePathLeak[0].Path)" }
if ($runtimePrefixLeak.Count -gt 0) { throw "machine-prefix audit found build-machine path data in worker runtime: $($runtimePrefixLeak[0].FullName)" }

$sumPath = Join-Path $stage "SHA256SUMS.txt"
@(Get-ChildItem -LiteralPath $stage -Recurse -File | Where-Object { $_.FullName -ne $sumPath } | Sort-Object FullName | ForEach-Object {
    $relative = $_.FullName.Substring($stage.Length).TrimStart("\", "/").Replace("\", "/")
    "$(Get-PortableFileSha256 -Path $_.FullName)  $relative"
}) | Set-Content -LiteralPath $sumPath -Encoding UTF8

& $buildPython (Join-Path $stagedBundle "portable_packages.py") validate-manifest --manifest (Join-Path $stage "package\tts-more-package.json") --package-root $stage
if ($LASTEXITCODE -ne 0) { throw "staged schema v2 manifest validation failed" }
& $buildPython (Join-Path $stagedBundle "portable_packages.py") verify-sha256 --package-root $stage
if ($LASTEXITCODE -ne 0) { throw "staged package SHA256SUMS exact coverage validation failed" }

New-Item -ItemType Directory -Force -Path $OutputRoot | Out-Null
$zip = Join-Path $OutputRoot "$packageName.zip"
& $buildPython (Join-Path $stagedBundle "portable_packages.py") create-zip --package-root $stage --output $zip --archive-root $packageName
if ($LASTEXITCODE -ne 0) { throw "ZIP64 package creation failed" }
if ($Profile -eq "Full") { Assert-WorkerFullArchiveBoundary -ArchivePath $zip }
$auditPassed = $false
if ($Profile -eq "Bootstrap") {
    & $buildPython (Join-Path $stagedBundle "portable_packages.py") audit-release --zip $zip
    if ($LASTEXITCODE -ne 0) { throw "GitHub bootstrap release audit failed" }
    $auditPassed = $true
}
$hash = Get-PortableFileSha256 -Path $zip
"$hash  $([IO.Path]::GetFileName($zip))" | Set-Content -LiteralPath "$zip.sha256" -Encoding ASCII
$provenance = [ordered]@{ component=$config.component; version=$Version; profile=$profileName; source_revision=$revision; integration_revision=$integrationManifest.source_revision; model_snapshot=$modelLock.snapshot_revision; sha256=$hash }
if ($Profile -eq "Full") { $provenance.resolved_profile = $resolvedProfile }
$provenance | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath "$zip.provenance.json" -Encoding UTF8

$lockProfile = if ($Profile -eq "Full") { $resolvedProfile } elseif ($Device -eq "Auto") { "cu128" } else { $Device.ToLowerInvariant() }
$selectedLock = Join-Path $Bundle "locks\requirements-$lockProfile.lock.txt"
$packages = @()
if (Test-Path -LiteralPath $selectedLock) {
    foreach ($line in Get-Content -LiteralPath $selectedLock) { if ($line -match "^([A-Za-z0-9_.-]+)==([^ \\]+)") { $spdxId = ($Matches[1] -replace '[^A-Za-z0-9.-]', '-'); $packages += @{ SPDXID="SPDXRef-Package-$spdxId"; name=$Matches[1]; versionInfo=$Matches[2]; downloadLocation="NOASSERTION"; filesAnalyzed=$false } } }
}
$deliveryResolvedProfile = if ($Profile -eq "Full") { $resolvedProfile } else { "none" }
$deliveryComment = "TTS-More delivery binding: component=$($config.component);version=$Version;profile=$profileName;resolved_profile=$deliveryResolvedProfile;source_revision=$revision;sha256=$hash"
@{ spdxVersion="SPDX-2.3"; dataLicense="CC0-1.0"; SPDXID="SPDXRef-DOCUMENT"; name=$packageName; documentNamespace="https://tts-more.local/spdx/$($config.component)/$Version/$hash"; comment=$deliveryComment; creationInfo=@{created=[DateTime]::UtcNow.ToString("o");creators=@("Tool: TTS-More-Build-Package-2.0.0")}; packages=$packages } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath "$zip.spdx.json" -Encoding UTF8
$licenseSidecar = Get-Content -LiteralPath (Join-Path $stage "licenses\THIRD_PARTY_NOTICES.json") -Raw | ConvertFrom-Json
$licenseDelivery = [ordered]@{ component=$config.component; version=$Version; profile=$profileName; source_revision=$revision; sha256=$hash }
if ($Profile -eq "Full") { $licenseDelivery.resolved_profile = $resolvedProfile }
$licenseSidecar | Add-Member -NotePropertyName delivery -NotePropertyValue $licenseDelivery -Force
$licenseSidecar | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath "$zip.licenses.json" -Encoding UTF8
$acceptance = [ordered]@{ schema_version=1; component=$config.component; version=$Version; profile=$profileName; source_revision=$revision; sha256=$hash; manifest_valid=$true; schema_audit=$true; path_audit=$true; sha256_manifest_audit=$true; bootstrap_audit=$auditPassed; machine_path_scan=$true; generated_at=[DateTime]::UtcNow.ToString("o") }
if ($Profile -eq "Full") {
    $acceptance.resolved_profile = $resolvedProfile
    $acceptance.runtime_reparse_scan = $true
    $acceptance.runtime_hardlink_scan = $true
    $acceptance.runtime_cross_volume_probe = $runtimeCrossVolumeProbe
}
$acceptance | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath "$zip.acceptance.json" -Encoding UTF8
Write-Host "Created $Profile package: $zip"
}
finally {
    try {
        if ($workCreated) {
            $workPathExists = Assert-PortableWorkPath -CandidatePath $work
            if (!$workPathExists) {
                throw "worker package cleanup path disappeared after creation; refusing path-based cleanup: $work"
            }
            $resolvedWork = [IO.Path]::GetFullPath($work)
            $resolvedWorkParent = [IO.Path]::GetFullPath((Split-Path -Parent $resolvedWork))
            $resolvedWorkLeaf = Split-Path -Leaf $resolvedWork
            if (![string]::Equals($resolvedWorkParent.TrimEnd("\", "/"), $workBase.TrimEnd("\", "/"), [StringComparison]::OrdinalIgnoreCase) -or $resolvedWorkLeaf -ne $workIdentity) {
                throw "refusing to clean a worker package staging directory that is not the unique directory created by this build: $resolvedWork"
            }
            $cleanupIdentity = [TtsMorePortableDirectoryHandle]::Identity($createdWorkHandle)
            if (![string]::Equals($cleanupIdentity, $createdWorkIdentity, [StringComparison]::Ordinal)) {
                throw "worker package staging handle identity changed unexpectedly: $resolvedWork"
            }
            foreach ($child in @(Get-ChildItem -LiteralPath $resolvedWork -Force)) {
                Remove-Item -LiteralPath $child.FullName -Recurse -Force
            }
            if (@(Get-ChildItem -LiteralPath $resolvedWork -Force).Count -ne 0) {
                throw "worker package staging directory is not empty after child cleanup; refusing recursive root deletion: $resolvedWork"
            }
            [TtsMorePortableDirectoryHandle]::MarkDirectoryForDeletion($createdWorkHandle)
            $createdWorkHandle.Dispose()
            $createdWorkHandle = $null
            $workCreated = $false
        }
    }
    finally {
        if ($createdWorkHandle -ne $null) { $createdWorkHandle.Dispose() }
        if ($workBaseHandle -ne $null) { $workBaseHandle.Dispose() }
    }
}
