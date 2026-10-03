# Aula Project Bridge

This Home Assistant app exposes a deliberately small file API for one local project only:

`/addons/aula_assistant`

Inside the bridge container the Home Assistant local-apps volume is mounted at
`/local_apps`, and the API hard-codes its jail to
`/local_apps/aula_assistant`.

## Security model

- No SSH, shell or command execution endpoint.
- No Home Assistant API token, Supervisor API access or Docker socket.
- No published TCP port; access is through Home Assistant Ingress.
- Absolute paths and `..` traversal are rejected.
- Existing symlinks in any requested path are rejected.
- The project root itself cannot be deleted, renamed or overwritten.
- Reads and writes are limited to 16 MiB per file/request.

The container must receive Home Assistant's `local_apps` mount read/write so it can
edit the local Aula project. That mount contains the local-apps directory as a whole;
the bridge's API is the enforcement boundary restricting requests to
`aula_assistant`.

## API

All paths below are relative to the Aula project root.

- `GET /health`
- `GET /stat?path=...`
- `GET /files?path=...`
- `GET /file?path=...&encoding=utf-8|base64`
- `PUT /file` with JSON `{"path":"...", "content":"..."}` or `content_base64`
- `POST /mkdir`
- `POST /rename`
- `POST /copy`
- `POST /chmod`
- `POST /delete`

There is intentionally no endpoint that can execute a command.
