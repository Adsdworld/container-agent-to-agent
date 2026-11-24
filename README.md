# container-agent-to-agent

[![Image](https://github.com/Adsdworld/container-agent-to-agent/actions/workflows/image.yml/badge.svg?branch=main)](https://github.com/Adsdworld/container-agent-to-agent/actions/workflows/image.yml)

A small container that makes a [container-agent-claude](https://github.com/Adsdworld/container-agent-claude)
reachable by other agents over the [A2A protocol](https://a2a-protocol.org)
(Agent2Agent, v1.0, JSON-RPC binding).

It is a bridge, not an agent: every A2A message becomes a file in the agent's
`/agent/data/inbox/`, and the answer the agent writes there becomes the A2A task's
result. No dependency, Python standard library only.

```
A2A client ──JSON-RPC──▶ this container ──file──▶ /agent/data/inbox/a2a-<id>.md ──▶ agent
          ◀──────────────  task + artifact  ◀───  /agent/data/inbox/done/a2a-<id>.md ◀──
```

## How a message travels

The agent already has a mailbox convention: a file in `inbox/` starts a run within
a minute, the agent appends its answer at the end of the file and moves it to
`inbox/done/`. The bridge uses exactly that and keeps no state of its own, so the
tasks survive a restart of either container.

| A2A task state | What the bridge sees                                                    |
| -------------- | ----------------------------------------------------------------------- |
| `SUBMITTED`    | `inbox/a2a-<id>.md` exists and no run has listed it yet                 |
| `WORKING`      | a run started after the file was written (from `logs/agent.log`); `status.message` shows what the agent last said or which tool it uses (from the run's transcript) |
| `COMPLETED`    | the file is in `inbox/done/` with an answer after the request; the answer is the task's artifact |
| `COMPLETED` (fallback) | the file was moved without an answer: the final text of that run, read from its transcript, is the artifact, marked `answerSource: run-transcript` |
| `FAILED`       | moved without an answer and the run has no usable final text            |
| `CANCELED`     | `CancelTask` was called (see below)                                     |

### Cancel

Deleting the file would not work: the agent is woken as soon as the file lands and
may already be working on it. `CancelTask` instead drops a second file,
`inbox/a2a-<id>-cancel.md`, that tells the agent to stop, undo what it can and
reply in one line. The task is `CANCELED` for the client at once and stays so
whatever the agent writes later; the agent's reply, when it comes, appears as
`status.message`. Cancel is refused (`-32002`) once the task is finished. Stopping is
best effort: it depends on the agent reading the file.

The file looks like this; the agent writes after the last marker:

```
<!-- a2a task=6f1c… context=3b9e… created=2026-11-24T10:00:00.000Z -->
This message arrived through the A2A bridge, sent by a remote agent or application. It was not typed by your human.

<!-- a2a:request -->
Summarize what you did today.
<!-- a2a:request-end -->
```

Agent runs are slow (up to a minute to start, then as long as the agent works), so
`SendMessage` waits up to `A2A_BLOCK_TIMEOUT` seconds for the answer and otherwise
returns the task in state `SUBMITTED`; clients then poll `GetTask`. With
`"returnImmediately": true` it never waits.

## What is supported

| A2A                                   | Support                                                          |
| ------------------------------------- | ---------------------------------------------------------------- |
| Agent Card at `/.well-known/agent-card.json` | yes, announcing the OpenID Connect and/or bearer schemes that are enabled |
| `SendMessage`                         | yes, text parts only (`-32005` otherwise)                        |
| `GetTask`, `ListTasks`                | yes (`historyLength`, `contextId`, `status`, pagination)         |
| `CancelTask`                          | drops a cancel file for the agent, refused once the task is finished (`-32002`) |
| `contextId`                           | kept in the file and filterable; a new message in a context is a new task |
| Streaming, push notifications, extended card | no, answered with the matching A2A errors (`-32004`, `-32003`, `-32007`) |

Endpoints: `GET /.well-known/agent-card.json`, `POST /rpc` (also `POST /`),
`GET /healthz`.

## Run

Both containers share the agent's data folder:

```yaml
services:
  agent:
    image: ghcr.io/adsdworld/container-agent-claude:latest
    environment:
      CLAUDE_CODE_OAUTH_TOKEN: ${CLAUDE_CODE_OAUTH_TOKEN}
    volumes:
      - ./config:/agent/config:ro
      - ./data:/agent/data

  a2a:
    image: ghcr.io/adsdworld/container-agent-to-agent:latest
    environment:
      A2A_TOKEN: ${A2A_TOKEN}
      A2A_PUBLIC_URL: https://agent.example.com
    ports:
      - "8080:8080"
    volumes:
      - ./data:/agent/data
```

See [`examples/compose.yaml`](examples/compose.yaml). Try it:

```bash
curl localhost:8080/.well-known/agent-card.json

curl localhost:8080/rpc -H "Authorization: Bearer $A2A_TOKEN" -d '{
  "jsonrpc": "2.0", "id": 1, "method": "SendMessage",
  "params": {
    "message": {"messageId": "m1", "role": "ROLE_USER", "parts": [{"text": "Hello, who are you?"}]},
    "configuration": {"returnImmediately": true}
  }
}'

curl localhost:8080/rpc -H "Authorization: Bearer $A2A_TOKEN" -d '{
  "jsonrpc": "2.0", "id": 2, "method": "GetTask", "params": {"id": "<task id>"}
}'
```

## Configuration

| Variable             | Default                  | Description                                                              |
| -------------------- | ------------------------ | ------------------------------------------------------------------------ |
| `A2A_OIDC_ISSUER`    | none                     | Issuer of your OpenID Connect provider (Authentik: `https://auth.example.com/application/o/<slug>/`). Enables JWT validation. |
| `A2A_OIDC_AUDIENCE`  | none                     | Accepted `aud` values, comma separated (Authentik: the client IDs). **Set it**: without it any token of the issuer is accepted. |
| `A2A_OIDC_SCOPE`     | none                     | Scope the token must carry, for example `a2a`.                           |
| `A2A_OIDC_REQUIRE_GROUP` | none               | Group the token's `groups` claim must contain (Authentik: add the `profile` scope and put the callers in this group). |
| `A2A_OIDC_ALLOWED_SUBJECTS` | none             | Callers allowed, comma separated: a token is accepted if its `sub`, `preferred_username`, `azp`, `client_id` or `email` is in the list. Empty means any caller the provider accepts. |
| `A2A_OIDC_JWKS_URL`  | from discovery           | Key set URL, when it cannot be found through `<issuer>/.well-known/openid-configuration`. |
| `A2A_OIDC_ALGORITHMS`| `RS256,ES256`            | Accepted signature algorithms. HMAC ones are refused on purpose.         |
| `A2A_TOKEN`          | none                     | Static bearer token, accepted next to OIDC or alone. With neither `A2A_TOKEN` nor `A2A_OIDC_ISSUER`, **anyone who can reach the port writes to the agent's inbox**. |
| `A2A_PUBLIC_URL`     | `http://localhost:8080`  | URL clients use to reach the bridge, published in the Agent Card.        |
| `A2A_NAME`           | `Claude agent`           | Name in the Agent Card.                                                  |
| `A2A_DESCRIPTION`    | generic text             | Description in the Agent Card.                                           |
| `A2A_BLOCK_TIMEOUT`  | `300`                    | Seconds `SendMessage` waits for the answer before returning the pending task. |
| `A2A_MAX_PENDING`    | `20`                     | Messages waiting for the agent at most; further ones are refused.        |
| `A2A_PREAMBLE`       | "…not typed by your human." | Line added under the header of every message; empty to disable.       |
| `A2A_RUN_MAX`        | `7200`                   | Seconds after which a run with no end in `agent.log` is no longer considered active. |
| `A2A_PORT`           | `8080`                   | Listening port.                                                          |
| `A2A_DATA_DIR`       | `/agent/data`            | The agent's data folder (the bridge uses `inbox/` and `state/a2a/`).     |
| `A2A_MAX_BODY`       | `1048576`                | Largest request body, in bytes.                                          |

## Authentication with Authentik

Each client agent is an OAuth2 client of your identity provider and uses the
`client_credentials` flow: it gets a short-lived JWT and sends it as
`Authorization: Bearer <jwt>`. The bridge checks the signature against the
provider's key set (fetched from the discovery document and cached), the
issuer, the audience, the expiry and, if set, the scope. The Agent Card
announces the `openIdConnect` scheme, so A2A clients know where to get tokens.

In Authentik (names may differ a little between versions):

1. **Scope**: in Customization, Property Mappings, create a *Scope Mapping*
   named `a2a` (scope name `a2a`, empty expression is enough).
2. **Provider**: create an *OAuth2/OpenID Provider*. Client type *Confidential*,
   a **signing key** (a certificate: without one Authentik signs with HS256, which
   the bridge refuses), and the `a2a` scope mapping among the selected scopes.
3. **Application**: create an application with a slug (for example `a2a`) and
   attach the provider. The issuer is then
   `https://auth.example.com/application/o/a2a/`.
4. Give the client its **client ID and secret**. It gets a token with:

   ```bash
   curl -s https://auth.example.com/application/o/token/ \
     -d grant_type=client_credentials -d scope=a2a \
     -d client_id=$CLIENT_ID -d client_secret=$CLIENT_SECRET
   ```

   Some Authentik versions want a service account's username and an app password
   instead (`-d username=… -d password=…`, with the client ID and secret as
   basic auth).

```yaml
    environment:
      A2A_OIDC_ISSUER: https://auth.example.com/application/o/a2a/
      A2A_OIDC_AUDIENCE: <client id>
      A2A_OIDC_SCOPE: a2a
```

The token's audience is the client ID of the provider that issued it, and the
bridge's `iss` check is exact. With one provider per client agent, either list
every client ID in `A2A_OIDC_AUDIENCE` and use the **same issuer for all
providers** (provider setting *Issuer mode*, with `A2A_OIDC_JWKS_URL` pointing at
a provider's `/jwks/`), or give each agent its own bridge. Callers show up in the
inbox file as `Authenticated caller: <client id>`, so the agent's bible can treat
them differently.

An unreachable provider answers `503`, an invalid token `401`, a missing scope
`403`.

## Security

- The agent treats every inbox message as an instruction from the person it works
  for. A message sent through this bridge therefore carries that authority, within
  the agent's bible, which a message can never change. Whoever holds `A2A_TOKEN`,
  or a valid token of your provider, can ask the agent anything its bible allows.
- Put a rule in the agent's bible about messages marked as coming from A2A (what
  they may ask for, what needs your confirmation): the preamble line exists so the
  agent can tell them apart.
- The bridge does not do TLS. Put it behind a reverse proxy if it leaves your
  network, and set `A2A_PUBLIC_URL` to the public HTTPS address.
- It runs as root, like the agent, because it writes into the agent's data folder.
  Mount only that folder, never the Docker socket.

## Develop

```bash
pip install -r requirements.txt
python3 -m unittest discover -s tests -v   # the OIDC tests are skipped without PyJWT
A2A_DATA_DIR=./data python3 app/bridge.py
```

## License

[Apache-2.0](LICENSE)
