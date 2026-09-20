' ManageBac 后台预热启动器（由 make_startup.py 自动生成，勿手动修改）
' 作用：开机后静默抓取数据，全程不显示任何窗口。
Option Explicit
Dim sh, cmd
Set sh = CreateObject("WScript.Shell")
WScript.Sleep 20000
cmd = "D:\Python hub\campus-pulse\.venv\Scripts\pythonw.exe" & " " & "D:\Python hub\campus-pulse\warmup.py" & " --quiet"
sh.CurrentDirectory = "D:\Python hub\campus-pulse"
' 参数2 = 0 → 隐藏窗口；参数3 = False → 不等待返回
sh.Run cmd, 0, False
