"""SAP, as the SAP System API sees it: SAP's own tables (backend-systems/sap/README.md), the
credit check, billing, and the mapping to the platform's contract. Nobody else reads these tables.
"""

from datetime import datetime, timedelta, timezone

import kit
from kit import Fact

CREDIT_LIMIT = 250_000  # simulated credit check: above this the sales order is refused
SALES_ORDERS, INVOICES = 8000043216, 90004710  # number ranges: first 8000043217, 90004711
PAYMENTS = 1400000000
CREDIT_NOTES = 70004710  # credit memos (billing type G2): first 70004711
PAYMENT_TERMS_DAYS = 30


def sales_order(vbak: dict) -> dict:
    """VBAK (with its VBAP items) → the contract's salesOrder."""
    currency = vbak["WAERK"]
    order = {
        "_links": {"self": {"href": f"/sales-orders/{vbak['id']}"}},
        "erpOrderNumber": vbak["id"], "orderId": vbak["BSTKD"], "customerId": vbak["KUNNR"], "status": vbak["STATUS"],
        "refusalReason": vbak.get("ABGRU"), "netValue": {"value": vbak["NETWR"], "currency": currency},
        "items": [{"itemNumber": i["POSNR"], "productId": i["MATNR"], "quantity": i["KWMENG"],
                   "netPrice": {"value": i["NETPR"], "currency": currency}} for i in vbak["VBAP"]],
        "invoiceNumber": vbak.get("INVOICE"), "createdAt": vbak["ERDAT"],
    }
    if vbak.get("CANCELLED_AT"):
        order["cancellation"] = {"reason": vbak["CANCEL_REASON"], "cancelledAt": vbak["CANCELLED_AT"],
                                 **({"creditNoteNumber": vbak["CREDIT_MEMO"]} if vbak.get("CREDIT_MEMO") else {})}
    return {k: v for k, v in order.items() if v is not None}


def invoice(vbrk: dict) -> dict:
    """VBRK → the contract's invoice."""
    money = lambda value: {"value": value, "currency": vbrk["WAERK"]}  # noqa: E731
    out = {"_links": {"self": {"href": f"/invoices/{vbrk['id']}"}}, "invoiceNumber": vbrk["id"], "erpOrderNumber": vbrk["VGBEL"],
           "orderId": vbrk["BSTKD"], "customerId": vbrk["KUNRG"], "amount": money(vbrk["NETWR"]), "paidAmount": money(vbrk["PAID"]),
           "status": vbrk["STATUS"], "issuedAt": vbrk["FKDAT"], "dueDate": vbrk["ZFBDT"]}
    if vbrk.get("CREDIT_MEMO"):
        out["creditNote"] = {"creditNoteNumber": vbrk["CREDIT_MEMO"], "amount": money(vbrk["NETWR"]), "issuedAt": vbrk["CREDITED_AT"]}
    return out


def payment(doc: dict) -> dict:
    return {"_links": {"self": {"href": f"/payments/{doc['id']}"}}, "paymentId": doc["id"], "invoiceNumber": doc["REBZG"],
            "orderId": doc["BSTKD"], "amount": {"value": doc["WRBTR"], "currency": doc["WAERS"]},
            "invoicePaidInFull": doc["PAID_IN_FULL"], "receivedAt": doc["BUDAT"]}


def create_sales_order(store, request: dict) -> tuple[int, dict, list[Fact]]:
    """The order enters SAP: credit check, sales order, and the invoice when accepted."""
    existing = store.find("VBAK", BSTKD=request["orderId"])
    if existing:  # one sales order per order: a retry changes nothing
        return 200, sales_order(existing[0]), []

    total = request["totalAmount"]
    keys = {"order_id": request["orderId"], "customer_id": request["customerId"]}
    accepted = total["value"] <= CREDIT_LIMIT
    vbak = {
        "id": store.next_number("VBAK", SALES_ORDERS), "BSTKD": request["orderId"], "KUNNR": request["customerId"],
        "STATUS": "accepted" if accepted else "refused", "NETWR": total["value"], "WAERK": total["currency"],
        "VBAP": [{"POSNR": 10 * n, "MATNR": line["productId"], "KWMENG": line["quantity"], "NETPR": line["unitPrice"]["value"]}
                 for n, line in enumerate(request["orderLines"], start=1)],
        "ERDAT": kit.now(),
    }
    if not accepted:
        vbak["ABGRU"] = f"Credit limit exceeded: {total['value']:.2f} {total['currency']} is above {CREDIT_LIMIT}."
        store.create("VBAK", vbak)
        return 201, sales_order(vbak), [Fact("SalesOrderRefused", {"orderId": request["orderId"], "erpOrderNumber": vbak["id"],
                                                                     "reason": vbak["ABGRU"], "refusedAt": vbak["ERDAT"]}, **keys)]

    due = (datetime.now(timezone.utc) + timedelta(days=PAYMENT_TERMS_DAYS)).strftime("%Y-%m-%d")
    vbrk = {"id": store.next_number("VBRK", INVOICES), "VGBEL": vbak["id"], "BSTKD": request["orderId"], "KUNRG": request["customerId"],
            "NETWR": total["value"], "WAERK": total["currency"], "PAID": 0.0, "STATUS": "open", "FKDAT": vbak["ERDAT"], "ZFBDT": due}
    vbak["INVOICE"] = vbrk["id"]
    store.create("VBAK", vbak)
    store.create("VBRK", vbrk)
    return 201, sales_order(vbak), [
        Fact("SalesOrderAccepted", {"orderId": request["orderId"], "erpOrderNumber": vbak["id"], "netValue": total,
                                    "acceptedAt": vbak["ERDAT"]}, **keys),
        Fact("InvoiceIssued", {"orderId": request["orderId"], "invoiceNumber": vbrk["id"], "erpOrderNumber": vbak["id"],
                               "amount": total, "dueDate": due, "issuedAt": vbrk["FKDAT"]}, **keys),
    ]


class SalesOrderNotFound(Exception):
    pass


class SalesOrderRefused(Exception):
    pass


def cancel_sales_order(store, erp_order_number: str, reason: str) -> tuple[dict, list[Fact]]:
    """Cancel a sales order (soft delete) and reverse its invoice with a credit note (billing type G2)."""
    vbak = store.get("VBAK", erp_order_number)
    if vbak is None:
        raise SalesOrderNotFound
    if vbak["STATUS"] == "refused":
        raise SalesOrderRefused
    if vbak.get("CANCELLED_AT"):  # a repeated cancellation changes nothing
        return sales_order(vbak), []
    data = {"orderId": vbak["BSTKD"], "erpOrderNumber": vbak["id"], "reason": reason}
    if vbak.get("INVOICE"):
        vbrk = store.get("VBRK", vbak["INVOICE"])
        credit = {"id": store.next_number("G2", CREDIT_NOTES), "FKART": "G2", "VGBEL": vbak["id"], "BSTKD": vbak["BSTKD"],
                  "KUNRG": vbak["KUNNR"], "NETWR": -vbrk["NETWR"], "WAERK": vbrk["WAERK"], "REFERENCE": vbrk["id"], "FKDAT": kit.now()}
        store.create("VBRK", credit)  # SAP keeps credit memos with the invoices
        vbrk.update(CREDIT_MEMO=credit["id"], CREDITED_AT=credit["FKDAT"])
        store.replace("VBRK", vbrk)
        vbak["CREDIT_MEMO"] = credit["id"]
        data.update(invoiceNumber=vbrk["id"], creditNoteNumber=credit["id"], creditedAmount={"value": vbrk["NETWR"], "currency": vbrk["WAERK"]})
    vbak.update(CANCEL_REASON=reason, CANCELLED_AT=kit.now())
    store.replace("VBAK", vbak)
    data["cancelledAt"] = vbak["CANCELLED_AT"]
    return sales_order(vbak), [Fact("SalesOrderCancelled", data, order_id=vbak["BSTKD"], customer_id=vbak["KUNNR"])]


class InvoiceNotFound(Exception):
    pass


class InvoicePaid(Exception):
    pass


class CurrencyMismatch(Exception):
    pass


def record_payment(store, request: dict) -> tuple[dict, list[Fact]]:
    """Money received against an invoice; the invoice is paid once the payments cover it."""
    while True:
        vbrk = store.get("VBRK", request["invoiceNumber"])
        if vbrk is None or vbrk.get("FKART") == "G2":
            raise InvoiceNotFound
        if vbrk["STATUS"] == "paid" or vbrk.get("CREDIT_MEMO"):
            raise InvoicePaid
        if request["amount"]["currency"] != vbrk["WAERK"]:
            raise CurrencyMismatch
        vbrk["PAID"] = round(vbrk["PAID"] + request["amount"]["value"], 2)
        vbrk["STATUS"] = "paid" if vbrk["PAID"] >= vbrk["NETWR"] else "open"
        try:
            store.replace("VBRK", vbrk)
            break
        except kit.Changed:
            continue  # another payment came in meanwhile
    doc = {"id": store.next_number("Payment", PAYMENTS), "REBZG": vbrk["id"], "BSTKD": vbrk["BSTKD"],
           "WRBTR": request["amount"]["value"], "WAERS": vbrk["WAERK"], "PAID_IN_FULL": vbrk["STATUS"] == "paid", "BUDAT": kit.now()}
    store.create("Payment", doc)
    return payment(doc), [Fact("PaymentReceived", {"orderId": vbrk["BSTKD"], "invoiceNumber": vbrk["id"], "paymentId": doc["id"],
                                                    "amount": request["amount"], "invoicePaidInFull": doc["PAID_IN_FULL"],
                                                    "receivedAt": doc["BUDAT"]}, order_id=vbrk["BSTKD"], customer_id=vbrk["KUNRG"])]
