"""HTTP service for the RTDB table definitions and data."""

__all__ = ["RTDBWebApp", "create_server"]


def __getattr__(name):
    if name in __all__:
        from .server import RTDBWebApp, create_server
        return {"RTDBWebApp": RTDBWebApp, "create_server": create_server}[name]
    raise AttributeError(name)

