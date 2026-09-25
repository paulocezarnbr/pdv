using Pdv.Data.Auth;
using Pdv.Data.Sales;

namespace Pdv.App;

/// <summary>A abertura do caixa entre o login e a venda — o que o <c>main.py</c> fazia antes da janela.</summary>
/// <remarks>
/// Sem sessão aberta, pergunta o fundo de troco e abre. Com sessão do mesmo
/// operador, segue. Com sessão de <b>outro</b> operador, não entra: a gaveta
/// tem dono até alguém fechar, senão a divergência do fim do dia não tem de
/// quem ser.
/// </remarks>
public sealed class CashOpening(CashSessionService sessions)
{
    public enum Outcome
    {
        Ready,
        Canceled,
        Blocked,
    }

    public const string BlockedMessage =
        "Há uma sessão de caixa aberta por outro operador. Entre com o operador responsável para encerrá-la.";

    public async Task<(Outcome Outcome, string? Message)> EnsureOpenAsync(
        Identity operatorIdentity, Func<string, Task<string?>> askText)
    {
        if (sessions.Current() is { } current)
        {
            return current.OperatorId == operatorIdentity.Id ? (Outcome.Ready, null) : (Outcome.Blocked, BlockedMessage);
        }

        var prompt = "Fundo de troco inicial (R$):";
        while (true)
        {
            var typed = await askText(prompt);
            if (typed is null) return (Outcome.Canceled, null);
            // Em branco é "sem fundo", como o 0,00 que o Python sugeria.
            var cents = string.IsNullOrWhiteSpace(typed) ? 0 : Money.Parse(typed);
            if (cents is null)
            {
                prompt = $"\"{typed.Trim()}\" não é um valor. Fundo de troco inicial (R$):";
                continue;
            }
            try
            {
                sessions.Open(operatorIdentity.Id, cents.Value);
                return (Outcome.Ready, null);
            }
            catch (CashSessionException error)
            {
                return (Outcome.Blocked, error.Message);
            }
        }
    }
}
