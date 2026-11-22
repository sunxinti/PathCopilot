# Security and privacy

## Credentials

Do not commit `webapp/config/api_keys.txt`, `.env`, local SQLite databases or
provider credentials. Use environment variables or the local settings page.
The repository contains examples and placeholders only.

## Clinical data

Do not place identifiable patient data in issues, logs, screenshots or example
files. De-identify slide names and metadata before sending prompts or images to
an external model provider, and review that provider's data-processing terms.

## Local deployment

The bundled Flask command is intended for local research use. For a networked
deployment, use a production WSGI server, TLS, authentication, access control,
audit logging and institution-approved storage. Do not expose the development
server directly to the public Internet.

## Reporting a vulnerability

Please report security issues privately to the maintainers through the contact
channel listed in the repository instead of opening a public issue containing
exploit details or sensitive data.
