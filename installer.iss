; ════════════════════════════════════════════════════════════════════
;  ManageBac-packer 安装包脚本（Inno Setup 6）
;
;  产物：一个 .exe。对方双击 → 下一步 → 装完自动启动 → 填账号密码 → 用。
;        不需要解压任何文件夹，不需要 Python。
;
;  编译：
;    & "C:\Users\<你>\AppData\Local\Programs\Inno Setup 6\ISCC.exe" installer.iss
;
;  设计说明
;    · 装到 %LOCALAPPDATA%\ManageBac-packer（不需要管理员权限）
;      —— 学生电脑常没有管理员权限，要求 UAC 会直接劝退一半人
;    · WebView2 是唯一的外部依赖，Win11 与较新 Win10 已预装；
;      没装的话装包时提示 + 打开下载页
;    · 开机自启由程序自己在首次填完账号后安装（见 widget.py），
;      不在这里装 —— 因为用户没填账号之前预热没意义
; ════════════════════════════════════════════════════════════════════

#define MyAppName "ManageBac-packer"
#define MyAppVersion "0.4.6"
#define MyAppPublisher "zhyunran"
#define MyAppURL "https://github.com/zhyunran/ManageBac-packer"
#define MyAppExeName "ManageBac-packer.exe"

[Setup]
AppId={{8F3A2C41-7B5E-4D9A-A1C8-4E6F2B9D3A57}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
DefaultDirName={localappdata}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes

;  免安装到 Program Files —— 不需要管理员权限
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

;  单文件安装包
OutputDir=release
OutputBaseFilename=ManageBac-packer-{#MyAppVersion}-安装包
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern

;  图标（有就带上，没有就跳过）
UninstallDisplayIcon={app}\{#MyAppExeName}
DisableDirPage=auto
DisableReadyPage=no

;  界面语言
;   Inno Setup 官方安装包不含简体中文语言文件（那是社区翻译）。
;   为了不因为缺一个 .isl 就编译失败，这里用英文界面，
;   但向导里所有「用户真正会读」的文案都覆盖成中文。
;   如果以后把 ChineseSimplified.isl 放进 Languages\ 目录，
;   把下面两行换成：Name: "cn"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
ShowLanguageDialog=no

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
;  下面这些是用户在向导里会看到的关键文案
en.AppName={#MyAppName}
en.WelcomeLabel2=即将安装 [name/ver] 到你的电脑。%n%n装好后打开，填学校账号和密码就能用。%n%n这个软件只读取你的作业和成绩，不会修改任何东西。
en.FinishedLabel=安装完成。%n%n点「完成」打开它，用学校账号登录即可。
en.RunLabel=打开 {#MyAppName}
en.CreateDesktopIcon=创建桌面快捷方式
en.AdditionalIcons=附加选项：
en.SelectDirLabel3=安装到下面这个位置（一般不用改）：
en.SelectTasksLabel2=要创建桌面快捷方式吗？

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; \
    GroupDescription: "附加选项:"; Flags: checkedonce

[Files]
;  exe 本体
Source: "dist\ManageBac-packer.exe"; DestDir: "{app}"; \
    Flags: ignoreversion
;  说明文档（能帮用户看懂这是干什么的）
Source: "_tools\_update_report.md"; DestDir: "{app}"; \
    DestName: "更新报告.md"; Flags: ignoreversion
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "LICENSE"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
;  开始菜单
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
;  桌面（用户勾了才建）
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; \
    Tasks: desktopicon

[Run]
;  装完直接启动 —— 少一步「去哪找图标」
Filename: "{app}\{#MyAppExeName}"; \
    Description: "立即打开 {#MyAppName}"; \
    Flags: nowait postinstall skipifsilent

[UninstallRun]
;  卸载时清掉开机自启的快捷方式（不然开机还会报找不到文件）
Filename: "{app}\{#MyAppExeName}"; Parameters: "--uninstall-cleanup"; \
    Flags: runhidden; RunOnceId: "CleanAutostart"

[UninstallDelete]
;  只删程序自己，不动用户的数据（在 %LOCALAPPDATA%\ManageBac-packer\data）
Type: files; Name: "{app}\*.md"

[Code]
// ══════════ WebView2 检测 ══════════
//  Windows 11 与较新的 Win10 都预装了。没有的话提前告诉他，
//  别等装完打不开才发现。
function WebView2Installed(): Boolean;
var
  key: String;
  ver: String;
begin
  Result := False;

  //  ① 查 64 位机器的注册表
  key := 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\' +
         '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  if RegQueryStringValue(HKLM, key, 'pv', ver) and (ver <> '') then
  begin
    Result := True;
    exit;
  end;

  //  ② 查当前用户安装的（有些是用户级安装）
  key := 'SOFTWARE\Microsoft\EdgeUpdate\Clients\' +
         '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  if RegQueryStringValue(HKCU, key, 'pv', ver) and (ver <> '') then
  begin
    Result := True;
    exit;
  end;

  //  ③ 兜底：看安装目录在不在
  if DirExists(ExpandConstant('{pf32}\Microsoft\EdgeWebView\Application')) then
    Result := True;
  if DirExists(ExpandConstant('{pf}\Microsoft\EdgeWebView\Application')) then
    Result := True;
end;

function InitializeSetup(): Boolean;
var
  err: Integer;
begin
  Result := True;
  if not WebView2Installed() then
  begin
    if MsgBox('这台电脑缺少一个微软的运行组件（WebView2），' + #13#10 +
              '不装的话软件打不开。' + #13#10 + #13#10 +
              '现在打开下载页吗？（装完那个组件再运行本安装包）',
              mbConfirmation, MB_YESNO) = IDYES then
    begin
      ShellExec('open',
        'https://go.microsoft.com/fwlink/p/?LinkId=2124703',
        '', '', SW_SHOWNORMAL, ewNoWait, err);
    end;
    //  还是让他继续 —— 有些人其实已经装了，只是我们没查出来
  end;
end;
