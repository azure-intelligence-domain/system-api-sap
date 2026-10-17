import json

import azure.functions as func
import pytest

import function_app as api
from contracts_test import assert_matches_asyncapi
from fakes import FakeTopic, MemoryStore

ORDER = {"orderId": "4711", "customerId": "ACME", "totalAmount": {"value": 84000.0, "currency": "EUR"},
         "orderLines": [{"lineNumber": 1, "productId": "SKU-1042", "quantity": 12, "unitPrice": {"value": 7000.0, "currency": "EUR"}}]}


def request(method, url, body=None, route=None, headers=None):
    return func.HttpRequest(method, url, headers={"Correlation-Id": "ord-4711", **(headers or {})}, route_params=route or {},
                            body=json.dumps(body).encode() if body is not None else b"")


@pytest.fixture
def sap():
    return MemoryStore(), FakeTopic()


def create(sap, order=ORDER):
    response = api.create_sales_order(request("POST", "/sales-orders", order), *sap)
    return response, json.loads(response.get_body())


def test_an_order_within_credit_is_accepted_and_billed(sap):
    response, order = create(sap)
    assert response.status_code == 201 and order["erpOrderNumber"] == "8000043217" and order["status"] == "accepted"
    assert order["invoiceNumber"] == "90004711" and order["items"][0]["itemNumber"] == 10
    assert sap[1].types == ["com.acme.finance.salesOrderAccepted", "com.acme.finance.invoiceIssued"]
    for event in sap[1].events:
        assert_matches_asyncapi(event)
        assert event["orderid"] == "4711" and event["customerid"] == "ACME" and event["correlationid"] == "ord-4711"
    vbak = sap[0].get("VBAK", "8000043217")
    assert vbak["BSTKD"] == "4711" and vbak["KUNNR"] == "ACME"  # SAP's own naming


def test_above_the_credit_limit_is_refused(sap):
    response, order = create(sap, {**ORDER, "totalAmount": {"value": 300000.0, "currency": "EUR"}})
    assert response.status_code == 201 and order["status"] == "refused" and "Credit limit" in order["refusalReason"]
    assert sap[1].types == ["com.acme.finance.salesOrderRefused"]
    assert_matches_asyncapi(sap[1].events[0])


def test_a_retry_creates_nothing(sap):
    create(sap)
    response, order = create(sap)
    assert response.status_code == 200 and order["erpOrderNumber"] == "8000043217" and len(sap[1].events) == 2


def test_payments_until_paid_in_full(sap):
    create(sap)
    half = {"invoiceNumber": "90004711", "amount": {"value": 42000.0, "currency": "EUR"}}
    first = json.loads(api.record_payment(request("POST", "/payments", half), *sap).get_body())
    assert first["invoicePaidInFull"] is False
    second = api.record_payment(request("POST", "/payments", half), *sap)
    assert second.status_code == 201 and json.loads(second.get_body())["invoicePaidInFull"] is True
    paid = [e for e in sap[1].events if e["type"] == "com.acme.finance.paymentReceived"]
    assert len(paid) == 2 and paid[1]["data"]["invoicePaidInFull"] and paid[1]["orderid"] == "4711"
    assert_matches_asyncapi(paid[1])
    again = api.record_payment(request("POST", "/payments", half), *sap)
    assert again.status_code == 409
    invoice = json.loads(api.retrieve_invoice(request("GET", "/invoices/90004711", route={"invoiceNumber": "90004711"}), sap[0]).get_body())
    assert invoice["status"] == "paid" and invoice["paidAmount"]["value"] == 84000.0


def test_payment_problems(sap):
    unknown = api.record_payment(request("POST", "/payments", {"invoiceNumber": "1", "amount": {"value": 1.0, "currency": "EUR"}}), *sap)
    assert unknown.status_code == 404
    create(sap)
    dollars = api.record_payment(request("POST", "/payments", {"invoiceNumber": "90004711", "amount": {"value": 1.0, "currency": "USD"}}), *sap)
    assert dollars.status_code == 400


def test_the_contract_and_the_journey_are_checked(sap):
    assert api.create_sales_order(request("POST", "/sales-orders", {"orderId": "4711"}), *sap).status_code == 400
    no_journey = request("POST", "/sales-orders", ORDER, headers={"Correlation-Id": ""})
    assert api.create_sales_order(no_journey, *sap).status_code == 400
    assert not sap[1].events


def test_the_invoices_of_an_order(sap):
    create(sap)
    response = api.retrieve_list_of_invoices(func.HttpRequest("GET", "/invoices", headers={"Correlation-Id": "ord-4711"},
                                                              params={"orderId": "4711"}, body=b""), sap[0])
    page = json.loads(response.get_body())
    assert response.status_code == 200 and page["totalCount"] == 1 and page["_embedded"]["invoices"][0]["invoiceNumber"] == "90004711"


def cancel(sap, number="8000043217", reason="Customer changed their mind."):
    req = func.HttpRequest("DELETE", f"/sales-orders/{number}", headers={"Correlation-Id": "ord-4711"},
                           route_params={"erpOrderNumber": number}, params={"reason": reason}, body=b"")
    response = api.cancel_sales_order(req, *sap)
    return response, json.loads(response.get_body())


def test_cancelling_reverses_the_invoice_with_a_credit_note(sap):
    create(sap)
    response, order = cancel(sap)
    assert response.status_code == 200 and order["cancellation"]["creditNoteNumber"] == "70004711" and order["status"] == "accepted"
    event = sap[1].events[-1]
    assert event["type"] == "com.acme.finance.salesOrderCancelled" and event["data"]["creditedAmount"]["value"] == 84000.0
    assert_matches_asyncapi(event)
    invoice = sap[0].get("VBRK", "90004711")
    assert invoice["CREDIT_MEMO"] == "70004711" and sap[0].get("VBRK", "70004711")["FKART"] == "G2"
    pay = api.record_payment(request("POST", "/payments", {"invoiceNumber": "90004711", "amount": {"value": 1.0, "currency": "EUR"}}), *sap)
    assert pay.status_code == 409
    again, _ = cancel(sap)
    assert again.status_code == 200 and len(sap[1].events) == 3  # nothing new
    page = json.loads(api.retrieve_list_of_invoices(func.HttpRequest("GET", "/invoices", headers={"Correlation-Id": "ord-4711"},
                                                                     params={"orderId": "4711"}, body=b""), sap[0]).get_body())
    assert [i["creditNote"]["creditNoteNumber"] for i in page["_embedded"]["invoices"]] == ["70004711"]  # the memo is not an invoice


def test_a_refused_or_unknown_sales_order_cannot_be_cancelled(sap):
    create(sap, {**ORDER, "totalAmount": {"value": 300000.0, "currency": "EUR"}})
    assert cancel(sap)[0].status_code == 409
    assert cancel(sap, "1")[0].status_code == 404
