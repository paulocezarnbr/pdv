using System.Globalization;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Routing;
using Pdv.Data;
using Pdv.Data.Auth;
using Pdv.Data.Edge;
using Pdv.Data.Sales;

namespace Pdv.Edge;

/// <summary>Os serviços do salão sobre a conexão do servidor, como o <c>create_app</c> do Python os monta.</summary>
public sealed class SalonServices
{
    public SalonServices(
        PdvDatabase database, TerminalProfile profile, AuditLedger ledger, EventHub hub,
        bool blockSaleOnNegativeStock = false, TimeProvider? clock = null)
    {
        Database = database;
        Profile = profile;
        Hub = hub;
        var terminal = profile.Identity;
        Tables = new TableService(database, terminal, clock);
        Orders = new TableOrderService(database, terminal, ledger, hub, clock, blockSaleOnNegativeStock);
        Kds = new KdsService(database, terminal, hub, clock);
        Auth = new EdgeAuth(database, terminal, clock);
        Staff = new StaffSessions(database, terminal.TenantId, clock);
        Managers = new ManagerSessions(new StaffAuthentication(database, terminal.TenantId, clock), clock);
        Report = new StaffReport(database, terminal.TenantId, clock);
    }

    public PdvDatabase Database { get; }
    public TerminalProfile Profile { get; }
    public EventHub Hub { get; }
    public TableService Tables { get; }
    public TableOrderService Orders { get; }
    public KdsService Kds { get; }
    public EdgeAuth Auth { get; }
    public StaffSessions Staff { get; }
    public ManagerSessions Managers { get; }
    public StaffReport Report { get; }
}

/// <summary>
/// As rotas do salão — o <c>edge/server.py</c>, resposta a resposta. Conferido
/// contra <c>contracts/salon-http.json</c>, gerado pelo FastAPI.
/// </summary>
/// <remarks>
/// <para>
/// <b>Nenhuma rota de negócio confia no IP de origem.</b> O servidor escuta na
/// LAN, que é a rede do Wi-Fi do cliente; o que vale é o token do aparelho,
/// emitido por pareamento presencial. As rotas que escrevem na comanda exigem
/// também a sessão de quem atende, e as de gerente, a concessão curta.
/// </para>
/// <para>
/// <b>A ordem de conferência é a do FastAPI</b>, e o app depende dela: JSON
/// ilegível é 422 antes de tudo; depois o aparelho (401), a sessão (403,
/// <c>X-Auth-Scope: staff</c>) e o gerente (403, <c>manager</c>); só então o
/// corpo. Um celular sem sessão que manda corpo incompleto precisa ouvir "entre
/// com o seu login", não "campo obrigatório".
/// </para>
/// <para>
/// <b>Uma requisição por vez no banco.</b> No Python as rotas são
/// <c>async</c> com acesso síncrono ao SQLite, e o laço de eventos único as
/// serializa. Aqui a trava faz o mesmo: uma conexão SQLite não é segura entre
/// threads, e o salão cheio é justamente quando dois celulares lançam juntos.
/// </para>
/// </remarks>
public static class SalonApi
{
    public const string Version = "1.0.0";

    private static readonly JsonSerializerOptions Json = new() { Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping };

    internal sealed class Call(HttpContext http, SalonServices services, JsonNode? json, bool bodyReadable)
    {
        private PairedDevice? _device;

        public HttpContext Http { get; } = http;
        public SalonServices S { get; } = services;

        public string Route(string name) => Http.GetRouteValue(name)?.ToString() ?? "";

        public string? Header(string name) => Http.Request.Headers.TryGetValue(name, out var v) ? v.ToString() : null;

        /// <summary>O aparelho pareado: o <c>current_device</c>.</summary>
        public PairedDevice Device()
        {
            if (_device is not null) return _device;
            var authorization = Header("Authorization");
            var token = authorization is not null && authorization.StartsWith("bearer ", StringComparison.OrdinalIgnoreCase)
                ? authorization[7..]
                : null;
            try
            {
                return _device = S.Auth.Authenticate(token);
            }
            catch (DeviceAuthException error)
            {
                throw new HttpError(401, error.Message);
            }
        }

        /// <summary>Quem está atendendo neste aparelho: o <c>current_staff</c>.</summary>
        public StaffSession Staff()
        {
            var device = Device();
            try
            {
                return S.Staff.Require(Header("X-Staff-Token"), device.Id);
            }
            catch (StaffAuthException error)
            {
                throw new HttpError(403, error.Message, "staff");
            }
        }

        /// <summary>A concessão de gerente, conferida contra ESTE aparelho: o <c>current_manager</c>.</summary>
        public Identity Manager()
        {
            var device = Device();
            try
            {
                return S.Managers.Require(Header("X-Manager-Token"), device.Id);
            }
            catch (AuthenticationException error)
            {
                throw new HttpError(403, error.Message, "manager");
            }
        }

        /// <summary>O corpo, depois das credenciais — nunca antes.</summary>
        public RequestBody Body() => bodyReadable ? new RequestBody(json) : RequestBody.Missing;
    }

    /// <summary>Monta as rotas numa aplicação ASP.NET Core.</summary>
    public static void Map(WebApplication app, SalonServices services)
    {
        var gate = new SemaphoreSlim(1, 1);

        void Route(string method, string pattern, Func<Call, JsonNode?> handler, bool body = false) =>
            app.MapMethods(pattern, [method], (HttpContext http) => Run(http, services, gate, handler, body));

        // -- abertas --------------------------------------------------------------------
        app.MapGet("/", () => Results.Text(Webapp.Index, "text/html; charset=utf-8", Encoding.UTF8));
        app.MapGet("/vendor/{filename}", async (string filename, HttpContext http) =>
        {
            if (Webapp.Vendor(filename) is not { } bytes)
            {
                await Error(http, new HttpError(404, "Arquivo inexistente."));
                return;
            }
            // Versionado pelo nome e trocado só na atualização do PDV: cache longo poupa o Wi-Fi da loja.
            http.Response.Headers.CacheControl = "public, max-age=604800";
            http.Response.ContentType = filename.EndsWith(".css", StringComparison.Ordinal)
                ? "text/css; charset=utf-8"
                : "application/javascript";
            await http.Response.Body.WriteAsync(bytes, http.RequestAborted);
        });
        Route("GET", "/manifest.webmanifest", call => new JsonObject
        {
            ["name"] = $"Salão — {call.S.Profile.StoreName}",
            ["short_name"] = "Salão",
            ["start_url"] = "/",
            ["display"] = "standalone",
            ["background_color"] = "#0e1116",
            ["theme_color"] = "#0e1116",
            ["orientation"] = "portrait",
        });
        // Aberta de propósito: é por ela que o app confirma que achou o PDV.
        Route("GET", "/health", call => new JsonObject
        {
            ["service"] = "pdv-edge",
            ["store_id"] = call.S.Profile.StoreId,
            ["store_name"] = call.S.Profile.StoreName,
            ["device_id"] = call.S.Profile.DeviceId,
            ["version"] = Version,
        });
        Route("POST", "/pair", Pair, body: true);
        Route("GET", "/menu", Menu);

        // -- comandas -------------------------------------------------------------------
        Route("GET", "/orders", call =>
        {
            call.Device();
            return new JsonArray([.. call.S.Orders.ListOpenOrders().Select(o => (JsonNode)AsOrder(o))]);
        });
        Route("POST", "/orders", OpenOrder, body: true);
        Route("GET", "/orders/{order_id}", call =>
        {
            call.Device();
            var id = call.Route("order_id");
            var order = Guard(() => call.S.Orders.GetOrder(id));
            var json = order.ToJson();
            json["items"] = call.S.Orders.ListItems(id);
            return json;
        });
        Route("POST", "/orders/{order_id}/bill", call =>
        {
            call.Device();
            call.Staff();
            return AsOrder(Guard(() => call.S.Orders.RequestBill(call.Route("order_id"))));
        });
        Route("DELETE", "/orders/{order_id}/bill", call =>
        {
            call.Device();
            call.Staff();
            return AsOrder(Guard(() => call.S.Orders.ClearBillRequest(call.Route("order_id"))));
        });
        Route("POST", "/orders/{order_id}/cancel", CancelOrder, body: true);
        Route("POST", "/orders/{order_id}/transfer", Transfer, body: true);
        Route("POST", "/orders/{order_id}/items", AddItem, body: true);

        // -- mesas ----------------------------------------------------------------------
        Route("GET", "/tables", call =>
        {
            call.Device();
            var all = Query(call, "include_inactive") is { } raw
                ? RequestBody.Flag(raw) ?? throw HttpError.Validation(["include_inactive: precisa ser verdadeiro ou falso."])
                : false;
            return new JsonObject { ["tables"] = Tables(call.S.Tables.List(all)) };
        });
        Route("POST", "/tables", CreateTable, body: true);
        Route("PATCH", "/tables/{table_id}", PatchTable, body: true);
        Route("POST", "/tables/seed", SeedTables, body: true);

        // -- garçom ---------------------------------------------------------------------
        Route("POST", "/staff/session", StaffLogin, body: true);
        Route("GET", "/staff/session", call => call.Staff().ToJson(call.S.Staff.Clock));
        Route("DELETE", "/staff/session", call =>
        {
            call.Device();
            return new JsonObject { ["ended"] = call.S.Staff.Logout(call.Header("X-Staff-Token")) };
        });
        // Só os números de quem está autenticado — nunca os do colega.
        Route("GET", "/staff/summary", call => call.S.Report.ForUser(call.Staff().UserId));

        // -- gerente --------------------------------------------------------------------
        Route("POST", "/manager/session", ManagerLogin, body: true);
        Route("DELETE", "/manager/session", call =>
        {
            call.Device();
            return new JsonObject { ["revoked"] = call.S.Managers.Revoke(call.Header("X-Manager-Token")) };
        });
        Route("GET", "/manager/session", call =>
        {
            var manager = call.Manager();
            return new JsonObject { ["user_id"] = manager.Id, ["name"] = manager.Name, ["role"] = manager.Role };
        });

        // -- cozinha --------------------------------------------------------------------
        Route("GET", "/kds/tickets", call =>
        {
            call.Device();
            return new JsonObject
            {
                ["tickets"] = new JsonArray([.. call.S.Kds.ListActive(Query(call, "station")).Select(t => (JsonNode)t.ToJson())]),
            };
        });
        Route("POST", "/kds/tickets/{ticket_id}/{action}", call =>
        {
            call.Device();
            var id = call.Route("ticket_id");
            var ticket = call.Route("action") switch
            {
                "bump" => Guard(() => call.S.Kds.Bump(id)),
                "recall" => Guard(() => call.S.Kds.Recall(id)),
                _ => throw new HttpError(404, "Ação desconhecida."),
            };
            return ticket.ToJson();
        });
        KdsStream.Map(app, services, gate);
    }

    // -- rotas com corpo -------------------------------------------------------------------

    private static JsonNode Pair(Call call)
    {
        var body = call.Body();
        var code = body.Text("code", 4, 16);
        var name = body.Text("device_name", 1, 64);
        var kind = body.Text("kind", 0, int.MaxValue, "waiter", "^(waiter|kds)$");
        body.Done();
        string token;
        try
        {
            token = call.S.Auth.Pair(code, name, kind);
        }
        catch (PairingException error)
        {
            throw new HttpError(403, error.Message);
        }
        var device = call.S.Auth.Authenticate(token);
        return new JsonObject { ["device_id"] = device.Id, ["token"] = token, ["store_name"] = call.S.Profile.StoreName };
    }

    private static JsonNode Menu(Call call)
    {
        call.Device();
        using var command = Sql.Command(call.S.Database.Connection, null,
            "SELECT id, sku, name, price_cents, pricing_mode FROM products " +
            "WHERE tenant_id = $tenant AND is_active = 1 AND deleted_at IS NULL ORDER BY name",
            ("$tenant", call.S.Profile.TenantId));
        using var reader = command.ExecuteReader();
        var products = new JsonArray();
        while (reader.Read())
        {
            var mode = reader.GetString(4);
            products.Add(new JsonObject
            {
                ["id"] = reader.GetString(0),
                ["sku"] = reader.GetString(1),
                ["name"] = reader.GetString(2),
                ["price_cents"] = reader.GetInt64(3),
                ["pricing_mode"] = mode,
                // O app esconde o que ele não pode vender, em vez do garçom descobrir no erro.
                ["sellable_by_waiter"] = mode != "weight",
            });
        }
        return new JsonObject { ["products"] = products };
    }

    private static JsonNode OpenOrder(Call call)
    {
        var device = call.Device();
        // Quem abre é a sessão que assina a requisição, nunca um campo do corpo.
        var session = call.Staff();
        var body = call.Body();
        var clientUuid = body.Text("client_uuid", 8, 64);
        var tableId = body.Text("table_id", 0, 64, "");
        var tableLabel = body.Text("table_label", 0, 32, "");
        body.Done();
        try
        {
            return AsOrder(call.S.Orders.OpenOrder(clientUuid, session.UserId, device.Id,
                tableId.Length > 0 ? tableId : null, tableLabel));
        }
        catch (TableOccupiedException error)
        {
            // 409 com a comanda existente: tocar numa mesa ocupada é querer lançar nela.
            throw new HttpError(409, new JsonObject { ["message"] = error.Message, ["order"] = error.Order.ToJson() });
        }
        catch (TableException error)
        {
            throw new HttpError(404, error.Message);
        }
        catch (Exception error) when (IsRefusal(error))
        {
            throw new HttpError(400, error.Message);
        }
    }

    private static JsonNode CancelOrder(Call call)
    {
        call.Device();
        call.Staff();
        var manager = call.Manager();
        var body = call.Body();
        var reason = body.Text("reason", 3, 200);
        body.Done();
        return AsOrder(Guard(() => call.S.Orders.CancelOrder(call.Route("order_id"), manager.Id, manager.Name, reason),
            refusal: 409));
    }

    private static JsonNode Transfer(Call call)
    {
        call.Device();
        call.Staff();
        var manager = call.Manager();
        var body = call.Body();
        var tableId = body.Text("table_id", 1, 64);
        body.Done();
        try
        {
            return AsOrder(Guard(() => call.S.Orders.Transfer(call.Route("order_id"), tableId, manager.Id, manager.Name),
                refusal: 409));
        }
        catch (TableOccupiedException error)
        {
            throw new HttpError(409, new JsonObject { ["message"] = error.Message, ["order"] = error.Order.ToJson() });
        }
    }

    private static JsonNode AddItem(Call call)
    {
        call.Device();
        var session = call.Staff();
        var body = call.Body();
        var clientUuid = body.Text("client_uuid", 8, 64);
        var productId = body.Text("product_id", 1, 64);
        var quantityText = body.Text("quantity", 0, 12, "1");
        var notes = body.Text("notes", 0, 200, "");
        var station = body.Text("station", 0, 32, "cozinha");
        body.Done();
        if (!decimal.TryParse(quantityText.Trim(), NumberStyles.Float, CultureInfo.InvariantCulture, out var quantity))
        {
            throw new HttpError(422, $"Quantidade inválida: {TableService.PyRepr(quantityText)}");
        }
        return AsOrder(Guard(() => call.S.Orders.AddItem(call.Route("order_id"), clientUuid, productId, quantity, notes,
            station, session.UserId), refusal: 409));
    }

    private static JsonNode CreateTable(Call call)
    {
        call.Device();
        call.Manager();
        var body = call.Body();
        var label = body.Text("label", 1, 32);
        var area = body.Text("area", 1, 32, "Salão");
        var seats = body.Number("seats", 1, 99, 4);
        var sortOrder = body.OptionalNumber("sort_order", 0, 9999);
        body.Done();
        return Conflict(() => call.S.Tables.Create(label, area, seats, sortOrder)).ToJson();
    }

    /// <summary>
    /// Reativar vem antes da edição e desativar vem depois: <c>Update</c> recusa
    /// mesa inativa, e assim renomear e tirar do mapa na mesma tela funciona nos
    /// dois sentidos.
    /// </summary>
    private static JsonNode PatchTable(Call call)
    {
        call.Device();
        call.Manager();
        var body = call.Body();
        var label = body.OptionalText("label", 1, 32);
        var area = body.OptionalText("area", 1, 32);
        var seats = body.OptionalNumber("seats", 1, 99);
        var sortOrder = body.OptionalNumber("sort_order", 0, 9999);
        var active = body.OptionalFlag("is_active");
        body.Done();
        var id = call.Route("table_id");
        var tables = call.S.Tables;
        return Conflict(() =>
        {
            StoreTable Edit() => tables.Update(id, label, area, seats, sortOrder);
            if (active == true)
            {
                tables.SetActive(id, true);
                return Edit();
            }
            if (active == false)
            {
                Edit();
                return tables.SetActive(id, false);
            }
            return Edit();
        }).ToJson();
    }

    private static JsonNode SeedTables(Call call)
    {
        call.Device();
        call.Manager();
        var body = call.Body();
        var count = body.Number("count", 1, 100, 12);
        var area = body.Text("area", 1, 32, "Salão");
        body.Done();
        var created = Conflict(() => call.S.Tables.SeedDefaultTables(count, area));
        return new JsonObject { ["created"] = created, ["tables"] = Tables(call.S.Tables.List()) };
    }

    /// <summary>
    /// O garçom entra no aparelho já pareado. Exige o token do aparelho: um PIN
    /// vazado não vale em celular de fora da loja.
    /// </summary>
    private static JsonNode StaffLogin(Call call)
    {
        var device = call.Device();
        var body = call.Body();
        var login = body.Text("login", 1, 64);
        var pin = body.Text("pin", 1, 64);
        body.Done();
        try
        {
            return call.S.Staff.Login(login, pin, device.Id).ToJson(call.S.Staff.Clock, withToken: true);
        }
        catch (AuthenticationException error)
        {
            throw new HttpError(403, error.Message);
        }
    }

    private static JsonNode ManagerLogin(Call call)
    {
        var device = call.Device();
        var body = call.Body();
        var login = body.Text("login", 1, 64);
        var pin = body.Text("pin", 1, 64);
        body.Done();
        try
        {
            return call.S.Managers.Authorize(login, pin, device.Id).ToJson(call.S.Managers.Clock);
        }
        catch (AuthenticationException error)
        {
            throw new HttpError(403, error.Message);
        }
    }

    // -- apoio -----------------------------------------------------------------------------

    /// <summary>O <c>OrderResponse</c>: só o que o app do garçom lê.</summary>
    private static JsonObject AsOrder(TableOrder order) => new()
    {
        ["order_id"] = order.Id,
        ["client_uuid"] = order.ClientUuid,
        ["local_number"] = order.LocalNumber,
        ["table_label"] = order.TableLabel,
        ["status"] = order.Status,
        ["total_cents"] = order.TotalCents,
        ["item_count"] = order.ItemCount,
    };

    private static JsonArray Tables(IEnumerable<StoreTable> tables) => new([.. tables.Select(t => (JsonNode)t.ToJson())]);

    private static string? Query(Call call, string name) =>
        call.Http.Request.Query.TryGetValue(name, out var value) ? value.ToString() : null;

    /// <summary>As recusas de negócio que no Python herdam de <c>PdvError</c>.</summary>
    private static bool IsRefusal(Exception error) => error is TableException or OrderNotFoundException
        or OrderClosedException or ProductNotSellableException or TableOccupiedException or TicketNotFoundException
        or InvalidTransitionException;

    /// <summary>Não encontrado é 404; o resto da recusa é <paramref name="refusal"/> (o 409 de estado).</summary>
    private static T Guard<T>(Func<T> action, int refusal = 409)
    {
        try
        {
            return action();
        }
        catch (Exception error) when (error is OrderNotFoundException or TicketNotFoundException)
        {
            throw new HttpError(404, error.Message);
        }
        catch (Exception error) when (error is not TableOccupiedException && IsRefusal(error))
        {
            throw new HttpError(refusal, error.Message);
        }
    }

    private static T Conflict<T>(Func<T> action)
    {
        try
        {
            return action();
        }
        catch (TableException error)
        {
            throw new HttpError(409, error.Message);
        }
    }

    private static async Task Run(HttpContext http, SalonServices services, SemaphoreSlim gate, Func<Call, JsonNode?> handler, bool body)
    {
        JsonNode? json = null;
        if (body)
        {
            // JSON ilegível é recusado antes de qualquer credencial, como no FastAPI.
            using var reader = new StreamReader(http.Request.Body, Encoding.UTF8);
            var text = await reader.ReadToEndAsync(http.RequestAborted);
            if (text.Length > 0)
            {
                try
                {
                    json = JsonNode.Parse(text);
                }
                catch (JsonException)
                {
                    await Error(http, HttpError.Validation(["O corpo não é um JSON válido."]));
                    return;
                }
            }
        }

        JsonNode? result;
        await gate.WaitAsync(http.RequestAborted);
        try
        {
            result = handler(new Call(http, services, json, bodyReadable: body && json is not null));
        }
        catch (HttpError error)
        {
            await Error(http, error);
            return;
        }
        finally
        {
            gate.Release();
        }
        await Write(http, 200, result);
    }

    internal static Task Error(HttpContext http, HttpError error)
    {
        if (error.Scope is not null) http.Response.Headers["X-Auth-Scope"] = error.Scope;
        return Write(http, error.Status, new JsonObject { ["detail"] = error.Detail?.DeepClone() });
    }

    internal static async Task Write(HttpContext http, int status, JsonNode? body)
    {
        http.Response.StatusCode = status;
        http.Response.ContentType = "application/json";
        await http.Response.WriteAsync(body?.ToJsonString(Json) ?? "null", http.RequestAborted);
    }
}
