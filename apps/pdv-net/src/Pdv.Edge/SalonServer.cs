using System.Net;
using System.Security.Cryptography.X509Certificates;
using System.Text.Json.Nodes;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Hosting.Server;
using Microsoft.AspNetCore.Hosting.Server.Features;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;

namespace Pdv.Edge;

/// <summary>
/// O servidor do salão dentro do PDV — o <c>edge/worker.py</c>: Kestrel numa
/// porta da LAN, com o app do garçom, a API e o WebSocket da cozinha.
/// </summary>
/// <remarks>
/// <para>
/// <b>Escuta em todas as interfaces e autentica sempre.</b> Precisa aceitar os
/// celulares da LAN, que é a rede do Wi-Fi do cliente; quem autoriza é o token
/// do aparelho, não a origem do pacote.
/// </para>
/// <para>
/// <b>Porta ocupada não impede a venda.</b> Outro PDV na mesma máquina, ou o
/// app aberto duas vezes: <see cref="StartAsync"/> devolve falso, o salão fica
/// sem servidor e o balcão segue vendendo.
/// </para>
/// </remarks>
public sealed class SalonServer(SalonServices services, Action<string>? log = null) : IAsyncDisposable
{
    /// <summary>Liberada no firewall pelo instalador, só no perfil privado.</summary>
    public const int DefaultPort = 8420;

    private WebApplication? _app;

    /// <summary>A porta em uso — a de verdade, quando se pediu a 0 (testes).</summary>
    public int Port { get; private set; }

    public bool IsRunning => _app is not null;

    /// <summary><c>https</c> ou <c>http</c>: o que o painel do caixa dita para o celular.</summary>
    public string Scheme { get; private set; } = "http";

    /// <summary>Sobe o servidor. Devolve se ficou de pé.</summary>
    /// <param name="address">Onde escutar; por padrão, todas as interfaces.</param>
    /// <param name="certificate">Sem certificado, o salão sobe em HTTP (diagnóstico): token e PIN em claro na rede.</param>
    public async Task<bool> StartAsync(IPAddress? address = null, int port = DefaultPort, X509Certificate2? certificate = null)
    {
        if (_app is not null) return true;
        try
        {
            var builder = WebApplication.CreateSlimBuilder();
            builder.Logging.ClearProviders();
            builder.WebHost.ConfigureKestrel(kestrel =>
            {
                // Documentação e cabeçalho de servidor desligados: superfície a mais no caixa da loja.
                kestrel.AddServerHeader = false;
                kestrel.Listen(address ?? IPAddress.Any, port, listen =>
                {
                    if (certificate is not null) listen.UseHttps(certificate);
                });
            });
            var app = builder.Build();
            app.UseWebSockets(new WebSocketOptions { KeepAliveInterval = KdsStream.PingInterval });
            // As respostas sem corpo do roteamento (rota inexistente, método errado)
            // saem no formato do FastAPI, que o app do garçom lê.
            app.UseStatusCodePages(async context =>
            {
                var status = context.HttpContext.Response.StatusCode;
                var detail = status switch
                {
                    404 => "Not Found",
                    405 => "Method Not Allowed",
                    _ => null,
                };
                if (detail is null) return;
                await SalonApi.Write(context.HttpContext, status, new JsonObject { ["detail"] = detail });
            });
            app.Use(async (context, next) =>
            {
                try
                {
                    await next(context);
                }
                catch (Exception error) when (error is not OperationCanceledException && !context.Response.HasStarted)
                {
                    log?.Invoke($"Salão: erro na rota {context.Request.Path}: {error}");
                    context.Response.Clear();
                    context.Response.StatusCode = 500;
                    context.Response.ContentType = "text/plain; charset=utf-8";
                    await context.Response.WriteAsync("Internal Server Error");
                }
            });
            SalonApi.Map(app, services);
            await app.StartAsync();

            var bound = app.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()?.Addresses
                .Select(a => new Uri(a.Replace("0.0.0.0", "127.0.0.1").Replace("[::]", "127.0.0.1")).Port)
                .FirstOrDefault() ?? port;
            Port = bound;
            Scheme = certificate is null ? "http" : "https";
            _app = app;
            log?.Invoke($"Salão ouvindo em {Scheme} na porta {Port}.");
            return true;
        }
        catch (Exception error) when (error is IOException or InvalidOperationException
                                          or System.Net.Sockets.SocketException)
        {
            log?.Invoke($"Salão: servidor local não subiu: {error.Message}");
            return false;
        }
    }

    public async Task StopAsync()
    {
        if (_app is null) return;
        var app = _app;
        _app = null;
        await app.StopAsync();
        await app.DisposeAsync();
    }

    public async ValueTask DisposeAsync() => await StopAsync();
}
