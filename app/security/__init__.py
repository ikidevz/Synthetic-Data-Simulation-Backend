"""Authentication and identity.

    api_keys.py  Global, header-only API-key authentication. It resolves every
                 caller to a Principal: a superuser (a key from API_KEY /
                 API_KEYS) or a provider (a key issued through /v1).
"""