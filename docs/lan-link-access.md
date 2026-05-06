# LAN Link Access

LAN links are the shared access entry point for DassieDrop.

Format:

```text
https://{fqdn}/s/{SHORT-CODE}
```

Example:

```text
https://example.com/s/AbC123XyZ
```

## Access Model

LAN links use one optional header only:

```text
X-Access-Password
```

Password evaluation order:

1. If the shared object has its own password, validate against the object password.
2. Otherwise, if the workspace has a password, validate against the workspace password.
3. Otherwise, allow access without a password.

Object password always overrides workspace password.

LAN link access uses the most specific applicable protection rule. Object-level password protection overrides workspace-level password protection. Workspace password protection applies only when the object does not define its own password.

LAN links do not require `X-API-Key`.

## Access Matrix

| Workspace password | Object password | Required access |
| --- | --- | --- |
| No | No | No password required |
| Yes | No | Workspace password via `X-Access-Password` |
| No | Yes | Object password via `X-Access-Password` |
| Yes | Yes | Object password via `X-Access-Password` |

## Curl Examples

No workspace password and no object password:

```bash
curl -ksSL \
  "https://example.com/s/AbC123XyZ"
```

Workspace password only:

```bash
curl -ksSL \
  "https://example.com/s/AbC123XyZ" \
  -H "X-Access-Password: myWorkspacePassword"
```

Object password only:

```bash
curl -ksSL \
  "https://example.com/s/AbC123XyZ" \
  -H "X-Access-Password: myObjectPassword"
```

Both workspace and object passwords exist:

```bash
curl -ksSL \
  "https://example.com/s/AbC123XyZ" \
  -H "X-Access-Password: myObjectPassword"
```

Store the shared text in a variable:

```bash
LAN_LINK='https://example.com/s/AbC123XyZ'
SHARED_TEXT="$(curl -ksSL "$LAN_LINK")"
printf '%s\n' "$SHARED_TEXT"
```

Run the shared text as a command:

```bash
LAN_LINK='https://example.com/s/AbC123XyZ'
bash -c "$(curl -ksSL "$LAN_LINK")"
```

## Failure Behavior

All LAN-link authorization failures return the same response:

```http
HTTP/1.1 401 Unauthorized
Content-Type: application/json; charset=utf-8
```

```json
{
  "message": "Access denied"
}
```

This applies to:

- unknown short code
- missing password
- wrong password
- rate-limited access attempts

The response does not reveal:

- whether the short code is valid
- whether the object exists
- whether the workspace is protected
- whether the object is protected

## Notes

- Short codes are opaque and non-sequential.
- LAN links are intended for sharing access without exposing workspace or object ids.
- Query-string passwords are not supported.
