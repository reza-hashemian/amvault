; Inno Setup script: wraps the frozen app (dist\amvault) in a setup program.
;
;   set AMVAULT_VERSION=0.1.0
;   iscc packaging\amvault.iss
;
; Installs for the current user only, so it needs no administrator rights.

#define AppVersion GetEnv("AMVAULT_VERSION")
#if AppVersion == ""
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6F0C2B5E-7A1D-4E0B-9C55-2D3A1B8E4F10}
AppName=amvault
AppVersion={#AppVersion}
AppPublisher=reza-hashemian
DefaultDirName={autopf}\amvault
DefaultGroupName=amvault
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=amvault-{#AppVersion}-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\amvault.exe

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "..\dist\amvault\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\amvault"; Filename: "{app}\amvault.exe"
Name: "{autodesktop}\amvault"; Filename: "{app}\amvault.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\amvault.exe"; Description: "Start amvault"; Flags: nowait postinstall skipifsilent
