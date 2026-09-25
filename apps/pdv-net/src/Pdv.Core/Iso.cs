using System.Globalization;

namespace Pdv.Core;

/// <summary>
/// Datas e identificadores no formato que o banco e a nuvem já conhecem.
/// </summary>
public static class Iso
{
    /// <summary>
    /// O <c>iso()</c> do PDV em Python: UTC, milissegundos e <c>+00:00</c>
    /// (<c>2026-09-25T03:49:25.134+00:00</c>). O <c>created_at</c> entra no HMAC
    /// da auditoria — outro formato daria outro hash.
    /// </summary>
    public static string Format(DateTimeOffset moment) =>
        moment.ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:ss.fff'+00:00'", CultureInfo.InvariantCulture);

    public static string Now(TimeProvider? clock = null) =>
        Format((clock ?? TimeProvider.System).GetUtcNow());

    /// <summary>UUIDv7, como o <c>new_id()</c>: ordenado pelo tempo.</summary>
    public static string NewId() => Guid.CreateVersion7().ToString();
}
