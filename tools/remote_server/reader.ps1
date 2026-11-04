# Administrator-owned Windows reader. Run with -NoProfile -NonInteractive.
# Configuration must be protected by NTFS ACLs; the SSH account may not modify it.
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
try {
    $config = Get-Content -LiteralPath 'C:\ProgramData\BOW\reader.json' -Raw | ConvertFrom-Json
    $line = [Console]::ReadLine()
    if ($line.Length -gt 32768) { throw 'Request too large' }
    $req = $line | ConvertFrom-Json
    $op = $req.operation
    if ($op -eq 'capabilities') {
        @{success=$true; content_type='json'; data=@{protocol=1; operations=$config.allowed_operations; roots=$config.allowed_paths}} | ConvertTo-Json -Depth 8 -Compress
        exit 0
    }
    if ($config.allowed_operations -notcontains $op) { throw 'Operation disabled by server administrator' }
    $limit = 200
    if ($req.limit) { $limit = [int]$req.limit }
    $limit = [Math]::Min([Math]::Min($limit, 2000), [int]$config.max_lines)
    $budget = [Math]::Min([Math]::Min([int]$req.max_bytes, 1048576), [int]$config.max_output_bytes)
    if ($limit -lt 1 -or $budget -lt 1) { throw 'Invalid limits' }
    if ($op -eq 'system_status') {
        $os = Get-CimInstance Win32_OperatingSystem
        $data = @{caption=$os.Caption; last_boot=$os.LastBootUpTime.ToString('o'); free_memory_kb=$os.FreePhysicalMemory; total_memory_kb=$os.TotalVisibleMemorySize}
    } else {
        $path = [string]$req.path
        if ($path -notmatch '^[a-zA-Z]:\\' -or $path.Substring(2).Contains(':') -or $path -match '(^|[\\/])\.\.([\\/]|$)') { throw 'Absolute local path required; streams and traversal are prohibited' }
        $path = [IO.Path]::GetFullPath($path)
        $allowed = $false
        foreach ($root in $config.allowed_paths) {
            $root = [IO.Path]::GetFullPath($root).TrimEnd('\')
            if ($path.Equals($root, [StringComparison]::OrdinalIgnoreCase) -or $path.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) { $allowed=$true }
        }
        if (-not $allowed) { throw 'Path outside server-approved roots' }
        # All directories must be administrator-controlled. Reject junctions/symlinks.
        $item = Get-Item -LiteralPath $path -Force
        $current = $item
        while ($null -ne $current) {
            if (($current.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Reparse points are prohibited' }
            if ($current -is [IO.DirectoryInfo]) { $current = $current.Parent } else { $current = $current.Directory }
        }
        if ($op -eq 'list_directory') {
            if (-not $item.PSIsContainer) { throw 'Directory required' }
            $data = @(Get-ChildItem -LiteralPath $path -Force | Select-Object -First $limit | ForEach-Object { @{name=$_.Name; directory=$_.PSIsContainer; reparse=(($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)} })
        } elseif ($op -eq 'read_file' -or $op -eq 'search_file') {
            if ($item.PSIsContainer) { throw 'Regular file required' }
            $stream = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
            try {
                $size = $stream.Length
                $prefix = New-Object byte[] 2
                [void]$stream.Read($prefix, 0, 2)
                $encoding = [Text.Encoding]::UTF8
                if ($prefix[0] -eq 255 -and $prefix[1] -eq 254) { $encoding = [Text.Encoding]::Unicode }
                if ($prefix[0] -eq 254 -and $prefix[1] -eq 255) { $encoding = [Text.Encoding]::BigEndianUnicode }
                $tail = ($op -eq 'read_file' -and $req.tail -ne $false)
                $offset = 0
                if ($tail) { $offset = [Math]::Max(0, $size - $budget) }
                if ($encoding -ne [Text.Encoding]::UTF8 -and ($offset % 2) -ne 0) { $offset += 1 }
                [void]$stream.Seek($offset, [IO.SeekOrigin]::Begin)
                $buffer = New-Object byte[] $budget
                $count = $stream.Read($buffer, 0, $budget)
                $text = $encoding.GetString($buffer, 0, $count).TrimStart([char]0xFEFF)
            } finally { $stream.Dispose() }
            $lines = $text -split '\r?\n'
            if ($op -eq 'search_file') {
                $needle = [string]$req.text
                if ($needle.Length -lt 1 -or $needle.Length -gt 512) { throw 'Bounded literal text required' }
                $lines = @($lines | Where-Object { $_.Contains($needle) } | Select-Object -First $limit)
            } elseif ($tail) { $lines = @($lines | Select-Object -Last $limit) }
            else { $lines = @($lines | Select-Object -First $limit) }
            $data = @{text=($lines -join "`n"); file_bytes=$size; scanned_bytes=$count; byte_window_truncated=($size -gt $count)}
        } else { throw 'Unsupported operation' }
    }
    @{success=$true; content_type='json'; data=$data} | ConvertTo-Json -Depth 8 -Compress
} catch {
    @{success=$false; content_type='text'; error=$_.Exception.Message} | ConvertTo-Json -Compress
}
