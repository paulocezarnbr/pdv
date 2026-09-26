using System.Globalization;
using System.Text.RegularExpressions;

namespace Pdv.Data.Customers;

/// <summary>
/// O cadastro do cliente (schema 15): quem é, como falar com ele e, sendo
/// morador do condomínio, em que apartamento mora.
/// </summary>
/// <param name="WhatsApp">Só dígitos, com DDD. É o <c>phone</c> do banco: a chave do balcão.</param>
/// <param name="Cpf">Só dígitos.</param>
/// <param name="UnitBlock">Bloco ou torre; vazio quando o prédio tem um só.</param>
/// <param name="UnitNumber">O apartamento. Obrigatório para morador.</param>
/// <param name="MarketingOptIn">Consentimento para ofertas por WhatsApp e e-mail (LGPD). Começa negado.</param>
public sealed record CustomerProfile(
    string Name,
    string? WhatsApp = null,
    string? Email = null,
    string? Cpf = null,
    bool IsResident = false,
    string? UnitBlock = null,
    string? UnitNumber = null,
    DateOnly? BirthDate = null,
    bool MarketingOptIn = false)
{
    /// <summary>"Bloco B · apto 101", "apto 101", ou vazio para quem não mora.</summary>
    public string Unit => !IsResident || string.IsNullOrEmpty(UnitNumber)
        ? ""
        : string.IsNullOrEmpty(UnitBlock) ? $"apto {UnitNumber}" : $"Bloco {UnitBlock} · apto {UnitNumber}";
}

/// <summary>O cadastro gravado: o perfil e desde quando ele autorizou mensagens.</summary>
public sealed record CustomerRecord(string Id, CustomerProfile Profile, string? MarketingOptInAt)
{
    public Customer Customer => new(Id, Profile.Name, Profile.WhatsApp);
}

/// <summary>A conferência do cadastro antes de gravar. A mensagem de cada recusa diz o que corrigir.</summary>
public static partial class CustomerRules
{
    public const int MaxName = 120;
    public const int MaxEmail = 254;
    public const int MaxUnit = 20;

    public static string Digits(string? text) => new((text ?? "").Where(char.IsAsciiDigit).ToArray());

    /// <summary>Os dois dígitos verificadores da Receita, e nada de 111.111.111-11.</summary>
    public static bool IsValidCpf(string? text)
    {
        var digits = Digits(text);
        if (digits.Length != 11 || digits.Distinct().Count() == 1) return false;
        for (var length = 9; length <= 10; length++)
        {
            var sum = 0;
            for (var i = 0; i < length; i++) sum += (digits[i] - '0') * (length + 1 - i);
            var check = sum * 10 % 11 % 10;
            if (check != digits[length] - '0') return false;
        }
        return true;
    }

    /// <summary>"123.456.789-09".</summary>
    public static string FormatCpf(string digits) => digits.Length == 11
        ? $"{digits[..3]}.{digits[3..6]}.{digits[6..9]}-{digits[9..]}"
        : digits;

    /// <summary>"(21) 99876-5432", "(21) 3456-7890".</summary>
    public static string FormatPhone(string digits) => digits.Length switch
    {
        11 => $"({digits[..2]}) {digits[2..7]}-{digits[7..]}",
        10 => $"({digits[..2]}) {digits[2..6]}-{digits[6..]}",
        _ => digits,
    };

    [GeneratedRegex(@"^[^@\s]+@[^@\s]+\.[^@\s.]{2,}$")]
    private static partial Regex EmailShape();

    /// <summary>Limpa e confere. Devolve o perfil como será gravado.</summary>
    /// <exception cref="CustomerException">O que está errado, em uma frase.</exception>
    public static CustomerProfile Normalize(CustomerProfile profile, DateOnly today)
    {
        var name = string.Join(' ', (profile.Name ?? "").Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries));
        if (name.Length == 0) throw new CustomerException("Nome do cliente é obrigatório.");
        if (name.Length > MaxName) throw new CustomerException($"Nome com mais de {MaxName} letras.");

        var phone = Digits(profile.WhatsApp);
        // Colado do próprio WhatsApp vem com o +55 na frente.
        if (phone.Length is 12 or 13 && phone.StartsWith("55", StringComparison.Ordinal)) phone = phone[2..];
        if (phone.Length != 0 && phone.Length is not (10 or 11))
        {
            throw new CustomerException("WhatsApp precisa do DDD e do número: 10 ou 11 dígitos, como (21) 99876-5432.");
        }

        var cpf = Digits(profile.Cpf);
        if (cpf.Length != 0 && !IsValidCpf(cpf)) throw new CustomerException("CPF inválido: confira os dígitos.");

        // Sem WhatsApp e sem CPF, o balcão não acharia o cliente de novo.
        if (phone.Length == 0 && cpf.Length == 0) throw new CustomerException("Informe o WhatsApp ou o CPF do cliente.");

        var email = (profile.Email ?? "").Trim().ToLowerInvariant();
        if (email.Length > MaxEmail || (email.Length != 0 && !EmailShape().IsMatch(email)))
        {
            throw new CustomerException("E-mail inválido: confira o endereço, como nome@exemplo.com.");
        }

        string? block = null, unit = null;
        if (profile.IsResident)
        {
            block = Unit(profile.UnitBlock, "Bloco");
            unit = Unit(profile.UnitNumber, "Apartamento");
            if (unit is null) throw new CustomerException("Morador precisa do número do apartamento.");
        }

        if (profile.BirthDate is { } birth && (birth > today || birth.Year < 1900))
        {
            throw new CustomerException("Data de nascimento inválida.");
        }
        if (profile.MarketingOptIn && phone.Length == 0 && email.Length == 0)
        {
            throw new CustomerException("Para receber ofertas, informe o WhatsApp ou o e-mail.");
        }

        return new CustomerProfile(name, NullIfEmpty(phone), NullIfEmpty(email), NullIfEmpty(cpf), profile.IsResident,
            block, unit, profile.BirthDate, profile.MarketingOptIn);
    }

    /// <summary>"b" e "B" são o mesmo bloco: o apartamento é chave de busca, e a busca não pode depender da caixa.</summary>
    private static string? Unit(string? value, string what)
    {
        var text = string.Join(' ', (value ?? "").Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries)).ToUpperInvariant();
        if (text.Length > MaxUnit) throw new CustomerException($"{what} com mais de {MaxUnit} caracteres.");
        return NullIfEmpty(text);
    }

    private static string? NullIfEmpty(string text) => text.Length == 0 ? null : text;

    /// <summary>"26/09/1990", "1990-09-26" ou vazio.</summary>
    public static bool TryParseBirthDate(string? text, out DateOnly? date)
    {
        date = null;
        var value = (text ?? "").Trim();
        if (value.Length == 0) return true;
        if (DateOnly.TryParseExact(value, ["dd/MM/yyyy", "d/M/yyyy", "yyyy-MM-dd"], CultureInfo.InvariantCulture,
                DateTimeStyles.None, out var parsed))
        {
            date = parsed;
            return true;
        }
        return false;
    }
}
