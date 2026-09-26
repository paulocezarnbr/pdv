using System.Buffers.Binary;
using System.Net;
using System.Net.Sockets;
using System.Text;

namespace Pdv.Edge;

/// <summary>O que o anúncio diz do terminal — o <c>ServiceInfoData</c> do Python.</summary>
/// <param name="Scheme">
/// <c>https</c> quando há certificado: o app descoberto monta a URL certa em vez
/// de tentar HTTP numa porta que só fala TLS, falha que o celular reporta como
/// "servidor não encontrado".
/// </param>
public sealed record ServiceInfo(string StoreId, string StoreName, string DeviceId, int Port, string Scheme, IPAddress Address)
{
    public const string ServiceType = "_pdvedge._tcp.local";

    /// <summary>O rótulo da instância: o nome da loja, num rótulo DNS só (até 63 bytes).</summary>
    public string InstanceLabel
    {
        get
        {
            var name = new string([.. StoreName.EnumerateRunes().Take(32).SelectMany(r => r.ToString())]);
            // Ponto partiria o nome em dois rótulos; e o rótulo DNS tem teto de 63 bytes.
            name = name.Replace('.', ' ');
            while (Encoding.UTF8.GetByteCount(name) > 63) name = name[..^1];
            return name.Length > 0 ? name : "PDV";
        }
    }

    public string InstanceName => $"{InstanceLabel}.{ServiceType}";

    public string HostName => $"pdv-{DeviceId[..Math.Min(8, DeviceId.Length)]}.local";

    /// <summary>As propriedades do TXT, na ordem do Python.</summary>
    public IReadOnlyList<(string Key, string Value)> Properties =>
    [
        ("store_id", StoreId), ("store_name", StoreName), ("device_id", DeviceId), ("version", SalonApi.Version),
        ("scheme", Scheme),
    ];
}

/// <summary>
/// As mensagens mDNS (RFC 6762) que o anúncio troca: só o necessário para
/// responder por <c>_pdvedge._tcp</c>. Conferidas pelo parser do próprio
/// <c>zeroconf</c> do Python no <c>crosscheck.py</c>.
/// </summary>
internal static class MdnsMessage
{
    public const ushort TypeA = 1;
    public const ushort TypePtr = 12;
    public const ushort TypeTxt = 16;
    public const ushort TypeSrv = 33;
    public const ushort TypeAny = 255;

    private const ushort ClassIn = 1;
    private const ushort CacheFlush = 0x8000;

    /// <summary>Os TTLs do <c>zeroconf</c>: nome e texto duram mais que endereço, que muda com o DHCP.</summary>
    public const uint OtherTtl = 4500;
    public const uint HostTtl = 120;

    /// <summary>O pedido de "quem oferece serviços aqui", que os navegadores de rede fazem antes de tudo.</summary>
    public const string ServicesEnumeration = "_services._dns-sd._udp.local";

    internal sealed record Question(string Name, ushort Type, bool UnicastResponse);

    /// <summary>As perguntas de uma consulta. Resposta, mensagem truncada ou malformada: nenhuma.</summary>
    public static IReadOnlyList<Question> Questions(ReadOnlySpan<byte> packet, out ushort id)
    {
        id = 0;
        var questions = new List<Question>();
        if (packet.Length < 12) return questions;
        id = BinaryPrimitives.ReadUInt16BigEndian(packet);
        var flags = BinaryPrimitives.ReadUInt16BigEndian(packet[2..]);
        // Só consulta padrão: resposta de outro aparelho não é pergunta para nós.
        if ((flags & 0x8000) != 0 || (flags & 0x7800) != 0) return questions;
        var count = BinaryPrimitives.ReadUInt16BigEndian(packet[4..]);
        var offset = 12;
        for (var i = 0; i < count; i++)
        {
            if (!TryReadName(packet, ref offset, out var name) || offset + 4 > packet.Length) return [];
            var type = BinaryPrimitives.ReadUInt16BigEndian(packet[offset..]);
            var klass = BinaryPrimitives.ReadUInt16BigEndian(packet[(offset + 2)..]);
            offset += 4;
            questions.Add(new Question(name, type, (klass & 0x8000) != 0));
        }
        return questions;
    }

    /// <summary>
    /// A resposta às perguntas que nos cabem, ou <c>null</c> se nenhuma cabe.
    /// Quem pergunta pelo serviço recebe tudo de uma vez (PTR, SRV, TXT e A):
    /// é o que poupa o celular de três perguntas a mais pela rede da loja.
    /// </summary>
    public static byte[]? Answer(ServiceInfo info, IReadOnlyList<Question> questions, ushort id = 0, bool legacyUnicast = false)
    {
        var wanted = new HashSet<ushort>();
        foreach (var question in questions)
        {
            bool Asks(ushort type) => question.Type == type || question.Type == TypeAny;
            if (Same(question.Name, ServicesEnumeration) && Asks(TypePtr)) wanted.Add(0);
            if (Same(question.Name, ServiceInfo.ServiceType) && Asks(TypePtr)) wanted.UnionWith([TypePtr, TypeSrv, TypeTxt, TypeA]);
            if (Same(question.Name, info.InstanceName))
            {
                if (Asks(TypeSrv)) wanted.UnionWith([TypeSrv, TypeA]);
                if (Asks(TypeTxt)) wanted.Add(TypeTxt);
            }
            if (Same(question.Name, info.HostName) && Asks(TypeA)) wanted.Add(TypeA);
        }
        return wanted.Count == 0 ? null : Build(info, wanted, id, legacyUnicast ? questions : null, goodbye: false);
    }

    /// <summary>O anúncio espontâneo da subida, ou a despedida (TTL zero) da saída.</summary>
    public static byte[] Announcement(ServiceInfo info, bool goodbye = false) =>
        Build(info, [TypePtr, TypeSrv, TypeTxt, TypeA], 0, null, goodbye);

    private static byte[] Build(ServiceInfo info, HashSet<ushort> wanted, ushort id, IReadOnlyList<Question>? echo, bool goodbye)
    {
        var records = new List<byte[]>();
        // Resposta a consulta "legada" (porta de origem que não é 5353): sem o
        // bit de cache e TTL curto, como manda a RFC 6762 §6.7.
        var legacy = echo is not null;
        uint Ttl(uint normal) => goodbye ? 0 : legacy ? Math.Min(normal, 10u) : normal;
        var unique = legacy ? ClassIn : (ushort)(ClassIn | CacheFlush);

        if (wanted.Contains(0)) records.Add(Record(ServicesEnumeration, TypePtr, ClassIn, Ttl(OtherTtl), Name(ServiceInfo.ServiceType)));
        if (wanted.Contains(TypePtr)) records.Add(Record(ServiceInfo.ServiceType, TypePtr, ClassIn, Ttl(OtherTtl), Name(info.InstanceName)));
        if (wanted.Contains(TypeSrv))
        {
            var target = Name(info.HostName);
            var data = new byte[6 + target.Length];
            BinaryPrimitives.WriteUInt16BigEndian(data.AsSpan(4), (ushort)info.Port);
            target.CopyTo(data, 6);
            records.Add(Record(info.InstanceName, TypeSrv, unique, Ttl(HostTtl), data));
        }
        if (wanted.Contains(TypeTxt))
        {
            using var text = new MemoryStream();
            foreach (var (key, value) in info.Properties)
            {
                var entry = Encoding.UTF8.GetBytes($"{key}={value}");
                if (entry.Length > 255) entry = entry[..255];
                text.WriteByte((byte)entry.Length);
                text.Write(entry);
            }
            records.Add(Record(info.InstanceName, TypeTxt, unique, Ttl(OtherTtl), text.ToArray()));
        }
        if (wanted.Contains(TypeA)) records.Add(Record(info.HostName, TypeA, unique, Ttl(HostTtl), info.Address.MapToIPv4().GetAddressBytes()));

        using var packet = new MemoryStream();
        Span<byte> header = stackalloc byte[12];
        BinaryPrimitives.WriteUInt16BigEndian(header, id);
        BinaryPrimitives.WriteUInt16BigEndian(header[2..], 0x8400); // resposta, autoritativa
        BinaryPrimitives.WriteUInt16BigEndian(header[4..], (ushort)(echo?.Count ?? 0));
        BinaryPrimitives.WriteUInt16BigEndian(header[6..], (ushort)records.Count);
        packet.Write(header);
        Span<byte> tail = stackalloc byte[4];
        foreach (var question in echo ?? [])
        {
            packet.Write(Name(question.Name));
            BinaryPrimitives.WriteUInt16BigEndian(tail, question.Type);
            BinaryPrimitives.WriteUInt16BigEndian(tail[2..], ClassIn);
            packet.Write(tail);
        }
        foreach (var record in records) packet.Write(record);
        return packet.ToArray();
    }

    private static byte[] Record(string name, ushort type, ushort klass, uint ttl, byte[] data)
    {
        var encoded = Name(name);
        var record = new byte[encoded.Length + 10 + data.Length];
        encoded.CopyTo(record, 0);
        var span = record.AsSpan(encoded.Length);
        BinaryPrimitives.WriteUInt16BigEndian(span, type);
        BinaryPrimitives.WriteUInt16BigEndian(span[2..], klass);
        BinaryPrimitives.WriteUInt32BigEndian(span[4..], ttl);
        BinaryPrimitives.WriteUInt16BigEndian(span[8..], (ushort)data.Length);
        data.CopyTo(record, encoded.Length + 10);
        return record;
    }

    /// <summary>Um nome em rótulos, sem compressão (permitida, não obrigatória).</summary>
    internal static byte[] Name(string name)
    {
        using var stream = new MemoryStream();
        foreach (var label in name.TrimEnd('.').Split('.'))
        {
            var bytes = Encoding.UTF8.GetBytes(label);
            stream.WriteByte((byte)bytes.Length);
            stream.Write(bytes);
        }
        stream.WriteByte(0);
        return stream.ToArray();
    }

    /// <summary>Lê um nome com ponteiros de compressão, recusando laço e ponteiro para fora.</summary>
    private static bool TryReadName(ReadOnlySpan<byte> packet, ref int offset, out string name)
    {
        var labels = new List<string>();
        var position = offset;
        var jumped = false;
        for (var hops = 0; hops < 32; hops++)
        {
            if (position >= packet.Length) break;
            var length = packet[position];
            if (length == 0)
            {
                if (!jumped) offset = position + 1;
                name = string.Join('.', labels);
                return true;
            }
            if ((length & 0xC0) == 0xC0)
            {
                if (position + 1 >= packet.Length) break;
                var target = ((length & 0x3F) << 8) | packet[position + 1];
                if (!jumped) offset = position + 2;
                jumped = true;
                position = target;
                continue;
            }
            if (position + 1 + length > packet.Length) break;
            labels.Add(Encoding.UTF8.GetString(packet.Slice(position + 1, length)));
            position += 1 + length;
        }
        name = "";
        return false;
    }

    private static bool Same(string a, string b) =>
        string.Equals(a.TrimEnd('.'), b.TrimEnd('.'), StringComparison.OrdinalIgnoreCase);
}

/// <summary>
/// O anúncio do PDV na LAN por mDNS (<c>_pdvedge._tcp</c>) — o
/// <c>edge/discovery.py</c>: o celular acha o caixa sem ninguém digitar
/// <c>192.168.0.14:8420</c>, inclusive depois que o DHCP troca o endereço.
/// </summary>
/// <remarks>
/// <para>
/// <b>Descobrir não é autenticar</b>: o anúncio diz onde o PDV está; entrar
/// ainda exige o pareamento. E mDNS não atravessa VLAN nem isolamento de
/// cliente do roteador — o endereço manual continua sendo a saída.
/// </para>
/// <para>
/// <b>Falhar aqui nunca impede a venda</b>: <see cref="Start"/> devolve falso e
/// o app do garçom chega pelo endereço digitado ou pelo QR do caixa.
/// </para>
/// </remarks>
public sealed class ServiceAnnouncer(ServiceInfo info, Action<string>? log = null) : IDisposable
{
    public const int Port = 5353;

    private static readonly IPAddress Group = IPAddress.Parse("224.0.0.251");

    private Socket? _socket;
    private CancellationTokenSource? _stop;
    private Task? _loop;

    public bool Start()
    {
        if (_socket is not null) return true;
        try
        {
            var socket = new Socket(AddressFamily.InterNetwork, SocketType.Dgram, ProtocolType.Udp);
            // Outro responder na máquina (Bonjour, o PDV em Python) divide a porta 5353.
            socket.SetSocketOption(SocketOptionLevel.Socket, SocketOptionName.ReuseAddress, true);
            socket.Bind(new IPEndPoint(IPAddress.Any, Port));
            socket.SetSocketOption(SocketOptionLevel.IP, SocketOptionName.AddMembership, new MulticastOption(Group, info.Address));
            socket.SetSocketOption(SocketOptionLevel.IP, SocketOptionName.MulticastInterface, info.Address.GetAddressBytes());
            socket.SetSocketOption(SocketOptionLevel.IP, SocketOptionName.MulticastTimeToLive, 255);
            _socket = socket;
            _stop = new CancellationTokenSource();
            _loop = Task.Run(() => ListenAsync(socket, _stop.Token));
            // Duas vezes, um segundo entre elas (RFC 6762 §8.3): um pacote UDP perdido não esconde o PDV.
            Send(MdnsMessage.Announcement(info));
            _ = Task.Delay(TimeSpan.FromSeconds(1), _stop.Token)
                .ContinueWith(_ => Send(MdnsMessage.Announcement(info)), TaskContinuationOptions.OnlyOnRanToCompletion);
            log?.Invoke($"Salão anunciado em {info.Address}:{info.Port} via mDNS.");
            return true;
        }
        catch (SocketException error)
        {
            log?.Invoke($"Salão: não foi possível anunciar na rede: {error.Message}");
            Dispose();
            return false;
        }
    }

    /// <summary>Retira o anúncio (TTL zero). Sem isto, o app persegue um PDV que já fechou.</summary>
    public void Dispose()
    {
        if (_socket is { } socket)
        {
            Send(MdnsMessage.Announcement(info, goodbye: true));
            _stop?.Cancel();
            socket.Dispose();
            try
            {
                _loop?.Wait(TimeSpan.FromSeconds(2));
            }
            catch (AggregateException)
            {
            }
        }
        _socket = null;
        _stop?.Dispose();
        _stop = null;
    }

    private void Send(byte[] packet, EndPoint? to = null)
    {
        try
        {
            _socket?.SendTo(packet, to ?? new IPEndPoint(Group, Port));
        }
        catch (Exception error) when (error is SocketException or ObjectDisposedException)
        {
            // Rede caiu ou o anúncio já foi retirado: o próximo pedido tenta de novo.
        }
    }

    private async Task ListenAsync(Socket socket, CancellationToken stop)
    {
        var buffer = new byte[9000];
        while (!stop.IsCancellationRequested)
        {
            SocketReceiveFromResult received;
            try
            {
                received = await socket.ReceiveFromAsync(buffer, new IPEndPoint(IPAddress.Any, 0), stop);
            }
            catch (Exception error) when (error is OperationCanceledException or ObjectDisposedException or SocketException)
            {
                return;
            }
            var questions = MdnsMessage.Questions(buffer.AsSpan(0, received.ReceivedBytes), out var id);
            if (questions.Count == 0) continue;
            var source = (IPEndPoint)received.RemoteEndPoint;
            // Porta de origem que não é 5353: consulta "legada", respondida direto a quem perguntou.
            var legacy = source.Port != Port;
            if (MdnsMessage.Answer(info, questions, legacy ? id : (ushort)0, legacy) is { } answer)
            {
                Send(answer, legacy || questions.Any(q => q.UnicastResponse) ? source : null);
            }
        }
    }
}
