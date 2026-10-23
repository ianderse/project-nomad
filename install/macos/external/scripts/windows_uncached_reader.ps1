# Read a file through Windows without the filesystem data cache. No disk writes.
Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using Microsoft.Win32.SafeHandles;
public static class NomadUncachedReader {
    public static string Hash(string path) {
        using (SHA256 hash = SHA256.Create()) {
            using (CryptoStream sink = new CryptoStream(Stream.Null, hash, CryptoStreamMode.Write)) {
                Copy(path, sink);
                sink.FlushFinalBlock();
                return BitConverter.ToString(hash.Hash).Replace("-", "").ToLowerInvariant();
            }
        }
    }
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern SafeFileHandle CreateFile(string name, uint access, uint share,
        IntPtr security, uint creation, uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool GetFileSizeEx(SafeFileHandle file, out long size);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool ReadFile(SafeFileHandle file, IntPtr buffer, uint count,
        out uint read, IntPtr overlapped);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern IntPtr VirtualAlloc(IntPtr address, UIntPtr size, uint type, uint protect);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool VirtualFree(IntPtr address, UIntPtr size, uint type);
    public static void Copy(string path, Stream output) {
        // NO_BUFFERING | SEQUENTIAL_SCAN; VirtualAlloc supplies sector-aligned memory.
        using (SafeFileHandle file = CreateFile(path, 0x80000000, 5, IntPtr.Zero,
                3, 0x28000000, IntPtr.Zero)) {
            if (file.IsInvalid) throw new Win32Exception(Marshal.GetLastWin32Error());
            long remaining;
            if (!GetFileSizeEx(file, out remaining)) throw new Win32Exception(Marshal.GetLastWin32Error());
            const int capacity = 1048576;
            IntPtr buffer = VirtualAlloc(IntPtr.Zero, (UIntPtr)capacity, 0x3000, 4);
            if (buffer == IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error());
            try {
                byte[] bytes = new byte[capacity];
                while (remaining > 0) {
                    uint read;
                    if (!ReadFile(file, buffer, capacity, out read, IntPtr.Zero))
                        throw new Win32Exception(Marshal.GetLastWin32Error());
                    if (read == 0) throw new EndOfStreamException();
                    int count = (int)Math.Min((long)read, remaining);
                    Marshal.Copy(buffer, bytes, 0, count);
                    output.Write(bytes, 0, count);
                    remaining -= count;
                }
                output.Flush();
            } finally { VirtualFree(buffer, UIntPtr.Zero, 0x8000); }
        }
    }
}
'@
