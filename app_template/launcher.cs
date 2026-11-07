// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Larpbase
// Unix launcher: starts the bundled engine with the Unix application template.
// Built by scripts/build_app.py with the .NET Framework C# compiler that ships with Windows.
using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Windows.Forms;

static class Launcher
{
    const string Engine = "unix.exe";
    const string Template = "Unix";

    [System.Runtime.InteropServices.DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr window);
    [System.Runtime.InteropServices.DllImport("user32.dll")] static extern bool ShowWindow(IntPtr window, int how);
    [System.Runtime.InteropServices.DllImport("user32.dll")] static extern bool IsIconic(IntPtr window);

    static string Quote(string arg)
    {
        if (arg.Length > 0 && arg.IndexOfAny(new[] { ' ', '\t', '"' }) < 0) return arg;
        return "\"" + arg.Replace("\\\"", "\\\\\"").Replace("\"", "\\\"") + "\"";
    }

    [STAThread]
    static int Main(string[] args)
    {
        string dir = AppDomain.CurrentDomain.BaseDirectory;
        string engine = Path.Combine(dir, Engine);
        if (!File.Exists(engine))
        {
            MessageBox.Show("Unix could not find " + Engine + " next to the launcher.", "Unix",
                            MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        // One copy at a time. A second copy shares the first one's settings and chats, and the graphics driver
        // gives it an empty shader cache (the first copy holds the cache file), so its first render freezes the
        // window for most of a minute. Starting the app again brings the running one to the front instead.
        if (Array.IndexOf(args, "--new-instance") < 0)
        {
            foreach (Process other in Process.GetProcessesByName(Path.GetFileNameWithoutExtension(Engine)))
            {
                try
                {
                    if (!string.Equals(other.MainModule.FileName, engine, StringComparison.OrdinalIgnoreCase)) continue;
                    IntPtr window = other.MainWindowHandle;
                    if (window != IntPtr.Zero)
                    {
                        if (IsIconic(window)) ShowWindow(window, 9);   // SW_RESTORE
                        SetForegroundWindow(window);
                    }
                    return 0;                                          // running, or still starting up
                }
                catch (Exception) { }                                  // a process we may not look into: not ours
            }
        }
        var line = new StringBuilder(Template.Length > 0 ? "--app-template " + Template : "");
        foreach (string arg in args)
            if (arg != "--new-instance") line.Append(line.Length > 0 ? " " : "").Append(Quote(arg));
        var info = new ProcessStartInfo(engine, line.ToString());
        info.UseShellExecute = false;
        info.CreateNoWindow = true;   // the engine is a console program; keep its console hidden
        try
        {
            Process.Start(info);
        }
        catch (Exception error)
        {
            MessageBox.Show("Unix failed to start:\n" + error.Message, "Unix",
                            MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        return 0;
    }
}
