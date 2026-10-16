"""[Finance] SAP System API (openapi.yaml): sales orders, invoices and payments in SAP, in the
platform's contract. Only the people gateway reaches it (Easy Auth), and only building blocks on
the platform's allow-list reach the gateway's system-apis product (ADR 0014). Every change is a
finance fact on evgt-intplat-finance (asyncapi.yaml).
"""

import azure.functions as func

import kit
import sap

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)  # Easy Auth guards the door


@kit.refusals
def create_sales_order(req: func.HttpRequest, store, topic) -> func.HttpResponse:
    journey = kit.journey(req)
    status, order, facts = sap.create_sales_order(store, kit.body_of(req, "salesOrderRequest"))
    topic.publish(facts, journey)
    return kit.hal(status, order, {"Location": order["_links"]["self"]["href"]})


@kit.refusals
def retrieve_sales_order(req: func.HttpRequest, store) -> func.HttpResponse:
    kit.journey(req)
    vbak = store.get("VBAK", req.route_params["erpOrderNumber"])
    if vbak is None:
        raise kit.refused(404, "Sales Order Not Found", "SAP has no sales order with this number.", "not-found", req)
    return kit.hal(200, sap.sales_order(vbak))


@kit.refusals
def retrieve_invoice(req: func.HttpRequest, store) -> func.HttpResponse:
    kit.journey(req)
    vbrk = store.get("VBRK", req.route_params["invoiceNumber"])
    if vbrk is None:
        raise kit.refused(404, "Invoice Not Found", "SAP has no invoice with this number.", "not-found", req)
    return kit.hal(200, sap.invoice(vbrk))


@kit.refusals
def retrieve_list_of_invoices(req: func.HttpRequest, store) -> func.HttpResponse:
    kit.journey(req)
    order_id = req.params.get("orderId", "")
    if not order_id.isdigit():
        raise kit.invalid("Query parameter orderId is required, e.g. 4711.", req)
    invoices = [sap.invoice(vbrk) for vbrk in store.find("VBRK", BSTKD=order_id)]
    return kit.hal(200, {"_links": {"self": {"href": f"/invoices?orderId={order_id}"}}, "totalCount": len(invoices),
                         "_embedded": {"invoices": invoices}})


@kit.refusals
def record_payment(req: func.HttpRequest, store, topic) -> func.HttpResponse:
    journey = kit.journey(req)
    try:
        paid, facts = sap.record_payment(store, kit.body_of(req, "paymentRequest"))
    except sap.InvoiceNotFound:
        raise kit.refused(404, "Invoice Not Found", "SAP has no invoice with this number.", "not-found", req) from None
    except sap.InvoicePaid:
        raise kit.refused(409, "Invoice Already Paid", "The invoice is paid in full already.", "invoice-already-paid", req) from None
    except sap.CurrencyMismatch:
        raise kit.invalid("amount: the payment's currency differs from the invoice's.", req) from None
    topic.publish(facts, journey)
    return kit.hal(201, paid, {"Location": paid["_links"]["self"]["href"]})


# --- Routes (host.json: no route prefix, so the paths are the contract's) -----------------------


@app.route(route="sales-orders", methods=["POST"])
def createSalesOrder(req: func.HttpRequest) -> func.HttpResponse:
    return create_sales_order(req, kit.system_store(), kit.domain_topic())


@app.route(route="sales-orders/{erpOrderNumber}", methods=["GET"])
def retrieveSalesOrder(req: func.HttpRequest) -> func.HttpResponse:
    return retrieve_sales_order(req, kit.system_store())


@app.route(route="invoices", methods=["GET"])
def retrieveListOfInvoices(req: func.HttpRequest) -> func.HttpResponse:
    return retrieve_list_of_invoices(req, kit.system_store())


@app.route(route="invoices/{invoiceNumber}", methods=["GET"])
def retrieveInvoice(req: func.HttpRequest) -> func.HttpResponse:
    return retrieve_invoice(req, kit.system_store())


@app.route(route="payments", methods=["POST"])
def recordPayment(req: func.HttpRequest) -> func.HttpResponse:
    return record_payment(req, kit.system_store(), kit.domain_topic())
