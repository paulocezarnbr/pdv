using System.Security.Cryptography;
using System.Text;

namespace Pdv.Data.Customers;

/// <summary>
/// O id do cliente sai de quem ele é na loja, não do caixa que o cadastrou.
/// </summary>
/// <remarks>
/// <para>
/// O cadastro é do estabelecimento: desce para todos os caixas da loja. Sem
/// internet, dois balcões podem cadastrar a mesma pessoa ao mesmo tempo. Com id
/// aleatório, viravam dois clientes, com o cashback de cada compra num deles.
/// Com o id derivado da loja e do WhatsApp (ou do CPF, quando não há WhatsApp),
/// os dois caixas chegam ao MESMO id: a nuvem reconhece o mesmo cliente, e os
/// saldos dos dois balcões apontam para ele.
/// </para>
/// <para>
/// UUID versão 5 (RFC 4122): SHA-1 de um espaço de nomes fixo e do texto.
/// Determinístico, e sem nada que se reverta para o número de telefone além do
/// que o próprio cadastro já guarda.
/// </para>
/// </remarks>
public static class CustomerIdentity
{
    /// <summary>O espaço de nomes dos clientes do PDV. Fixo para sempre: mudar muda todos os ids.</summary>
    public static readonly Guid Namespace = new("8d1f3c52-6a0e-4c5b-9f3e-2b7c1d9a4e60");

    /// <summary>O id do cliente desta loja com este WhatsApp (ou CPF). Só dígitos.</summary>
    public static string IdFor(string tenantId, string storeId, string? whatsApp, string? cpf)
    {
        var key = whatsApp is { Length: > 0 } ? "whatsapp:" + whatsApp
            : cpf is { Length: > 0 } ? "cpf:" + cpf
            : throw new ArgumentException("Cliente sem WhatsApp e sem CPF não tem id derivado.");
        return UuidV5(Namespace, $"{tenantId}|{storeId}|{key}");
    }

    /// <summary>UUID versão 5 (RFC 4122, seção 4.3), em texto minúsculo.</summary>
    public static string UuidV5(Guid space, string name)
    {
        var spaceBytes = space.ToByteArray(bigEndian: true);
        var nameBytes = Encoding.UTF8.GetBytes(name);
        var hash = SHA1.HashData([.. spaceBytes, .. nameBytes]);
        var bytes = hash[..16];
        bytes[6] = (byte)((bytes[6] & 0x0F) | 0x50);
        bytes[8] = (byte)((bytes[8] & 0x3F) | 0x80);
        return new Guid(bytes, bigEndian: true).ToString();
    }
}
