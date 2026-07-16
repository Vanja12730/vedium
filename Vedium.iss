; ─────────────────────────────────────────────────────────────────────────────
;  Vedium.iss  —  Inno Setup 6 installer script
;
;  Produces a single Setup.exe that:
;    1. Copies the spell_daemon package, run.vbs and a PRIVATE portable
;       Python 3.12 (with tkinter, pip, pystray, pillow, pynput pre-installed)
;       to the install folder — the user's own Python, if any, is never
;       touched or even looked at
;    2. Installs llama-cpp-python into that private Python (CPU or GPU build,
;       user's choice; CPU wheel picked to match the machine's AVX support)
;    3. Optionally downloads the recommended Gemma 3 GGUF model into the
;       install folder (with a live progress bar) and pre-selects it in config
;    4. Creates a Desktop shortcut that launches run.vbs
;    5. Registers the app in Add/Remove Programs with a clean uninstaller
;
;  HOW TO BUILD
;  ────────────
;    1. Download and install Inno Setup 6 from https://jrsoftware.org/isinfo.php
;    2. Build the portable Python bundle (once, and after dependency changes):
;         py -3.12 make_python_bundle.py
;    3. Right-click Vedium.iss → Compile  (or open in Inno Setup IDE)
;    4. The finished installer appears as Output\VediumSetup_v<AppVersion>.exe
;
; ─────────────────────────────────────────────────────────────────────────────

; Refuse to compile without the portable Python bundle — an installer built
; without it would install a broken app.
#if !FileExists("python_embed\python.exe")
  #error Portable Python bundle missing - run:  py -3.12 make_python_bundle.py
#endif

#define AppName      "Vedium"
#define AppVersion   "1.2"
#define AppPublisher "The Ved Project"
#define AppExeName   "run.vbs"

[Setup]
; Unique app identity for Windows (Add/Remove Programs, upgrade detection).
; NEVER change this once released — a new AppId makes Windows treat the next
; version as a different application and orphans existing installs.
AppId={{0AB6BC53-F06E-4176-8D02-A14FB363A564}
AppName={#AppName}
AppVersion={#AppVersion}
; Point these at the repo once it lives under the ved-project GitHub org
AppPublisherURL=
AppSupportURL=
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\{#AppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=VediumSetup_v{#AppVersion}
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
; Private portable Python 3.12 — built by make_python_bundle.py. run.vbs
; prefers {app}\python\pythonw.exe, so the app always runs on this exact
; interpreter regardless of what Python (if any) the user has installed.
Source: "python_embed\*"; DestDir: "{app}\python"; Flags: ignoreversion recursesubdirs createallsubdirs
; App icon — generate once before building: python make_ico.py
Source: "{#SourcePath}app.ico"; DestDir: "{app}"; Flags: ignoreversion

[InstallDelete]
; Wipe the bundled Python before copying the fresh one.  Inno Setup overwrites
; files it ships but never removes ones it doesn't, so without this, upgrading
; would leave obsolete package versions' files piling up in Lib\site-packages.
; Cost: the AI engine (llama-cpp-python) lives there too, so a reinstall asks
; for the CPU/GPU choice again and re-downloads that wheel.
; The user's config and downloaded models live in {app} itself, NOT here, and
; are preserved across reinstalls.
Type: filesandordirs; Name: "{app}\python"

[Icons]
; Desktop shortcut — wscript.exe runs run.vbs silently (no console window)
Name: "{userdesktop}\{#AppName}";  \
      Filename: "wscript.exe";       \
      Parameters: """{app}\{#AppExeName}"""; \
      WorkingDir: "{app}";             \
      IconFilename: "{app}\app.ico";   \
      Comment: "Start Vedium — reading & writing assistant"

[Code]
// ─────────────────────────────────────────────────────────────────────────────
//  Pascal script — provisions the AI engine and offers the model download,
//  all in the post-install step.  Inno Setup's [Code] section uses a subset
//  of Object Pascal.
//
//  Every python/pip call uses the full path to the PRIVATE portable Python
//  copied to {app}\python by [Files] — the user's own Python and PATH are
//  never consulted, so venvs, Store stubs, or a missing Python cannot break
//  the install.
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

var
  GPythonExe:        String;  // full path to the bundled {app}\python\python.exe
  GInstalledSummary: String;  // running log of installed packages/features;
                               // shown in the final completion dialog
  GAVX512Supported:  Boolean; // True if the CPU supports AVX-512; set by DetectAVX512()
  GCudaOK:           Boolean; // True if an NVIDIA driver supporting CUDA >= 12.8
                               // is present; set by DetectNvidiaCuda()
  GModelDownloaded:  Boolean; // True if the Gemma 3 model was downloaded into {app}
  GModelPath:        String;  // full path to the downloaded .gguf (if any)


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


// ── Detect an NVIDIA GPU with a CUDA 12.8-capable driver ────────────────────
// nvidia-smi ships with every NVIDIA driver.  Depending on driver generation
// its banner line reads either
//   | NVIDIA-SMI 570.86  Driver Version: 570.86   CUDA Version: 12.8     |
// or (newer drivers)
//   | NVIDIA-SMI 610.62  KMD Version: 610.62      CUDA UMD Version: 13.3 |
// so the parse anchors on 'CUDA' and takes the number after the following
// 'Version:'.  That number is the highest CUDA the DRIVER supports; drivers
// are backward compatible, so >= 12.8 can run the prebuilt CUDA 12.8 wheel.
// If nvidia-smi is missing (no NVIDIA driver) or reports < 12.8, GCudaOK
// stays False and the dialog recommends the CPU build instead.
procedure DetectNvidiaCuda();
var
  TmpFile: String;
  Lines:   TArrayOfString;
  Code, I, P, DotPos: Integer;
  S, Ver:  String;
  Major, Minor: Integer;
begin
  GCudaOK := False;

  TmpFile := ExpandConstant('{tmp}\ga_nvsmi.txt');
  Exec('cmd.exe', '/c nvidia-smi > "' + TmpFile + '" 2>&1',
       '', SW_HIDE, ewWaitUntilTerminated, Code);
  if Code <> 0 then Exit;

  if not LoadStringsFromFile(TmpFile, Lines) then Exit;
  for I := 0 to GetArrayLength(Lines) - 1 do
  begin
    S := Lines[I];
    P := Pos('CUDA', S);
    if P = 0 then Continue;
    S := Copy(S, P, Length(S));            // 'CUDA [UMD ]Version: NN.N ... |'
    P := Pos('Version:', S);
    if P = 0 then Continue;
    Ver := Trim(Copy(S, P + Length('Version:'), 8));
    P := Pos(' ', Ver);
    if P > 0 then Ver := Copy(Ver, 1, P - 1);
    P := Pos('|', Ver);
    if P > 0 then Ver := Copy(Ver, 1, P - 1);
    DotPos := Pos('.', Ver);
    if DotPos > 0 then
    begin
      Major := StrToIntDef(Copy(Ver, 1, DotPos - 1), 0);
      Minor := StrToIntDef(Copy(Ver, DotPos + 1, 3), -1);
      GCudaOK := (Major > 12) or ((Major = 12) and (Minor >= 8));
    end;
    Exit;
  end;
end;


// ── Create a word-wrapping description label ─────────────────────────────────
// TNewStaticText (unlike plain TLabel) keeps the width it is given and grows
// in HEIGHT only, so wrapped text never collapses into a narrow column.
function CreateWrappedText(AParent: TForm; ALeft, ATop, AWidth: Integer;
                           const ACaption: String): TNewStaticText;
begin
  Result := TNewStaticText.Create(AParent);
  Result.Parent   := AParent;
  Result.Left     := ALeft;
  Result.Top      := ATop;
  Result.Width    := AWidth;
  Result.WordWrap := True;
  Result.AutoSize := True;
  Result.Caption  := ACaption;
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
    Form.Caption      := 'Vedium - ' + GroupTitle;
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
        'You can retry manually in a Command Prompt with:' + #13#10 +
        '  "' + GPythonExe + '" -m pip install <package>',
        mbError, MB_OK);

  finally
    Form.Free;
  end;
end;


// ── AI engine: llama-cpp-python ──────────────────────────────────────────────
// Radio-button dialog lets the user choose CPU (latest) or GPU (v0.3.16).
// The pre-selected/recommended option follows hardware detection: GPU when an
// NVIDIA driver supporting CUDA 12.8 is present (GCudaOK), CPU otherwise.
// The engine is never pre-installed when this runs: [InstallDelete] wipes
// {app}\python before every install, so there is no upgrade path to handle.
// CPU path: installs via the official abetlen CPU wheel index.
// GPU path: downloads the 226 MB community CUDA 12.8 wheel via urllib then pip.
procedure CheckLlamaCpp();
var
  Form:             TForm;
  Intro:            TNewStaticText;
  LblCPU:           TNewStaticText;
  LblGPU:           TNewStaticText;
  LblSkip:          TNewStaticText;
  RbCPU:            TRadioButton;
  RbGPU:            TRadioButton;
  RbSkip:           TRadioButton;
  BtnOK:            TNewButton;
  Margin:           Integer;
  ContentW:         Integer;
  Y, Code:          Integer;
  IntroText:        String;
  GpuText:          String;
  PipSpec:          String;
  PkgArray:         TArrayOfString;
  WheelUrl:         String;
  WheelFile:        String;
  DlFile:           String;
  DlScript:         String;
begin
  // ── Build choice dialog ───────────────────────────────────────────────────
  // All coordinates are wrapped in ScaleX/ScaleY so the layout grows with the
  // dialog font on high-DPI displays.  Radio buttons cannot word-wrap, so each
  // option has a SHORT one-line radio caption plus a separate word-wrapped
  // description beneath it (TNewStaticText — see CreateWrappedText).
  Form := TForm.Create(nil);
  try
    Form.Caption     := 'Vedium — AI Engine';
    Form.ClientWidth := ScaleX(620);
    Form.Position    := poScreenCenter;
    Form.BorderStyle := bsDialog;

    Margin   := ScaleX(20);
    ContentW := Form.ClientWidth - (Margin * 2);
    Y        := ScaleY(16);

    // ── Intro paragraph ──────────────────────────────────────────────────────
    IntroText :=
      'llama-cpp-python is the engine that runs AI models locally on this ' +
      'computer. It is not installed yet.' + #13#10 + #13#10 +
      'Planning to use a cloud AI service (API key) or a CLI backend instead ' +
      'of a local model? Then no engine and no powerful hardware are needed — ' +
      'choose Skip.';
    Intro := CreateWrappedText(Form, Margin, Y, ContentW, IntroText);
    Y := Intro.Top + Intro.Height + ScaleY(16);

    // ── CPU option ───────────────────────────────────────────────────────────
    RbCPU := TRadioButton.Create(Form);
    RbCPU.Parent  := Form;
    RbCPU.Left    := Margin;
    RbCPU.Top     := Y;
    RbCPU.Width   := ContentW;
    RbCPU.Height  := ScaleY(20);
    if GAVX512Supported then
      RbCPU.Caption := 'CPU build — latest (AVX-512)'
    else
      RbCPU.Caption := 'CPU build — v0.3.19 (AVX2 compatible)';
    if not GCudaOK then
      RbCPU.Caption := RbCPU.Caption + '     [recommended for this PC]';
    RbCPU.Checked := not GCudaOK;
    Y := RbCPU.Top + RbCPU.Height + ScaleY(2);

    if GAVX512Supported then
      LblCPU := CreateWrappedText(Form, Margin + ScaleX(22), Y, ContentW - ScaleX(22),
        'Runs the model on the processor — works on any PC. Installs the newest ' +
        'version (AVX-512 detected). Supports Gemma 3, Qwen3, and most models.')
    else
      LblCPU := CreateWrappedText(Form, Margin + ScaleX(22), Y, ContentW - ScaleX(22),
        'Runs the model on the processor — works on any PC. Your CPU supports ' +
        'AVX2 but not AVX-512, so the stable v0.3.19 build is used. Supports ' +
        'Qwen3 and most models.');
    Y := LblCPU.Top + LblCPU.Height + ScaleY(14);

    // ── GPU option ───────────────────────────────────────────────────────────
    RbGPU := TRadioButton.Create(Form);
    RbGPU.Parent  := Form;
    RbGPU.Left    := Margin;
    RbGPU.Top     := Y;
    RbGPU.Width   := ContentW;
    RbGPU.Height  := ScaleY(20);
    RbGPU.Caption := 'GPU build — v0.3.16 (NVIDIA)';
    if GCudaOK then
      RbGPU.Caption := RbGPU.Caption + '     [recommended for this PC]';
    RbGPU.Checked := GCudaOK;
    Y := RbGPU.Top + RbGPU.Height + ScaleY(2);

    if GCudaOK then
      GpuText :=
        'An NVIDIA GPU with a CUDA 12.8-capable driver was detected on this PC ' +
        '— this is the fastest option for you. Supports Gemma 3 and Qwen3. ' +
        'Downloads a 226 MB wheel; a console window will show progress.'
    else
      GpuText :=
        'Requires an NVIDIA GPU with a CUDA 12.8-capable driver — none was ' +
        'detected on this PC. Supports Gemma 3 and Qwen3. Downloads a 226 MB ' +
        'wheel; a console window will show progress.';
    LblGPU := CreateWrappedText(Form, Margin + ScaleX(22), Y, ContentW - ScaleX(22),
                                GpuText);
    Y := LblGPU.Top + LblGPU.Height + ScaleY(14);

    // ── Skip option ──────────────────────────────────────────────────────────
    RbSkip := TRadioButton.Create(Form);
    RbSkip.Parent  := Form;
    RbSkip.Left    := Margin;
    RbSkip.Top     := Y;
    RbSkip.Width   := ContentW;
    RbSkip.Height  := ScaleY(20);
    RbSkip.Caption := 'Skip — cloud / CLI mode, or install the engine later';
    Y := RbSkip.Top + RbSkip.Height + ScaleY(2);

    LblSkip := CreateWrappedText(Form, Margin + ScaleX(22), Y, ContentW - ScaleX(22),
      'Vedium works without a local engine when a cloud API key or a CLI ' +
      'backend is set up in Settings. You can add the local engine any time.');
    Y := LblSkip.Top + LblSkip.Height + ScaleY(18);

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

      // Run the download from a temp script rather than a -c one-liner —
      // string literals inside a quoted -c argument need escaping that cmd,
      // Inno and Python all disagree about.
      DlFile   := ExpandConstant('{tmp}\ga_wheel_dl.py');
      DlScript :=
        'import urllib.request' + #13#10 +
        'print("Downloading the GPU wheel (~226 MB)...")' + #13#10 +
        'urllib.request.urlretrieve(r"' + WheelUrl + '", r"' + WheelFile + '")' + #13#10;

      if not SaveStringToFile(DlFile, DlScript, False) then
      begin
        MsgBox('Could not create the download helper. Skipping the engine install.',
               mbError, MB_OK);
        Exit;
      end;

      Exec(GPythonExe, '"' + DlFile + '"', '', SW_SHOW, ewWaitUntilTerminated, Code);

      if (Code <> 0) or not FileExists(WheelFile) then
      begin
        MsgBox(
          'Download failed.' + #13#10 + #13#10 +
          'You can install the GPU build manually after setup.' + #13#10 +
          'See the GPU install guide for the full wheel URL and instructions.',
          mbError, MB_OK);
        Exit;
      end;

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
        PipSpec := 'llama-cpp-python'
      else
        PipSpec := 'llama-cpp-python==0.3.19';
      MsgBox(
        'Skipped — cloud API and CLI modes work without the local engine;' + #13#10 +
        'local models will not work until it is installed.' + #13#10 + #13#10 +
        'To add the local engine later, run in a Command Prompt:' + #13#10 +
        '  "' + GPythonExe + '" -m pip install' + #13#10 +
        '      --only-binary llama-cpp-python' + #13#10 +
        '      --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu' + #13#10 +
        '      ' + PipSpec + #13#10 + #13#10 +
        'GPU build (NVIDIA + CUDA 12.8 only): see the GPU install guide.',
        mbInformation, MB_OK);
    end;

  finally
    Form.Free;
  end;
end;


// ── UI packages: pystray / pillow / pynput ───────────────────────────────────
// Safety net — the bundle ships all three pre-installed, so normally this just
// adds ✓ lines to the completion summary. It only prompts and installs when
// one is missing (e.g. a hand-edited or damaged install folder).
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
      '  "' + GPythonExe + '" -m pip install pystray pillow pynput',
      mbInformation, MB_OK);
    Exit;
  end;

  InstallWithProgress(MissingPkgs, 'Installing UI packages');
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
  CfgFile := ExpandConstant('{app}\vedium_config.json');
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
    Form.Caption      := 'Vedium — Downloading Gemma 3 model';
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
begin
  if CurStep = ssPostInstall then
  begin
    // All provisioning runs AFTER the files are copied, against the private
    // portable Python that now lives in {app}\python. Nothing outside the
    // install folder is inspected or modified.
    GPythonExe        := ExpandConstant('{app}\python\python.exe');
    GInstalledSummary :=
      '  ' + #$2713 + '  Portable Python 3.12  (private to Vedium, tkinter + UI packages included)' + #13#10;

    DetectAVX512();
    DetectNvidiaCuda();
    CheckLlamaCpp();
    InstallUIDeps();   // safety net — the bundle ships these pre-installed
    OfferModelDownload();
  end;

  if CurStep = ssDone then
  begin
    // ── "Installed with Vedium" summary block ─────────────────────────────────
    SummaryBlock := '';
    if GInstalledSummary <> '' then
      SummaryBlock :=
        '── Installed with Vedium ───────────────────────────' + #13#10 +
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
      'Vedium is installed!' + #13#10 + #13#10 +
      SummaryBlock +
      ModelBlock +
      '── Getting started ─────────────────────────────────' + #13#10 +
      'Double-click the Vedium shortcut on your Desktop.' + #13#10 +
      'A tray icon will appear — right-click it to open Settings.',
      mbInformation, MB_OK);
  end;
end;

[UninstallDelete]
; Remove the config file, any launcher variants, and a downloaded model on uninstall
Type: files; Name: "{app}\vedium_config.json"
; Pre-rename config name, in case this install was upgraded from GrammarAnywhere
Type: files; Name: "{app}\spell_daemon_config.json"
Type: files; Name: "{app}\run.bat"
; Models downloaded by the installer (or later by the user) into the app folder
Type: files; Name: "{app}\*.gguf"
; Bundled Python — [Files] entries are removed automatically, but packages
; pip-installed at install time (the AI engine) would otherwise linger
Type: filesandordirs; Name: "{app}\python"
