Dim objShell
Set objShell = CreateObject("WScript.Shell")

' 背景啟動 Bot 伺服器 (0 代表隱藏，False 代表不用等它跑完)
objShell.Run chr(34) & "D:\line-bot-shift-manager\run_bot.bat" & chr(34), 0, False

' 等待 3 秒讓 Bot 先啟動
WScript.Sleep 3000

' 背景啟動 ngrok 通道
objShell.Run chr(34) & "D:\line-bot-shift-manager\run_ngrok.bat" & chr(34), 0, False

Set objShell = Nothing