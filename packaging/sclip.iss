; Inno Setup definition for the S-Clip Windows installer.
;
; Normally driven by scripts/build_windows_installer.py, which freezes the
; application first and passes the version and paths in as defines. To compile
; it directly:
;
;     ISCC.exe /DAppVersion=2.0.0 packaging\sclip.iss
;
; Note on user data: S-Clip keeps settings, logs and recorded clips under
; %APPDATA%\S-Clip, which is deliberately outside the install directory. The
; uninstaller therefore removes the program and leaves the user's recordings
; alone. Do not add an [UninstallDelete] entry for that folder — deleting
; somebody's saved clips because they uninstalled the recorder would be a
; hostile thing to do.

#define AppName "S-Clip"
#define AppPublisher "Shiloh Malka"
#define AppURL "https://github.com/shilohhm/S-Clip"
#define AppExeName "S-Clip.exe"

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

#ifndef SourceDir
  #define SourceDir "..\build\dist\S-Clip"
#endif

#ifndef OutputDir
  #define OutputDir "..\build\installer"
#endif

[Setup]
; A fixed AppId is what lets a later version recognise and upgrade this one in
; place. It must never change between releases.
AppId={{8B5F2A31-9C4D-4E7A-B6F1-3D8E5C2A7B94}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
VersionInfoVersion={#AppVersion}

; Installing per-user rather than machine-wide. S-Clip is a single-user desktop
; tool that writes only to the user's own profile, so there is nothing to gain
; from an administrative install -- and plenty to lose, since a UAC prompt on an
; unsigned installer is exactly the moment a cautious person abandons it.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes

; The capture path relies on Desktop Duplication and WASAPI loopback, neither
; of which is present before Windows 10, and the build is 64-bit only.
MinVersion=10.0
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

LicenseFile=..\LICENSE
OutputDir={#OutputDir}
OutputBaseFilename=S-Clip-{#AppVersion}-windows-x64-setup
SetupIconFile=..\src\sclip\ui\assets\icon.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName} {#AppVersion}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes

; S-Clip lives in the tray, so the overwhelmingly likely state during an upgrade
; or an uninstall is "currently running". Left to itself that means locked DLLs
; and a half-removed install directory. "force" asks the application to close
; through the Restart Manager first and terminates it only if it will not go,
; which is the right trade for a capture tool the user has just chosen to
; remove or replace.
CloseApplications=force
RestartApplications=no

; Code signing is wired here but intentionally left unconfigured: signing needs
; a certificate tied to a real identity, which this project does not have.
; Provide one by defining a "sclip" sign tool in the Inno Setup IDE (or via
; ISCC /S) and uncommenting the line below. Until then, builds are unsigned and
; SmartScreen will warn on first run -- which the README says plainly.
; SignTool=sclip

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
; Unchecked by default. An installer that litters the desktop without being
; asked is a small rudeness that people remember.
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[UninstallDelete]
; Uninstalling while the app was running leaves the file removal fine but the
; directory removal short: Windows keeps handles on the folders that held
; loaded DLLs for a moment after the process dies, so the nested plugin
; directories survive as empty shells. "_internal" is entirely PyInstaller's
; output, so clearing it wholesale is safe and does not hardcode a layout that
; will drift with the next PySide6 release.
Type: filesandordirs; Name: "{app}\_internal"
Type: dirifempty; Name: "{app}"

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[Code]
function FFmpegIsAvailable(): Boolean;
var
  ResultCode: Integer;
begin
  { "where" exits non-zero when it finds nothing, which is all we need. }
  Result := Exec(
    ExpandConstant('{cmd}'), '/C where ffmpeg >nul 2>&1', '',
    SW_HIDE, ewWaitUntilTerminated, ResultCode
  ) and (ResultCode = 0);
end;

function AppIsRunning(): Boolean;
var
  ResultCode: Integer;
  ListingPath: String;
  Listing: AnsiString;
begin
  { tasklist's output is captured through a file rather than a pipe. Piping
    inside Exec's parameter string depends on cmd parsing quoting the way we
    expect, and getting that subtly wrong fails silently -- it simply reports
    "not running" forever, which is exactly the bug this replaced. }
  ListingPath := ExpandConstant('{tmp}\sclip-tasklist.txt');
  Result := False;
  if Exec(
    ExpandConstant('{cmd}'),
    '/C tasklist /NH /FI "IMAGENAME eq {#AppExeName}" > "' + ListingPath + '"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode
  ) then
  begin
    if LoadStringFromFile(ListingPath, Listing) then
      Result := Pos(Lowercase('{#AppExeName}'), Lowercase(String(Listing))) > 0;
    DeleteFile(ListingPath);
  end;
end;

function CloseRunningApp(): Boolean;
var
  ResultCode: Integer;
  Waited: Integer;
begin
  { Full path on purpose. Exec calls CreateProcess without a PATH search of its
    own, so a bare "taskkill.exe" simply fails to launch and the close silently
    does nothing. }
  Exec(
    ExpandConstant('{sys}\taskkill.exe'), '/IM "{#AppExeName}" /F', '',
    SW_HIDE, ewWaitUntilTerminated, ResultCode
  );

  { Windows releases the file handles a moment after the process dies. Removing
    the directory too early is what leaves a half-uninstalled folder behind, so
    wait for the name to actually disappear rather than trusting a fixed sleep. }
  Waited := 0;
  while AppIsRunning() and (Waited < 10000) do
  begin
    Sleep(500);
    Waited := Waited + 500;
  end;
  Sleep(1000);
  Result := not AppIsRunning();
end;

function RunningSilently(): Boolean;
begin
  { One [Code] section serves both Setup and the uninstaller, but the two have
    different functions for this. WizardSilent is Setup-only; reaching for it
    from the uninstaller is a script error, and a script error there aborts the
    whole uninstall -- which presents as "nothing was removed at all". }
  if IsUninstaller() then
    Result := UninstallSilent()
  else
    Result := WizardSilent();
end;

{ S-Clip runs from the tray, so "already running" is the normal state when
  somebody upgrades or uninstalls it. Inno's own CloseApplications support
  leans on the Restart Manager, which does not reliably claim this process --
  the observed result was a terminated shortcut but thirty-odd orphaned files
  left in the install directory. Closing it explicitly is less elegant and
  considerably more dependable. }
function EnsureAppClosed(const ActionText: String): Boolean;
begin
  Result := True;
  if not AppIsRunning() then
    Exit;

  if not RunningSilently() then
  begin
    if MsgBox(
      '{#AppName} is currently running.' #13#10 #13#10 +
      'It needs to close before ' + ActionText + '. Any recording in progress '
      + 'will be stopped.' #13#10 #13#10 +
      'Close {#AppName} and continue?',
      mbConfirmation, MB_YESNO) = IDNO then
    begin
      Result := False;
      Exit;
    end;
  end;

  if not CloseRunningApp() and not RunningSilently() then
  begin
    { Warn, but do not refuse. Aborting here would be worse than continuing:
      Inno reports any genuinely locked file itself, whereas a refusal leaves
      the whole install directory in place with nothing cleaned up at all. }
    MsgBox(
      '{#AppName} could not be closed automatically.' #13#10 #13#10 +
      'If anything is left behind, close it from the system tray and run this '
      + 'again.',
      mbInformation, MB_OK);
  end;
end;

function InitializeUninstall(): Boolean;
begin
  Result := EnsureAppClosed('it can be removed');
end;

function InitializeSetup(): Boolean;
begin
  Result := EnsureAppClosed('the installation can continue');
  if not Result then
    Exit;

  { S-Clip drives FFmpeg to capture and encode. Without it the interface still
    opens and explains itself on the About page, but nothing can be recorded --
    so it is far kinder to say so before installing than to let someone
    discover it when they press the clip key mid-game. }
  if not FFmpegIsAvailable() then
  begin
    if MsgBox(
      'FFmpeg was not found on this PC.' #13#10 #13#10 +
      'S-Clip uses FFmpeg to capture and encode; without it the app will open '
      + 'but will not be able to record.' #13#10 #13#10 +
      'You can install it afterwards with:' #13#10 +
      '    winget install Gyan.FFmpeg' #13#10 #13#10 +
      'Continue with the installation?',
      mbConfirmation, MB_YESNO) = IDNO then
    begin
      Result := False;
    end;
  end;
end;
