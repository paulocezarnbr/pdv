using System.Security.Cryptography;
using System.Text;

namespace Pdv.Core.Audit;

/// <summary>Um elo gravado no <c>audit_ledger</c>, como a verificação o lê.</summary>
public sealed record LedgerLink(
    long Seq,
    string EventType,
    string PayloadJson,
    string PrevHash,
    string Hash,
    string CreatedAt);

public sealed class AuditChainException(string message) : Exception(message);

/// <summary>
/// A cadeia de auditoria — mesma fórmula de <c>pdv/services/audit.py</c>.
/// </summary>
/// <remarks>
/// A adulteração local não é impedida, é <b>detectada</b>: cada elo é um
/// HMAC-SHA256, com o segredo do terminal, sobre o elo anterior e o conteúdo.
/// Apagar, reordenar ou editar uma linha quebra a cadeia daquele ponto em
/// diante.
/// </remarks>
public static class AuditChain
{
    public const string GenesisHash = "0000000000000000000000000000000000000000000000000000000000000000";

    /// <summary>
    /// <c>prev|seq|evento|payload|criado_em</c>. O <c>|</c> não é decorativo:
    /// sem separador, "ab"+"c" e "a"+"bc" dariam o mesmo material.
    /// </summary>
    public static string ComputeHash(
        byte[] secret, string prevHash, long seq, string eventType, string payloadJson, string createdAt)
    {
        if (secret.Length == 0)
        {
            throw new ArgumentException("device_secret vazio: a cadeia seria forjável.", nameof(secret));
        }
        var material = $"{prevHash}|{seq}|{eventType}|{payloadJson}|{createdAt}";
        return Convert.ToHexStringLower(HMACSHA256.HashData(secret, Encoding.UTF8.GetBytes(material)));
    }

    /// <summary>Revalida a cadeia inteira de um terminal, na ordem de <c>seq</c>.</summary>
    /// <exception cref="AuditChainException">Buraco na sequência ou hash divergente.</exception>
    public static void Verify(IEnumerable<LedgerLink> links, byte[] secret)
    {
        var expectedPrev = GenesisHash;
        var expectedSeq = 1L;

        foreach (var link in links)
        {
            if (link.Seq != expectedSeq)
            {
                throw new AuditChainException(
                    $"Buraco na auditoria: esperado seq {expectedSeq}, encontrado {link.Seq}. " +
                    "Entrada removida do banco local.");
            }
            if (link.PrevHash != expectedPrev)
            {
                throw new AuditChainException($"Cadeia quebrada no seq {link.Seq}: prev_hash não confere.");
            }

            var recomputed = ComputeHash(
                secret, link.PrevHash, link.Seq, link.EventType, link.PayloadJson, link.CreatedAt);
            if (!CryptographicOperations.FixedTimeEquals(
                    Encoding.ASCII.GetBytes(recomputed), Encoding.ASCII.GetBytes(link.Hash)))
            {
                throw new AuditChainException($"Conteúdo adulterado no seq {link.Seq}: hash não confere.");
            }

            expectedPrev = link.Hash;
            expectedSeq++;
        }
    }
}
