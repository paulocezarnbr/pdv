using Pdv.Core.Printing;
using Pdv.Data.Hardware;

namespace Pdv.Data.Sales;

/// <summary>O cupom de uma venda fechada, montado do que está no <b>banco</b> — não da memória da tela.</summary>
/// <remarks>
/// Ler do banco é o que garante que o papel diz o que foi gravado: itens
/// cancelados ficam de fora, o desconto do painel entra, e reimprimir amanhã
/// dá o mesmo cupom.
/// </remarks>
public sealed class ReceiptComposer(PdvDatabase database, TerminalProfile terminal, PrinterSettings printer, TimeProvider? clock = null)
{
    private readonly TimeProvider _clock = clock ?? TimeProvider.System;

    public byte[] Compose(
        string orderId, string operatorName, string? customerName = null, long cashbackEarnedCents = 0, long? prepaidBalanceCents = null)
    {
        long localNumber, discount;
        string documentId;
        using (var order = Sql.Command(database.Connection, null,
                   "SELECT local_number, client_uuid, discount_cents FROM orders WHERE id = $id", ("$id", orderId)))
        using (var reader = order.ExecuteReader())
        {
            if (!reader.Read()) throw new OrderNotOpenException($"Pedido {orderId} não existe.");
            localNumber = reader.GetInt64(0);
            documentId = reader.GetString(1);
            discount = reader.GetInt64(2);
        }

        var items = new List<ReceiptItem>();
        using (var read = Sql.Command(database.Connection, null,
                   "SELECT product_name, total_cents, unit_price_cents, quantity, net_weight_grams, tare_grams FROM order_items " +
                   "WHERE order_id = $id AND canceled_at IS NULL ORDER BY created_at, rowid", ("$id", orderId)))
        using (var reader = read.ExecuteReader())
        {
            while (reader.Read())
            {
                items.Add(new ReceiptItem(reader.GetString(0), reader.GetInt64(1), reader.GetInt64(2),
                    reader.GetValue(3).ToString()!, reader.GetInt64(4), reader.GetInt64(5)));
            }
        }

        var payments = new List<ReceiptPayment>();
        using (var read = Sql.Command(database.Connection, null,
                   "SELECT method, amount_cents, change_cents FROM payments WHERE order_id = $id ORDER BY rowid", ("$id", orderId)))
        using (var reader = read.ExecuteReader())
        {
            while (reader.Read()) payments.Add(new ReceiptPayment(reader.GetString(0), reader.GetInt64(1), reader.GetInt64(2)));
        }

        var sale = new ReceiptSale(localNumber, documentId, items, discount, _clock.GetLocalNow().DateTime);
        var context = new ReceiptContext(
            terminal.StoreName, printer.StoreDocument, printer.StoreAddress, operatorName,
            $"PDV {terminal.DeviceId[..Math.Min(8, terminal.DeviceId.Length)]}",
            customerName, cashbackEarnedCents, prepaidBalanceCents);
        return ReceiptLayout.BuildSaleReceipt(sale, payments, context, printer.Layout);
    }
}
