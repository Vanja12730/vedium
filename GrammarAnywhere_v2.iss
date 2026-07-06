; ─────────────────────────────────────────────────────────────────────────────
;  GrammarAnywhere.iss  —  Inno Setup 6 installer script
;
;  Produces a single Setup.exe that:
;    1. Checks Python is installed and on PATH
;    2. Checks pip is available
;    3. Installs / upgrades llama-cpp-python (CPU or GPU build, user's choice)
;    4. Installs pystray, pillow, pynput via pip (with user confirmation)
;    5. Copies the refactored spell_daemon package + run.vbs to the install folder
;    6. Optionally downloads the recommended Gemma 3 GGUF model into the
;       install folder (with a live progress bar) and pre-selects it in config
;    7. Creates a Desktop shortcut that launches run.vbs
;    8. Registers the app in Add/Remove Programs with a clean uninstaller
;
;  HOW TO BUILD
;  ────────────
;    1. Download and install Inno Setup 6 from https://jrsoftware.org/isinfo.php
;    2. Place this file in the same folder as the spell_daemon package and run.vbs
;    3. Right-click GrammarAnywhere.iss → Compile  (or open in Inno Setup IDE)
;    4. The finished installer appears as Output\GrammarAnywhereSetup.exe
;
; ─────────────────────────────────────────────────────────────────────────────

#define AppName      "GrammarAnywhere"
#define AppVersion   "1.2"
#define AppPublisher "Your Name"
#define AppExeName   "run.vbs"

[Setup]
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisherURL=
AppSupportURL=
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\{#AppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=GrammarAnywhereSetup_v1.2
Compression=lzma
SolidCompression=yes
WizardStyle=modern
; Always prompt for install location — never silently reuse the previous path
UsePreviousAppDir=no
; Run as the current user — avoids PATH mismatch between user and admin accounts
PrivilegesRequired=lowest
; Minimum Windows version: Windows 10
MinVersion=10.0
; Show a friendly name in Add/Remove Programs
UninstallDisplayName={#AppName}

; ── Wizard appearance ────────────────────────────────────────────────────────
; Uncomment and set paths to your own images if you have them:
; WizardImageFile=installer_banner.bmp     ; 164x314 px sidebar image
; WizardSmallImageFile=installer_icon.bmp  ; 55x58 px top-right logo
; {#SourcePath} expands to the .iss file's own folder at compile time,
; so Inno Setup finds app.ico regardless of working directory.
; icon on Setup.exe in Explorer
SetupIconFile={#SourcePath}app.ico
; icon in Add/Remove Programs
UninstallDisplayIcon={app}\app.ico

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
; Core application files — copied to the install directory
Source: "spell_daemon\*"; DestDir: "{app}\spell_daemon"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "run.vbs";         DestDir: "{app}"; Flags: ignoreversion
; App icon — generate once before building: python make_ico.py
Source: "{#SourcePath}app.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; Desktop shortcut — wscript.exe runs run.vbs silently (no console window)
Name: "{userdesktop}\{#AppName}";  \
      Filename: "wscript.exe";       \
      Parameters: """{app}\{#AppExeName}"""; \
      WorkingDir: "{app}";             \
      IconFilename: "{app}\app.ico";   \
      Comment: "Start GrammarAnywhere background spell-checker"

[Code]
// ─────────────────────────────────────────────────────────────────────────────
//  Pascal script — runs checks and pip installs during the install wizard.
//  Inno Setup's [Code] section uses a subset of Object Pascal.
//
//  We locate python.exe by its full path rather than relying on PATH
//  inheritance, which is unreliable when Inno Setup spawns child processes.
// ─────────────────────────────────────────────────────────────────────────────

// Recommended local model — Gemma 3 4B instruct, Q4_K_M quant (~2.49 GB).
// Gemma 3 is used (NOT Gemma 4): Gemma 4's architecture is not supported by the
// pinned llama-cpp-python builds (CPU v0.3.19 / GPU v0.3.16) this installer ships.
// This repo is public and ungated, so a direct urllib download needs no login.
const
  GEMMA3_FILENAME = 'gemma-3-4b-it-Q4_K_M.gguf';
  GEMMA3_URL =
    'https://huggingface.co/unsloth/gemma-3-4b-it-GGUF/resolve/main/' +
    'gemma-3-4b-it-Q4_K_M.gguf';

  // Portable Python 3.12 embeddable — downloaded only when no system Python is found.
  // 3.12 is chosen to match the GPU wheel (cp312) so CPU and GPU paths both work.
  PYTHON_EMBED_URL = 'https://www.python.org/ftp/python/3.12.9/python-3.12.9-embed-amd64.zip';
  PYTHON_EMBED_ZIP = 'python-3.12.9-embed-amd64.zip';
  GET_PIP_URL      = 'https://bootstrap.pypa.io/get-pip.py';

var
  GPythonExe:        String;  // full path to python.exe, set by FindPython()
  GInstalledSummary: String;  // running log of already-present packages/features;
                               // shown in the final completion dialog
  GAVX512Supported:  Boolean; // True if the CPU supports AVX-512; set by DetectAVX512()
  GModelDownloaded:  Boolean; // True if the Gemma 3 model was downloaded into {app}
  GModelPath:        String;  // full path to the downloaded .gguf (if any)
  GUsingEmbedded:    Boolean; // True when portable Python was downloaded by the installer
  GEmbedDir:         String;  // staging dir for embedded Python ({tmp}\python_embed)


// ── Detect AVX-512 CPU support ────────────────────────────────────────────────
// Uses Windows' IsProcessorFeaturePresent(41) — PF_AVX512F_INSTRUCTIONS_AVAILABLE.
// Requires no Python packages; the kernel32 call is available on all Windows 10+.
// Result is stored in GAVX512Supported so CheckLlamaCpp() can pick the right wheel.
//
// AVX-512 → install latest llama-cpp-python (supports Qwen3, Gemma 3, all models)
// AVX2 only (Ryzen 5000/7000, older Intel) → install v0.3.19 (stable, avoids crash)
procedure DetectAVX512();
var
  TmpFile: String;
  Lines:   TArrayOfString;
  Code:    Integer;
  PySnip:  String;
begin
  GAVX512Supported := False;  // safe default — assume AVX2-only if detection fails

  TmpFile := ExpandConstant('{tmp}\ga_avx512.txt');

  // PF_AVX512F_INSTRUCTIONS_AVAILABLE = 41
  // IsProcessorFeaturePresent returns nonzero BOOL if the feature is present.
  PySnip := '-c "import ctypes; ' +
            'r = ctypes.windll.kernel32.IsProcessorFeaturePresent(41); ' +
            'print(1 if r else 0)"';

  Exec('cmd.exe',
       '/c "' + GPythonExe + '" ' + PySnip + ' > "' + TmpFile + '" 2>&1',
       '', SW_HIDE, ewWaitUntilTerminated, Code);

  if LoadStringsFromFile(TmpFile, Lines) and (GetArrayLength(Lines) > 0) then
    GAVX512Supported := (Trim(Lines[0]) = '1');
end;


// ── Locate python.exe ────────────────────────────────────────────────────────
// Tries 'where python' first (respects PATH), then falls back to the standard
// per-user and system install locations for Python 3.10 through 3.13.
function FindPython(): String;
var
  TmpFile: String;
  Lines: TArrayOfString;
  ResultCode, I: Integer;
  Candidate: String;
  Searches: TArrayOfString;
begin
  Result := '';

  // ── Strategy 1: ask Windows where python is ───────────────────────────────
  TmpFile := ExpandConstant('{tmp}\ga_where.txt');
  Exec('cmd.exe', '/c where python > "' + TmpFile + '" 2>&1',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  if ResultCode = 0 then
  begin
    if LoadStringsFromFile(TmpFile, Lines) then
      for I := 0 to GetArrayLength(Lines) - 1 do
      begin
        Candidate := Trim(Lines[I]);
        if (Candidate <> '') and FileExists(Candidate) then
        begin
          Result := Candidate;
          Exit;
        end;
      end;
  end;

  // ── Strategy 2: check well-known install paths ────────────────────────────
  SetArrayLength(Searches, 16);
  Searches[0]  := ExpandConstant('{localappdata}') + '\Programs\Python\Python313\python.exe';
  Searches[1]  := ExpandConstant('{localappdata}') + '\Programs\Python\Python312\python.exe';
  Searches[2]  := ExpandConstant('{localappdata}') + '\Programs\Python\Python311\python.exe';
  Searches[3]  := ExpandConstant('{localappdata}') + '\Programs\Python\Python310\python.exe';
  Searches[4]  := 'C:\Python313\python.exe';
  Searches[5]  := 'C:\Python312\python.exe';
  Searches[6]  := 'C:\Python311\python.exe';
  Searches[7]  := 'C:\Python310\python.exe';
  Searches[8]  := ExpandConstant('{pf}') + '\Python313\python.exe';
  Searches[9]  := ExpandConstant('{pf}') + '\Python312\python.exe';
  Searches[10] := ExpandConstant('{pf}') + '\Python311\python.exe';
  Searches[11] := ExpandConstant('{pf}') + '\Python310\python.exe';
  Searches[12] := ExpandConstant('{pf32}') + '\Python313\python.exe';
  Searches[13] := ExpandConstant('{pf32}') + '\Python312\python.exe';
  Searches[14] := ExpandConstant('{pf32}') + '\Python311\python.exe';
  Searches[15] := ExpandConstant('{pf32}') + '\Python310\python.exe';

  for I := 0 to GetArrayLength(Searches) - 1 do
    if FileExists(Searches[I]) then
    begin
      Result := Searches[I];
      Exit;
    end;
end;


// ── Download and set up a portable Python ────────────────────────────────────
// Called when no system Python is found.  Downloads the embeddable package,
// extracts it, enables site-packages, and bootstraps pip — all automatically.
// On success sets GPythonExe and GUsingEmbedded; returns True.
function SetupEmbeddedPython(): Boolean;
var
  ZipFile, PipFile, PthFile: String;
  Code: Integer;
  Form: TForm;
  Lbl: TLabel;
begin
  Result := False;
  GEmbedDir := ExpandConstant('{tmp}\python_embed');
  ZipFile   := ExpandConstant('{tmp}\') + PYTHON_EMBED_ZIP;
  PipFile   := ExpandConstant('{tmp}\get-pip.py');

  Form := TForm.Create(nil);
  try
    Form.Caption      := 'GrammarAnywhere — Setting up Python';
    Form.ClientWidth  := ScaleX(460);
    Form.ClientHeight := ScaleY(60);
    Form.Position     := poScreenCenter;
    Form.BorderStyle  := bsDialog;

    Lbl := TLabel.Create(Form);
    Lbl.Parent   := Form;
    Lbl.Left     := ScaleX(16);
    Lbl.Top      := ScaleY(20);
    Lbl.Width    := Form.ClientWidth - ScaleX(32);
    Lbl.AutoSize := False;
    Lbl.Caption  := 'Downloading portable Python 3.12...';
    Form.Show;
    Form.Refresh;

    // Step 1: Download the embeddable zip (~11 MB)
    Exec('powershell.exe',
         '-ExecutionPolicy Bypass -NoProfile -Command "' +
         '[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; ' +
         'Invoke-WebRequest -Uri ''''' + PYTHON_EMBED_URL + ''''' -OutFile ''''' + ZipFile + '''''"',
         '', SW_HIDE, ewWaitUntilTerminated, Code);

    if (Code <> 0) or not FileExists(ZipFile) then
    begin
      MsgBox(
        'Could not download portable Python.' + #13#10 + #13#10 +
        'Please check your internet connection and try again,' + #13#10 +
        'or install Python manually from https://www.python.org/downloads/',
        mbError, MB_OK);
      Exit;
    end;

    // Step 2: Extract
    Lbl.Caption := 'Extracting Python...';
    Form.Refresh;

    ForceDirectories(GEmbedDir);
    Exec('powershell.exe',
         '-ExecutionPolicy Bypass -NoProfile -Command "' +
         'Expand-Archive -Path ''''' + ZipFile + ''''' -DestinationPath ''''' + GEmbedDir + ''''' -Force"',
         '', SW_HIDE, ewWaitUntilTerminated, Code);

    if (Code <> 0) or not FileExists(GEmbedDir + '\python.exe') then
    begin
      MsgBox('Could not extract portable Python. Setup cannot continue.',
             mbError, MB_OK);
      Exit;
    end;

    // Step 3: Enable site-packages — the embeddable ships with
    // '#import site' commented out in python312._pth; rewrite it enabled.
    Lbl.Caption := 'Configuring Python...';
    Form.Refresh;

    PthFile := GEmbedDir + '\python312._pth';
    if FileExists(PthFile) then
    begin
      DeleteFile(PthFile);
      SaveStringToFile(PthFile,
        'python312.zip' + #13#10 +
        '.' + #13#10 +
        'Lib\site-packages' + #13#10 +
        'import site' + #13#10, False);
    end;

    // Step 4: Download get-pip.py
    Lbl.Caption := 'Downloading pip...';
    Form.Refresh;

    Exec('powershell.exe',
         '-ExecutionPolicy Bypass -NoProfile -Command "' +
         '[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; ' +
         'Invoke-WebRequest -Uri ''''' + GET_PIP_URL + ''''' -OutFile ''''' + PipFile + '''''"',
         '', SW_HIDE, ewWaitUntilTerminated, Code);

    if (Code <> 0) or not FileExists(PipFile) then
    begin
      MsgBox('Could not download pip. Setup cannot continue.', mbError, MB_OK);
      Exit;
    end;

    // Step 5: Bootstrap pip into the portable Python
    Lbl.Caption := 'Installing pip...';
    Form.Refresh;

    Exec(GEmbedDir + '\python.exe',
         '"' + PipFile + '" --no-warn-script-location',
         '', SW_HIDE, ewWaitUntilTerminated, Code);

    if Code <> 0 then
    begin
      MsgBox('Could not install pip into portable Python.', mbError, MB_OK);
      Exit;
    end;

    // Done — point all subsequent pip installs at this Python.
    GPythonExe     := GEmbedDir + '\python.exe';
    GUsingEmbedded := True;
    Result         := True;

    Lbl.Caption := 'Portable Python is ready.';
    Form.Refresh;
    Sleep(600);

  finally
    Form.Free;
  end;
end;


// ── Run python.exe with given args; return True if exit code = 0 ─────────────
// Uses the full path stored in GPythonExe — no PATH dependency.
function PyRun(const Args: String): Boolean;
var
  ResultCode: Integer;
begin
  Exec(GPythonExe, Args, '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := (ResultCode = 0);
end;


// ── CHECK 1: Python ──────────────────────────────────────────────────────────
// Returns True if a system Python was found; False triggers the embedded
// Python download in PrepareToInstall (no user-facing error shown here).
function CheckPython(): Boolean;
begin
  GPythonExe := FindPython();
  Result := (GPythonExe <> '') and FileExists(GPythonExe);
end;


// ── CHECK 2: pip ─────────────────────────────────────────────────────────────
function CheckPip(): Boolean;
begin
  Result := PyRun('-m pip --version');
  if not Result then
    MsgBox(
      'pip was not found.' + #13#10 + #13#10 +
      'pip is normally included with Python. To restore it, open' + #13#10 +
      'a Command Prompt and run:' + #13#10 + #13#10 +
      '  python -m ensurepip --upgrade' + #13#10 + #13#10 +
      'Then run this installer again.',
      mbError, MB_OK);
end;


// ── Install packages one at a time with a real progress bar ──────────────────
// Uses TNewProgressBar (available in Inno Setup) stepped once per package, plus
// two labels:
//   LblPkg  - "Installing pillow  (2 / 3)..."  updated before each pip call
//   LblDone - running tick list, e.g. "OK: pystray   OK: pillow   FAIL: pynput"
// Any packages that fail are collected and shown in an error dialog at the end.
procedure InstallWithProgress(const Packages: TArrayOfString;
                              const GroupTitle: String);
var
  Form:      TForm;
  LblPkg:    TLabel;
  LblDone:   TLabel;
  Bar:       TNewProgressBar;
  I, Code:   Integer;
  Total:     Integer;
  Failed:    String;
  DoneLine:  String;
begin
  Total := GetArrayLength(Packages);
  if Total = 0 then Exit;

  Failed   := '';
  DoneLine := '';

  Form := TForm.Create(nil);
  try
    Form.Caption      := 'GrammarAnywhere - ' + GroupTitle;
    Form.ClientWidth  := ScaleX(480);
    Form.ClientHeight := ScaleY(110);
    Form.Position     := poScreenCenter;
    Form.BorderStyle  := bsDialog;

    // Current package — updated BEFORE each pip call so the user sees what
    // is being fetched while pip is running.
    LblPkg := TLabel.Create(Form);
    LblPkg.Parent   := Form;
    LblPkg.Left     := ScaleX(16);
    LblPkg.Top      := ScaleY(14);
    LblPkg.Width    := Form.ClientWidth - ScaleX(32);
    LblPkg.AutoSize := False;
    LblPkg.Caption  := 'Preparing...';

    // Real progress bar — stepped once per completed package.
    Bar := TNewProgressBar.Create(Form);
    Bar.Parent   := Form;
    Bar.Left     := ScaleX(16);
    Bar.Top      := ScaleY(38);
    Bar.Width    := Form.ClientWidth - ScaleX(32);
    Bar.Height   := ScaleY(18);
    Bar.Min      := 0;
    Bar.Max      := Total;
    Bar.Position := 0;

    // Completed packages — grows as each one finishes.
    LblDone := TLabel.Create(Form);
    LblDone.Parent   := Form;
    LblDone.Left     := ScaleX(16);
    LblDone.Top      := ScaleY(66);
    LblDone.Width    := Form.ClientWidth - ScaleX(32);
    LblDone.AutoSize := False;
    LblDone.Caption  := '';

    Form.Show;

    for I := 0 to Total - 1 do
    begin
      LblPkg.Caption :=
        'Installing  ' + Packages[I] +
        '  (' + IntToStr(I + 1) + ' / ' + IntToStr(Total) + ')...';
      LblPkg.Refresh;

      Exec(GPythonExe,
           '-m pip install --quiet --disable-pip-version-check ' + Packages[I],
           '', SW_HIDE, ewWaitUntilTerminated, Code);

      if Code = 0 then
        DoneLine := DoneLine + 'OK: ' + Packages[I] + '   '
      else begin
        DoneLine := DoneLine + 'FAIL: ' + Packages[I] + '   ';
        Failed   := Failed + '  ' + Packages[I] + #13#10;
      end;

      Bar.Position    := I + 1;
      LblDone.Caption := DoneLine;
      Form.Refresh;
    end;

    if Failed = '' then
      LblPkg.Caption := 'All packages installed successfully.'
    else
      LblPkg.Caption := 'Finished - some packages could not be installed.';

    Sleep(1200);

    // Show error dialog while the form is still alive so the MsgBox
    // appears on top of it, then the form closes in the finally block.
    if Failed <> '' then
      MsgBox(
        'The following packages could not be installed:' + #13#10 + #13#10 +
        Failed + #13#10 +
        'You can retry manually in a Command Prompt:' + #13#10 +
        '  pip install ' + Failed,
        mbError, MB_OK);

  finally
    Form.Free;
  end;
end;


// ── CHECK 3: llama-cpp-python ────────────────────────────────────────────────
// Radio-button dialog lets the user choose CPU (latest) or GPU (v0.3.16).
// If already installed, shows the current version and offers to upgrade.
// CPU path: upgrades via the official abetlen CPU wheel index.
// GPU path: downloads the 226 MB community CUDA 12.8 wheel via urllib then pip.
procedure CheckLlamaCpp();
var
  AlreadyInstalled: Boolean;
  CurrentVer:       String;
  TmpVerFile:       String;
  Lines:            TArrayOfString;
  Form:             TForm;
  Lbl:              TLabel;
  LblCPU:           TLabel;
  LblGPU:           TLabel;
  RbCPU:            TRadioButton;
  RbGPU:            TRadioButton;
  RbSkip:           TRadioButton;
  BtnOK:            TNewButton;
  Margin:           Integer;
  ContentW:         Integer;
  Y, Code:          Integer;
  PkgArray:         TArrayOfString;
  WheelUrl:         String;
  WheelFile:        String;
  DownloadArgs:     String;
begin
  // ── Detect current state ──────────────────────────────────────────────────
  AlreadyInstalled := PyRun('-c "import llama_cpp"');
  CurrentVer := '';
  if AlreadyInstalled then
  begin
    TmpVerFile := ExpandConstant('{tmp}\llama_ver.txt');
    Exec('cmd.exe',
         '/c "' + GPythonExe + '" -c "import llama_cpp; print(llama_cpp.__version__)" > "' +
         TmpVerFile + '" 2>&1',
         '', SW_HIDE, ewWaitUntilTerminated, Code);
    if LoadStringsFromFile(TmpVerFile, Lines) and (GetArrayLength(Lines) > 0) then
      CurrentVer := Trim(Lines[0]);
  end;

  // ── Build choice dialog ───────────────────────────────────────────────────
  // All coordinates are wrapped in ScaleX/ScaleY so the layout grows with the
  // dialog font on high-DPI displays.  Radio buttons cannot word-wrap, so each
  // option has a SHORT one-line radio caption plus a separate auto-sizing,
  // word-wrapped description label beneath it.
  Form := TForm.Create(nil);
  try
    Form.Caption     := 'GrammarAnywhere — AI Engine';
    Form.ClientWidth := ScaleX(600);
    Form.Position    := poScreenCenter;
    Form.BorderStyle := bsDialog;

    Margin   := ScaleX(20);
    ContentW := Form.ClientWidth - (Margin * 2);
    Y        := ScaleY(16);

    // ── Intro paragraph (auto-height wrapped label) ──────────────────────────
    Lbl := TLabel.Create(Form);
    Lbl.Parent   := Form;
    Lbl.Left     := Margin;
    Lbl.Top      := Y;
    Lbl.Width    := ContentW;
    Lbl.WordWrap := True;
    Lbl.AutoSize := True;
    if AlreadyInstalled then
      Lbl.Caption :=
        'llama-cpp-python ' + CurrentVer + ' is already installed. Upgrading to the ' +
        'latest CPU build is recommended — it fixes a known GPU-fallback error ' +
        '(AttributeError: sampler) on machines without a compatible NVIDIA GPU.'
    else
      Lbl.Caption :=
        'llama-cpp-python is the engine that runs the local AI model. ' +
        'It is not installed yet. Please choose a build:';
    Y := Lbl.Top + Lbl.Height + ScaleY(14);

    // ── CPU option ───────────────────────────────────────────────────────────
    RbCPU := TRadioButton.Create(Form);
    RbCPU.Parent  := Form;
    RbCPU.Left    := Margin;
    RbCPU.Top     := Y;
    RbCPU.Width   := ContentW;
    RbCPU.Height  := ScaleY(20);
    if GAVX512Supported then
      RbCPU.Caption := 'CPU build — latest (AVX-512)     [recommended]'
    else
      RbCPU.Caption := 'CPU build — v0.3.19 (AVX2 compatible)     [recommended]';
    RbCPU.Checked := True;
    Y := RbCPU.Top + RbCPU.Height + ScaleY(2);

    LblCPU := TLabel.Create(Form);
    LblCPU.Parent   := Form;
    LblCPU.Left     := Margin + ScaleX(22);
    LblCPU.Top      := Y;
    LblCPU.Width    := ContentW - ScaleX(22);
    LblCPU.WordWrap := True;
    LblCPU.AutoSize := True;
    if GAVX512Supported then
      LblCPU.Caption :=
        'Installs the newest version. Supports Gemma 3, Qwen3, and most models. ' +
        'AVX-512 detected — full feature set available.'
    else
      LblCPU.Caption :=
        'Your CPU supports AVX2 but not AVX-512. This build runs stably on your ' +
        'hardware. Supports Qwen3 and most models; GPU offload still works.';
    Y := LblCPU.Top + LblCPU.Height + ScaleY(14);

    // ── GPU option ───────────────────────────────────────────────────────────
    RbGPU := TRadioButton.Create(Form);
    RbGPU.Parent  := Form;
    RbGPU.Left    := Margin;
    RbGPU.Top     := Y;
    RbGPU.Width   := ContentW;
    RbGPU.Height  := ScaleY(20);
    RbGPU.Caption := 'GPU build — v0.3.16 (NVIDIA only)';
    Y := RbGPU.Top + RbGPU.Height + ScaleY(2);

    LblGPU := TLabel.Create(Form);
    LblGPU.Parent   := Form;
    LblGPU.Left     := Margin + ScaleX(22);
    LblGPU.Top      := Y;
    LblGPU.Width    := ContentW - ScaleX(22);
    LblGPU.WordWrap := True;
    LblGPU.AutoSize := True;
    LblGPU.Caption  :=
      'Requires an NVIDIA GPU + CUDA 12.8. Faster inference. Supports Qwen3 and ' +
      'Gemma 3. Requires Python 3.12. Downloads a 226 MB wheel — a console window ' +
      'will show progress.';
    Y := LblGPU.Top + LblGPU.Height + ScaleY(14);

    // ── Skip option ──────────────────────────────────────────────────────────
    RbSkip := TRadioButton.Create(Form);
    RbSkip.Parent  := Form;
    RbSkip.Left    := Margin;
    RbSkip.Top     := Y;
    RbSkip.Width   := ContentW;
    RbSkip.Height  := ScaleY(20);
    RbSkip.Caption := 'Skip — I will install / update manually later';
    Y := RbSkip.Top + RbSkip.Height + ScaleY(18);

    // ── Continue button (centred) ────────────────────────────────────────────
    BtnOK := TNewButton.Create(Form);
    BtnOK.Parent      := Form;
    BtnOK.Caption     := 'Continue';
    BtnOK.Width       := ScaleX(110);
    BtnOK.Height      := ScaleY(30);
    BtnOK.Left        := (Form.ClientWidth - BtnOK.Width) div 2;
    BtnOK.Top         := Y;
    BtnOK.ModalResult := mrOK;
    BtnOK.Default     := True;
    Y := BtnOK.Top + BtnOK.Height + ScaleY(16);
    Form.ClientHeight := Y;

    if Form.ShowModal <> mrOK then Exit;

    // ── CPU path — pick version based on detected CPU instruction set ────────
    // AVX-512 CPUs (most Intel 12th gen+, Xeon): install latest from cpu index.
    // AVX2-only CPUs (all AMD Ryzen, older Intel): pin to v0.3.19 — the latest
    // wheels on the cpu index are compiled with AVX-512 and crash with
    // WinError 0xC000001D (STATUS_ILLEGAL_INSTRUCTION) on AVX2-only machines.
    // --only-binary prevents pip from trying to compile from source (needs MSVC).
    if RbCPU.Checked then
    begin
      SetArrayLength(PkgArray, 1);
      if GAVX512Supported then
      begin
        PkgArray[0] := '--upgrade --only-binary llama-cpp-python ' +
                       '--extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu ' +
                       'llama-cpp-python';
        InstallWithProgress(PkgArray, 'Installing AI engine — CPU build (latest, AVX-512)');
        GInstalledSummary :=
          GInstalledSummary +
          '  ' + #$2713 + '  llama-cpp-python    (CPU build — latest, AVX-512 optimised)' + #13#10;
      end
      else
      begin
        PkgArray[0] := '--only-binary llama-cpp-python ' +
                       '--extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu ' +
                       'llama-cpp-python==0.3.19';
        InstallWithProgress(PkgArray, 'Installing AI engine — CPU build v0.3.19 (AVX2)');
        GInstalledSummary :=
          GInstalledSummary +
          '  ' + #$2713 + '  llama-cpp-python    (CPU build v0.3.19 — AVX2 compatible)' + #13#10;
      end;
    end

    // ── GPU path — download prebuilt CUDA 12.8 wheel then pip-install it ─────
    else if RbGPU.Checked then
    begin
      WheelUrl  := 'https://huggingface.co/boneylizardwizard/' +
                   'llama_cpp_python-0.3.16-cp312-cp312-win_amd64/resolve/main/' +
                   'llama_cpp_python-0.3.16-cp312-cp312-win_amd64.whl';
      WheelFile := ExpandConstant('{tmp}') + '\llama_cpp_python-0.3.16-cp312-cp312-win_amd64.whl';

      MsgBox(
        'The GPU wheel will now be downloaded (~226 MB).' + #13#10 + #13#10 +
        'A console window will open showing download progress.' + #13#10 +
        'Do NOT close it — the installer continues when the download is done.' + #13#10 + #13#10 +
        'Click OK to begin.',
        mbInformation, MB_OK);

      DownloadArgs := '-c "import urllib.request; ' +
                     'print(chr(68)+chr(111)+chr(119)+chr(110)+chr(108)+chr(111)+chr(97)+chr(100)+chr(105)+chr(110)+chr(103)+chr(46)+chr(46)+chr(46)); ' +
                     'urllib.request.urlretrieve(r""' + WheelUrl + '"", r""' + WheelFile + '"")"';

      Exec(GPythonExe, DownloadArgs, '', SW_SHOW, ewWaitUntilTerminated, Code);

      if (Code <> 0) or not FileExists(WheelFile) then
      begin
        MsgBox(
          'Download failed.' + #13#10 + #13#10 +
          'You can install the GPU build manually after setup.' + #13#10 +
          'See the GPU install guide for the full wheel URL and instructions.',
          mbError, MB_OK);
        Exit;
      end;

      if AlreadyInstalled then
        PyRun('-m pip uninstall llama-cpp-python -y');

      SetArrayLength(PkgArray, 1);
      PkgArray[0] := WheelFile;
      InstallWithProgress(PkgArray, 'Installing AI engine — GPU v0.3.16');
      GInstalledSummary :=
        GInstalledSummary +
        '  ' + #$2713 + '  llama-cpp-python    (GPU build v0.3.16 — set GPU to Auto in Settings)' + #13#10;
    end

    // ── Skip ─────────────────────────────────────────────────────────────────
    else
    begin
      if GAVX512Supported then
        MsgBox(
          'Skipped. The grammar engine will not work until llama-cpp-python is installed.' + #13#10 + #13#10 +
          'Your CPU supports AVX-512. Install the latest build any time:' + #13#10 +
          '  pip install --upgrade --only-binary llama-cpp-python' + #13#10 +
          '      --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu' + #13#10 +
          '      llama-cpp-python' + #13#10 + #13#10 +
          'GPU build (NVIDIA + CUDA 12.8 only): see the GPU install guide.',
          mbInformation, MB_OK)
      else
        MsgBox(
          'Skipped. The grammar engine will not work until llama-cpp-python is installed.' + #13#10 + #13#10 +
          'Your CPU supports AVX2 only. Install the compatible build any time:' + #13#10 +
          '  pip install --only-binary llama-cpp-python' + #13#10 +
          '      --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu' + #13#10 +
          '      llama-cpp-python==0.3.19' + #13#10 + #13#10 +
          'GPU build (NVIDIA + CUDA 12.8 only): see the GPU install guide.',
          mbInformation, MB_OK);
    end;

  finally
    Form.Free;
  end;
end;


// ── CHECK 4: pystray / pillow / pynput ───────────────────────────────────────
// Shows two lists: what is already installed (✓) and what needs to be added (•).
// Only prompts and installs if something is actually missing.
procedure InstallUIDeps();
var
  MissingPkgs: TArrayOfString;
  Present:     String;
  MissingStr:  String;
  MsgText:     String;
  Answer:      Integer;
  Count:       Integer;
begin
  Present    := '';
  MissingStr := '';
  Count      := 0;
  SetArrayLength(MissingPkgs, 3);

  if PyRun('-c "import pystray"') then
    Present := Present + '  ✓  pystray' + #13#10
  else begin
    MissingPkgs[Count] := 'pystray'; Inc(Count);
    MissingStr := MissingStr + '  •  pystray' + #13#10;
  end;

  if PyRun('-c "from PIL import Image"') then
    Present := Present + '  ✓  pillow' + #13#10
  else begin
    MissingPkgs[Count] := 'pillow'; Inc(Count);
    MissingStr := MissingStr + '  •  pillow' + #13#10;
  end;

  if PyRun('-c "import pynput"') then
    Present := Present + '  ✓  pynput' + #13#10
  else begin
    MissingPkgs[Count] := 'pynput'; Inc(Count);
    MissingStr := MissingStr + '  •  pynput' + #13#10;
  end;

  SetArrayLength(MissingPkgs, Count);

  // Accumulate already-present packages into the global summary
  if Present <> '' then
    GInstalledSummary := GInstalledSummary + Present;

  if Count = 0 then Exit;  // all present — nothing to do

  MsgText :=
    '── UI packages (required) ────────────────────────────' + #13#10 + #13#10;
  if Present <> '' then
    MsgText := MsgText + 'Already installed:' + #13#10 + Present + #13#10;
  MsgText := MsgText +
    'Need to install:' + #13#10 + MissingStr + #13#10 +
    'Would you like to install the missing packages now?';

  Answer := MsgBox(MsgText, mbConfirmation, MB_YESNO);

  if Answer = IDNO then
  begin
    MsgBox(
      'Skipped. You can install them manually any time:' + #13#10 + #13#10 +
      '  pip install pystray pillow pynput',
      mbInformation, MB_OK);
    Exit;
  end;

  InstallWithProgress(MissingPkgs, 'Installing UI packages');
end;


// ── Main gate: called by Inno Setup before the install step ──────────────────
// Step 1: ensure Python is available (system or portable download).
// Step 2: check remaining dependencies with a status window.
// Step 3: interactive package-install prompts.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  CheckForm:  TForm;
  CheckLabel: TLabel;
begin
  Result            := '';
  GInstalledSummary := '';
  GUsingEmbedded   := False;

  // ── Step 1: Ensure Python is available ─────────────────────────────────────
  // Try system Python first; if not found, download a portable copy.
  if not CheckPython() then
  begin
    if not SetupEmbeddedPython() then
    begin
      Result := 'Python is required and the automatic download failed.' + #13#10 +
                'Please check your internet connection, or install Python manually' + #13#10 +
                'from https://www.python.org/downloads/ and run this installer again.';
      Exit;
    end;
    GInstalledSummary := GInstalledSummary +
      '  ' + #$2713 + '  Portable Python 3.12  (bundled with GrammarAnywhere)' + #13#10;
  end;

  // ── Step 2: Check remaining requirements with a status window ──────────────
  CheckForm := TForm.Create(nil);
  try
    CheckForm.Caption      := 'GrammarAnywhere — Checking requirements';
    CheckForm.ClientWidth  := ScaleX(420);
    CheckForm.ClientHeight := ScaleY(70);
    CheckForm.Position     := poScreenCenter;
    CheckForm.BorderStyle  := bsDialog;

    CheckLabel := TLabel.Create(CheckForm);
    CheckLabel.Parent   := CheckForm;
    CheckLabel.Left     := ScaleX(20);
    CheckLabel.Top      := ScaleY(24);
    CheckLabel.Width    := CheckForm.ClientWidth - ScaleX(40);
    CheckLabel.AutoSize := False;

    CheckLabel.Caption := 'Checking pip...';
    CheckForm.Show;

    if not CheckPip() then
    begin
      Result := 'pip is required. Please restore it and run this installer again.';
      Exit;
    end;

    CheckLabel.Caption := 'Checking AI engine (llama-cpp-python)...';
    CheckForm.Refresh;
    DetectAVX512();
    CheckLlamaCpp();

    CheckLabel.Caption := 'Scanning installed packages...';
    CheckForm.Refresh;
    Sleep(400);

  finally
    CheckForm.Free;
  end;

  // ── Step 3: Interactive package-install prompts ─────────────────────────────
  InstallUIDeps();
end;


// ── Write a minimal config that pre-selects the downloaded model ─────────────
// Only writes if no config exists yet, so a reinstall never clobbers the user's
// existing settings. spell_daemon's load_cfg() fills every other key from its
// own DEFAULT_CFG, so a single "model_path" entry is enough to make the app
// work on first launch. JSON requires backslashes to be doubled.
procedure WriteModelConfigIfAbsent(const ModelPath: String);
var
  CfgFile: String;
  JsonPath: String;
  Json: String;
begin
  CfgFile := ExpandConstant('{app}\spell_daemon_config.json');
  if FileExists(CfgFile) then Exit;  // never overwrite existing settings

  JsonPath := ModelPath;
  StringChangeEx(JsonPath, '\', '\\', True);  // escape for JSON
  Json := '{' + #13#10 + '  "model_path": "' + JsonPath + '"' + #13#10 + '}' + #13#10;
  SaveStringToFile(CfgFile, Json, False);
end;


// ── Download the Gemma 3 model with a live percentage progress bar ───────────
// urllib reports bytes via a reporthook, so we can show TRUE progress (not just
// a spinner).  The download runs as a detached process (ewNoWait) that writes
// the current percent to a temp file; the wizard polls that file and drives the
// progress bar, repainting the form each tick (Refresh) so the bar animates.
function DownloadModelWithProgress(const Url, Dest: String): Boolean;
var
  PyFile, ProgFile, DoneFile: String;
  Script: String;
  Code, Elapsed, Pct: Integer;
  Form: TForm; Lbl: TLabel; Bar: TNewProgressBar;
  Lines: TArrayOfString;
  DoneStatus: String;
begin
  Result   := False;
  PyFile   := ExpandConstant('{tmp}\ga_dl.py');
  ProgFile := ExpandConstant('{tmp}\ga_dl_progress.txt');
  DoneFile := ExpandConstant('{tmp}\ga_dl_done.txt');
  DeleteFile(ProgFile);
  DeleteFile(DoneFile);
  DeleteFile(Dest);  // clear any partial file from a previous attempt

  // Downloader script: writes integer percent to ProgFile during the transfer
  // and a final OK / FAIL line to DoneFile so the poll loop knows when to stop.
  Script :=
    'import urllib.request' + #13#10 +
    'url  = r"' + Url + '"' + #13#10 +
    'dest = r"' + Dest + '"' + #13#10 +
    'prog = r"' + ProgFile + '"' + #13#10 +
    'done = r"' + DoneFile + '"' + #13#10 +
    'def hook(blocks, bs, total):' + #13#10 +
    '    if total and total > 0:' + #13#10 +
    '        p = int(min(100, blocks * bs * 100 / total))' + #13#10 +
    '        try:' + #13#10 +
    '            f = open(prog, "w"); f.write(str(p)); f.close()' + #13#10 +
    '        except Exception:' + #13#10 +
    '            pass' + #13#10 +
    'try:' + #13#10 +
    '    urllib.request.urlretrieve(url, dest, hook)' + #13#10 +
    '    f = open(done, "w"); f.write("OK"); f.close()' + #13#10 +
    'except Exception as e:' + #13#10 +
    '    f = open(done, "w"); f.write("FAIL: " + str(e)); f.close()' + #13#10;

  if not SaveStringToFile(PyFile, Script, False) then
  begin
    MsgBox('Could not create the download helper. Skipping model download.',
           mbError, MB_OK);
    Exit;
  end;

  Form := TForm.Create(nil);
  try
    Form.Caption      := 'GrammarAnywhere — Downloading Gemma 3 model';
    Form.ClientWidth  := ScaleX(480);
    Form.ClientHeight := ScaleY(90);
    Form.Position     := poScreenCenter;
    Form.BorderStyle  := bsDialog;

    Lbl := TLabel.Create(Form);
    Lbl.Parent   := Form;
    Lbl.Left     := ScaleX(16);
    Lbl.Top      := ScaleY(16);
    Lbl.Width    := Form.ClientWidth - ScaleX(32);
    Lbl.AutoSize := False;
    Lbl.Caption  := 'Starting download (~2.5 GB)...';

    Bar := TNewProgressBar.Create(Form);
    Bar.Parent   := Form;
    Bar.Left     := ScaleX(16);
    Bar.Top      := ScaleY(44);
    Bar.Width    := Form.ClientWidth - ScaleX(32);
    Bar.Height   := ScaleY(20);
    Bar.Min      := 0;
    Bar.Max      := 100;
    Bar.Position := 0;

    Form.Show;
    Form.Refresh;

    // Launch the downloader without blocking so we can poll its progress.
    if not Exec(GPythonExe, '"' + PyFile + '"', '', SW_HIDE, ewNoWait, Code) then
    begin
      MsgBox('Could not start the download. Skipping model download.',
             mbError, MB_OK);
      Exit;
    end;

    Elapsed := 0;
    while not FileExists(DoneFile) do
    begin
      if LoadStringsFromFile(ProgFile, Lines) and (GetArrayLength(Lines) > 0) then
      begin
        Pct := StrToIntDef(Trim(Lines[0]), 0);
        if Pct < 0 then Pct := 0;
        if Pct > 100 then Pct := 100;
        Bar.Position := Pct;
        Lbl.Caption  := 'Downloading Gemma 3 model...  ' + IntToStr(Pct) + '%';
      end;
      Form.Refresh;
      Sleep(300);
      Elapsed := Elapsed + 300;
      if Elapsed > 2400000 then Break;  // 40-minute safety timeout
    end;

    DoneStatus := '';
    if LoadStringsFromFile(DoneFile, Lines) and (GetArrayLength(Lines) > 0) then
      DoneStatus := Trim(Lines[0]);

    if (Copy(DoneStatus, 1, 2) = 'OK') and FileExists(Dest) then
    begin
      Bar.Position := 100;
      Lbl.Caption  := 'Download complete.';
      Form.Refresh;
      Sleep(600);
      Result := True;
    end
    else
    begin
      DeleteFile(Dest);  // remove any partial download
      MsgBox(
        'The model download did not complete.' + #13#10 + #13#10 +
        DoneStatus + #13#10 + #13#10 +
        'No problem — you can add a model later from Settings.' + #13#10 +
        'See the completion screen for the recommended model name.',
        mbInformation, MB_OK);
    end;

  finally
    Form.Free;
  end;
end;


// ── Optional model download — offered, never forced ──────────────────────────
// Called from CurStepChanged at ssPostInstall, after {app} exists and the
// engine has been installed.
procedure OfferModelDownload();
var
  Dest: String;
begin
  GModelDownloaded := False;
  GModelPath       := '';

  if MsgBox(
       'Download the recommended grammar model now?' + #13#10 + #13#10 +
       'Gemma 3 (4B, Q4_K_M GGUF) — about 2.5 GB.' + #13#10 +
       'It will be saved into your install folder and selected automatically.' + #13#10 + #13#10 +
       'You can also skip this and add a model later from Settings.' + #13#10 + #13#10 +
       'Download now?',
       mbConfirmation, MB_YESNO) = IDNO then
    Exit;

  Dest := ExpandConstant('{app}\' + GEMMA3_FILENAME);

  if DownloadModelWithProgress(GEMMA3_URL, Dest) then
  begin
    GModelDownloaded := True;
    GModelPath       := Dest;
    WriteModelConfigIfAbsent(Dest);
    GInstalledSummary :=
      GInstalledSummary +
      '  ' + #$2713 + '  Gemma 3 model       (downloaded & pre-selected)' + #13#10;
  end;
end;



procedure CurStepChanged(CurStep: TSetupStep);
var
  SummaryBlock: String;
  ModelBlock: String;
  CopyCode: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    // Copy the portable Python into the install directory so it persists
    // after {tmp} is cleaned up.  xcopy /E /I /Y copies the full tree
    // (python.exe, Lib\site-packages\*, Scripts\*) silently.
    if GUsingEmbedded and (GEmbedDir <> '') then
      Exec('cmd.exe',
           '/c xcopy "' + GEmbedDir + '" "' + ExpandConstant('{app}\python') + '" /E /I /Y /Q',
           '', SW_HIDE, ewWaitUntilTerminated, CopyCode);
    // After files are copied (so {app} exists), offer the optional model download.
    OfferModelDownload();
  end;

  if CurStep = ssDone then
  begin
    // ── "Already on your system" summary block ────────────────────────────────
    SummaryBlock := '';
    if GInstalledSummary <> '' then
      SummaryBlock :=
        '── Already on your system ──────────────────────────' + #13#10 +
        GInstalledSummary + #13#10;

    // ── Model block: skip the "go download one" instructions if we already
    //    fetched the model during install. ───────────────────────────────────
    if GModelDownloaded then
      ModelBlock :=
        '── Grammar / LLM model ─────────────────────────────' + #13#10 +
        'The Gemma 3 model is installed and already selected — you are ready' + #13#10 +
        'to go. To switch models later, use Settings → Browse next to' + #13#10 +
        '"Model (.gguf)".' + #13#10 + #13#10
    else
      ModelBlock :=
        '── Grammar / LLM model ─────────────────────────────' + #13#10 +
        'You still need a GGUF model file (2-3 GB). Recommended:' + #13#10 + #13#10 +
        '  Gemma 3 (4B):  unsloth/gemma-3-4b-it-GGUF' + #13#10 +
        '  Qwen3 (4B):    chaparro2001/Qwen3-4B-Instruct-2507-Q4_K_M-GGUF' + #13#10 + #13#10 +
        'Note: Gemma 4 is NOT supported by this build''s engine — use Gemma 3.' + #13#10 + #13#10 +
        'Search Hugging Face for the model name above, download the .gguf' + #13#10 +
        'file, then in Settings click Browse next to "Model (.gguf)".' + #13#10 +
        'Leave "Prompt format" on Auto. Click Save & Load Model.' + #13#10 + #13#10;

    MsgBox(
      'GrammarAnywhere is installed!' + #13#10 + #13#10 +
      SummaryBlock +
      ModelBlock +
      '── Getting started ─────────────────────────────────' + #13#10 +
      'Double-click the GrammarAnywhere shortcut on your Desktop.' + #13#10 +
      'A tray icon will appear — right-click it to open Settings.',
      mbInformation, MB_OK);
  end;
end;

[UninstallDelete]
; Remove the config file, any launcher variants, and a downloaded model on uninstall
Type: files; Name: "{app}\spell_daemon_config.json"
Type: files; Name: "{app}\run.bat"
; Models downloaded by the installer (or later by the user) into the app folder
Type: files; Name: "{app}\*.gguf"
; Portable Python directory (if the installer downloaded one)
Type: filesandordirs; Name: "{app}\python"
