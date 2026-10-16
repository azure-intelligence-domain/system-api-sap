# CLAUDE.md

The [Finance] SAP System API. Owned by the finance team. Ground rules live in the
[ai-intelligence-domain](https://github.com/azure-intelligence-domain/ai-intelligence-domain)
repository (ADR 0007 source systems, ADR 0014 process layer, ADR 0015 intake flows).

- A System API translates, it does not decide for other domains. It keeps the system's own rules
  (SAP's) and publishes the system's own facts (SalesOrderAccepted · SalesOrderRefused · InvoiceIssued · PaymentReceived).
- SAP's model and naming stay in `sap.py`; everything else speaks the contract.
- `kit.py` is the same in every System API repository: change it everywhere or nowhere.
- Contract first: `openapi.yaml` / `asyncapi.yaml` change before the code, with a higher
  `info.version`. Event names are business facts; ask the user before adding one.
- Tests: `uv run --no-project --python 3.12 --with-requirements requirements.txt --with pytest pytest -q`
