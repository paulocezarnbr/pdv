using Fiscal.Service;

// O serviço fiscal interno: só a retaguarda fala com ele, pela rede do Coolify,
// com o token de FISCAL_SERVICE_TOKEN. A porta vem de ASPNETCORE_HTTP_PORTS
// (8081 no Dockerfile, como o serviço em Python).
var builder = WebApplication.CreateBuilder(args);
builder.Services.AddFiscalService(FiscalOptions.FromEnvironment());
var app = builder.Build();
app.MapFiscalRoutes();
app.Run();

public partial class Program;
