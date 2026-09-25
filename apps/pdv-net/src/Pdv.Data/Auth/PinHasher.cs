using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using Konscious.Security.Cryptography;

namespace Pdv.Data.Auth;

/// <summary>
/// Argon2id no formato do <c>argon2-cffi</c>:
/// <c>$argon2id$v=19$m=65536,t=3,p=4$&lt;sal&gt;$&lt;hash&gt;</c> (base64 sem padding).
/// </summary>
/// <remarks>
/// <para>
/// Os dois PDVs leem a mesma tabela <c>users</c>: o hash que o Python gravou (ou
/// que desceu da nuvem) tem de verificar aqui, e o que o C# gravar tem de
/// verificar lá. <c>contracts/pin-hashes.json</c> cobre a ida; o
/// <c>crosscheck.py</c> no CI, a volta.
/// </para>
/// <para>
/// Parâmetros iguais aos padrões do <c>PasswordHasher</c> (m=64 MiB, t=3, p=4),
/// e não reduzidos: o custo por tentativa é o que sustenta o freio.
/// </para>
/// </remarks>
public static class PinHasher
{
    private const int MemoryKib = 65536;
    private const int Iterations = 3;
    private const int Parallelism = 4;
    private const int SaltBytes = 16;
    private const int HashBytes = 32;

    // Teto para parâmetros lidos do banco. Um hash plantado com m=4 GiB
    // transformaria cada tentativa de login num travamento do caixa.
    private const int MaxMemoryKib = 262144;
    private const int MaxIterations = 10;
    private const int MaxParallelism = 16;

    public static string Hash(string pin)
    {
        var salt = RandomNumberGenerator.GetBytes(SaltBytes);
        var hash = Compute(pin, salt, MemoryKib, Iterations, Parallelism, HashBytes);
        return string.Create(CultureInfo.InvariantCulture,
            $"$argon2id$v=19$m={MemoryKib},t={Iterations},p={Parallelism}${Encode(salt)}${Encode(hash)}");
    }

    /// <summary>Confere o PIN. Hash corrompido ou de outro tipo nunca vira "autorizado".</summary>
    public static bool Verify(string encoded, string pin)
    {
        if (!TryParse(encoded, out var memory, out var iterations, out var parallelism, out var salt, out var expected))
        {
            return false;
        }
        try
        {
            // PIN vazio (Enter sem digitar): o Konscious recusa senha vazia com
            // exceção. Gasta o mesmo tempo com outra entrada e responde "não" —
            // uma exceção aqui derrubaria o login sem contar a tentativa.
            var actual = Compute(pin.Length == 0 ? "\0" : pin, salt, memory, iterations, parallelism, expected.Length);
            return pin.Length > 0 && CryptographicOperations.FixedTimeEquals(actual, expected);
        }
        catch (Exception error) when (error is ArgumentException or OutOfMemoryException)
        {
            return false;
        }
    }

    private static byte[] Compute(string pin, byte[] salt, int memory, int iterations, int parallelism, int length)
    {
        using var argon = new Argon2id(Encoding.UTF8.GetBytes(pin))
        {
            Salt = salt,
            MemorySize = memory,
            Iterations = iterations,
            DegreeOfParallelism = parallelism,
        };
        return argon.GetBytes(length);
    }

    private static bool TryParse(
        string encoded, out int memory, out int iterations, out int parallelism, out byte[] salt, out byte[] hash)
    {
        memory = iterations = parallelism = 0;
        salt = hash = [];

        // "", "argon2id", "v=19", "m=..,t=..,p=..", sal, hash
        var parts = encoded.Split('$');
        if (parts.Length != 6 || parts[0].Length != 0 || parts[1] != "argon2id" || parts[2] != "v=19")
        {
            return false;
        }

        foreach (var setting in parts[3].Split(','))
        {
            var pair = setting.Split('=');
            if (pair.Length != 2 || !int.TryParse(pair[1], NumberStyles.None, CultureInfo.InvariantCulture, out var value))
            {
                return false;
            }
            switch (pair[0])
            {
                case "m": memory = value; break;
                case "t": iterations = value; break;
                case "p": parallelism = value; break;
                default: return false;
            }
        }

        if (memory is < 8 or > MaxMemoryKib || iterations is < 1 or > MaxIterations ||
            parallelism is < 1 or > MaxParallelism)
        {
            return false;
        }

        try
        {
            salt = Decode(parts[4]);
            hash = Decode(parts[5]);
        }
        catch (FormatException)
        {
            return false;
        }
        return salt.Length >= 8 && hash.Length >= 16;
    }

    private static string Encode(byte[] bytes) => Convert.ToBase64String(bytes).TrimEnd('=');

    private static byte[] Decode(string text)
    {
        var padding = (4 - text.Length % 4) % 4;
        return Convert.FromBase64String(text + new string('=', padding));
    }
}
