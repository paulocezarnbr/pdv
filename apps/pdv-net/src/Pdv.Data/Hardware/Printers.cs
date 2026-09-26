using System.Collections.Concurrent;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
using Pdv.Core.Printing;

namespace Pdv.Data.Hardware;

public class PrinterException(string message, Exception? inner = null) : Exception(message, inner);

/// <summary>Entrega bytes ESC/POS ao dispositivo.</summary>
public interface IPrinter
{
    /// <exception cref="PrinterException">A entrega falhou.</exception>
    void Send(byte[] payload, string jobName = "PDV Cupom");
}

/// <summary>Como a impressora está ligada — as chaves <c>printer.*</c> de <c>device_settings</c>.</summary>
/// <remarks>
/// <c>win32raw</c> é a rota de produção: o driver Epson fica instalado e o
/// spooler entrega os bytes sem interpretar. <c>file</c> grava o cupom em
/// disco (demonstração e testes). A rota libusb do Python fica de fora: ela
/// troca o driver da impressora e impede outros programas de usá-la.
/// </remarks>
public sealed record PrinterSettings(
    string Backend = "file",
    string WindowsPrinterName = "EPSON TM-T20X Receipt",
    string OutputDirectory = "",
    string StoreDocument = "00.000.000/0001-00",
    string StoreAddress = "Rua Exemplo, 123 - Centro")
{
    public PrinterLayout Layout { get; init; } = new();

    public static PrinterSettings Load(PdvDatabase database, string outputDirectory)
    {
        string? Value(string key) => database.Scalar(
            "SELECT value FROM device_settings WHERE key = $key", ("$key", key)) as string is { Length: > 0 } value ? value : null;

        var defaults = new PrinterSettings(OutputDirectory: outputDirectory);
        return defaults with
        {
            Backend = Value("printer.backend") ?? defaults.Backend,
            WindowsPrinterName = Value("printer.name") ?? defaults.WindowsPrinterName,
            StoreDocument = Value("store.document") ?? defaults.StoreDocument,
            StoreAddress = Value("store.address") ?? defaults.StoreAddress,
        };
    }

    public IPrinter Build() => Backend == "win32raw"
        ? new Win32RawPrinter(WindowsPrinterName)
        : new FilePrinter(OutputDirectory);
}

/// <summary>Grava o cupom em disco: <c>.bin</c> (os bytes) e <c>.txt</c> (prévia legível).</summary>
public sealed class FilePrinter(string outputDirectory, TimeProvider? clock = null) : IPrinter
{
    private readonly TimeProvider _clock = clock ?? TimeProvider.System;

    public void Send(byte[] payload, string jobName = "PDV Cupom")
    {
        try
        {
            Directory.CreateDirectory(outputDirectory);
            var stamp = _clock.GetLocalNow().ToString("yyyyMMdd_HHmmss_ffffff", System.Globalization.CultureInfo.InvariantCulture);
            var basePath = Path.Combine(outputDirectory, $"cupom_{stamp}");
            File.WriteAllBytes(basePath + ".bin", payload);
            Encoding.RegisterProvider(CodePagesEncodingProvider.Instance);
            var preview = Encoding.GetEncoding(850).GetString(payload);
            File.WriteAllText(basePath + ".txt",
                new string(preview.Where(c => c == '\n' || !char.IsControl(c)).ToArray()), Encoding.UTF8);
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
        {
            throw new PrinterException($"Falha ao gravar o cupom em {outputDirectory}: {error.Message}", error);
        }
    }
}

/// <summary>O spooler do Windows em modo RAW — a rota de produção.</summary>
public sealed class Win32RawPrinter(string printerName) : IPrinter
{
    public void Send(byte[] payload, string jobName = "PDV Cupom")
    {
        if (!OpenPrinter(printerName, out var handle, IntPtr.Zero))
        {
            throw new PrinterException(
                $"Impressora '{printerName}' não encontrada. Confira o nome exato em Dispositivos e Impressoras.",
                new Win32Exception(Marshal.GetLastWin32Error()));
        }
        try
        {
            // RAW: o spooler entrega os bytes sem interpretar — indispensável para ESC/POS.
            var document = new DocInfo { DocName = jobName, DataType = "RAW" };
            if (StartDocPrinter(handle, 1, document) == 0) throw Failure("abrir o documento");
            try
            {
                if (!StartPagePrinter(handle)) throw Failure("abrir a página");
                if (!WritePrinter(handle, payload, payload.Length, out var written) || written != payload.Length)
                {
                    throw Failure("enviar os bytes");
                }
                EndPagePrinter(handle);
            }
            finally
            {
                EndDocPrinter(handle);
            }
        }
        finally
        {
            ClosePrinter(handle);
        }
    }

    private static PrinterException Failure(string step) =>
        new($"Falha ao imprimir ({step}).", new Win32Exception(Marshal.GetLastWin32Error()));

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private sealed class DocInfo
    {
        [MarshalAs(UnmanagedType.LPWStr)] public string DocName = "";
        [MarshalAs(UnmanagedType.LPWStr)] public string? OutputFile;
        [MarshalAs(UnmanagedType.LPWStr)] public string DataType = "RAW";
    }

    [DllImport("winspool.drv", EntryPoint = "OpenPrinterW", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern bool OpenPrinter(string name, out IntPtr handle, IntPtr defaults);

    [DllImport("winspool.drv", SetLastError = true)]
    private static extern bool ClosePrinter(IntPtr handle);

    [DllImport("winspool.drv", EntryPoint = "StartDocPrinterW", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern int StartDocPrinter(IntPtr handle, int level, [In] DocInfo document);

    [DllImport("winspool.drv", SetLastError = true)]
    private static extern bool EndDocPrinter(IntPtr handle);

    [DllImport("winspool.drv", SetLastError = true)]
    private static extern bool StartPagePrinter(IntPtr handle);

    [DllImport("winspool.drv", SetLastError = true)]
    private static extern bool EndPagePrinter(IntPtr handle);

    [DllImport("winspool.drv", SetLastError = true)]
    private static extern bool WritePrinter(IntPtr handle, byte[] bytes, int count, out int written);
}

/// <summary>A fila de impressão, em thread própria, com três tentativas — o <c>PrintService</c> do Python.</summary>
/// <remarks>
/// Quando o cupom entra na fila, a venda já está gravada e o estoque já
/// baixou. Papel acabado não desfaz nada: troca-se a bobina e reimprime-se.
/// E impressora travada não congela o caixa (invariante 9).
/// </remarks>
public sealed class PrintService : IDisposable
{
    private readonly IPrinter _printer;
    private readonly int _maxAttempts;
    private readonly TimeSpan _backoff;
    private readonly BlockingCollection<(byte[] Payload, string Name)> _queue = new();
    private readonly Thread _thread;

    public PrintService(IPrinter printer, int maxAttempts = 3, TimeSpan? backoff = null)
    {
        _printer = printer;
        _maxAttempts = maxAttempts;
        _backoff = backoff ?? TimeSpan.FromMilliseconds(500);
        _thread = new Thread(Run) { IsBackground = true, Name = "printer-worker" };
        _thread.Start();
    }

    /// <summary>Falha depois de todas as tentativas. Sai da thread da impressora.</summary>
    public event Action<string>? Failed;

    /// <summary>A última impressão que chegou à impressora, para reimprimir.</summary>
    public byte[]? LastPrinted { get; private set; }

    /// <summary>Enfileira sem bloquear quem chama.</summary>
    public void Submit(byte[] payload, string jobName = "PDV Cupom") => _queue.Add((payload, jobName));

    private void Run()
    {
        foreach (var (payload, name) in _queue.GetConsumingEnumerable())
        {
            Exception? last = null;
            for (var attempt = 1; attempt <= _maxAttempts; attempt++)
            {
                try
                {
                    _printer.Send(payload, name);
                    LastPrinted = payload;
                    last = null;
                    break;
                }
                catch (PrinterException error)
                {
                    last = error;
                    Thread.Sleep(_backoff * attempt);
                }
            }
            if (last is not null) Failed?.Invoke($"Impressão falhou após {_maxAttempts} tentativas: {last.Message}");
        }
    }

    /// <summary>Termina o que está na fila (até o prazo) e para.</summary>
    public void Dispose()
    {
        _queue.CompleteAdding();
        _thread.Join(TimeSpan.FromSeconds(5));
        _queue.Dispose();
    }
}
