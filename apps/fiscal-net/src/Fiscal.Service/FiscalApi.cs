using System.Text.Json;
using Fiscal.Service.Nfce;

namespace Fiscal.Service;

/// <summary>
/// As rotas do serviço — o mesmo contrato do FastAPI que existia antes:
/// <c>GET /health</c>, <c>POST /v1/fiscal/authorize</c> e <c>POST /v1/fiscal/status</c>,
/// com <c>{"detail": ...}</c> nos erros.
/// </summary>
public static class FiscalApi
{
    public static IServiceCollection AddFiscalService(this IServiceCollection services, FiscalOptions options)
    {
        services.AddSingleton(options);
        services.AddSingleton(TimeProvider.System);
        services.AddSingleton(provider => new ResultStore(provider.GetRequiredService<FiscalOptions>().StateDatabase));
        services.AddSingleton(provider => new SecretResolver(provider.GetRequiredService<FiscalOptions>().SecretsDirectory));
        services.AddSingleton<ISefazGateway>(provider =>
        {
            var configured = provider.GetRequiredService<FiscalOptions>();
            return new ZeusSefazGateway(configured.SchemasDirectory, configured.SefazTimeout);
        });
        services.AddSingleton<IFiscalEngine>(provider =>
        {
            var configured = provider.GetRequiredService<FiscalOptions>();
            var logger = provider.GetRequiredService<ILoggerFactory>().CreateLogger("fiscal");
            return configured.Engine == "gate"
                ? new HomologationGateEngine()
                : new NfceEngine(
                    configured, provider.GetRequiredService<SecretResolver>(), provider.GetRequiredService<ISefazGateway>(),
                    provider.GetRequiredService<TimeProvider>(), message => logger.LogWarning("{Message}", message));
        });
        services.AddSingleton(provider =>
        {
            var logger = provider.GetRequiredService<ILoggerFactory>().CreateLogger("fiscal");
            return new FiscalWorkflow(
                provider.GetRequiredService<ResultStore>(), provider.GetRequiredService<IFiscalEngine>(),
                message => logger.LogWarning("{Message}", message));
        });
        return services;
    }

    public static WebApplication MapFiscalRoutes(this WebApplication app)
    {
        app.MapGet("/health", (IFiscalEngine engine) => Results.Json(new { ok = true, engine = engine.Name }));

        app.MapPost("/v1/fiscal/authorize", async (HttpRequest request, FiscalOptions options, FiscalWorkflow workflow, CancellationToken cancellation) =>
        {
            if (!BearerAuthentication.Authenticate(request.Headers.Authorization.ToString(), options.Token)) return Unauthorized();
            var (intent, error) = await Read<FiscalIntent>(request, cancellation);
            if (error is not null) return error;
            var problems = IntentValidator.Validate(intent);
            if (problems.Count > 0) return Results.Json(new { detail = string.Join("; ", problems) }, statusCode: 422);
            return Results.Json(await workflow.AuthorizeAsync(intent!, cancellation), Json.Options);
        });

        app.MapPost("/v1/fiscal/status", async (HttpRequest request, FiscalOptions options, FiscalWorkflow workflow, CancellationToken cancellation) =>
        {
            if (!BearerAuthentication.Authenticate(request.Headers.Authorization.ToString(), options.Token)) return Unauthorized();
            var (query, error) = await Read<StatusRequest>(request, cancellation);
            if (error is not null) return error;
            if (string.IsNullOrEmpty(query?.RequestUuid)) return Results.Json(new { detail = "request_uuid: obrigatório" }, statusCode: 422);
            return Results.Json(await workflow.StatusAsync(query.RequestUuid, cancellation), Json.Options);
        });
        return app;
    }

    private static IResult Unauthorized() =>
        Results.Json(new { detail = "Serviço interno não autenticado." }, statusCode: 401);

    private static async Task<(T? Value, IResult? Error)> Read<T>(HttpRequest request, CancellationToken cancellation)
    {
        try
        {
            return (await JsonSerializer.DeserializeAsync<T>(request.Body, Json.Options, cancellation), null);
        }
        catch (JsonException)
        {
            return (default, Results.Json(new { detail = "corpo: JSON inválido para este contrato" }, statusCode: 422));
        }
    }
}
