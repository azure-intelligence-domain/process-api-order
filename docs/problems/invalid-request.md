# Invalid Request

`400 Bad Request`, `application/problem+json`

The request does not match the contract ([`openapi.yaml`](../../openapi.yaml)), or the
`Correlation-Id` header is missing or invalid. Validation errors are listed in `errors`, each with
a `title` and `detail` (adidas API guidelines, Problem Detail).
