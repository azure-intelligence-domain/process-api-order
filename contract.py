"""The contract is the validator: requests are checked against openapi.yaml itself."""

from pathlib import Path

import yaml
from jsonschema import Draft7Validator

SPEC = yaml.safe_load(Path(__file__).with_name("openapi.yaml").read_text())


def _validator(schema: dict) -> Draft7Validator:
    # $refs point at #/components/..., so the schema travels with the spec's components.
    return Draft7Validator({**schema, "components": SPEC["components"]}, format_checker=Draft7Validator.FORMAT_CHECKER)


SCHEMAS = {name: _validator(schema) for name, schema in SPEC["components"]["schemas"].items()}
PARAMETERS = {name: _validator(p["schema"]) for name, p in SPEC["components"]["parameters"].items()}


def violations(validator: Draft7Validator, document) -> list[str]:
    """Human-readable contract violations, empty when the document is valid."""
    return [
        f"{'/'.join(map(str, error.absolute_path)) or 'value'}: {error.message}"
        for error in sorted(validator.iter_errors(document), key=lambda e: list(map(str, e.absolute_path)))
    ]
