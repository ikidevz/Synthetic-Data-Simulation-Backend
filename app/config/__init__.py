"""What an entity is, and how it is declared.

    entities.py  EntityConfig / FieldConfig (pydantic) plus the YAML loader.
                 Adding an entity to the system means adding a *.yaml file —
                 no Python code changes required. Invalid configs (bad types,
                 dangling 'ref' relationships) fail loudly at load time.
    provider.py  The stricter pass applied to configs that arrive over HTTP
                 from a provider: name rules, quotas, and a trial generation.
"""