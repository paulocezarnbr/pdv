using System.Net.WebSockets;
using System.Text;
using System.Text.Json.Nodes;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Http;
using Pdv.Data.Edge;

namespace Pdv.Edge;

/// <summary>
/// O WebSocket da tela da cozinha — o <c>/kds/stream</c> do Python. Só empurra
/// fato consumado; nunca aceita comando.
/// </summary>
/// <remarks>
/// <para>
/// A tela avança ticket por <c>POST</c>, que passa pela validação de transição.
/// Um WebSocket que aceitasse escrita viraria o caminho sem autenticação por
/// onde a próxima versão do app derruba o estado da loja — por isso o que chega
/// por ele é lido e descartado.
/// </para>
/// <para>
/// O token vem na consulta porque o WebSocket do navegador não define cabeçalho
/// no handshake; é token de aparelho, revogável do caixa, nunca senha de pessoa.
/// Recusado, o handshake não completa (403), como no Starlette.
/// </para>
/// </remarks>
internal static class KdsStream
{
    /// <summary>Sem isto, um roteador doméstico derruba a conexão ociosa e a tela congela mostrando dado velho.</summary>
    public static readonly TimeSpan PingInterval = TimeSpan.FromSeconds(20);

    private static readonly string[] Topics = ["ticket.queued", "ticket.changed"];

    public static void Map(WebApplication app, SalonServices services, SemaphoreSlim gate)
    {
        app.Map("/kds/stream", async (HttpContext http) =>
        {
            if (!http.WebSockets.IsWebSocketRequest)
            {
                await SalonApi.Error(http, new HttpError(400, "Esta rota é um WebSocket."));
                return;
            }

            await gate.WaitAsync(http.RequestAborted);
            PairedDevice device;
            JsonNode snapshot;
            try
            {
                device = services.Auth.Authenticate(http.Request.Query["token"].ToString());
                snapshot = Snapshot(services);
            }
            catch (DeviceAuthException)
            {
                http.Response.StatusCode = 403;
                return;
            }
            finally
            {
                gate.Release();
            }

            // A assinatura nasce antes do estado completo: um ticket lançado entre
            // os dois chegaria pelo barramento, e não se perderia no intervalo.
            using var subscription = services.Hub.Subscribe(Topics);
            using var socket = await http.WebSockets.AcceptWebSocketAsync();
            using var closing = CancellationTokenSource.CreateLinkedTokenSource(http.RequestAborted);
            var drain = Drain(socket, closing);
            try
            {
                // Estado completo primeiro: quem reconecta após queda precisa da fila inteira.
                await Send(socket, snapshot, closing.Token);
                while (!closing.IsCancellationRequested && socket.State == WebSocketState.Open)
                {
                    var next = await subscription.NextAsync(PingInterval, closing.Token);
                    await Send(socket, next?.ToJson() ?? new JsonObject { ["kind"] = "ping" }, closing.Token);
                }
            }
            catch (Exception error) when (error is OperationCanceledException or WebSocketException)
            {
                // Uma tela caindo não derruba o servidor.
            }
            finally
            {
                await closing.CancelAsync();
                await drain;
            }
        });
    }

    private static JsonObject Snapshot(SalonServices services)
    {
        // O "snapshot" que as telas já conhecem: o tipo e a fila ativa inteira.
        return new JsonObject
        {
            ["kind"] = "snapshot",
            ["tickets"] = new JsonArray([.. services.Kds.ListActive().Select(t => (JsonNode)t.ToJson())]),
        };
    }

    private static Task Send(WebSocket socket, JsonNode message, CancellationToken cancellation) =>
        socket.SendAsync(Encoding.UTF8.GetBytes(message.ToJsonString()), WebSocketMessageType.Text, true, cancellation);

    /// <summary>Lê e descarta o que a tela mandar; o fechamento dela encerra o envio.</summary>
    private static async Task Drain(WebSocket socket, CancellationTokenSource closing)
    {
        var buffer = new byte[1024];
        try
        {
            while (!closing.IsCancellationRequested)
            {
                var received = await socket.ReceiveAsync(buffer, closing.Token);
                if (received.MessageType == WebSocketMessageType.Close) break;
            }
        }
        catch (Exception error) when (error is OperationCanceledException or WebSocketException)
        {
        }
        await closing.CancelAsync();
        if (socket.State is WebSocketState.Open or WebSocketState.CloseReceived)
        {
            try
            {
                await socket.CloseOutputAsync(WebSocketCloseStatus.NormalClosure, null, CancellationToken.None);
            }
            catch (WebSocketException)
            {
            }
        }
    }
}
