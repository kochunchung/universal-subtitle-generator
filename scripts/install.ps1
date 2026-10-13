#Requires -Version 5.1
<#
  萬能字幕產生器 安裝程式(第 0 階段)
  - 不需要系統管理員權限,不修改系統設定或 PATH
  - 可重複執行:已完成的步驟會自動跳過
  - 所有東西都裝在程式資料夾內(python\ ffmpeg\ models\ tools\)
  由 install.bat 呼叫,也可直接執行:
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install.ps1 [-SkipModel] [-SkipSmokeTest]
#>
param(
    [switch]$SkipModel,
    [switch]$SkipSmokeTest
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # 關閉進度條,Invoke-WebRequest 會快很多
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

# ---------------------------------------------------------------- 路徑與設定
$Root      = Split-Path -Parent $PSScriptRoot
$PyDir     = Join-Path $Root 'python'
$Py        = Join-Path $PyDir 'python.exe'
$ToolsDir  = Join-Path $Root 'tools'
$Uv        = Join-Path $ToolsDir 'uv.exe'
$FfDir     = Join-Path $Root 'ffmpeg'
$ModelsDir = Join-Path $Root 'models'
$LogsDir   = Join-Path $Root 'logs'
$CacheDir  = Join-Path $Root '.cache'
$TempDir   = Join-Path $CacheDir 'tmp'
$LockFile  = Join-Path $Root 'requirements.lock'
$LogFile   = Join-Path $LogsDir 'install.log'

$PythonVersion  = '3.11'
$TorchVersion   = '2.5.1'
$TorchCuda      = 'cu121'
$MinDriver      = [version]'531.14'   # CUDA 12.1 在 Windows 上的最低驅動版本
$MinFreeGB      = 10
$DefaultModel   = 'openai/whisper-large-v3-turbo'

$UvUrl      = 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip'
$FfmpegUrls = @(
    'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip',
    'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip'
)

foreach ($d in @($ToolsDir, $LogsDir, $CacheDir, $TempDir, $ModelsDir)) {
    if (-not (Test-Path -LiteralPath $d)) { New-Item -ItemType Directory -Path $d | Out-Null }
}

# 讓 uv、Hugging Face、Python 都只使用程式資料夾內的位置
$env:UV_CACHE_DIR              = Join-Path $CacheDir 'uv'
$env:UV_PYTHON_INSTALL_DIR     = Join-Path $CacheDir 'pythons'
$env:UV_LINK_MODE              = 'copy'
$env:UV_NO_CONFIG              = '1'
$env:NO_COLOR                  = '1'
$env:HF_HOME                   = $ModelsDir
$env:HF_HUB_DISABLE_SYMLINKS_WARNING = '1'
$env:PYTHONUTF8                = '1'
$env:PYTHONIOENCODING          = 'utf-8'
$env:PYTHONNOUSERSITE          = '1'
$env:PATH                      = "$FfDir;$env:PATH"

# ---------------------------------------------------------------- 共用函式
# 日誌檔只開啟一次並允許共用讀寫。若每行都重新開檔寫入,防毒軟體掃描時會短暫鎖住檔案,
# 出現「資料流是不可讀取的」錯誤。
$script:LogWriter = $null
try {
    $fs = [System.IO.File]::Open($LogFile, [System.IO.FileMode]::Append,
                                 [System.IO.FileAccess]::Write, [System.IO.FileShare]::ReadWrite)
    $script:LogWriter = New-Object System.IO.StreamWriter($fs, (New-Object System.Text.UTF8Encoding($true)))
    $script:LogWriter.AutoFlush = $true
} catch {
    Write-Host "無法開啟日誌檔 $LogFile,本次安裝只顯示在畫面上:$($_.Exception.Message)" -ForegroundColor Yellow
}

function Write-LogLine([string]$Line) {
    if ($script:LogWriter) {
        try { $script:LogWriter.WriteLine($Line) } catch { }
    }
}

function Write-Log([string]$Message, [string]$Color = 'Gray') {
    $line = '[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Write-Host $line -ForegroundColor $Color
    Write-LogLine $line
}

function Write-Step([int]$No, [string]$Title) {
    Write-Host ''
    Write-Log ("===== 步驟 {0}/9:{1} =====" -f $No, $Title) 'Cyan'
}

# 執行外部程式:輸出同時顯示並寫入日誌,結束代碼不為 0 就丟出錯誤
function Invoke-Native([string]$What, [scriptblock]$Command) {
    Write-Log "  > $What"
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Command 2>&1 | ForEach-Object {
            $text = "$_"
            Write-Host "    $text"
            Write-LogLine "    $text"
        }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $old
    }
    if ($code -ne 0) { throw ("{0} 失敗(結束代碼 {1})" -f $What, $code) }
}

# 靜默執行外部程式,只回傳是否成功
function Test-Native([scriptblock]$Command) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $Command 2>&1 | Out-Null; return ($LASTEXITCODE -eq 0) }
    catch { return $false }
    finally { $ErrorActionPreference = $old }
}

function Get-File([string]$Url, [string]$OutFile) {
    for ($i = 1; $i -le 3; $i++) {
        try {
            Write-Log "  下載 $Url(第 $i 次)"
            Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing
            return
        } catch {
            Write-Log "  下載失敗:$($_.Exception.Message)" 'Yellow'
            if ($i -eq 3) { throw }
            Start-Sleep -Seconds (5 * $i)
        }
    }
}

# ---------------------------------------------------------------- 主流程
$started = Get-Date
Write-Log "萬能字幕產生器 安裝開始,程式資料夾:$Root" 'Green'

try {
    # 1. 顯示卡與驅動 ----------------------------------------------------
    Write-Step 1 '檢查 NVIDIA 顯示卡與驅動'
    $smi = Get-Command 'nvidia-smi' -ErrorAction SilentlyContinue
    $smiPath = $null
    if ($smi) { $smiPath = $smi.Source }
    elseif (Test-Path "$env:WINDIR\System32\nvidia-smi.exe") { $smiPath = "$env:WINDIR\System32\nvidia-smi.exe" }
    if (-not $smiPath) { throw '找不到 nvidia-smi,請確認已安裝 NVIDIA 顯示卡驅動。' }
    $gpuLine = (& $smiPath --query-gpu=name,driver_version,memory.total --format=csv,noheader,nounits | Select-Object -First 1)
    $gpu = $gpuLine.Split(',') | ForEach-Object { $_.Trim() }
    Write-Log ("  顯示卡:{0},驅動:{1},顯示卡記憶體:{2} MiB" -f $gpu[0], $gpu[1], $gpu[2])
    if ([version]$gpu[1] -lt $MinDriver) {
        throw ("驅動版本 {0} 太舊,CUDA 12.1 需要 {1} 以上,請先更新 NVIDIA 驅動。" -f $gpu[1], $MinDriver)
    }
    if ([int]$gpu[2] -lt 5500) { Write-Log '  注意:顯示卡記憶體低於 6 GB,執行時請選用較小的模型或批次。' 'Yellow' }

    # 2. 磁碟空間 ------------------------------------------------------
    Write-Step 2 '檢查磁碟空間'
    $drive = Get-PSDrive -Name $Root.Substring(0, 1)
    $freeGB = [math]::Round($drive.Free / 1GB, 1)
    Write-Log "  $($drive.Name): 槽剩餘 $freeGB GB"
    $alreadyHasTorch = (Test-Path $Py) -and (Test-Native { & $Py -c "import torch" })
    if ($freeGB -lt $MinFreeGB -and -not $alreadyHasTorch) {
        Write-Log "  剩餘空間低於 $MinFreeGB GB,安裝過程可能失敗。" 'Yellow'
        $ans = Read-Host '  仍要繼續嗎?(Y/N)'
        if ($ans -notmatch '^[Yy]') { throw '使用者取消安裝(磁碟空間不足)。' }
    }

    # 3. uv --------------------------------------------------------------
    Write-Step 3 '準備 uv(Python 套件管理工具)'
    if (Test-Path $Uv) {
        Write-Log '  已存在,略過下載。'
    } else {
        $zip = Join-Path $TempDir 'uv.zip'
        $ext = Join-Path $TempDir 'uv'
        Get-File $UvUrl $zip
        if (Test-Path $ext) { Remove-Item $ext -Recurse -Force }
        Expand-Archive -LiteralPath $zip -DestinationPath $ext -Force
        $found = Get-ChildItem -Path $ext -Recurse -Filter 'uv.exe' | Select-Object -First 1
        if (-not $found) { throw '解壓縮後找不到 uv.exe。' }
        Copy-Item $found.FullName $Uv -Force
    }
    Invoke-Native 'uv 版本' { & $Uv --version }

    # 4. 可攜式 Python ---------------------------------------------------
    Write-Step 4 "安裝可攜式 Python $PythonVersion"
    if ((Test-Path $Py) -and (Test-Native { & $Py -c "import sys; sys.exit(0 if sys.version_info[:2]==(3,11) else 1)" })) {
        Write-Log '  已存在,略過。'
    } else {
        Invoke-Native "下載 Python $PythonVersion" { & $Uv python install $PythonVersion --no-bin --no-registry }
        $src = Get-ChildItem -Path $env:UV_PYTHON_INSTALL_DIR -Directory |
               Where-Object { $_.Name -like 'cpython-3.11*windows*' } |
               Sort-Object Name -Descending | Select-Object -First 1
        if (-not $src) { throw '找不到 uv 安裝的 Python 3.11。' }
        if (Test-Path $PyDir) { Remove-Item $PyDir -Recurse -Force }
        Copy-Item -Path $src.FullName -Destination $PyDir -Recurse
        # 這份 Python 由本程式專用,移除「外部管理」標記,讓套件可以直接裝進去
        Get-ChildItem -Path $PyDir -Recurse -Filter 'EXTERNALLY-MANAGED' -ErrorAction SilentlyContinue |
            Remove-Item -Force -ErrorAction SilentlyContinue
        Remove-Item $env:UV_PYTHON_INSTALL_DIR -Recurse -Force -ErrorAction SilentlyContinue
    }
    Invoke-Native 'Python 版本' { & $Py -c "import sys; print(sys.version)" }

    # 5. PyTorch (CUDA 12.1) --------------------------------------------
    Write-Step 5 "安裝 PyTorch $TorchVersion($TorchCuda),約 2.5 GB,需要一些時間"
    $torchOk = Test-Native { & $Py -c "import torch, sys; sys.exit(0 if torch.__version__.startswith('$TorchVersion') and torch.version.cuda == '12.1' else 1)" }
    if ($torchOk) {
        Write-Log '  已安裝正確版本,略過。'
    } else {
        Invoke-Native '安裝 torch / torchaudio' {
            & $Uv pip install --python $Py --break-system-packages `
                --index-url "https://download.pytorch.org/whl/$TorchCuda" `
                --extra-index-url 'https://pypi.org/simple' --index-strategy unsafe-best-match `
                "torch==$TorchVersion+$TorchCuda" "torchaudio==$TorchVersion+$TorchCuda"
        }
    }

    # 6. 其他套件 ------------------------------------------------------
    Write-Step 6 '安裝其他套件(依 requirements.lock 鎖定版本)'
    if (-not (Test-Path $LockFile)) { throw "找不到 $LockFile" }
    Invoke-Native '安裝套件' {
        & $Uv pip install --python $Py --break-system-packages --no-deps -r $LockFile
    }

    # 7. ffmpeg ----------------------------------------------------------
    Write-Step 7 '準備 ffmpeg(免安裝版)'
    if ((Test-Path (Join-Path $FfDir 'ffmpeg.exe')) -and (Test-Path (Join-Path $FfDir 'ffprobe.exe'))) {
        Write-Log '  已存在,略過下載。'
    } else {
        $zip = Join-Path $TempDir 'ffmpeg.zip'
        $ext = Join-Path $TempDir 'ffmpeg'
        $ok = $false
        foreach ($url in $FfmpegUrls) {
            try { Get-File $url $zip; $ok = $true; break }
            catch { Write-Log "  改用下一個下載來源。" 'Yellow' }
        }
        if (-not $ok) { throw 'ffmpeg 下載失敗。' }
        Write-Log '  解壓縮中…'
        if (Test-Path $ext) { Remove-Item $ext -Recurse -Force }
        Expand-Archive -LiteralPath $zip -DestinationPath $ext -Force
        if (-not (Test-Path $FfDir)) { New-Item -ItemType Directory -Path $FfDir | Out-Null }
        foreach ($exe in @('ffmpeg.exe', 'ffprobe.exe')) {
            $f = Get-ChildItem -Path $ext -Recurse -Filter $exe | Select-Object -First 1
            if (-not $f) { throw "壓縮檔內找不到 $exe" }
            Copy-Item $f.FullName (Join-Path $FfDir $exe) -Force
        }
    }
    Invoke-Native 'ffmpeg 可執行' { & (Join-Path $FfDir 'ffprobe.exe') -hide_banner -loglevel error -version }

    # 8. 模型 ------------------------------------------------------------
    Write-Step 8 "下載轉錄模型 $DefaultModel(約 1.6 GB)"
    if ($SkipModel) {
        Write-Log '  依參數 -SkipModel 略過。' 'Yellow'
    } else {
        Push-Location $Root
        try { Invoke-Native '下載模型' { & $Py -m app.tools.download_model $DefaultModel } }
        finally { Pop-Location }
    }

    # 9. 環境檢查 --------------------------------------------------------
    Write-Step 9 '環境檢查'
    Push-Location $Root
    try {
        if ($SkipSmokeTest -or $SkipModel) {
            Invoke-Native '環境檢查' { & $Py -m app.envcheck }
        } else {
            Invoke-Native '環境檢查(含 GPU 推論測試)' { & $Py -m app.envcheck --smoke }
        }
    } finally { Pop-Location }

    # 清理下載暫存(保留 uv 與已安裝的東西)
    Remove-Item $TempDir -Recurse -Force -ErrorAction SilentlyContinue
    Test-Native { & $Uv cache clean } | Out-Null

    $mins = [math]::Round(((Get-Date) - $started).TotalMinutes, 1)
    Write-Host ''
    Write-Log "安裝完成,耗時 $mins 分鐘。請雙擊 start-desktop.bat 開啟程式。" 'Green'
    exit 0
}
catch {
    Write-Host ''
    Write-Log "安裝失敗:$($_.Exception.Message)" 'Red'
    Write-Log "完整紀錄在 $LogFile。修正問題後重新執行 install.bat,已完成的步驟會自動略過。" 'Red'
    exit 1
}
