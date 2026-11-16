Option Explicit
Dim shell, files, folder, pythonw, version, root, candidate
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
folder = files.GetParentFolderName(WScript.ScriptFullName)
pythonw = folder & "\.venv\Scripts\pythonw.exe"

If Not files.FileExists(pythonw) Then
  pythonw = ""
  For Each version In Array("3.14", "3.13", "3.12", "3.11", "3.10")
    For Each root In Array("HKCU\Software\Python\PythonCore\", "HKLM\Software\Python\PythonCore\")
      candidate = ""
      On Error Resume Next
      candidate = shell.RegRead(root & version & "\InstallPath\WindowedExecutablePath")
      Err.Clear
      On Error GoTo 0
      If candidate <> "" Then
        If files.FileExists(candidate) Then
          pythonw = candidate
          Exit For
        End If
      End If
    Next
    If pythonw <> "" Then Exit For
  Next
End If

If pythonw = "" Then
  MsgBox "Python was not found. Follow the setup steps in README.md.", 48, "Galaxy Book3 Pro"
  WScript.Quit 1
End If
shell.CurrentDirectory = folder
shell.Run Chr(34) & pythonw & Chr(34) & " " & Chr(34) & folder & "\widget.py" & Chr(34), 0, False
