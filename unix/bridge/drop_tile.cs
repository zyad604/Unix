// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Larpbase
// Unix drop tile: a small always-on-top tile that you drag into any application.
// Blender cannot start an operating-system drag itself, so the add-on launches this
// helper with the exported file(s). Dragging the tile drops those files on the target:
// a game engine's project window, Roblox Studio, Explorer, a chat, anything.
//
// Usage: UnixDrop.exe <file> [<file> ...]      (--selftest prints OK and exits)
// Built on first use by unix/bridge/drop_tile.py with the C# compiler in Windows.
using System;
using System.Collections.Specialized;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.IO;
using System.Windows.Forms;

sealed class Tile : Form
{
    readonly string[] files;
    Point pressed;
    bool moving;

    public Tile(string[] paths)
    {
        files = paths;
        FormBorderStyle = FormBorderStyle.None;
        ShowInTaskbar = false;
        TopMost = true;
        StartPosition = FormStartPosition.Manual;
        Size = new Size(300, 84);
        Rectangle area = Screen.FromPoint(Cursor.Position).WorkingArea;
        Location = new Point(area.Right - Width - 28, area.Bottom - Height - 28);
        DoubleBuffered = true;
        BackColor = Color.FromArgb(19, 18, 23);
        Cursor = Cursors.Hand;
        using (GraphicsPath path = Round(new Rectangle(0, 0, Width, Height), 18)) Region = new Region(path);
        Timer timer = new Timer();
        timer.Interval = 15 * 60 * 1000;          // close itself after 15 minutes
        timer.Tick += delegate { Close(); };
        timer.Start();
        KeyPreview = true;
        KeyDown += delegate(object s, KeyEventArgs e) { if (e.KeyCode == Keys.Escape) Close(); };
    }

    static GraphicsPath Round(Rectangle r, int radius)
    {
        int d = radius * 2;
        GraphicsPath p = new GraphicsPath();
        p.AddArc(r.X, r.Y, d, d, 180, 90);
        p.AddArc(r.Right - d, r.Y, d, d, 270, 90);
        p.AddArc(r.Right - d, r.Bottom - d, d, d, 0, 90);
        p.AddArc(r.X, r.Bottom - d, d, d, 90, 90);
        p.CloseFigure();
        return p;
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        Graphics g = e.Graphics;
        g.SmoothingMode = SmoothingMode.AntiAlias;
        g.TextRenderingHint = System.Drawing.Text.TextRenderingHint.ClearTypeGridFit;
        Rectangle badge = new Rectangle(14, 14, 56, 56);
        using (LinearGradientBrush brush = new LinearGradientBrush(badge, Color.FromArgb(110, 90, 245), Color.FromArgb(190, 60, 225), 35f))
        using (GraphicsPath path = Round(badge, 14)) g.FillPath(brush, path);
        using (Font big = new Font("Segoe UI", 20f, FontStyle.Bold))
        using (StringFormat centre = new StringFormat { Alignment = StringAlignment.Center, LineAlignment = StringAlignment.Center })
            g.DrawString("M", big, Brushes.White, badge, centre);
        string title = files.Length == 1 ? Path.GetFileName(files[0]) : files.Length + " files";
        using (Font bold = new Font("Segoe UI Semibold", 10.5f))
        using (Font small = new Font("Segoe UI", 8.5f))
        using (Brush dim = new SolidBrush(Color.FromArgb(160, 160, 172)))
        using (StringFormat trim = new StringFormat { Trimming = StringTrimming.EllipsisCharacter, FormatFlags = StringFormatFlags.NoWrap })
        {
            g.DrawString(title, bold, Brushes.White, new RectangleF(82, 16, Width - 112, 22), trim);
            g.DrawString("Drag me into your engine", small, dim, 82, 40);
            g.DrawString("x", bold, dim, Width - 24, 6);
        }
    }

    protected override void OnMouseDown(MouseEventArgs e)
    {
        if (e.Button == MouseButtons.Right || (e.X > Width - 30 && e.Y < 28)) { Close(); return; }
        pressed = e.Location;
        moving = (ModifierKeys & Keys.Alt) == Keys.Alt;   // Alt + drag moves the tile itself
    }

    protected override void OnMouseMove(MouseEventArgs e)
    {
        if (e.Button != MouseButtons.Left) return;
        if (moving) { Location = new Point(Location.X + e.X - pressed.X, Location.Y + e.Y - pressed.Y); return; }
        if (Math.Abs(e.X - pressed.X) + Math.Abs(e.Y - pressed.Y) < 6) return;
        StringCollection list = new StringCollection();
        list.AddRange(files);
        DataObject data = new DataObject();
        data.SetFileDropList(list);
        DoDragDrop(data, DragDropEffects.Copy);
    }
}

static class Program
{
    [STAThread]
    static int Main(string[] args)
    {
        if (args.Length == 1 && args[0] == "--selftest") { Console.WriteLine("OK"); return 0; }
        string[] files = Array.FindAll(args, File.Exists);
        if (files.Length == 0) return 2;
        Application.EnableVisualStyles();
        Application.Run(new Tile(files));
        return 0;
    }
}
