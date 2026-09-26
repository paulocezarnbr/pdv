using System.Buffers.Binary;
using System.Net;
using System.Net.Sockets;
using Pdv.Edge;

namespace Pdv.Core.Tests;

/// <summary>
/// O anúncio mDNS do salão: as mensagens que ele troca, e a volta pelo parser
/// do <c>zeroconf</c> do Python no <c>crosscheck.py</c>.
/// </summary>
public sealed class ServiceAnnouncerTests
{
    /// <summary>Os mesmos de <c>crosscheck.py</c>.</summary>
    private static readonly ServiceInfo Info = new(
        "bbbbbbbb-0000-0000-0000-000000000002", "Confeitaria Aurora", "cccccccc-0000-0000-0000-000000000003", 8420, "https",
        IPAddress.Parse("192.168.0.14"));

    /// <summary>Uma consulta como um cliente faria, com o nome inteiro (sem compressão).</summary>
    private static byte[] Query(string name, ushort type, ushort id = 0, bool unicast = false)
    {
        var encoded = MdnsMessage.Name(name);
        var packet = new byte[12 + encoded.Length + 4];
        BinaryPrimitives.WriteUInt16BigEndian(packet, id);
        BinaryPrimitives.WriteUInt16BigEndian(packet.AsSpan(4), 1);
        encoded.CopyTo(packet, 12);
        BinaryPrimitives.WriteUInt16BigEndian(packet.AsSpan(12 + encoded.Length), type);
        BinaryPrimitives.WriteUInt16BigEndian(packet.AsSpan(14 + encoded.Length), (ushort)(unicast ? 0x8001 : 1));
        return packet;
    }

    private static int Answers(byte[] packet) => BinaryPrimitives.ReadUInt16BigEndian(packet.AsSpan(6));

    [Fact]
    public void Asking_for_the_service_gets_everything_at_once()
    {
        var questions = MdnsMessage.Questions(Query("_pdvedge._tcp.local", MdnsMessage.TypePtr), out _);

        var answer = MdnsMessage.Answer(Info, questions)!;

        // PTR, SRV, TXT e A numa resposta só: três perguntas a menos pelo Wi-Fi da loja.
        Assert.Equal(4, Answers(answer));
    }

    [Theory]
    [InlineData("_PDVEDGE._TCP.LOCAL.", MdnsMessage.TypePtr, 4)]
    [InlineData("Confeitaria Aurora._pdvedge._tcp.local", MdnsMessage.TypeSrv, 2)]
    [InlineData("Confeitaria Aurora._pdvedge._tcp.local", MdnsMessage.TypeTxt, 1)]
    [InlineData("Confeitaria Aurora._pdvedge._tcp.local", MdnsMessage.TypeAny, 3)]
    [InlineData("pdv-cccccccc.local", MdnsMessage.TypeA, 1)]
    [InlineData("_services._dns-sd._udp.local", MdnsMessage.TypePtr, 1)]
    public void Answers_what_is_ours(string name, ushort type, int records)
    {
        var answer = MdnsMessage.Answer(Info, MdnsMessage.Questions(Query(name, type), out _));

        Assert.Equal(records, Answers(answer!));
    }

    [Theory]
    [InlineData("_http._tcp.local", MdnsMessage.TypePtr)]
    [InlineData("Outra Loja._pdvedge._tcp.local", MdnsMessage.TypeSrv)]
    [InlineData("pdv-cccccccc.local", MdnsMessage.TypeTxt)]
    public void Stays_quiet_about_what_is_not(string name, ushort type)
    {
        Assert.Null(MdnsMessage.Answer(Info, MdnsMessage.Questions(Query(name, type), out _)));
    }

    [Fact]
    public void A_response_from_another_device_is_not_a_question()
    {
        // A resposta legada repete a pergunta: é a que enganaria um parser que
        // não olhasse o bit de resposta, e poria dois PDVs respondendo um ao outro.
        var questions = MdnsMessage.Questions(Query("_pdvedge._tcp.local", MdnsMessage.TypePtr, id: 7), out var id);
        var response = MdnsMessage.Answer(Info, questions, id, legacyUnicast: true)!;

        Assert.Empty(MdnsMessage.Questions(response, out _));
        Assert.Empty(MdnsMessage.Questions(MdnsMessage.Announcement(Info), out _));
    }

    [Theory]
    [InlineData(new byte[0])]
    [InlineData(new byte[] { 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0 })]
    [InlineData(new byte[] { 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0xC0, 12, 0, 12, 0, 1 })]
    [InlineData(new byte[] { 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 40, 0x61 })]
    public void A_malformed_or_looping_query_is_ignored(byte[] packet)
    {
        // O ponteiro que aponta para si mesmo, o rótulo maior que o pacote: a
        // rede da loja é a do Wi-Fi do cliente, e qualquer um manda pacote.
        Assert.Empty(MdnsMessage.Questions(packet, out _));
    }

    [Fact]
    public void Reads_a_compressed_name()
    {
        // Pergunta 1: "_pdvedge._tcp.local"; pergunta 2: "pdv-cccccccc" + ponteiro para ".local".
        var first = MdnsMessage.Name("_pdvedge._tcp.local");
        var packet = new List<byte> { 0, 0, 0, 0, 0, 2, 0, 0, 0, 0, 0, 0 };
        packet.AddRange(first);
        packet.AddRange([0, 12, 0, 1]);
        var localAt = 12 + 1 + "_pdvedge".Length + 1 + "_tcp".Length;
        packet.Add(12);
        packet.AddRange("pdv-cccccccc"u8.ToArray());
        packet.AddRange([0xC0, (byte)localAt, 0, 1, 0, 1]);

        var questions = MdnsMessage.Questions(packet.ToArray(), out _);

        Assert.Equal(["_pdvedge._tcp.local", "pdv-cccccccc.local"], questions.Select(q => q.Name));
    }

    [Fact]
    public void A_legacy_query_is_echoed_with_its_id_and_short_ttl()
    {
        var questions = MdnsMessage.Questions(Query("_pdvedge._tcp.local", MdnsMessage.TypePtr, id: 0x1234), out var id);

        var answer = MdnsMessage.Answer(Info, questions, id, legacyUnicast: true)!;

        Assert.Equal(0x1234, BinaryPrimitives.ReadUInt16BigEndian(answer));
        Assert.Equal(1, BinaryPrimitives.ReadUInt16BigEndian(answer.AsSpan(4)));
    }

    [Fact]
    public void The_instance_is_one_dns_label()
    {
        var info = Info with { StoreName = "Pães & Doces Ltda. Unidade Centro Histórico" };

        Assert.DoesNotContain('.', info.InstanceLabel);
        Assert.InRange(System.Text.Encoding.UTF8.GetByteCount(info.InstanceLabel), 1, 63);
        Assert.Equal("PDV", (Info with { StoreName = "" }).InstanceLabel);
    }

    /// <summary>
    /// A resposta à consulta que o <c>zeroconf</c> montou, o anúncio e a
    /// despedida, para o <c>crosscheck.py</c> ler com o parser dele. O CI roda
    /// <c>crosscheck.py write-mdns</c> antes e aponta <c>PDV_PY_MDNS_DIR</c>.
    /// </summary>
    [Fact]
    public void Answers_the_query_a_zeroconf_client_makes()
    {
        var source = Environment.GetEnvironmentVariable("PDV_PY_MDNS_DIR");
        var query = string.IsNullOrEmpty(source)
            ? Query("_pdvedge._tcp.local", MdnsMessage.TypePtr)
            : File.ReadAllBytes(Path.Combine(source, "query.bin"));

        var answer = MdnsMessage.Answer(Info, MdnsMessage.Questions(query, out _));

        Assert.NotNull(answer);
        var target = Environment.GetEnvironmentVariable("PDV_CROSSCHECK_OUT");
        if (string.IsNullOrEmpty(target)) return;
        var folder = Path.Combine(Path.GetDirectoryName(target)!, "mdns");
        Directory.CreateDirectory(folder);
        File.WriteAllBytes(Path.Combine(folder, "answer.bin"), answer);
        File.WriteAllBytes(Path.Combine(folder, "announcement.bin"), MdnsMessage.Announcement(Info));
        File.WriteAllBytes(Path.Combine(folder, "goodbye.bin"), MdnsMessage.Announcement(Info, goodbye: true));
    }
}

/// <summary>O laço de rede do anúncio: uma consulta UDP de verdade, respondida a quem perguntou.</summary>
public sealed class ServiceAnnouncerNetworkTests
{
    [Fact]
    public async Task A_legacy_query_over_the_network_is_answered_directly()
    {
        var address = IPAddress.Parse(SalonCertificate.LocalIpAddress());
        var info = new ServiceInfo("s", "Loja", "dddddddd-0000", 8420, "https", address);
        var messages = new List<string>();
        using var announcer = new ServiceAnnouncer(info, messages.Add);
        Assert.True(announcer.Start(), string.Join(" | ", messages));

        using var client = new System.Net.Sockets.UdpClient(new IPEndPoint(IPAddress.Any, 0));
        var query = new byte[12 + MdnsMessage.Name("_pdvedge._tcp.local").Length + 4];
        BinaryPrimitives.WriteUInt16BigEndian(query, 0x4242);
        BinaryPrimitives.WriteUInt16BigEndian(query.AsSpan(4), 1);
        MdnsMessage.Name("_pdvedge._tcp.local").CopyTo(query, 12);
        BinaryPrimitives.WriteUInt16BigEndian(query.AsSpan(query.Length - 4), MdnsMessage.TypePtr);
        BinaryPrimitives.WriteUInt16BigEndian(query.AsSpan(query.Length - 2), 1);
        // Pelo grupo, como o celular pergunta. Endereçada ao IP da máquina, a
        // consulta cai num só dos sockets da 5353 — no Windows com Chrome aberto,
        // no do Chrome ou no do serviço DNS, e o PDV nunca a vê.
        client.Client.SetSocketOption(SocketOptionLevel.IP, SocketOptionName.MulticastInterface, address.GetAddressBytes());
        await client.SendAsync(query, new IPEndPoint(IPAddress.Parse("224.0.0.251"), ServiceAnnouncer.Port));

        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        var received = await client.ReceiveAsync(timeout.Token);

        // Consulta de porta que não é a 5353: resposta direta, com o id de quem perguntou.
        Assert.Equal(0x4242, BinaryPrimitives.ReadUInt16BigEndian(received.Buffer));
        Assert.Equal(4, BinaryPrimitives.ReadUInt16BigEndian(received.Buffer.AsSpan(6)));
    }
}
