using System.Xml.Linq;
using Pdv.App;

namespace Pdv.Core.Tests;

/// <summary>A ajuda do F1 e as teclas da tela não divergem.</summary>
public sealed class ShortcutsTests
{
    private static string CounterPage()
    {
        var folder = new DirectoryInfo(AppContext.BaseDirectory);
        while (folder is not null && !File.Exists(Path.Combine(folder.FullName, "src", "Pdv.WinUI", "CounterPage.xaml")))
        {
            folder = folder.Parent;
        }
        return folder is null
            ? throw new FileNotFoundException("src/Pdv.WinUI/CounterPage.xaml não encontrado")
            : Path.Combine(folder.FullName, "src", "Pdv.WinUI", "CounterPage.xaml");
    }

    [Fact]
    public void Every_key_bound_on_the_screen_is_in_the_help_and_nothing_else()
    {
        XNamespace ui = "http://schemas.microsoft.com/winfx/2006/xaml/presentation";
        var bound = XDocument.Load(CounterPage()).Descendants(ui + "KeyboardAccelerator")
            .Select(accelerator => (string?)accelerator.Attribute("Modifiers") switch
            {
                null => (string)accelerator.Attribute("Key")!,
                "Control" => "Ctrl+" + (string)accelerator.Attribute("Key")!,
                var other => $"{other}+{(string)accelerator.Attribute("Key")!}",
            })
            .Order().ToList();

        var documented = Shortcuts.All.Select(s => s.Key).Where(k => k != Shortcuts.Enter).Order().ToList();

        Assert.Equal(documented, bound);
    }

    [Fact]
    public void No_key_is_listed_twice() =>
        Assert.Equal(Shortcuts.All.Count, Shortcuts.All.Select(s => s.Key).Distinct().Count());
}
