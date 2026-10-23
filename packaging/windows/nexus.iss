; Inno Setup 6 script for the Windows edition. Packs dist\<AppName>\ (built by
; nexus.spec) into <AppName>-Setup-<version>.exe.
;
;   iscc /DAppVersion=0.3.0 packaging\windows\nexus.iss
;
; Per user, no admin: installs to %LOCALAPPDATA%\Programs\<AppName>, adds a
; Start Menu entry and, if ticked, a Startup shortcut that starts Nexus in
; the tray. The uninstaller leaves the user's data (%APPDATA%\<AppName>,
; %LOCALAPPDATA%\<AppName>) unless they say yes to removing it.
;
; Everything below can be overridden with /D on the iscc command line; the
; CI passes AppName (platform.windows.APP_NAME) and AppVersion
; (nexus.__version__) so neither is written down twice.

#ifndef AppName
  #define AppName "nexus-launcher"
#endif
#ifndef DisplayName
  #define DisplayName "Nexus"
#endif
#ifndef DistDir
  #define DistDir AddBackslash(SourcePath) + "..\..\dist\" + AppName
#endif
#ifndef OutputDir
  #define OutputDir AddBackslash(SourcePath) + "..\..\dist"
#endif
#ifndef IconFile
  ; Where nexus.spec leaves the icon it renders (pyinstaller's default workpath).
  #define IconFile AddBackslash(SourcePath) + "..\..\build\nexus\" + AppName + ".ico"
#endif
#define AppExe AppName + ".exe"
#ifndef AppVersion
  ; Not given: the version the spec stamped into the exe.
  #define AppVersion GetStringFileInfo(DistDir + "\" + AppExe, "ProductVersion")
#endif
#if AppVersion == ""
  #error AppVersion is unknown: pass /DAppVersion=x.y.z
#endif

[Setup]
; Never change AppId: it is how an update finds the installed copy.
AppId={{61CDF7BE-C9C7-4EE4-ACB5-6DFDFDA45A61}
AppName={#DisplayName}
AppVersion={#AppVersion}
AppVerName={#DisplayName} {#AppVersion}
AppPublisher=cyberpunk89
AppPublisherURL=https://github.com/cyberpunk89/nexus-launcher
AppSupportURL=https://github.com/cyberpunk89/nexus-launcher/issues
; Per user: {userpf} is %LOCALAPPDATA%\Programs. No UAC prompt, ever.
PrivilegesRequired=lowest
DefaultDirName={userpf}\{#AppName}
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
; A running Nexus (or a CLI job) holds files open: offer to close it.
CloseApplications=yes
RestartApplications=no
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#DisplayName}
VersionInfoProductName={#DisplayName}
VersionInfoProductTextVersion={#AppVersion}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
OutputDir={#OutputDir}
OutputBaseFilename={#AppName}-Setup-{#AppVersion}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "startup"; Description: "Start {#DisplayName} when I sign in to Windows (in the tray)"; \
    GroupDescription: "Other:"; Flags: unchecked

[InstallDelete]
; An update replaces the whole bundle: no stale DLLs or modules from the
; previous version left in _internal.
Type: filesandordirs; Name: "{app}\_internal"
; No "Tasks: not startup" delete of the Startup shortcut here: Settings ->
; "Start with Windows" writes the same Nexus.lnk (cli/agent.py STARTUP_NAME),
; and an update with the box unticked would silently take it away.

[UninstallDelete]
; The Startup shortcut, also when Settings made it rather than this setup:
; left behind, it would point at a deleted exe.
Type: files; Name: "{userstartup}\{#DisplayName}.lnk"

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\{#DisplayName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"
Name: "{userstartup}\{#DisplayName}"; Filename: "{app}\{#AppExe}"; Parameters: "--hidden"; \
    WorkingDir: "{app}"; Tasks: startup

[Run]
Filename: "{app}\{#AppExe}"; Description: "Start {#DisplayName}"; \
    Flags: nowait postinstall skipifsilent

[UninstallRun]
; Stop Nexus and its CLI jobs so their files can be removed.
Filename: "{sys}\taskkill.exe"; Parameters: "/f /t /im {#AppExe}"; Flags: runhidden; \
    RunOnceId: "StopGui"
Filename: "{sys}\taskkill.exe"; Parameters: "/f /t /im nexus-cli.exe"; Flags: runhidden; \
    RunOnceId: "StopCli"

[Code]
// The uninstaller keeps settings, the library, artwork and logs, so a
// reinstall picks up where it left off, unless the user asks otherwise.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Roaming, Local, Menu: String;
begin
  if (CurUninstallStep <> usPostUninstall) or UninstallSilent then
    Exit;
  Roaming := ExpandConstant('{userappdata}\{#AppName}');
  Local := ExpandConstant('{localappdata}\{#AppName}');
  // Games' Start Menu shortcuts (platform.windows.app_menu_dir).
  Menu := ExpandConstant('{userprograms}\{#AppName}');
  if not (DirExists(Roaming) or DirExists(Local) or DirExists(Menu)) then
    Exit;
  if MsgBox('Also delete your {#DisplayName} settings, game library, save backups, artwork and logs?' + #13#10#13#10 +
            Roaming + #13#10 + Local + #13#10 + Menu + #13#10#13#10 +
            'Your installed games are not touched either way. Choose No to keep this ' +
            'data for a later reinstall.',
            mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
  begin
    DelTree(Roaming, True, True, True);
    DelTree(Local, True, True, True);
    DelTree(Menu, True, True, True);
  end;
end;
