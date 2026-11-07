// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Larpbase
// Unix installer and uninstaller, one source. Built by scripts/build_release.py with the .NET Framework C# compiler
// that ships with Windows (C# 5: no string interpolation, no ?. operator).
//
//   setup:      the compiled stub with the app's zip appended:  [stub][zip][8-byte zip length][8-byte magic]
//   uninstall:  the same source compiled with /define:UNINSTALL, shipped inside the app folder.
//
// Installs per user (no admin): %LOCALAPPDATA%\Programs\<App>, Start menu shortcut, optional desktop shortcut,
// an entry in Settings > Apps. Re-installing over an existing copy keeps the user's settings and chats.
//
// Test switches (no window): --quiet --dir <folder> [--no-shortcuts] [--no-register]
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Text;
using System.Threading;
using System.Windows.Forms;
using Microsoft.Win32;

[assembly: System.Reflection.AssemblyTitle("@NAME@")]
[assembly: System.Reflection.AssemblyProduct("@NAME@")]
[assembly: System.Reflection.AssemblyCompany("Larpbase")]
[assembly: System.Reflection.AssemblyCopyright("Copyright (C) 2026 Larpbase. GPL-3.0-or-later.")]
[assembly: System.Reflection.AssemblyVersion("@VERSION@.0")]
[assembly: System.Reflection.AssemblyFileVersion("@VERSION@.0")]

static class Product
{
    public const string Name = "@NAME@";
    public const string Version = "@VERSION@";
    public const string Launcher = "@NAME@.exe";
    public const string Engine = "@ENGINE@";
    public const string Uninstaller = "uninstall.exe";
    public const string Magic = "UNIXPAK1";
    public const string ClaudeHelp = "https://docs.anthropic.com/en/docs/claude-code/setup";
    public static readonly Color Accent = Color.FromArgb(124, 58, 237);
    public static readonly string RegKey = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\" + Name;

    public static string DefaultDir()
    {
        return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", Name);
    }

    public static string StartMenuLink()
    {
        return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Programs), Name + ".lnk");
    }

    public static string DesktopLink()
    {
        return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory), Name + ".lnk");
    }

    /// The app's engine running from this folder, if any.
    public static bool Running(string dir)
    {
        string engine = Path.GetFullPath(Path.Combine(dir, Engine));
        foreach (Process p in Process.GetProcessesByName(Path.GetFileNameWithoutExtension(Engine)))
        {
            try
            {
                if (string.Equals(p.MainModule.FileName, engine, StringComparison.OrdinalIgnoreCase)) return true;
            }
            catch (Exception) { }
        }
        return false;
    }

    /// Where the Claude CLI would be found by the app (agent/runner.py looks in the same places).
    public static bool ClaudeFound()
    {
        var places = new List<string>();
        string path = Environment.GetEnvironmentVariable("PATH") ?? "";
        foreach (string d in path.Split(';'))
        {
            if (d.Trim().Length == 0) continue;
            places.Add(Path.Combine(d.Trim(), "claude.exe"));
            places.Add(Path.Combine(d.Trim(), "claude.cmd"));
        }
        string home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        places.Add(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), @"npm\claude.cmd"));
        places.Add(Path.Combine(home, @".local\bin\claude.exe"));
        places.Add(Path.Combine(home, @".claude\local\claude.exe"));
        foreach (string p in places)
        {
            try { if (File.Exists(p)) return true; } catch (Exception) { }
        }
        return false;
    }

    public static void MakeLink(string link, string target, string workDir)
    {
        Type shellType = Type.GetTypeFromProgID("WScript.Shell");
        object shell = Activator.CreateInstance(shellType);
        object lnk = shellType.InvokeMember("CreateShortcut", System.Reflection.BindingFlags.InvokeMethod, null, shell,
                                            new object[] { link });
        Type t = lnk.GetType();
        t.InvokeMember("TargetPath", System.Reflection.BindingFlags.SetProperty, null, lnk, new object[] { target });
        t.InvokeMember("WorkingDirectory", System.Reflection.BindingFlags.SetProperty, null, lnk, new object[] { workDir });
        t.InvokeMember("IconLocation", System.Reflection.BindingFlags.SetProperty, null, lnk, new object[] { target + ",0" });
        t.InvokeMember("Description", System.Reflection.BindingFlags.SetProperty, null, lnk,
                       new object[] { Name + ": an AI 3D artist" });
        t.InvokeMember("Save", System.Reflection.BindingFlags.InvokeMethod, null, lnk, null);
    }

    public static Form Window(string title, int w, int h)
    {
        var f = new Form();
        f.Text = title;
        f.ClientSize = new Size(w, h);
        f.FormBorderStyle = FormBorderStyle.FixedDialog;
        f.MaximizeBox = false;
        f.StartPosition = FormStartPosition.CenterScreen;
        f.BackColor = Color.White;
        f.Font = new Font("Segoe UI", 9.5f);
        try { f.Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch (Exception) { }
        var band = new Panel();
        band.BackColor = Accent;
        band.Dock = DockStyle.Top;
        band.Height = 64;
        var head = new Label();
        head.Text = Name;
        head.ForeColor = Color.White;
        head.Font = new Font("Segoe UI Semibold", 18f);
        head.AutoSize = true;
        head.Location = new Point(20, 14);
        band.Controls.Add(head);
        f.Controls.Add(band);
        return f;
    }
}

/// A read-only window onto part of a file: the zip appended to the setup program.
class Slice : Stream
{
    readonly Stream inner; readonly long start, length; long pos;
    public Slice(Stream inner, long start, long length) { this.inner = inner; this.start = start; this.length = length; }
    public override bool CanRead { get { return true; } }
    public override bool CanSeek { get { return true; } }
    public override bool CanWrite { get { return false; } }
    public override long Length { get { return length; } }
    public override long Position { get { return pos; } set { pos = value; } }
    public override void Flush() { }
    public override void SetLength(long v) { throw new NotSupportedException(); }
    public override void Write(byte[] b, int o, int c) { throw new NotSupportedException(); }
    public override long Seek(long offset, SeekOrigin origin)
    {
        if (origin == SeekOrigin.Begin) pos = offset;
        else if (origin == SeekOrigin.Current) pos += offset;
        else pos = length + offset;
        return pos;
    }
    public override int Read(byte[] buffer, int offset, int count)
    {
        long left = length - pos;
        if (left <= 0) return 0;
        if (count > left) count = (int)left;
        inner.Position = start + pos;
        int n = inner.Read(buffer, offset, count);
        pos += n;
        return n;
    }
}

#if !UNINSTALL
static class Setup
{
    static string dir;
    static bool quiet, shortcuts = true, register = true, desktop = true;

    [STAThread]
    static int Main(string[] args)
    {
        dir = Product.DefaultDir();
        for (int i = 0; i < args.Length; i++)
        {
            if (args[i] == "--quiet") quiet = true;
            else if (args[i] == "--dir" && i + 1 < args.Length) dir = args[++i];
            else if (args[i] == "--no-shortcuts") shortcuts = false;
            else if (args[i] == "--no-register") register = false;
        }
        if (quiet)
        {
            try { Install(delegate (int pct, string what) { }); Console.WriteLine("installed " + dir); return 0; }
            catch (Exception e) { Console.Error.WriteLine(e); return 1; }
        }
        Application.EnableVisualStyles();
        Application.Run(MakeForm());
        return 0;
    }

    static Form MakeForm()
    {
        Form f = Product.Window(Product.Name + " Setup", 520, 330);
        var intro = new Label();
        intro.Text = "An AI 3D artist. Version " + Product.Version + ". About 1 GB on disk.\n\nInstall to:";
        intro.Location = new Point(20, 80); intro.Size = new Size(480, 56);
        var box = new TextBox();
        box.Text = dir; box.Location = new Point(20, 138); box.Size = new Size(390, 26);
        var browse = new Button();
        browse.Text = "Browse..."; browse.Location = new Point(416, 136); browse.Size = new Size(84, 28);
        browse.Click += delegate
        {
            using (var pick = new FolderBrowserDialog())
            {
                if (pick.ShowDialog(f) == DialogResult.OK) box.Text = Path.Combine(pick.SelectedPath, Product.Name);
            }
        };
        var desk = new CheckBox();
        desk.Text = "Put a shortcut on the desktop"; desk.Checked = true;
        desk.Location = new Point(20, 176); desk.AutoSize = true;
        var bar = new ProgressBar();
        bar.Location = new Point(20, 214); bar.Size = new Size(480, 18); bar.Visible = false;
        var status = new Label();
        status.Location = new Point(20, 238); status.Size = new Size(480, 40);
        var go = new Button();
        go.Text = "Install"; go.Location = new Point(400, 284); go.Size = new Size(100, 32);
        go.BackColor = Product.Accent; go.ForeColor = Color.White; go.FlatStyle = FlatStyle.Flat;
        var cancel = new Button();
        cancel.Text = "Cancel"; cancel.Location = new Point(292, 284); cancel.Size = new Size(100, 32);
        cancel.Click += delegate { f.Close(); };
        f.AcceptButton = go;
        bool done = false;
        go.Click += delegate
        {
            if (done)
            {
                Process.Start(new ProcessStartInfo(Path.Combine(dir, Product.Launcher)) { WorkingDirectory = dir });
                f.Close();
                return;
            }
            dir = box.Text.Trim();
            desktop = desk.Checked;
            if (Product.Running(dir))
            {
                MessageBox.Show(f, Product.Name + " is open. Close it first, then press Install again.", Product.Name);
                return;
            }
            go.Enabled = cancel.Enabled = box.Enabled = browse.Enabled = desk.Enabled = false;
            bar.Visible = true;
            var worker = new Thread(delegate ()
            {
                string error = null;
                try
                {
                    Install(delegate (int pct, string what)
                    {
                        f.BeginInvoke((MethodInvoker)delegate { bar.Value = Math.Min(100, pct); status.Text = what; });
                    });
                }
                catch (Exception e) { error = e.Message; }
                f.BeginInvoke((MethodInvoker)delegate
                {
                    if (error != null)
                    {
                        status.Text = "Install failed: " + error;
                        go.Enabled = cancel.Enabled = box.Enabled = browse.Enabled = desk.Enabled = true;
                        return;
                    }
                    done = true;
                    bar.Value = 100;
                    cancel.Text = "Close"; cancel.Enabled = true;
                    go.Text = "Open " + Product.Name; go.Width = 120; go.Left = 380; go.Enabled = true;
                    cancel.Left = 272;
                    if (Product.ClaudeFound())
                        status.Text = "Installed. " + Product.Name + " uses your own Claude Code sign-in; no key is needed.";
                    else
                    {
                        status.Text = "Installed. One more step: " + Product.Name + " runs on Claude Code, signed in with "
                                    + "your own Claude account. Install it, then run 'claude' once to sign in.";
                        var link = new LinkLabel();
                        link.Text = "How to install Claude Code";
                        link.Location = new Point(20, 290); link.AutoSize = true;
                        link.LinkClicked += delegate { Process.Start(Product.ClaudeHelp); };
                        f.Controls.Add(link);
                    }
                });
            });
            worker.IsBackground = true;
            worker.Start();
        };
        f.Controls.AddRange(new Control[] { intro, box, browse, desk, bar, status, go, cancel });
        return f;
    }

    delegate void Progress(int pct, string what);

    static void Install(Progress progress)
    {
        string me = Application.ExecutablePath;
        Directory.CreateDirectory(dir);
        // An update replaces the add-on folder whole, so files removed from a newer version do not linger.
        string addon = Path.Combine(dir, @"4.5\scripts\addons_core\unix");
        if (Directory.Exists(addon)) Directory.Delete(addon, true);
        string root = Path.GetFullPath(dir).TrimEnd('\\') + "\\";
        using (var file = new FileStream(me, FileMode.Open, FileAccess.Read, FileShare.Read))
        {
            var tail = new byte[16];
            file.Position = file.Length - 16;
            file.Read(tail, 0, 16);
            if (Encoding.ASCII.GetString(tail, 8, 8) != Product.Magic)
                throw new InvalidDataException("This setup file is damaged (no app inside). Download it again.");
            long size = BitConverter.ToInt64(tail, 0);
            using (var zip = new ZipArchive(new Slice(file, file.Length - 16 - size, size), ZipArchiveMode.Read))
            {
                long total = 0, written = 0;
                int shown = -1;
                foreach (ZipArchiveEntry e in zip.Entries) total += e.Length;
                var buffer = new byte[1 << 20];
                foreach (ZipArchiveEntry e in zip.Entries)
                {
                    string target = Path.GetFullPath(Path.Combine(dir, e.FullName.Replace('/', '\\')));
                    if (!target.StartsWith(root, StringComparison.OrdinalIgnoreCase)) continue;
                    if (e.FullName.EndsWith("/")) { Directory.CreateDirectory(target); continue; }
                    // Keep the user's own settings (preferences, chat layout) on an update.
                    if (e.FullName.StartsWith("portable/") && File.Exists(target)) { written += e.Length; continue; }
                    Directory.CreateDirectory(Path.GetDirectoryName(target));
                    using (Stream src = e.Open())
                    using (var dst = new FileStream(target, FileMode.Create, FileAccess.Write))
                    {
                        int n;
                        while ((n = src.Read(buffer, 0, buffer.Length)) > 0)
                        {
                            dst.Write(buffer, 0, n);
                            written += n;
                        }
                    }
                    int pct = (int)(written * 100 / Math.Max(1, total));
                    if (pct != shown) { shown = pct; progress(pct, "Copying files... " + pct + "%"); }
                }
                if (register) Register(total);
            }
        }
        string launcher = Path.Combine(dir, Product.Launcher);
        if (shortcuts)
        {
            progress(100, "Adding shortcuts");
            Product.MakeLink(Product.StartMenuLink(), launcher, dir);
            if (desktop) Product.MakeLink(Product.DesktopLink(), launcher, dir);
        }
        progress(100, "Done");
    }

    static void Register(long bytes)
    {
        using (RegistryKey k = Registry.CurrentUser.CreateSubKey(Product.RegKey))
        {
            k.SetValue("DisplayName", Product.Name);
            k.SetValue("DisplayVersion", Product.Version);
            k.SetValue("Publisher", Product.Name);
            k.SetValue("InstallLocation", dir);
            k.SetValue("DisplayIcon", Path.Combine(dir, Product.Launcher));
            k.SetValue("UninstallString", "\"" + Path.Combine(dir, Product.Uninstaller) + "\"");
            k.SetValue("NoModify", 1, RegistryValueKind.DWord);
            k.SetValue("NoRepair", 1, RegistryValueKind.DWord);
            k.SetValue("EstimatedSize", (int)(bytes / 1024), RegistryValueKind.DWord);
        }
    }
}
#else
static class Uninstall
{
    [STAThread]
    static int Main(string[] args)
    {
        Application.EnableVisualStyles();
        string dir = Path.GetDirectoryName(Application.ExecutablePath);
        bool quiet = Array.IndexOf(args, "--quiet") >= 0;
        if (Product.Running(dir))
        {
            if (!quiet) MessageBox.Show(Product.Name + " is open. Close it first, then uninstall again.", Product.Name);
            return 1;
        }
        string data = Path.Combine(dir, "portable");
        DialogResult answer = DialogResult.No;
        if (!quiet)
        {
            answer = MessageBox.Show(
                "Remove " + Product.Name + " from this computer?\n\n"
                + "Also delete your chats, settings and the copies " + Product.Name + " saved while it worked?\n"
                + "(Yes = delete them too, No = keep them in " + data + ")\n\n"
                + "Your own .blend files saved elsewhere are never touched.",
                Product.Name + " uninstall", MessageBoxButtons.YesNoCancel, MessageBoxIcon.Question);
            if (answer == DialogResult.Cancel) return 0;
        }
        bool keepData = answer != DialogResult.Yes;
        string launcher = Path.Combine(dir, Product.Launcher);
        foreach (string link in new[] { Product.StartMenuLink(), Product.DesktopLink() })
        {
            try { if (File.Exists(link)) File.Delete(link); } catch (Exception) { }
        }
        try
        {
            using (RegistryKey k = Registry.CurrentUser.OpenSubKey(Product.RegKey))
            {
                object loc = k == null ? null : k.GetValue("InstallLocation");
                if (loc != null && string.Equals(Path.GetFullPath((string)loc).TrimEnd('\\'), dir.TrimEnd('\\'),
                                                 StringComparison.OrdinalIgnoreCase))
                    Registry.CurrentUser.DeleteSubKeyTree(Product.RegKey, false);
            }
        }
        catch (Exception) { }
        string me = Path.GetFileName(Application.ExecutablePath);
        foreach (string sub in Directory.GetDirectories(dir))
        {
            if (keepData && string.Equals(Path.GetFileName(sub), "portable", StringComparison.OrdinalIgnoreCase)) continue;
            try { Directory.Delete(sub, true); } catch (Exception) { }
        }
        foreach (string f in Directory.GetFiles(dir))
        {
            if (string.Equals(Path.GetFileName(f), me, StringComparison.OrdinalIgnoreCase)) continue;
            try { File.Delete(f); } catch (Exception) { }
        }
        // This program cannot delete itself while it runs: a hidden command does it a moment after it exits, and
        // removes the folder too when nothing was kept in it.
        var info = new ProcessStartInfo("cmd.exe",
            "/c ping 127.0.0.1 -n 3 >nul & del /f /q \"" + Path.Combine(dir, me) + "\" & rmdir \"" + dir + "\"");
        info.CreateNoWindow = true;
        info.UseShellExecute = false;
        Process.Start(info);
        if (!quiet)
            MessageBox.Show(Product.Name + " was removed." + (keepData ? "\nYour chats and settings are still in " + data : ""),
                            Product.Name);
        return 0;
    }
}
#endif
