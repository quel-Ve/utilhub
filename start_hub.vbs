' Utility Hub 启动器 — 无窗口触发计划任务 (最高权限运行, 无需 UAC)
' 供开始菜单快捷方式调用: wscript.exe start_hub.vbs
Set sh = CreateObject("WScript.Shell")
sh.Run "schtasks /run /tn UtilityHub", 0, False
