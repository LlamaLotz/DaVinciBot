#define AppName "DaVinciBot"
#define AppVersion "0.1.0"
#define AppExeName "DaVinciBot.exe"

[Setup]
AppId={{7D511F48-01EA-48B7-A544-944A7A2DCF1C}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\DaVinciBot
DefaultGroupName=DaVinciBot
PrivilegesRequired=lowest
OutputBaseFilename=DaVinciBot-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Files]
Source: "..\dist\DaVinciBot.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\DaVinciBot"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\DaVinciBot"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch DaVinciBot"; Flags: nowait postinstall skipifsilent

