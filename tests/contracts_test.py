"""The code against its own contracts: every operation is a function, every event matches asyncapi.yaml."""

from pathlib import Path

import yaml
from integration_envelope import validate
from jsonschema import Draft7Validator

ROOT = Path(__file__).parent.parent
OPENAPI = yaml.safe_load((ROOT / "openapi.yaml").read_text())
ASYNCAPI = yaml.safe_load((ROOT / "asyncapi.yaml").read_text())


def assert_matches_asyncapi(event: dict) -> None:
    """The envelope (contracts) and the data (this repository's asyncapi.yaml) of a published event."""
    validate(event)
    for message in ASYNCAPI["components"]["messages"].values():
        own = message["payload"]["allOf"][1]
        if own["properties"]["type"]["const"] == event["type"]:
            for key in own["required"]:
                assert key in event, f"{event['type']} lacks {key}"
            schema = ASYNCAPI["components"]["schemas"][own["properties"]["data"]["$ref"].rsplit("/", 1)[-1]]
            errors = list(Draft7Validator({**schema, "components": ASYNCAPI["components"]}).iter_errors(event["data"]))
            assert not errors, [e.message for e in errors]
            return
    raise AssertionError(f"{event['type']} is not in asyncapi.yaml")


def test_every_operation_is_a_function():
    import function_app
    names = {f.get_function_name() for f in function_app.app.get_functions()}
    operations = {op["operationId"] for item in OPENAPI["paths"].values() for op in item.values() if isinstance(op, dict)}
    assert operations == names


def test_every_declared_event_is_published_somewhere():
    declared = {e for item in OPENAPI["paths"].values() for op in item.values() if isinstance(op, dict) for e in op.get("x-events", [])}
    in_asyncapi = {m["name"][0].upper() + m["name"][1:] for m in ASYNCAPI["components"]["messages"].values()}
    assert declared == in_asyncapi
