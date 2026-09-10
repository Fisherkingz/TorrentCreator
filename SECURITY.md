# Security

## API keys and credentials

Do not commit HamsterImg API keys or other credentials to this repository.
TorrentCreator is designed to store the HamsterImg API key in Windows Credential
Manager when the user chooses **Save key securely**.

The API key is not intended to be stored in source code, `settings.json`, build
scripts, or release archives.

If a key is accidentally committed or published, revoke/rotate it immediately
in the relevant service and remove it from the repository history.

## Reporting a security issue

If you discover a security issue, avoid posting credentials or other sensitive
information in a public GitHub issue. Use a private reporting channel provided
by the repository owner where available.
