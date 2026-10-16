"""What every System API needs from the platform, the same in every System API repository:
responses (HAL, Problem Details), the journey (Correlation-Id, traceparent), the contract as the
validator, its system's Cosmos DB, and publishing facts in the platform's envelope. Runtime
identity only: no keys anywhere (ADR 0007, 0014). The system's own logic lives next to this file.
"""

import json
import logging
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import cache, wraps
from pathlib import Path

import azure.functions as func
import yaml
from azure.core import MatchConditions
from azure.cosmos import CosmosClient, exceptions
from azure.identity import DefaultAzureCredential
from integration_envelope import BusinessKeys, new_event
from jsonschema import Draft7Validator

SPEC = yaml.safe_load(Path(__file__).with_name("openapi.yaml").read_text())
VERSION = SPEC["info"]["version"]
REPOSITORY = SPEC["info"]["contact"]["url"]
SERVICE = REPOSITORY.rsplit("/", 1)[-1]
DOMAIN = re.match(r"\[(\w+)\]", SPEC["info"]["title"]).group(1).lower()
PROBLEMS = REPOSITORY + "/blob/main/docs/problems/"


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@cache
def credential() -> DefaultAzureCredential:
    return DefaultAzureCredential(managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID"))


# --- The contract is the validator --------------------------------------------------------------


def _validator(schema: dict) -> Draft7Validator:
    return Draft7Validator({**schema, "components": SPEC["components"]}, format_checker=Draft7Validator.FORMAT_CHECKER)


SCHEMAS = {name: _validator(schema) for name, schema in SPEC["components"]["schemas"].items()}
CORRELATION = _validator(SPEC["components"]["parameters"]["correlationId"]["schema"])


def violations(validator: Draft7Validator, document) -> list[str]:
    return [f"{'/'.join(map(str, e.absolute_path)) or 'body'}: {e.message}"
            for e in sorted(validator.iter_errors(document), key=lambda e: list(map(str, e.absolute_path)))]


# --- Responses ----------------------------------------------------------------------------------


def hal(status: int, body: dict, headers: dict | None = None) -> func.HttpResponse:
    return func.HttpResponse(json.dumps(body), status_code=status, mimetype="application/hal+json", headers=headers)


def problem(status: int, title: str, detail: str, slug: str, req: func.HttpRequest) -> func.HttpResponse:
    body = {"type": PROBLEMS + slug + ".md", "title": title, "status": status, "detail": detail, "instance": req.url}
    return func.HttpResponse(json.dumps(body), status_code=status, mimetype="application/problem+json")


class Refused(Exception):
    """A request the contract or the system refuses; carries its Problem Details response."""

    def __init__(self, response: func.HttpResponse):
        self.response = response


def refused(status: int, title: str, detail: str, slug: str, req: func.HttpRequest) -> Refused:
    return Refused(problem(status, title, detail, slug, req))


def invalid(detail: str, req: func.HttpRequest) -> Refused:
    return refused(400, "Invalid Request", detail, "invalid-request", req)


def refusals(handler):
    @wraps(handler)
    def handle(*args, **kwargs):
        try:
            return handler(*args, **kwargs)
        except Refused as r:
            return r.response
    return handle


# --- Requests -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Journey:
    correlation_id: str
    traceparent: str | None = None


def journey(req: func.HttpRequest) -> Journey:
    """The Correlation-Id (required) and the W3C trace context to pass on: one trace per journey."""
    correlation_id = req.headers.get("Correlation-Id", "")
    if violations(CORRELATION, correlation_id):
        raise invalid("The Correlation-Id header is missing or invalid, for example ord-4711.", req)
    logging.info("Correlation-Id %s: %s %s", correlation_id, req.method, req.url)
    return Journey(correlation_id, req.headers.get("traceparent"))


def body_of(req: func.HttpRequest, schema: str) -> dict:
    try:
        body = req.get_json()
    except ValueError:
        raise invalid("The body is not JSON.", req) from None
    errors = violations(SCHEMAS[schema], body)
    if errors:
        raise invalid("; ".join(errors), req)
    return body


# --- The system's own database ------------------------------------------------------------------


class Changed(Exception):
    """The document changed since it was read (ETag)."""


class Cosmos:
    """The system's Cosmos DB, as its System API's runtime identity. Partition key /id everywhere."""

    def __init__(self, endpoint: str, database: str):
        self.db = CosmosClient(endpoint, credential()).get_database_client(database)

    def get(self, container: str, id: str) -> dict | None:
        try:
            return self.db.get_container_client(container).read_item(id, partition_key=id)
        except exceptions.CosmosResourceNotFoundError:
            return None

    def create(self, container: str, doc: dict) -> dict:
        return self.db.get_container_client(container).create_item(doc)

    def replace(self, container: str, doc: dict) -> dict:
        """Replace if unchanged since read (the document's own _etag), else Changed."""
        try:
            return self.db.get_container_client(container).replace_item(
                doc["id"], doc, etag=doc["_etag"], match_condition=MatchConditions.IfNotModified)
        except exceptions.CosmosAccessConditionFailedError as e:
            raise Changed from e

    def find(self, container: str, **match) -> list[dict]:
        """Documents whose fields equal the given values, e.g. find("VBAK", BSTKD="4711")."""
        where = " AND ".join(f"c.{field} = @{field}" for field in match)
        params = [{"name": f"@{field}", "value": value} for field, value in match.items()]
        return list(self.db.get_container_client(container).query_items(
            f"SELECT * FROM c WHERE {where}", parameters=params, enable_cross_partition_query=True))

    def next_number(self, number_range: str, start: int) -> str:
        """An SAP-style number range: the next number, with optimistic concurrency."""
        while True:
            counter = self.get("NumberRange", number_range)
            if counter is None:
                try:
                    self.create("NumberRange", {"id": number_range, "last": start + 1})
                    return str(start + 1)
                except exceptions.CosmosResourceExistsError:
                    continue
            counter["last"] += 1
            try:
                self.replace("NumberRange", counter)
                return str(counter["last"])
            except Changed:
                continue


# --- Facts --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Fact:
    """A business fact to publish: StockReserved with its data and business keys."""
    name: str
    data: dict
    order_id: str | None = None
    customer_id: str | None = None
    partner_id: str | None = None


def event_type(fact: str) -> str:
    """StockReserved → com.acme.supply.stockReserved (asyncapi.yaml)."""
    return f"com.acme.{DOMAIN}.{fact[0].lower()}{fact[1:]}"


class Topic:
    """The domain's Event Grid topic, as this System API's runtime identity."""

    def __init__(self, endpoint: str):
        self.endpoint = endpoint

    def envelope(self, fact: Fact, journey: Journey) -> dict:
        return new_event(type=event_type(fact.name), source=f"/{SERVICE}/{VERSION}", correlation_id=journey.correlation_id,
                         keys=BusinessKeys(order_id=fact.order_id, customer_id=fact.customer_id, partner_id=fact.partner_id),
                         data=fact.data, traceparent=journey.traceparent)

    def publish(self, facts: list[Fact], journey: Journey) -> None:
        if not facts:
            return
        events = [self.envelope(f, journey) for f in facts]
        token = credential().get_token("https://eventgrid.azure.net/.default").token
        request = urllib.request.Request(self.endpoint, data=json.dumps(events).encode(), method="POST", headers={
            "Authorization": f"Bearer {token}", "Content-Type": "application/cloudevents-batch+json; charset=utf-8"})
        try:
            with urllib.request.urlopen(request, timeout=30):
                pass
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"Event Grid refused {[f.name for f in facts]}: {error.code} {error.read()[:300]}") from error


@cache
def system_store() -> Cosmos:
    return Cosmos(os.environ["SYSTEM_ENDPOINT"], os.environ["SYSTEM_DATABASE"])


@cache
def domain_topic() -> Topic:
    return Topic(os.environ["TOPIC_ENDPOINT"])
