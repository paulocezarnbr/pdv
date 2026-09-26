namespace Pdv.App;

/// <summary>Uma tecla do caixa e o que ela faz.</summary>
public sealed record Shortcut(string Key, string Description);

/// <summary>
/// A tabela ÚNICA de atalhos do caixa, que a ajuda do F1 mostra — o
/// <c>SHORTCUTS</c> do <c>counter_window.py</c>.
/// </summary>
/// <remarks>
/// A tela liga as teclas no XAML, e uma tabela escrita à mão diverge no dia
/// em que alguém acrescenta uma tecla e esquece a ajuda. Um teste lê o
/// <c>CounterPage.xaml</c> e exige que as teclas de lá sejam exatamente estas.
/// </remarks>
public static class Shortcuts
{
    /// <summary>Enter não é atalho de botão: é o campo de busca. Fica na ajuda, fora da conferência.</summary>
    public const string Enter = "Enter";

    public static IReadOnlyList<Shortcut> All { get; } =
    [
        new("F1", "Ajuda — todos os atalhos"),
        new(Enter, "No campo: bipar o código ou buscar pelo nome"),
        new("F4", "Cancelar item (gerente)"),
        new("Ctrl+F4", "Aceite de pedido do painel (quando houver)"),
        new("F5", "Fiado / pendura"),
        new("F6", "Desconto (gerente)"),
        new("Ctrl+F6", "Níveis de desconto"),
        new("F7", "Cashback"),
        new("F8", "Painel do salão"),
        new("F9", "Mesas e conta da mesa"),
        new("F11", "Crédito pré-pago"),
        new("F12", "Fechar o caixa"),
        new("Ctrl+K", "Identificar o cliente"),
        new("Ctrl+E", "Editar o cadastro do cliente"),
        new("Ctrl+P", "Reimprimir o último cupom"),
    ];
}
