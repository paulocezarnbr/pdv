namespace Pdv.Data.Auth;

public sealed class WeakPinException(string message) : Exception(message);

/// <summary>A política de PIN do Python (<c>validate_pin</c>), com as mesmas mensagens.</summary>
/// <remarks>
/// O PIN é a única coisa entre um operador curioso e a autorização de
/// cancelamento. Sequência e dígito repetido são o que as pessoas escolhem
/// quando ninguém as impede — e o que um atacante tenta primeiro. O veredito de
/// cada caso de <c>contracts/pin-hashes.json</c> tem de bater com o do Python.
/// </remarks>
public static class PinPolicy
{
    public const int MinLength = 6;
    public const int MaxLength = 12;

    private static readonly HashSet<string> Forbidden =
    [
        "123456", "654321", "112233", "121212", "123123", "000000", "111111",
        "696969", "666666", "159753", "147258", "102030", "123321", "010203",
    ];

    /// <returns>O PIN normalizado (sem espaços nas pontas).</returns>
    /// <exception cref="WeakPinException">Com a explicação do que precisa mudar.</exception>
    public static string Validate(string pin)
    {
        pin = pin.Trim();

        // Só 0-9. O Python aceita também dígitos de outras escritas; no teclado
        // do caixa eles não existem, e aceitá-los só abriria espaço para PIN
        // que ninguém consegue digitar de novo.
        if (pin.Length == 0 || !pin.All(char.IsAsciiDigit))
            throw new WeakPinException("O PIN deve conter apenas dígitos.");
        if (pin.Length < MinLength)
            throw new WeakPinException($"O PIN precisa de pelo menos {MinLength} dígitos.");
        if (pin.Length > MaxLength)
            throw new WeakPinException($"O PIN pode ter no máximo {MaxLength} dígitos.");
        if (Forbidden.Contains(pin))
            throw new WeakPinException("Este PIN é um dos mais tentados. Escolha outro.");
        if (pin.Distinct().Count() == 1)
            throw new WeakPinException("O PIN não pode ser um único dígito repetido.");
        if (IsRun(pin, 1) || IsRun(pin, -1))
            throw new WeakPinException("O PIN não pode ser uma sequência de dígitos.");
        if (pin.Distinct().Count() == 2 && IsAlternating(pin))
            throw new WeakPinException("O PIN não pode ser dois dígitos alternados.");
        return pin;
    }

    /// <summary>Sequência com volta: 890123 também é sequência.</summary>
    private static bool IsRun(string pin, int step)
    {
        for (var i = 0; i < pin.Length - 1; i++)
        {
            var difference = ((pin[i + 1] - '0') - (pin[i] - '0') + 10) % 10;
            if (difference != (step + 10) % 10) return false;
        }
        return true;
    }

    private static bool IsAlternating(string pin)
    {
        for (var i = 0; i < pin.Length; i++)
        {
            if (pin[i] != pin[i % 2]) return false;
        }
        return true;
    }
}
