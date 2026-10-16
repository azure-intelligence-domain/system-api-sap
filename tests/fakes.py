"""In-memory stand-ins for Cosmos DB and the topic: the System API's logic, tested without Azure."""

import copy
import itertools

import kit


class MemoryStore:
    def __init__(self):
        self.containers, self.etags = {}, itertools.count(1)

    def _c(self, container):
        return self.containers.setdefault(container, {})

    def get(self, container, id):
        doc = self._c(container).get(id)
        return copy.deepcopy(doc) if doc else None

    def create(self, container, doc):
        assert doc["id"] not in self._c(container), f"{container}/{doc['id']} exists"
        saved = {**copy.deepcopy(doc), "_etag": str(next(self.etags))}
        self._c(container)[doc["id"]] = saved
        return copy.deepcopy(saved)

    def replace(self, container, doc):
        if self._c(container)[doc["id"]]["_etag"] != doc["_etag"]:
            raise kit.Changed
        saved = {**copy.deepcopy(doc), "_etag": str(next(self.etags))}
        self._c(container)[doc["id"]] = saved
        return copy.deepcopy(saved)

    def find(self, container, **match):
        return [copy.deepcopy(d) for d in self._c(container).values() if all(d.get(k) == v for k, v in match.items())]

    def next_number(self, number_range, start):
        counter = self._c("NumberRange").setdefault(number_range, {"id": number_range, "last": start})
        counter["last"] += 1
        return str(counter["last"])


class FakeTopic:
    def __init__(self):
        self.events = []

    def publish(self, facts, journey):
        self.events += [kit.Topic("https://example").envelope(f, journey) for f in facts]

    @property
    def types(self):
        return [e["type"] for e in self.events]
