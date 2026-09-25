# Serviço fiscal interno (NFC-e 4.00, DFe.NET) — o substituto do FastAPI.
# Mesma porta (8081), mesmos caminhos (/data, /run/secrets/fiscal) e mesmo
# usuário (uid 10001): o volume de estado do serviço anterior continua valendo.
FROM mcr.microsoft.com/dotnet/sdk:10.0 AS build
WORKDIR /src
COPY Directory.Build.props ./
COPY src/Fiscal.Service/Fiscal.Service.csproj src/Fiscal.Service/
RUN dotnet restore src/Fiscal.Service/Fiscal.Service.csproj
COPY src ./src
RUN dotnet publish src/Fiscal.Service/Fiscal.Service.csproj -c Release -o /app --no-restore

FROM mcr.microsoft.com/dotnet/aspnet:10.0
WORKDIR /app
COPY --from=build /app .
RUN groupadd --system --gid 10001 fiscal \
    && useradd --system --uid 10001 --gid fiscal --no-create-home fiscal \
    && mkdir -p /data /run/secrets/fiscal \
    && chown -R fiscal:fiscal /data /run/secrets/fiscal
ENV ASPNETCORE_HTTP_PORTS=8081 \
    FISCAL_STATE_DB=/data/fiscal-state.sqlite3 \
    FISCAL_SECRETS_DIR=/run/secrets/fiscal \
    DOTNET_NOLOGO=1
USER fiscal
EXPOSE 8081
ENTRYPOINT ["dotnet", "fiscal-service.dll"]
