using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

namespace MkTool
{
    static class Program
    {
        [STAThread]
        static void Main(string[] args)
        {
            try
            {
                string baseDir = AppDomain.CurrentDomain.BaseDirectory;
                
                // 1. Check runtime pythonw.exe (local portable first, then installed fallback)
                string pythonw = Path.Combine(baseDir, "runtime", "pythonw.exe");
                if (!File.Exists(pythonw))
                {
                    pythonw = @"C:\Users\mohda\AppData\Local\Programs\Frameloom\runtime\pythonw.exe";
                }
                
                // 2. Check launch script
                string script = Path.Combine(baseDir, "launch_desktop_app.py");
                if (!File.Exists(script))
                {
                    script = Path.Combine(baseDir, "app", "launch_desktop_app.py");
                }
                
                if (!File.Exists(pythonw))
                {
                    MessageBox.Show("Python runtime nahi mila!\nPath: " + pythonw, "Mk-Tool Error", MessageBoxButtons.OK, MessageBoxIcon.Error);
                    return;
                }
                if (!File.Exists(script))
                {
                    MessageBox.Show("Launch script nahi mila!\nPath: " + script, "Mk-Tool Error", MessageBoxButtons.OK, MessageBoxIcon.Error);
                    return;
                }

                ProcessStartInfo psi = new ProcessStartInfo();
                psi.FileName = pythonw;
                psi.Arguments = "\"" + script + "\"";
                psi.WorkingDirectory = Path.GetDirectoryName(script);
                psi.UseShellExecute = false;
                psi.CreateNoWindow = true;
                psi.WindowStyle = ProcessWindowStyle.Hidden;

                Process.Start(psi);
            }
            catch (Exception ex)
            {
                MessageBox.Show("Launch Error: " + ex.Message, "Mk-Tool Error", MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
        }
    }
}
