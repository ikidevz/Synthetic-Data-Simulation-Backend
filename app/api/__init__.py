"""The HTTP surface: routers and the error envelope.

    error_handlers.py  Turns every failure mode — HTTPException, request
                       validation, ApiError — into one consistent
                       {"error": {"code", "message"}} body.
    v1/routes.py       The /v1 API: providers, the configs they publish, and
                       the data generated from them.

Both surfaces are thin: they validate, authorise and translate, then delegate
to app/services.
"""