; Copyright (C) 2026 vehab
; SPDX-License-Identifier: GPL-3.0-only
;
; ─────────────────────────────────────────────────────────────────────────────
;  Vedium.iss  —  Inno Setup 6 installer script
;
;  Produces a single Setup.exe that:
;    1. Copies the vedium package, run.vbs and a PRIVATE portable
;       Python 3.12 (with tkinter, pip, pystray, pillow, pynput pre-installed)
;       to the install folder — the user's own Python, if any, is never
;       touched or even looked at. Compatible reinstalls preserve this private
;       environment so already-installed packages are reused.
;    2. Reuses or downloads a current managed llama-server engine (CPU or
;       NVIDIA CUDA). The engine is independent of private Python and supports
;       current GGUF architectures such as Qwen3.5 and Gemma 4.
;    3. Optionally downloads Gemma 4 E4B (best quality) or Gemma 3 4B
;       (lightweight), with a live progress bar, and pre-selects it in config
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
#define AppVersion   "1.3.4"
#define AppPublisher "The Ved Project"
; Single source of truth for the project URLs; update here if the repo moves
; (e.g. transferred to the ved-project org) and both Setup fields follow.
#define AppUrl       "https://github.com/Vanja12730/vedium"
#define AppExeName   "run.vbs"
; Bump this only when the portable Python/runtime dependency bundle changes in
; an incompatible way. Code-only Vedium updates should leave it unchanged so
; reinstalls retain private packages and the separately cached local engine.
#define PythonBundleVersion "3.12.9-ui2"

[Setup]
; Unique app identity for Windows (Add/Remove Programs, upgrade detection).
; NEVER change this once released — a new AppId makes Windows treat the next
; version as a different application and orphans existing installs.
AppId={{0AB6BC53-F06E-4176-8D02-A14FB363A564}
AppName={#AppName}
AppVersion={#AppVersion}
; Shown in the SmartScreen "More info" pane and in Add/Remove Programs.
AppPublisherURL={#AppUrl}
AppSupportURL={#AppUrl}/issues
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\{#AppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=VediumSetup_v{#AppVersion}
Compression=lzma
SolidCompression=yes
; The managed llama-server engine ships as .zip archives; "full" is the
; extractor mode that handles .zip (the default handles only .7z).
ArchiveExtraction=full
WizardStyle=modern
; Default to the existing install folder on upgrades/reinstalls. The directory
; page is still shown, so the user can choose another location explicitly.
UsePreviousAppDir=yes
; Run as the current user — avoids PATH mismatch between user and admin accounts
PrivilegesRequired=lowest
; Minimum Windows version: Windows 10
MinVersion=10.0
; Every bundled runtime and managed engine binary is 64-bit.
ArchitecturesAllowed=x64compatible
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
Source: "vedium\*"; DestDir: "{app}\vedium"; Excludes: "__pycache__\*,*.pyc"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "run.vbs";         DestDir: "{app}"; Flags: ignoreversion
Source: "LICENSE";         DestDir: "{app}"; Flags: ignoreversion
Source: "THIRD_PARTY_LICENSES.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "licenses\*"; DestDir: "{app}\licenses"; Flags: ignoreversion recursesubdirs createallsubdirs
; Private portable Python 3.12 — built by make_python_bundle.py. run.vbs
; prefers {app}\python\pythonw.exe, so the app always runs on this exact
; interpreter regardless of what Python (if any) the user has installed.
Source: "python_embed\*"; DestDir: "{app}\python"; Flags: ignoreversion recursesubdirs createallsubdirs
; App icon — generate once before building: python make_ico.py
Source: "{#SourcePath}app.ico"; DestDir: "{app}"; Flags: ignoreversion

[InstallDelete]
; Preserve the private Python on ordinary reinstalls so its verified UI packages
; are reused. Reset it only when the bundle compatibility marker changes, or
; when a legacy install is not Python 3.12.
; The fresh bundle is copied immediately afterwards by [Files].
Type: filesandordirs; Name: "{app}\python"; Check: ShouldResetPythonBundle
; Pre-rename package dir (the package was called spell_daemon before v1.2) —
; without this an upgrade would leave the old copy of the app behind.
Type: filesandordirs; Name: "{app}\spell_daemon"
; Remove the retired preview widget from installations that included it.
Type: files; Name: "{app}\vedium\ui_markdown.py"
Type: files; Name: "{app}\vedium\__pycache__\ui_markdown.*.pyc"
; Remove an obsolete dependency log name left by pre-managed-engine installers.
Type: files; Name: "{app}\install-engine.log"

[Icons]
; Desktop shortcut — wscript.exe runs run.vbs silently (no console window)
Name: "{userdesktop}\{#AppName}";  \
      Filename: "wscript.exe";       \
      Parameters: """{app}\{#AppExeName}"""; \
      WorkingDir: "{app}";             \
      IconFilename: "{app}\app.ico";   \
      Comment: "Start Vedium — reading & writing assistant"
; Start menu shortcut — makes Vedium searchable in Start, where right-click
; offers Windows' standard Uninstall. Per-user, since setup runs unelevated.
Name: "{autoprograms}\{#AppName}";  \
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

// Recommended public, ungated local models. Gemma 4 E4B is the quality choice;
// Gemma 3 4B remains available for modest hardware and CPU-oriented installs.
// Each URL is pinned to a Hugging Face commit and each file to its SHA-256,
// so the installer only accepts the exact files that were tested.
const
  GEMMA4_FILENAME = 'gemma-4-E4B-it-Q4_K_M.gguf';
  GEMMA4_URL =
    'https://huggingface.co/lmstudio-community/gemma-4-E4B-it-GGUF/resolve/' +
    '99210a71094c6aa8559f764e0cadedd7d89de94d/gemma-4-E4B-it-Q4_K_M.gguf';
  GEMMA4_SHA256 = '0ffb122c8b6921f13cbc34186e052524d0b5803b17f4867b7197a561400b3770';
  GEMMA3_FILENAME = 'gemma-3-4b-it-Q4_K_M.gguf';
  GEMMA3_URL =
    'https://huggingface.co/unsloth/gemma-3-4b-it-GGUF/resolve/' +
    '5a3566e716d80f709ed7b79817eaf7733d2a1fce/' +
    'gemma-3-4b-it-Q4_K_M.gguf';
  GEMMA3_SHA256 = '04a43a22e8d2003deda5acc262f68ec1005fa76c735a9962a8c77042a74a7d19';

  // SHA-256 of the llama.cpp release archives for ServerBuild (b10327).
  // Update these together with ServerBuild.
  ENGINE_CPU_SHA256  = 'c2781932f9af623c9498a12f002f667d2b668f65e0f19b4401e12b5fe9f860c3';
  ENGINE_CUDA_SHA256 = '8932f819b9ba0d7866911bac5db4a01d9ebefd0071c4293b1ef9ab5c22919637';
  CUDART_SHA256      = '8c79a9b226de4b3cacfd1f83d24f962d0773be79f1e7b75c6af4ded7e32ae1d6';

var
  GPythonExe:        String;  // full path to the bundled {app}\python\python.exe
  GInstalledSummary: String;  // running log of installed packages/features;
                               // shown in the final completion dialog
  GCudaOK:           Boolean; // True if an NVIDIA driver supporting CUDA >= 12.4
                               // is present; set by DetectNvidiaCuda()
  GEngineInstalled:  Boolean; // True only after the selected managed server starts
  GInstallLogFile:   String;  // persistent dependency log used for actionable failures
  GModelDownloaded:  Boolean; // True if a recommended model is selected in {app}
  GModelPath:        String;  // full path to the downloaded .gguf (if any)
  GModelDisplayName: String;  // selected recommended model name for summaries
  GExistingModelConfig: Boolean; // True when a returning user's config is preserved
  GEngineDownloadPage: TDownloadWizardPage;   // in-wizard engine download progress
  GEngineExtractPage:  TExtractionWizardPage; // in-wizard engine unpack progress


// ── Incremental reinstall / private Python compatibility ────────────────────
// Ordinary code-only reinstalls preserve {app}\python so its UI packages are
// not downloaded again. A future installer can deliberately
// refresh the whole private runtime by bumping PythonBundleVersion above.
function ShouldResetPythonBundle(): Boolean;
var
  PythonDir:      String;
  PythonExe:      String;
  MarkerFile:     String;
  ExistingMarker: String;
  Lines:          TArrayOfString;
  Code:           Integer;
begin
  Result     := False;
  PythonDir  := ExpandConstant('{app}\python');
  PythonExe  := PythonDir + '\python.exe';
  MarkerFile := PythonDir + '\.vedium-bundle-version';

  if not DirExists(PythonDir) then Exit;

  // Current installers leave an explicit compatibility marker. A mismatch
  // means the bundled interpreter/dependencies changed enough to require a
  // clean replacement instead of mixing old and new native files.
  if FileExists(MarkerFile) then
  begin
    ExistingMarker := '';
    if LoadStringsFromFile(MarkerFile, Lines) and
       (GetArrayLength(Lines) > 0) then
      ExistingMarker := Trim(Lines[0]);
    Result := ExistingMarker <> '{#PythonBundleVersion}';
    Exit;
  end;

  // One-time migration for installs made before the marker existed. Preserve a
  // working Python 3.12 bundle; reset only an incompatible or broken legacy runtime.
  if not FileExists(PythonExe) then
  begin
    Result := True;
    Exit;
  end;

  if not Exec(
       PythonExe,
       '-c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)"',
       '', SW_HIDE, ewWaitUntilTerminated, Code) then
  begin
    Result := True;
    Exit;
  end;
  Result := Code <> 0;
end;


// ── Detect an NVIDIA GPU with a CUDA 12.4-capable driver ────────────────────
// nvidia-smi ships with every NVIDIA driver.  Depending on driver generation
// its banner line reads either
//   | NVIDIA-SMI 570.86  Driver Version: 570.86   CUDA Version: 12.8     |
// or (newer drivers)
//   | NVIDIA-SMI 610.62  KMD Version: 610.62      CUDA UMD Version: 13.3 |
// so the parse anchors on 'CUDA' and takes the number after the following
// 'Version:'.  That number is the highest CUDA the DRIVER supports; drivers
// are backward compatible, so >= 12.4 can run the bundled CUDA 12.4 runtime.
// Setup itself is a 32-bit process, so a bare `nvidia-smi` can be redirected to
// SysWOW64 even though NVIDIA installs it in the real 64-bit System32 folder.
// Use Windows' Sysnative alias explicitly on 64-bit systems, then fall back to
// the normal system path/PATH for unusual driver layouts. If nvidia-smi is
// genuinely missing or reports < 12.4, GCudaOK stays False and the dialog
// recommends the CPU build instead.
procedure DetectNvidiaCuda();
var
  TmpFile:  String;
  NvidiaSmi: String;
  CmdExe:   String;
  Params:   String;
  Lines:   TArrayOfString;
  Code, I, P, DotPos: Integer;
  S, Ver:  String;
  Major, Minor: Integer;
begin
  GCudaOK := False;

  TmpFile := ExpandConstant('{tmp}\ga_nvsmi.txt');
  DeleteFile(TmpFile);

  NvidiaSmi := '';
  if IsWin64 then
  begin
    NvidiaSmi := ExpandConstant('{sysnative}\nvidia-smi.exe');
    if not FileExists(NvidiaSmi) then
      NvidiaSmi := '';
  end;
  if NvidiaSmi = '' then
  begin
    NvidiaSmi := ExpandConstant('{sys}\nvidia-smi.exe');
    if not FileExists(NvidiaSmi) then
      NvidiaSmi := 'nvidia-smi.exe';  // final PATH fallback
  end;

  CmdExe := ExpandConstant('{cmd}');
  Params := '/d /c ""' + NvidiaSmi + '" > "' + TmpFile + '" 2>&1"';
  Code := -1;
  if not Exec(CmdExe, Params, '', SW_HIDE, ewWaitUntilTerminated, Code) then Exit;
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
      GCudaOK := (Major > 12) or ((Major = 12) and (Minor >= 4));
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
function InstallWithProgress(const Packages: TArrayOfString;
                             const GroupTitle: String): Boolean;
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
  Result := True;
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

      if Exec(GPythonExe,
              '-m pip install --quiet --disable-pip-version-check ' +
              '--no-warn-script-location --log "' + GInstallLogFile + '" ' + Packages[I],
              '', SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0) then
      begin
        DoneLine := DoneLine + 'OK: ' + Packages[I] + '   ';
      end
      else begin
        Result   := False;
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
        'The detailed pip log is here:' + #13#10 +
        '  ' + GInstallLogFile,
        mbError, MB_OK);

  finally
    Form.Free;
  end;
end;


// ── Managed local engine: current llama-server ───────────────────────────────
// The server process is versioned independently from Vedium's private Python,
// so new GGUF architectures do not require a compatible Python native wheel.
// CUDA installs include the matching runtime DLL archive; users need a recent
// NVIDIA driver, but do not need to install the CUDA toolkit themselves.
//
// Downloading and unpacking run on Inno Setup's own wizard pages, which show
// the archive name, a live progress bar and a Stop download button inside the
// setup window, so the ~630 MB CUDA download never looks stalled.
procedure InitializeWizard();
begin
  GEngineDownloadPage := CreateDownloadPage(
    'Installing the local AI engine',
    'Downloading llama-server. This happens once; later reinstalls reuse it.',
    nil);
  GEngineDownloadPage.ShowBaseNameInsteadOfUrl := True;
  GEngineExtractPage := CreateExtractionPage(
    'Installing the local AI engine',
    'Unpacking llama-server...',
    nil);
end;


// The download page saves into {tmp}; move a finished archive into the
// persistent engine cache. FileCopy covers an install folder on another drive.
function MoveToEngineCache(const DestFile: String): Boolean;
var
  TmpFile: String;
begin
  Result := FileExists(DestFile);
  if Result then Exit;
  TmpFile := ExpandConstant('{tmp}\') + ExtractFileName(DestFile);
  Result := RenameFile(TmpFile, DestFile) or FileCopy(TmpFile, DestFile, False);
  if not Result then DeleteFile(DestFile);  // never leave a partial cache file
end;


// Delete a cached archive whose SHA-256 differs from the pinned one, so it is
// downloaded again instead of being extracted.
procedure DropUnverifiedCache(const Archive, Sha256: String);
var
  Matches: Boolean;
begin
  if not FileExists(Archive) then Exit;
  try
    Matches := CompareText(GetSHA256OfFile(Archive), Sha256) = 0;
  except
    Matches := False;
  end;
  if not Matches then DeleteFile(Archive);
end;


// Fetch whichever archives are not cached yet. RuntimeUrl is empty for the CPU
// engine. The download page rejects a file whose SHA-256 does not match.
// Returns False after explaining a failure, or quietly when the user
// pressed Stop download (the completion screen then says the engine was skipped).
function DownloadEngineArchives(
  MainUrl, MainArchive, MainSha256,
  RuntimeUrl, RuntimeArchive, RuntimeSha256: String): Boolean;
var
  Pending: Boolean;
begin
  Result := False;
  Pending := False;
  ForceDirectories(ExtractFileDir(MainArchive));
  DropUnverifiedCache(MainArchive, MainSha256);
  if RuntimeUrl <> '' then DropUnverifiedCache(RuntimeArchive, RuntimeSha256);
  GEngineDownloadPage.Clear;
  if not FileExists(MainArchive) then
  begin
    GEngineDownloadPage.Add(MainUrl, ExtractFileName(MainArchive), MainSha256);
    Pending := True;
  end;
  if (RuntimeUrl <> '') and not FileExists(RuntimeArchive) then
  begin
    GEngineDownloadPage.Add(RuntimeUrl, ExtractFileName(RuntimeArchive), RuntimeSha256);
    Pending := True;
  end;

  if Pending then
  begin
    GEngineDownloadPage.Show;
    try
      try
        GEngineDownloadPage.Download;
      except
        if not GEngineDownloadPage.AbortedByUser then
          MsgBox(
            'The managed local engine download failed:' + #13#10 +
            GetExceptionMessage + #13#10 + #13#10 +
            'Run setup again to retry.', mbError, MB_OK);
        Exit;
      end;
    finally
      GEngineDownloadPage.Hide;
    end;
  end;

  Result := MoveToEngineCache(MainArchive) and
            ((RuntimeUrl = '') or MoveToEngineCache(RuntimeArchive));
  if not Result then
    MsgBox(
      'The managed local engine was downloaded but could not be saved to' + #13#10 +
      '  ' + ExtractFileDir(MainArchive) + #13#10 + #13#10 +
      'Check free disk space, then run setup again.', mbError, MB_OK);
end;


function ExtractServerArchives(
  DestDir, MainArchive, RuntimeArchive, MarkerValue: String): Boolean;
var
  MarkerFile: String;
begin
  Result := False;
  // Start from an empty folder so DLLs from an older build never mix in.
  if DirExists(DestDir) then DelTree(DestDir, True, True, True);
  ForceDirectories(DestDir);

  GEngineExtractPage.Clear;
  GEngineExtractPage.Add(MainArchive, DestDir, True);
  if RuntimeArchive <> '' then
    GEngineExtractPage.Add(RuntimeArchive, DestDir, True);
  GEngineExtractPage.Show;
  try
    try
      GEngineExtractPage.Extract;
    except
      Exit;
    end;
  finally
    GEngineExtractPage.Hide;
  end;
  if not FileExists(DestDir + '\llama-server.exe') then Exit;

  MarkerFile := ExpandConstant('{app}\llama-server\.vedium-server-version');
  ForceDirectories(ExtractFileDir(MarkerFile));
  SaveStringToFile(MarkerFile, MarkerValue + #13#10, False);
  Result := True;
end;


procedure CheckManagedLlamaServer();
var
  ServerBuild: String;
  BaseUrl: String;
  Choice: Integer;
  ServerRoot: String;
  ServerDir: String;
  ServerExe: String;
  CacheDir: String;
  MainArchive: String;
  RuntimeArchive: String;
  MainUrl: String;
  RuntimeUrl: String;
  MainSha256: String;
  RuntimeSha256: String;
  MarkerFile: String;
  MarkerValue: String;
  Lines: TArrayOfString;
  Code: Integer;
  IsGPU: Boolean;
  EngineChoice: String;
begin
  ServerBuild := 'b10327';
  BaseUrl := 'https://github.com/ggml-org/llama.cpp/releases/download/' + ServerBuild + '/';
  GEngineInstalled := False;
  ServerRoot := ExpandConstant('{app}\llama-server');
  MarkerFile := ServerRoot + '\.vedium-server-version';

  // Incremental reinstall: retain a verified current server and all of its
  // runtime DLLs. Code-only updates do not redownload hundreds of megabytes.
  if LoadStringsFromFile(MarkerFile, Lines) and (GetArrayLength(Lines) > 0) then
  begin
    MarkerValue := Trim(Lines[0]);
    if Pos(ServerBuild, MarkerValue) = 1 then
    begin
      if Pos('-cuda', MarkerValue) > 0 then
        ServerExe := ServerRoot + '\cuda\llama-server.exe'
      else
        ServerExe := ServerRoot + '\cpu\llama-server.exe';
      if FileExists(ServerExe) and
         Exec(ServerExe, '--version', ExtractFileDir(ServerExe),
              SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0) then
      begin
        GEngineInstalled := True;
        GInstalledSummary := GInstalledSummary +
          '  ' + #$2713 + '  llama-server ' + MarkerValue +
          '    (existing managed engine — reused)' + #13#10;
        Exit;
      end;
    end;
  end;

  // Named parameters make clean installer smoke tests possible without UI:
  // /VediumEngine=cpu|cuda|skip and /VediumModel=skip|gemma3|gemma4.
  EngineChoice := Lowercase(Trim(ExpandConstant('{param:VediumEngine|}')));
  if EngineChoice = 'skip' then Exit;
  if EngineChoice = 'cpu' then
    IsGPU := False
  else if EngineChoice = 'cuda' then
  begin
    if not GCudaOK then
    begin
      MsgBox('The requested CUDA engine needs an NVIDIA driver supporting CUDA 12.4 or newer.',
             mbError, MB_OK);
      Exit;
    end;
    IsGPU := True;
  end
  else
  begin
    if GCudaOK then
      Choice := MsgBox(
        'Choose the engine for local GGUF models:' + #13#10 + #13#10 +
        'YES — NVIDIA GPU acceleration (recommended; faster)' + #13#10 +
        'Runs supported models with CUDA. Downloads about 630 MB once,' + #13#10 +
        'including the CUDA runtime.' + #13#10 + #13#10 +
        'NO — CPU (same models; slower)' + #13#10 +
        'Runs the same supported GGUF models entirely on the processor — no' + #13#10 +
        'GPU is required. Downloads about 18 MB once. Larger models need' + #13#10 +
        'enough system RAM and will generate more slowly.' + #13#10 + #13#10 +
        'CANCEL — skip local models (cloud and CLI modes still work).',
        mbConfirmation, MB_YESNOCANCEL)
    else
      Choice := MsgBox(
        'No compatible NVIDIA CUDA driver was detected.' + #13#10 + #13#10 +
        'Local GGUF models can still run on your CPU.' + #13#10 + #13#10 +
        'Choose YES to install the current CPU llama-server engine (~18 MB).' + #13#10 +
        'It supports the same current GGUF models, but generation is slower.' + #13#10 + #13#10 +
        'Choose NO to skip local models; cloud and CLI modes still work.',
        mbConfirmation, MB_YESNO);

    if Choice = IDCANCEL then Exit;
    if (not GCudaOK) and (Choice = IDNO) then Exit;
    IsGPU := GCudaOK and (Choice = IDYES);
  end;

  CacheDir := ExpandConstant('{app}\engine-cache');
  ForceDirectories(CacheDir);
  RuntimeArchive := '';
  RuntimeUrl := '';
  RuntimeSha256 := '';
  if IsGPU then
  begin
    MarkerValue := ServerBuild + '-cuda12.4';
    ServerDir := ServerRoot + '\cuda';
    MainArchive := CacheDir + '\llama-' + ServerBuild + '-bin-win-cuda-12.4-x64.zip';
    RuntimeArchive := CacheDir + '\cudart-llama-bin-win-cuda-12.4-x64.zip';
    MainUrl := BaseUrl + 'llama-' + ServerBuild + '-bin-win-cuda-12.4-x64.zip';
    RuntimeUrl := BaseUrl + 'cudart-llama-bin-win-cuda-12.4-x64.zip';
    MainSha256 := ENGINE_CUDA_SHA256;
    RuntimeSha256 := CUDART_SHA256;
  end
  else
  begin
    MarkerValue := ServerBuild + '-cpu';
    ServerDir := ServerRoot + '\cpu';
    MainArchive := CacheDir + '\llama-' + ServerBuild + '-bin-win-cpu-x64.zip';
    MainUrl := BaseUrl + 'llama-' + ServerBuild + '-bin-win-cpu-x64.zip';
    MainSha256 := ENGINE_CPU_SHA256;
  end;

  if not DownloadEngineArchives(MainUrl, MainArchive, MainSha256,
                                RuntimeUrl, RuntimeArchive, RuntimeSha256) then
    Exit;

  if not ExtractServerArchives(
       ServerDir, MainArchive, RuntimeArchive, MarkerValue) then
  begin
    MsgBox('The managed local engine could not be extracted.', mbError, MB_OK);
    Exit;
  end;

  ServerExe := ServerDir + '\llama-server.exe';
  if Exec(ServerExe, '--version', ServerDir, SW_HIDE,
          ewWaitUntilTerminated, Code) and (Code = 0) then
  begin
    GEngineInstalled := True;
    GInstalledSummary := GInstalledSummary +
      '  ' + #$2713 + '  llama-server ' + MarkerValue +
      '    (managed local GGUF engine)' + #13#10;
  end
  else
    MsgBox(
      'The managed engine was downloaded but could not start.' + #13#10 +
      'Run setup again or choose the CPU engine.', mbError, MB_OK);
end;


// ── UI packages: pystray / pillow / pynput ───────────────────────────────────
// Safety net — the bundle ships all four pre-installed, so normally this just
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
  SetArrayLength(MissingPkgs, 4);

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

  if PyRun('-c "import pynput; from importlib.metadata import version; assert tuple(map(int, version(''pynput'').split(''.''))) >= (1, 8, 2)"') then
    Present := Present + '  ✓  pynput' + #13#10
  else begin
    MissingPkgs[Count] := 'pynput>=1.8.2'; Inc(Count);
    MissingStr := MissingStr + '  •  pynput' + #13#10;
  end;

  if PyRun('-c "import comtypes"') then
    Present := Present + '  ✓  comtypes' + #13#10
  else begin
    MissingPkgs[Count] := 'comtypes>=1.4,<2'; Inc(Count);
    MissingStr := MissingStr + '  •  comtypes' + #13#10;
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
      '  "' + GPythonExe + '" -m pip install pystray pillow "pynput>=1.8.2" "comtypes>=1.4,<2"',
      mbInformation, MB_OK);
    Exit;
  end;

  InstallWithProgress(MissingPkgs, 'Installing UI packages');
end;


// ── Write a minimal config that pre-selects the downloaded model ─────────────
// Only writes if no config exists yet, so a reinstall never clobbers the user's
// existing settings. vedium's load_cfg() fills every other key from its
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


// ── Download a recommended model with a live percentage progress bar ─────────
// urllib reports bytes via a reporthook, so we can show TRUE progress (not just
// a spinner).  The download runs as a detached process (ewNoWait) that writes
// the current percent to a temp file; the wizard polls that file and drives the
// progress bar, repainting the form each tick (Refresh) so the bar animates.
function DownloadModelWithProgress(
  const Url, Sha256, Dest, ModelName, SizeLabel: String): Boolean;
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
    'import hashlib, os, urllib.request' + #13#10 +
    'url  = r"' + Url + '"' + #13#10 +
    'sha  = "' + Sha256 + '"' + #13#10 +
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
    '    h = hashlib.sha256()' + #13#10 +
    '    with open(dest, "rb") as f:' + #13#10 +
    '        for chunk in iter(lambda: f.read(1 << 20), b""):' + #13#10 +
    '            h.update(chunk)' + #13#10 +
    '    if h.hexdigest() != sha:' + #13#10 +
    '        os.remove(dest)' + #13#10 +
    '        raise ValueError("the file does not match the tested model, so it was deleted")' + #13#10 +
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
    Form.Caption      := 'Vedium — Downloading ' + ModelName;
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
    Lbl.Caption  := 'Starting download (' + SizeLabel + ')...';

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
        Lbl.Caption  := 'Downloading ' + ModelName + '...  ' + IntToStr(Pct) + '%';
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


// ── Choose between the two tested recommended models ────────────────────────
// Gemma 4 is pre-selected when a compatible NVIDIA driver was detected;
// Gemma 3 is pre-selected on CPU-only systems. Both remain available so the
// user can trade download size and speed against output quality.
function ChooseRecommendedModel(): String;
var
  Form: TForm;
  RbGemma4: TRadioButton;
  RbGemma3: TRadioButton;
  RbSkip: TRadioButton;
  BtnContinue: TNewButton;
  Intro: TNewStaticText;
  Gemma4Desc: TNewStaticText;
  Gemma3Desc: TNewStaticText;
  SkipDesc: TNewStaticText;
  Gemma4Caption: String;
  Gemma3Caption: String;
begin
  Result := 'skip';

  Gemma4Caption := 'Gemma 4 E4B Q4_K_M — best quality (~5.0 GB)';
  Gemma3Caption := 'Gemma 3 4B Q4_K_M — lightweight (~2.5 GB)';
  if GCudaOK then
    Gemma4Caption := Gemma4Caption + '  [recommended for this PC]'
  else
    Gemma3Caption := Gemma3Caption + '  [recommended for this PC]';

  Form := TForm.Create(nil);
  try
    Form.Caption      := 'Vedium — Recommended local model';
    Form.ClientWidth  := ScaleX(640);
    Form.ClientHeight := ScaleY(350);
    Form.Position     := poScreenCenter;
    Form.BorderStyle  := bsDialog;

    Intro := CreateWrappedText(
      Form, ScaleX(18), ScaleY(16), Form.ClientWidth - ScaleX(36),
      'Choose a local model. Vedium downloads it once, saves it in the install ' +
      'folder, and selects it automatically. You can switch models later.');

    RbGemma4 := TRadioButton.Create(Form);
    RbGemma4.Parent  := Form;
    RbGemma4.Left    := ScaleX(18);
    RbGemma4.Top     := ScaleY(68);
    RbGemma4.Width   := Form.ClientWidth - ScaleX(36);
    RbGemma4.Caption := Gemma4Caption;
    RbGemma4.Checked := GCudaOK;

    Gemma4Desc := CreateWrappedText(
      Form, ScaleX(42), ScaleY(92), Form.ClientWidth - ScaleX(60),
      'Our tested quality choice, especially for explanations and reading tasks. ' +
      'It works on CPU too. An 8 GB NVIDIA GPU is a practical fit for typical ' +
      'use; llama.cpp can use system RAM when full GPU offload is not possible.');

    RbGemma3 := TRadioButton.Create(Form);
    RbGemma3.Parent  := Form;
    RbGemma3.Left    := ScaleX(18);
    RbGemma3.Top     := ScaleY(150);
    RbGemma3.Width   := Form.ClientWidth - ScaleX(36);
    RbGemma3.Caption := Gemma3Caption;
    RbGemma3.Checked := not GCudaOK;

    Gemma3Desc := CreateWrappedText(
      Form, ScaleX(42), ScaleY(174), Form.ClientWidth - ScaleX(60),
      'Works on both CPU and GPU. This is the smaller, faster choice for CPU-only ' +
      'and lower-memory PCs, while remaining a capable writing and grammar model.');

    RbSkip := TRadioButton.Create(Form);
    RbSkip.Parent  := Form;
    RbSkip.Left    := ScaleX(18);
    RbSkip.Top     := ScaleY(232);
    RbSkip.Width   := Form.ClientWidth - ScaleX(36);
    RbSkip.Caption := 'Skip — add a model later';

    SkipDesc := CreateWrappedText(
      Form, ScaleX(42), ScaleY(256), Form.ClientWidth - ScaleX(60),
      'Use this for cloud or CLI mode, or if you already have another GGUF file.');

    BtnContinue := TNewButton.Create(Form);
    BtnContinue.Parent      := Form;
    BtnContinue.Caption     := 'Continue';
    BtnContinue.Width       := ScaleX(92);
    BtnContinue.Height      := ScaleY(26);
    BtnContinue.Left        := Form.ClientWidth - BtnContinue.Width - ScaleX(18);
    BtnContinue.Top         := Form.ClientHeight - BtnContinue.Height - ScaleY(16);
    BtnContinue.Default     := True;
    BtnContinue.ModalResult := mrOK;

    Form.ActiveControl := BtnContinue;
    if Form.ShowModal <> mrOK then Exit;

    if RbGemma4.Checked then
      Result := 'gemma4'
    else if RbGemma3.Checked then
      Result := 'gemma3';
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
  CfgFile: String;
  ModelChoice: String;
  ModelUrl: String;
  ModelSha256: String;
  ModelFileName: String;
  ModelName: String;
  SizeLabel: String;
begin
  GModelDownloaded     := False;
  GExistingModelConfig := False;
  GModelPath           := '';
  GModelDisplayName    := '';

  // A returning user may already point at a model anywhere on disk. Preserve
  // that configuration and do not repeat the large first-install offer.
  CfgFile := ExpandConstant('{app}\vedium_config.json');
  if FileExists(CfgFile) then
  begin
    GExistingModelConfig := True;
    GInstalledSummary := GInstalledSummary +
      '  ' + #$2713 + '  Existing model/backend configuration preserved' + #13#10;
    Exit;
  end;

  // Recover cleanly if the model file survived a manually interrupted or
  // partial reinstall but its config did not. Prefer the quality model when
  // both known files are present.
  Dest := ExpandConstant('{app}\' + GEMMA4_FILENAME);
  if FileExists(Dest) then
  begin
    GModelDownloaded  := True;
    GModelPath        := Dest;
    GModelDisplayName := 'Gemma 4 E4B';
    WriteModelConfigIfAbsent(Dest);
    GInstalledSummary := GInstalledSummary +
      '  ' + #$2713 + '  Gemma 4 E4B model   (existing file reused & selected)' + #13#10;
    Exit;
  end;

  Dest := ExpandConstant('{app}\' + GEMMA3_FILENAME);
  if FileExists(Dest) then
  begin
    GModelDownloaded  := True;
    GModelPath        := Dest;
    GModelDisplayName := 'Gemma 3 4B';
    WriteModelConfigIfAbsent(Dest);
    GInstalledSummary := GInstalledSummary +
      '  ' + #$2713 + '  Gemma 3 4B model    (existing file reused & selected)' + #13#10;
    Exit;
  end;

  ModelChoice := Lowercase(Trim(ExpandConstant('{param:VediumModel|}')));
  if ModelChoice = '' then
  begin
    if WizardSilent then
      ModelChoice := 'skip'
    else
      ModelChoice := ChooseRecommendedModel();
  end;

  if ModelChoice = 'skip' then Exit;

  if ModelChoice = 'gemma4' then
  begin
    ModelUrl      := GEMMA4_URL;
    ModelSha256   := GEMMA4_SHA256;
    ModelFileName := GEMMA4_FILENAME;
    ModelName     := 'Gemma 4 E4B';
    SizeLabel     := '~5.0 GB';
  end
  else if ModelChoice = 'gemma3' then
  begin
    ModelUrl      := GEMMA3_URL;
    ModelSha256   := GEMMA3_SHA256;
    ModelFileName := GEMMA3_FILENAME;
    ModelName     := 'Gemma 3 4B';
    SizeLabel     := '~2.5 GB';
  end
  else
    Exit;

  Dest := ExpandConstant('{app}\' + ModelFileName);
  if DownloadModelWithProgress(ModelUrl, ModelSha256, Dest, ModelName, SizeLabel) then
  begin
    GModelDownloaded  := True;
    GModelPath        := Dest;
    GModelDisplayName := ModelName;
    WriteModelConfigIfAbsent(Dest);
    GInstalledSummary :=
      GInstalledSummary +
      '  ' + #$2713 + '  ' + ModelName + ' model  (downloaded & pre-selected)' + #13#10;
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
    GInstallLogFile   := ExpandConstant('{app}\install-dependencies.log');
    GEngineInstalled  := False;
    GExistingModelConfig := False;
    // Written after [Files] finishes. On the next reinstall, [InstallDelete]
    // compares it with PythonBundleVersion before deciding whether to retain
    // or refresh the private runtime.
    SaveStringToFile(
      ExpandConstant('{app}\python\.vedium-bundle-version'),
      '{#PythonBundleVersion}' + #13#10,
      False);
    DeleteFile(GInstallLogFile);
    GInstalledSummary :=
      '  ' + #$2713 + '  Portable Python 3.12  (private to Vedium, tkinter + UI packages included)' + #13#10;

    DetectNvidiaCuda();
    CheckManagedLlamaServer();
    InstallUIDeps();   // safety net — the bundle ships these pre-installed
    // Do not encourage a large model download when the engine needed to run
    // it was skipped or failed verification.
    if GEngineInstalled then
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
    if not GEngineInstalled then
      ModelBlock :=
        '── Local model engine ─────────────────────────────' + #13#10 +
        'The local AI engine was skipped or could not be installed, so local' + #13#10 +
        'GGUF models are not available yet. Python 3.12 is installed correctly.' + #13#10 +
        'Run the installer again and choose a local CPU or GPU engine.' + #13#10 +
        'Cloud API, CLI, and manually configured server modes are unaffected.' + #13#10 + #13#10
    else if GExistingModelConfig then
      ModelBlock :=
        '── Grammar / LLM model ─────────────────────────────' + #13#10 +
        'Your existing model and backend configuration was preserved. Vedium' + #13#10 +
        'will keep using the model or backend you selected previously.' + #13#10 + #13#10
    else if GModelDownloaded then
      ModelBlock :=
        '── Grammar / LLM model ─────────────────────────────' + #13#10 +
        'The ' + GModelDisplayName + ' model is installed and already selected — you are ready' + #13#10 +
        'to go. To switch models later, use Settings → Browse next to' + #13#10 +
        '"Model (.gguf)".' + #13#10 + #13#10
    else
      ModelBlock :=
        '── Grammar / LLM model ─────────────────────────────' + #13#10 +
        'You still need a GGUF model file. Recommended:' + #13#10 + #13#10 +
        '  Best quality (~5.0 GB): lmstudio-community/gemma-4-E4B-it-GGUF' + #13#10 +
        '  Lightweight  (~2.5 GB): unsloth/gemma-3-4b-it-GGUF' + #13#10 + #13#10 +
        'Current managed-engine builds support Gemma 4 and Qwen3.5 GGUF files.' + #13#10 + #13#10 +
        'Search Hugging Face for the model name above, download the .gguf' + #13#10 +
        'file, then in Settings click Browse next to "Model (.gguf)".' + #13#10 +
        'Leave "Prompt format" on Auto. Click Save & Load Model.' + #13#10 + #13#10;

    if not WizardSilent then
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
Type: files; Name: "{app}\install-engine.log"
Type: files; Name: "{app}\install-dependencies.log"
Type: files; Name: "{app}\managed-server.log"
; Managed llama-server and its backend/runtime DLLs.
Type: filesandordirs; Name: "{app}\llama-server"
; Versioned engine downloads are retained across reinstalls but removed on a true uninstall.
Type: filesandordirs; Name: "{app}\engine-cache"
; Models downloaded by the installer (or later by the user) into the app folder
Type: files; Name: "{app}\*.gguf"
; Bundled Python — removes runtime caches and any dependency-repair packages
; that were not part of the original [Files] manifest.
Type: filesandordirs; Name: "{app}\python"
; App package — removes runtime __pycache__ dirs that [Files] doesn't know
; about, plus a pre-rename spell_daemon dir if upgrading from before v1.2
Type: filesandordirs; Name: "{app}\vedium"
Type: filesandordirs; Name: "{app}\spell_daemon"
