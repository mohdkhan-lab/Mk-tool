using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

namespace MkStoryStudio
{
    static class Program
    {
        [STAThread]
        static void Main(string[] args)
        {
            try
            {
                string baseDir = AppDomain.CurrentDomain.BaseDirectory;

                // 1. Check runtime pythonw.exe (bundled first, then the
                //    installed copy as a fallback)
                string pythonw = Path.Combine(baseDir, "runtime", "pythonw.exe");
                if (!File.Exists(pythonw))
                {
                    pythonw = Path.Combine(
                        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                        "Programs", "MKStoryStudio", "runtime", "pythonw.exe");
                }

                // 2. Check launch script
                string script = Path.Combine(baseDir, "launch_desktop_app.py");
                if (!File.Exists(script))
                {
                    script = Path.Combine(baseDir, "app", "launch_desktop_app.py");
                }

                if (!File.Exists(pythonw))
                {
                    MessageBox.Show("Python runtime nahi mila!\nPath: " + pythonw, "MK Story Studio", MessageBoxButtons.OK, MessageBoxIcon.Error);
                    return;
                }
                if (!File.Exists(script))
                {
                    MessageBox.Show("Launch script nahi mila!\nPath: " + script, "MK Story Studio", MessageBoxButtons.OK, MessageBoxIcon.Error);
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
                MessageBox.Show("Launch Error: " + ex.Message, "MK Story Studio", MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
        }
    }
}
